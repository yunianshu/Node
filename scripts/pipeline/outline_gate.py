#!/usr/bin/env python3
"""Outline quality-gate orchestration used by Coordinator and outline lanes."""
from __future__ import annotations

import re
import sys
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from core.outline_batch_lock import is_chapter_locked, unlock_chapters
from core.outline_quality_gate import aggregate_outline_reviews
from core.workflow_state import (
    atomic_write_json,
    load_outline_review_status,
    outline_chapter_path,
    report_path,
)
from maintenance.build_outline_ledgers import build_ledgers


_outline_review_budget_lock = threading.Lock()
_outline_review_call_counts: dict[int, int] = {}


def outline_gate_passed(rt, chapter: int) -> bool:
    min_score = float(rt.CONFIG.get("outline_reviewer", {}).get("min_score", 8.5))
    _, _, _, ok = load_outline_review_status(
        rt._outline_review_file(chapter),
        min_score,
        require_quality_gate=outline_quality_gate_config(rt)["enabled"],
    )
    return ok


def outline_quality_gate_config(rt) -> dict:
    cfg = rt.CONFIG.get("outline_quality_gate", {})
    if not isinstance(cfg, dict):
        cfg = {}
    rounds = max(1, int(cfg.get("review_rounds", 3) or 3))
    required_votes = max(1, int(cfg.get("required_votes", rounds // 2 + 1) or 1))
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "review_rounds": rounds,
        "required_votes": min(rounds, required_votes),
        "max_score_spread": max(0.0, float(cfg.get("max_score_spread", 0.6) or 0.6)),
        "screening_score": float(cfg.get("screening_score", 8.7) or 8.7),
        "max_review_calls_per_candidate": max(
            1,
            int(cfg.get("max_review_calls_per_candidate", rounds) or rounds),
        ),
        "max_review_calls_per_chapter": max(
            1,
            int(cfg.get("max_review_calls_per_chapter", 15) or 15),
        ),
    }


def _consume_outline_review_budget(rt, chapter: int) -> tuple[bool, int, int]:
    limit = outline_quality_gate_config(rt)["max_review_calls_per_chapter"]
    with _outline_review_budget_lock:
        used = _outline_review_call_counts.get(chapter, 0)
        if used >= limit:
            return False, used, limit
        used += 1
        _outline_review_call_counts[chapter] = used
        return True, used, limit


def _aggregate_review_files(rt, chapter: int, review_files: list[Path], aggregate_file: Path) -> dict:
    reviews = [rt._load_json_file(path) for path in review_files]
    reviews = [review for review in reviews if review.get("status") == "completed"]
    gate_cfg = outline_quality_gate_config(rt)
    aggregate = aggregate_outline_reviews(
        chapter,
        reviews,
        min_score=float(rt.CONFIG.get("outline_reviewer", {}).get("min_score", 8.5)),
        required_rounds=gate_cfg["review_rounds"],
        required_votes=gate_cfg["required_votes"],
        max_score_spread=gate_cfg["max_score_spread"],
    )
    atomic_write_json(aggregate_file, aggregate)
    return aggregate


def run_outline_review_rounds(
    rt,
    chapter: int,
    outline_file: Path,
    aggregate_file: Path,
    *,
    child_log: Path | None = None,
    stop_event: threading.Event | None = None,
) -> tuple[int, dict]:
    gate_cfg = outline_quality_gate_config(rt)
    if not gate_cfg["enabled"]:
        args = [
            "--chapter",
            str(chapter),
            "--outline-file",
            str(outline_file),
            "--review-file",
            str(aggregate_file),
        ]
        rc = rt.run_script("outline_reviewer.py", *args)
        return rc, rt._load_json_file(aggregate_file)

    round_dir = aggregate_file.parent / f"{aggregate_file.stem}_rounds"
    round_dir.mkdir(parents=True, exist_ok=True)
    review_files: list[Path] = []
    review_limit = min(gate_cfg["review_rounds"], gate_cfg["max_review_calls_per_candidate"])
    for round_number in range(1, review_limit + 1):
        allowed, used, budget = _consume_outline_review_budget(rt, chapter)
        if not allowed:
            aggregate = aggregate_outline_reviews(
                chapter,
                [rt._load_json_file(path) for path in review_files],
                min_score=float(rt.CONFIG.get("outline_reviewer", {}).get("min_score", 8.5)),
                required_rounds=gate_cfg["review_rounds"],
                required_votes=gate_cfg["required_votes"],
                max_score_spread=gate_cfg["max_score_spread"],
            )
            aggregate["review_budget"] = {"exhausted": True, "used": used, "limit": budget}
            atomic_write_json(aggregate_file, aggregate)
            rt.log(f"[Coordinator] 第{chapter}章审查调用预算耗尽 {used}/{budget}")
            return 0, aggregate

        review_file = round_dir / f"round_{round_number:02d}.json"
        rt._safe_unlink(review_file)
        reviewer_args = [
            sys.executable,
            str(rt.resolve_script_path("outline_reviewer.py")),
            "--project",
            str(rt.NOVELS_DIR),
            "--chapter",
            str(chapter),
            "--outline-file",
            str(outline_file),
            "--review-file",
            str(review_file),
        ]
        rt.log(
            f"[Coordinator] 第{chapter}章独立质量审查 "
            f"{round_number}/{gate_cfg['review_rounds']}，章节预算{used}/{budget}"
        )
        if child_log is not None:
            rc = rt.run_cancellable_process(reviewer_args, child_log, stop_event or threading.Event())
        else:
            rc = rt.run_streaming_process(reviewer_args, rt.LOGS_DIR / "coordinator_outline_reviewer.log")
        if rc != 0:
            return rc, {}
        review_files.append(review_file)

        if round_number == 1:
            first_review = rt._load_json_file(review_file)
            first_score = first_review.get("overall_score")
            first_design_ok = first_review.get("design_gate_passed") is True
            if (
                not isinstance(first_score, (int, float))
                or float(first_score) < gate_cfg["screening_score"]
                or not first_design_ok
            ):
                aggregate = aggregate_outline_reviews(
                    chapter,
                    [first_review],
                    min_score=float(rt.CONFIG.get("outline_reviewer", {}).get("min_score", 8.5)),
                    required_rounds=gate_cfg["review_rounds"],
                    required_votes=gate_cfg["required_votes"],
                    max_score_spread=gate_cfg["max_score_spread"],
                )
                aggregate["screening"] = {
                    "passed": False,
                    "score": first_score,
                    "min_score": gate_cfg["screening_score"],
                    "design_gate_passed": first_design_ok,
                }
                atomic_write_json(aggregate_file, aggregate)
                rt.log(
                    f"[Coordinator] 第{chapter}章初筛未过，"
                    f"score={first_score} design={first_design_ok}，停止后续复审"
                )
                return 0, aggregate

    aggregate = _aggregate_review_files(rt, chapter, review_files, aggregate_file)
    gate = aggregate.get("quality_gate", {})
    rt.log(
        f"[Coordinator] 第{chapter}章三层质量门: "
        f"median={aggregate.get('overall_score')} "
        f"score_votes={gate.get('score_pass_votes')}/{gate.get('required_votes')} "
        f"design={aggregate.get('design_gate_passed')} "
        f"passed={gate.get('passed')}"
    )
    return 0, aggregate


def outline_race_config(rt) -> dict:
    cfg = rt.CONFIG.get("outline_race", {}) if isinstance(rt.CONFIG.get("outline_race"), dict) else {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "candidates": max(1, int(cfg.get("candidates", 3) or 3)),
        "stop_on_first_pass": bool(cfg.get("stop_on_first_pass", True)),
        "max_workers": max(1, int(cfg.get("max_workers", cfg.get("candidates", 3)) or 3)),
    }


def _candidate_root(rt, chapter: int, round_no: int, attempt: int) -> Path:
    return rt.LOGS_DIR / "outline_candidates" / f"ch{chapter:04d}" / f"round{round_no}_attempt{attempt}"


def _candidate_paths(rt, chapter: int, round_no: int, attempt: int, candidate_no: int) -> tuple[Path, Path, Path]:
    root = _candidate_root(rt, chapter, round_no, attempt)
    prefix = f"candidate_{candidate_no:02d}"
    return root / f"{prefix}.json", root / f"{prefix}_review.json", root / f"{prefix}.log"


def _candidate_feedback(rt, review_data: dict, chapter: int, candidate_no: int, round_no: int, attempt: int) -> dict:
    item = rt._review_feedback(review_data, chapter, "outline", round_no, attempt)
    item["candidate"] = candidate_no
    return item


def _candidate_score(result: dict) -> float:
    try:
        return float(result.get("overall_score"))
    except (TypeError, ValueError):
        return -1.0


def _publish_outline_candidate(rt, chapter: int, candidate_file: Path, candidate_review_file: Path) -> None:
    outline_data = rt._load_json_file(candidate_file)
    review_data = rt._load_json_file(candidate_review_file)
    if not outline_data:
        raise ValueError(f"候选大纲为空: {candidate_file}")
    if not review_data:
        raise ValueError(f"候选审查为空: {candidate_review_file}")
    if outline_data.get("chapter_number") != chapter:
        raise ValueError(f"候选大纲章节号不匹配: expected={chapter}, actual={outline_data.get('chapter_number')}")
    if review_data.get("chapter_number") != chapter:
        raise ValueError(f"候选审查章节号不匹配: expected={chapter}, actual={review_data.get('chapter_number')}")

    min_score = float(rt.CONFIG.get("outline_reviewer", {}).get("min_score", 8.5))
    _, status, score, ok = load_outline_review_status(
        candidate_review_file,
        min_score,
        require_quality_gate=outline_quality_gate_config(rt)["enabled"],
    )
    if not ok:
        raise ValueError(f"候选审查未达标: status={status}, score={score}, min_score={min_score:g}")

    rt._drop_text_artifacts(chapter, reason="大纲候选赛马已发布新正式大纲")
    rt._safe_unlink(outline_chapter_path(rt.NOVELS_DIR, chapter))
    rt._safe_unlink(rt._outline_review_file(chapter))
    atomic_write_json(outline_chapter_path(rt.NOVELS_DIR, chapter), outline_data)
    atomic_write_json(rt._outline_review_file(chapter), review_data)
    build_ledgers(rt.NOVELS_DIR)
    rt.log(
        f"[Coordinator] 第{chapter}章采用候选大纲: {candidate_file.name}, "
        f"score={review_data.get('overall_score')} verdict={review_data.get('verdict')}"
    )


def _run_outline_candidate(
    rt,
    chapter: int,
    round_no: int,
    attempt: int,
    candidate_no: int,
    feedback_file: Path | None,
    rescue: bool,
    stop_event: threading.Event,
) -> dict:
    candidate_file, candidate_review_file, child_log = _candidate_paths(rt, chapter, round_no, attempt, candidate_no)
    candidate_file.parent.mkdir(parents=True, exist_ok=True)
    rt._safe_unlink(candidate_file)
    rt._safe_unlink(candidate_review_file)

    outliner_args = [
        sys.executable,
        str(rt.resolve_script_path("outliner.py")),
        "--project",
        str(rt.NOVELS_DIR),
        "--chapter",
        str(chapter),
        "--candidate-file",
        str(candidate_file),
    ]
    if feedback_file:
        outliner_args += ["--review-feedback", str(feedback_file)]
    if rescue:
        outliner_args.append("--rescue")

    rt.log(f"[Coordinator] 第{chapter}章候选{candidate_no}开始生成")
    rc = rt.run_cancellable_process(outliner_args, child_log, stop_event)
    if rc != 0:
        return {
            "chapter": chapter,
            "candidate": candidate_no,
            "status": "outliner_failed" if rc != -9 else "cancelled",
            "weaknesses": ["候选大纲生成失败、被取消、JSON解析失败或结构字段不完整"],
            "suggestions": ["重新生成时必须补齐summary、key_events、foreshadowing、power_progression等必填字段"],
            "candidate_file": str(candidate_file),
            "review_file": str(candidate_review_file),
        }

    rt.log(f"[Coordinator] 第{chapter}章候选{candidate_no}开始多轮审查")
    rc, review_data = run_outline_review_rounds(
        rt,
        chapter,
        candidate_file,
        candidate_review_file,
        child_log=child_log,
        stop_event=stop_event,
    )
    if rc != 0:
        return {
            "chapter": chapter,
            "candidate": candidate_no,
            "status": "outline_reviewer_failed" if rc != -9 else "cancelled",
            "weaknesses": ["候选大纲审查器执行失败或被取消"],
            "suggestions": ["重新生成大纲并确保结构完整、剧情冲突明确、伏笔和能力进展具体"],
            "candidate_file": str(candidate_file),
            "review_file": str(candidate_review_file),
        }

    min_score = float(rt.CONFIG.get("outline_reviewer", {}).get("min_score", 8.5))
    _, _, _, ok = load_outline_review_status(
        candidate_review_file,
        min_score,
        require_quality_gate=outline_quality_gate_config(rt)["enabled"],
    )
    result = _candidate_feedback(rt, review_data, chapter, candidate_no, round_no, attempt)
    result["passed"] = ok
    result["candidate_file"] = str(candidate_file)
    result["review_file"] = str(candidate_review_file)
    return result


def _process_outline_gate_race(rt, chapter: int, *, push_on_failure: bool = True, initial_feedback: Path | None = None) -> bool:
    if initial_feedback is None and outline_chapter_path(rt.NOVELS_DIR, chapter).exists() and outline_gate_passed(rt, chapter):
        rt.log(f"[Coordinator] 第{chapter}章大纲和大纲审已通过，跳过")
        return True

    max_rounds = int(rt.CONFIG.get("coordinator", {}).get("outline_analysis_rounds", 3) or 3)
    attempts_per_round = int(rt.CONFIG.get("coordinator", {}).get("outline_attempts_per_round", 3) or 3)
    race_cfg = outline_race_config(rt)
    candidates = race_cfg["candidates"]
    max_workers = min(candidates, race_cfg["max_workers"])
    reviews: list[dict] = []
    feedback_file: Path | None = initial_feedback

    for round_no in range(1, max_rounds + 1):
        if reviews:
            feedback_file = rt._write_gate_feedback(chapter, "outline", reviews, round_no)
            rt.log(f"[Coordinator] 第{chapter}章大纲赛马进入第{round_no}轮原因调整: {feedback_file}")

        for attempt in range(1, attempts_per_round + 1):
            rt.log(f"[Coordinator] 第{chapter}章大纲候选赛马 {round_no}.{attempt} candidates={candidates}")
            existing_review = rt._load_json_file(rt._outline_review_file(chapter))
            if existing_review:
                reviews.append(rt._review_feedback(existing_review, chapter, "outline", round_no, attempt))
                feedback_file = rt._write_gate_feedback(chapter, "outline", reviews, round_no)
                rt.log(f"[Coordinator] 第{chapter}章读取现有大纲审查意见，反馈给候选赛马: {feedback_file}")

            stop_event = threading.Event()
            accepted: dict | None = None
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_candidates = {
                    executor.submit(
                        _run_outline_candidate,
                        rt,
                        chapter,
                        round_no,
                        attempt,
                        candidate_no,
                        feedback_file,
                        len(reviews) >= 8,
                        stop_event,
                    ): candidate_no
                    for candidate_no in range(1, candidates + 1)
                }
                pending = set(future_candidates)
                while pending:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        candidate_no = future_candidates.get(future, 0)
                        try:
                            result = future.result()
                        except Exception as exc:
                            result = {
                                "chapter": chapter,
                                "candidate": candidate_no,
                                "status": "candidate_exception",
                                "weaknesses": [f"候选进程异常: {exc}"],
                                "suggestions": ["检查候选日志、模型响应和候选文件写入"],
                            }
                        reviews.append(result)
                        if not result.get("passed"):
                            continue
                        if race_cfg["stop_on_first_pass"]:
                            accepted = result
                            stop_event.set()
                            break
                        if accepted is None or _candidate_score(result) > _candidate_score(accepted):
                            accepted = result
                    if stop_event.is_set():
                        break
                if stop_event.is_set():
                    wait(pending, timeout=10)

            if accepted:
                try:
                    _publish_outline_candidate(
                        rt,
                        chapter,
                        Path(str(accepted["candidate_file"])),
                        Path(str(accepted["review_file"])),
                    )
                except Exception as exc:
                    reviews.append({
                        "chapter": chapter,
                        "status": "candidate_publish_failed",
                        "weaknesses": [f"候选发布失败: {exc}"],
                        "suggestions": ["重新生成候选并检查候选文件与正式目录写入权限"],
                    })
                    feedback_file = rt._write_gate_feedback(chapter, "outline", reviews, round_no)
                    rt.log(f"[Coordinator] 第{chapter}章候选发布失败，下一次重试使用反馈: {feedback_file}")
                    continue
                rt.log(f"[Coordinator] 第{chapter}章大纲候选赛马通过")
                return True

            feedback_file = rt._write_gate_feedback(chapter, "outline", reviews, round_no)
            best = rt._failure_analysis(chapter, "outline", reviews).get("best_score")
            rt.log(f"[Coordinator] 第{chapter}章本轮候选均未通过，最佳分数={best}，下一次使用汇总反馈: {feedback_file}")

    report = rt._write_failure_report(chapter, "outline", reviews)
    if push_on_failure:
        rt._push_gate_failure(chapter, "大纲初审", report)
    return False


def process_outline_gate(rt, chapter: int, *, push_on_failure: bool = True, initial_feedback: Path | None = None) -> bool:
    if is_chapter_locked(rt.NOVELS_DIR, chapter):
        if initial_feedback is None:
            rt.log(f"[Coordinator] 第{chapter}章所在25章批次已锁定，跳过重生成")
            return outline_gate_passed(rt, chapter)
        unlocked = unlock_chapters(rt.NOVELS_DIR, [chapter])
        rt.log(f"[Coordinator] 应用修订反馈前解锁批次: {unlocked}")

    if outline_race_config(rt)["enabled"]:
        return _process_outline_gate_race(rt, chapter, push_on_failure=push_on_failure, initial_feedback=initial_feedback)

    if initial_feedback is None and outline_chapter_path(rt.NOVELS_DIR, chapter).exists() and outline_gate_passed(rt, chapter):
        rt.log(f"[Coordinator] 第{chapter}章大纲和大纲审已通过，跳过")
        return True

    max_rounds = int(rt.CONFIG.get("coordinator", {}).get("outline_analysis_rounds", 3) or 3)
    attempts_per_round = int(rt.CONFIG.get("coordinator", {}).get("outline_attempts_per_round", 3) or 3)
    reviews: list[dict] = []
    feedback_file: Path | None = initial_feedback

    for round_no in range(1, max_rounds + 1):
        if reviews:
            feedback_file = rt._write_gate_feedback(chapter, "outline", reviews, round_no)
            rt.log(f"[Coordinator] 第{chapter}章大纲进入第{round_no}轮原因调整: {feedback_file}")

        for attempt in range(1, attempts_per_round + 1):
            rt.log(f"[Coordinator] 第{chapter}章大纲生成/初审 {round_no}.{attempt}")
            existing_review = rt._load_json_file(rt._outline_review_file(chapter))
            if existing_review:
                reviews.append(rt._review_feedback(existing_review, chapter, "outline", round_no, attempt))
                feedback_file = rt._write_gate_feedback(chapter, "outline", reviews, round_no)
                rt.log(f"[Coordinator] 第{chapter}章读取现有大纲审查意见，反馈给本次重生成: {feedback_file}")
            rt._drop_text_artifacts(chapter, reason="大纲正在重生成")
            rt._safe_unlink(outline_chapter_path(rt.NOVELS_DIR, chapter))
            rt._safe_unlink(rt._outline_review_file(chapter))

            args = ["--chapter", str(chapter)]
            if feedback_file:
                args += ["--review-feedback", str(feedback_file)]
            if len(reviews) >= 8:
                args.append("--rescue")
                rt.log(f"[Coordinator] 第{chapter}章大纲累计失败{len(reviews)}次，启用卡章救援模式")
            if rt.run_script("outliner.py", *args) != 0:
                reviews.append({
                    "chapter": chapter,
                    "status": "outliner_failed",
                    "weaknesses": ["大纲生成失败、JSON解析失败或结构字段不完整"],
                    "suggestions": ["重新生成时必须补齐summary、key_events、foreshadowing、power_progression等必填字段"],
                })
                feedback_file = rt._write_gate_feedback(chapter, "outline", reviews, round_no)
                rt.log(f"[Coordinator] 第{chapter}章大纲生成失败，下一次重生成将使用反馈: {feedback_file}")
                continue

            rc, review_data = run_outline_review_rounds(
                rt,
                chapter,
                outline_chapter_path(rt.NOVELS_DIR, chapter),
                rt._outline_review_file(chapter),
            )
            if rc != 0:
                reviews.append({
                    "chapter": chapter,
                    "status": "outline_reviewer_failed",
                    "weaknesses": ["大纲审查器执行失败"],
                    "suggestions": ["重新生成大纲并确保结构完整、剧情冲突明确、伏笔和能力进展具体"],
                })
                feedback_file = rt._write_gate_feedback(chapter, "outline", reviews, round_no)
                rt.log(f"[Coordinator] 第{chapter}章大纲审查失败，下一次重生成将使用反馈: {feedback_file}")
                continue

            reviews.append(rt._review_feedback(review_data, chapter, "outline", round_no, attempt))
            if outline_gate_passed(rt, chapter):
                build_ledgers(rt.NOVELS_DIR)
                rt.log(f"[Coordinator] 第{chapter}章大纲初审通过")
                return True
            if review_data.get("review_budget", {}).get("exhausted") is True:
                rt.log(f"[Coordinator] 第{chapter}章审查预算耗尽，停止继续盲目重生成")
                report = rt._write_failure_report(chapter, "outline", reviews)
                if push_on_failure:
                    rt._push_gate_failure(chapter, "大纲初审", report)
                return False
            feedback_file = rt._write_gate_feedback(chapter, "outline", reviews, round_no)
            rt.log(f"[Coordinator] 第{chapter}章大纲未通过，下一次重生成将使用反馈: {feedback_file}")

    report = rt._write_failure_report(chapter, "outline", reviews)
    if push_on_failure:
        rt._push_gate_failure(chapter, "大纲初审", report)
    return False


def outline_lookahead_window(chapter: int, end: int, lookahead: int) -> tuple[int, int]:
    window = max(1, lookahead)
    return chapter, min(end, chapter + window - 1)


def load_outline_failure_report(rt, chapter: int) -> dict:
    return rt._load_json_file(report_path(rt.NOVELS_DIR, f"outline_failure_chapter_{chapter:04d}.json"))


def _extract_later_overlap_chapter(chapter: int, report: dict) -> int | None:
    texts: list[str] = []
    for key in ("likely_reasons", "adjustments", "statuses"):
        values = report.get(key, [])
        if isinstance(values, list):
            texts.extend(str(item) for item in values if item)
        elif values:
            texts.append(str(values))

    candidate_chapters: list[int] = []
    overlap_markers = ("重叠", "重复", "冲突", "断裂", "冲突", "不自洽", "bug")
    for text in texts:
        if not any(marker in text for marker in overlap_markers):
            continue
        for match in re.finditer(r"第\s*(\d+)\s*章", text):
            try:
                target = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if target > chapter:
                candidate_chapters.append(target)
        for match in re.finditer(r"chapter[_\s-]?(\d+)", text, re.IGNORECASE):
            try:
                target = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if target > chapter:
                candidate_chapters.append(target)

    if candidate_chapters:
        return min(candidate_chapters)
    return None


def drop_outline_artifacts(rt, chapter: int) -> None:
    outline_file = outline_chapter_path(rt.NOVELS_DIR, chapter)
    review_file = rt._outline_review_file(chapter)
    if outline_file.exists():
        outline_file.unlink()
        rt.log(f"[Coordinator] 删除第{chapter}章大纲，准备按后章重叠规则重写")
    if review_file.exists():
        review_file.unlink()
        rt.log(f"[Coordinator] 删除第{chapter}章大纲审查报告，准备按后章重叠规则重写")
    rt._drop_text_artifacts(chapter, reason="大纲已排除/重写")


def repair_later_overlap(rt, chapter: int, report: dict) -> int | None:
    overlap_chapter = _extract_later_overlap_chapter(chapter, report)
    if overlap_chapter is None:
        return None
    drop_outline_artifacts(rt, overlap_chapter)
    rt.log(f"[Coordinator] 第{chapter}章审查指向第{overlap_chapter}章存在重叠，已先排除后章，后续将重生成第{overlap_chapter}章大纲")
    return overlap_chapter


def ensure_outline_lookahead(rt, chapter: int, end: int, lookahead: int) -> tuple[bool, int | None]:
    start_chapter, end_chapter = outline_lookahead_window(chapter, end, lookahead)
    rt.log(f"[Coordinator] 写第{chapter}章前，确保第{start_chapter}-{end_chapter}章大纲已通过")
    for outline_chapter in range(start_chapter, end_chapter + 1):
        if not process_outline_gate(rt, outline_chapter, push_on_failure=False):
            return False, outline_chapter
    return True, None


def run_outline_book_review(rt, force: bool = False) -> bool:
    script = rt.MAINTENANCE_SCRIPTS["outline_book_reviewer.py"]
    cmd = [sys.executable, str(script), "--project", str(rt.NOVELS_DIR)]
    if force:
        cmd.append("--force")
    child_log = rt.LOGS_DIR / "outline_book_reviewer_child.log"
    rc = rt.run_streaming_process(cmd, child_log)
    report = rt._load_json_file(report_path(rt.NOVELS_DIR, "outline_book_review") / "final_outline_review.json")
    if rc != 0 or not report.get("gate_passed"):
        rt.log(
            f"[Coordinator] 整本大纲总审未通过: rc={rc}, "
            f"score={report.get('score')}, verdict={report.get('verdict')}"
        )
        return False
    rt.log(f"[Coordinator] 整本大纲总审通过: score={report.get('score')}")
    return True


def _outline_book_review_report(rt) -> dict:
    return rt._load_json_file(report_path(rt.NOVELS_DIR, "outline_book_review") / "final_outline_review.json")


def _global_outline_feedback(rt, chapter: int, issues: list[dict], round_no: int) -> Path:
    relevant = []
    for issue in issues:
        chapters = issue.get("chapters", [])
        if isinstance(chapters, list) and chapter in chapters:
            relevant.append(issue)
    reasons = [str(item.get("detail", "")).strip() for item in relevant if item.get("detail")]
    suggestions = [str(item.get("suggestion", "")).strip() for item in relevant if item.get("suggestion")]
    payload = {
        str(chapter): {
            "chapter": chapter,
            "gate": "outline_book_review",
            "analysis_round": round_no,
            "failure_analysis": {
                "attempts": len(relevant),
                "likely_reasons": reasons[:8],
                "adjustments": suggestions[:8] or reasons[:8],
            },
            "reviews": [{
                "overall_score": None,
                "verdict": "需重写",
                "weaknesses": reasons[:6],
                "suggestions": suggestions[:6],
                "continuity_issues": reasons[:3],
                "summary": "整本大纲总审要求修复跨章结构问题",
            }],
        }
    }
    path = rt.LOGS_DIR / f"outline_book_feedback_ch{chapter:04d}_round{round_no}.json"
    atomic_write_json(path, payload)
    return path


def repair_outline_book_review(rt, round_no: int) -> bool:
    report = _outline_book_review_report(rt)
    issues = report.get("issues", []) if isinstance(report.get("issues"), list) else []
    chapters = []
    for issue in issues:
        values = issue.get("chapters", []) if isinstance(issue, dict) else []
        if isinstance(values, list):
            chapters.extend(value for value in values if isinstance(value, int))
    total = int(rt.CONFIG["total_chapters"])
    limit = int(rt.CONFIG.get("outline_book_reviewer", {}).get("max_repair_chapters", 80) or 80)
    targets = sorted({chapter for chapter in chapters if 1 <= chapter <= total})[:max(1, limit)]
    if not targets:
        rt.log("[Coordinator] 整本大纲总审未提供可定位章节，无法自动修复")
        return False
    unlocked = unlock_chapters(rt.NOVELS_DIR, targets)
    if unlocked:
        rt.log(f"[Coordinator] 整本审查修复前已解锁批次: {unlocked}")

    rt.log(f"[Coordinator] 整本大纲总审第{round_no}轮修复，重生成章节: {targets}")
    for chapter in targets:
        feedback = _global_outline_feedback(rt, chapter, issues, round_no)
        drop_outline_artifacts(rt, chapter)
        if not process_outline_gate(rt, chapter, push_on_failure=False, initial_feedback=feedback):
            rt.log(f"[Coordinator] 第{chapter}章应用整本总审反馈后仍未通过逐章大纲门")
            return False
    return True


def prepare_all_outlines_and_book_review(rt, end: int, force_review: bool = False) -> bool:
    rt.log(f"[Coordinator] 全量大纲优先模式：先完成第1-{end}章逐章大纲质量门")
    for chapter in range(1, end + 1):
        if not process_outline_gate(rt, chapter):
            return False
    if run_outline_book_review(rt, force=force_review):
        return True

    max_rounds = int(rt.CONFIG.get("outline_book_reviewer", {}).get("max_repair_rounds", 2) or 2)
    for round_no in range(1, max_rounds + 1):
        if not repair_outline_book_review(rt, round_no):
            return False
        if run_outline_book_review(rt, force=True):
            return True
    return False
