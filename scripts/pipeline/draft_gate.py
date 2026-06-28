#!/usr/bin/env python3
"""Draft quality-gate orchestration used by Coordinator."""
from __future__ import annotations

import sys
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from core.workflow_state import load_review_status, read_text_length, scan_one_chapter


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _atomic_copy_text(source: Path, target: Path) -> None:
    _atomic_write_text(target, source.read_text(encoding="utf-8", errors="ignore"))


def draft_min_score(rt, chapter: int | None = None) -> float:
    """返回正文通过门禁的最低评分。

    G10 黄金三章专项门禁：第1-3章使用更高阈值（golden_chapter_min_score），
    因为前3章决定全书追读率。后续章节使用标准 min_score。
    """
    base = float(rt.CONFIG.get("reviewer", {}).get("min_score", 8.5))
    if chapter is not None and chapter <= 3:
        golden = float(rt.CONFIG.get("reviewer", {}).get("golden_chapter_min_score", 9.0))
        return max(base, golden)
    return base


def draft_gate_passed(rt, chapter: int) -> bool:
    _, _, _, ok = load_review_status(rt._review_file(chapter), draft_min_score(rt, chapter))
    return ok


def promote_draft_to_final(rt, chapter: int) -> bool:
    source = rt._draft_file(chapter)
    target = rt._final_file(chapter)
    if not source.exists():
        return False
    _atomic_copy_text(source, target)
    rt.log(f"[Coordinator] 第{chapter}章初稿审查通过，已写入终稿 -> {target}")
    return True


def draft_race_config(rt) -> dict:
    cfg = rt.CONFIG.get("draft_race", {}) if isinstance(rt.CONFIG.get("draft_race"), dict) else {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "candidates": max(1, int(cfg.get("candidates", 3) or 3)),
        "max_workers": max(1, int(cfg.get("max_workers", cfg.get("candidates", 3)) or 3)),
        "stop_on_first_pass": bool(cfg.get("stop_on_first_pass", False)),
        "early_stop_score": float(cfg.get("early_stop_score", 9.5) or 9.5),
    }


def final_ready(rt, chapter: int) -> bool:
    return scan_one_chapter(rt.NOVELS_DIR, chapter).final_ok


def _draft_candidate_paths(rt, chapter: int, round_no: int, attempt: int, candidate_no: int) -> tuple[Path, Path, Path]:
    root = rt.LOGS_DIR / "draft_candidates" / f"ch{chapter:04d}" / f"round{round_no}_attempt{attempt}"
    prefix = f"candidate_{candidate_no:02d}"
    return root / f"{prefix}.txt", root / f"{prefix}_review.json", root / f"{prefix}.log"


def _candidate_score(result: dict) -> float:
    try:
        return float(result.get("overall_score"))
    except (TypeError, ValueError):
        return -1.0


def _candidate_feedback(rt, review_data: dict, chapter: int, candidate_no: int, round_no: int, attempt: int) -> dict:
    item = rt._review_feedback(review_data, chapter, "draft", round_no, attempt)
    item["candidate"] = candidate_no
    return item


def _run_draft_candidate(
    rt,
    chapter: int,
    round_no: int,
    attempt: int,
    candidate_no: int,
    feedback_file: Path | None,
    stop_event: threading.Event,
) -> dict:
    draft_file, candidate_review_file, child_log = _draft_candidate_paths(rt, chapter, round_no, attempt, candidate_no)
    draft_file.parent.mkdir(parents=True, exist_ok=True)
    rt._safe_unlink(draft_file)
    rt._safe_unlink(candidate_review_file)

    writer_args = [
        sys.executable,
        str(rt.resolve_script_path("writer.py")),
        "--project",
        str(rt.NOVELS_DIR),
        "--chapter",
        str(chapter),
        "--output-file",
        str(draft_file),
    ]
    if feedback_file:
        writer_args += ["--review-feedback", str(feedback_file)]
    rt.log(f"[Coordinator] 第{chapter}章正文候选{candidate_no}开始生成")
    rc = rt.run_cancellable_process(writer_args, child_log, stop_event)
    if rc != 0:
        return {
            "chapter": chapter,
            "candidate": candidate_no,
            "status": "writer_failed" if rc != -9 else "cancelled",
            "weaknesses": ["候选正文生成失败、被取消或输出不完整"],
            "suggestions": ["重新生成时必须严格执行大纲、满足字数并强化冲突推进"],
        }

    exists, words, text_ok = read_text_length(draft_file)
    if not exists or not text_ok:
        return {
            "chapter": chapter,
            "candidate": candidate_no,
            "status": "candidate_text_quality_failed",
            "weaknesses": [f"候选正文未通过本地质量门，字数={words}"],
            "suggestions": ["下一次必须写到5000字以上，确保结尾完整、无截断、无重复段落"],
            "candidate_file": str(draft_file),
            "review_file": str(candidate_review_file),
        }

    reviewer_args = [
        sys.executable,
        str(rt.resolve_script_path("reviewer.py")),
        "--project",
        str(rt.NOVELS_DIR),
        "--chapter",
        str(chapter),
        "--chapter-file",
        str(draft_file),
        "--review-file",
        str(candidate_review_file),
    ]
    rt.log(f"[Coordinator] 第{chapter}章正文候选{candidate_no}开始审查")
    rc = rt.run_cancellable_process(reviewer_args, child_log, stop_event)
    if rc != 0:
        return {
            "chapter": chapter,
            "candidate": candidate_no,
            "status": "reviewer_failed" if rc != -9 else "cancelled",
            "weaknesses": ["候选正文审查器执行失败或被取消"],
            "suggestions": ["检查候选日志、模型响应和审查JSON写入"],
        }

    review_data = rt._load_json_file(candidate_review_file)
    result = _candidate_feedback(rt, review_data, chapter, candidate_no, round_no, attempt)
    _, _, _, ok = load_review_status(candidate_review_file, draft_min_score(rt, chapter))
    result["passed"] = ok
    result["candidate_file"] = str(draft_file)
    result["review_file"] = str(candidate_review_file)
    return result


def _publish_draft_candidate(rt, chapter: int, draft_file: Path, review_file: Path) -> tuple[bool, str]:
    exists, words, text_ok = read_text_length(draft_file)
    if not exists or not text_ok:
        return False, f"candidate_text_quality_failed words={words}"
    _, status, score, review_ok = load_review_status(review_file, draft_min_score(rt, chapter))
    if not review_ok:
        return False, f"candidate_review_failed status={status} score={score} min_score={draft_min_score(rt, chapter):g}"

    target_draft = rt._draft_file(chapter)
    target_review = rt._review_file(chapter)
    _atomic_copy_text(draft_file, target_draft)
    _atomic_copy_text(review_file, target_review)
    promote_draft_to_final(rt, chapter)
    if not final_ready(rt, chapter):
        return False, "final_quality_failed"
    return True, f"review_score={score} words={words}"


def _process_draft_gate_race(rt, chapter: int) -> bool:
    if final_ready(rt, chapter):
        return True

    max_rounds = int(rt.CONFIG.get("coordinator", {}).get("draft_analysis_rounds", 3) or 3)
    attempts_per_round = int(rt.CONFIG.get("coordinator", {}).get("draft_attempts_per_round", 3) or 3)
    race_cfg = draft_race_config(rt)
    candidates = race_cfg["candidates"]
    max_workers = min(candidates, race_cfg["max_workers"])
    reviews: list[dict] = []
    feedback_file: Path | None = None

    existing_review = rt._load_json_file(rt._review_file(chapter))
    if existing_review:
        reviews.append(rt._review_feedback(existing_review, chapter, "draft", 1, 0))
        feedback_file = rt._write_gate_feedback(chapter, "draft", reviews, 1)
        rt.log(f"[Coordinator] 第{chapter}章读取现有初稿审查意见，反馈给正文候选: {feedback_file}")

    for round_no in range(1, max_rounds + 1):
        if reviews:
            feedback_file = rt._write_gate_feedback(chapter, "draft", reviews, round_no)
            rt.log(f"[Coordinator] 第{chapter}章正文赛马进入第{round_no}轮原因调整: {feedback_file}")

        for attempt in range(1, attempts_per_round + 1):
            rt.log(f"[Coordinator] 第{chapter}章正文候选赛马 {round_no}.{attempt} candidates={candidates}")
            stop_event = threading.Event()
            accepted: dict | None = None
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_candidates = {
                    executor.submit(
                        _run_draft_candidate,
                        rt,
                        chapter,
                        round_no,
                        attempt,
                        candidate_no,
                        feedback_file,
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
                        if (
                            race_cfg["stop_on_first_pass"]
                            and _candidate_score(result) >= race_cfg["early_stop_score"]
                        ):
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
                ok, detail = _publish_draft_candidate(
                    rt,
                    chapter,
                    Path(str(accepted["candidate_file"])),
                    Path(str(accepted["review_file"])),
                )
                if ok:
                    rt.log(f"[Coordinator] 第{chapter}章正文候选赛马通过，已发布 ({detail})")
                    return True
                reviews.append({
                    "chapter": chapter,
                    "status": "candidate_publish_failed",
                    "weaknesses": [detail],
                    "suggestions": ["重新生成候选并检查正式目录写入和本地质量门"],
                })
                feedback_file = rt._write_gate_feedback(chapter, "draft", reviews, round_no)
                rt.log(f"[Coordinator] 第{chapter}章正文候选发布失败，下一次使用反馈: {feedback_file}")
                continue

            feedback_file = rt._write_gate_feedback(chapter, "draft", reviews, round_no)
            best = rt._failure_analysis(chapter, "draft", reviews).get("best_score")
            rt.log(f"[Coordinator] 第{chapter}章本轮正文候选均未通过，最佳分数={best}，下一次使用汇总反馈: {feedback_file}")

    report = rt._write_failure_report(chapter, "draft", reviews)
    rt._push_gate_failure(chapter, "初稿审查", report)
    return False


def _run_polish_only(rt, chapter: int, candidate_id: int, temperature: float) -> bool:
    rc = rt.run_script(
        "polisher.py",
        "--chapter",
        str(chapter),
        "--candidate-id",
        str(candidate_id),
        "--temperature",
        str(temperature),
    )
    if rc != 0:
        rt.log(f"[Coordinator] 第{chapter}章 Polisher 候选{candidate_id} 生成失败")
        return False
    return True


def _review_candidate(rt, chapter: int, candidate_id: int) -> tuple[float, dict]:
    cand_draft = rt._candidate_draft_file(chapter, candidate_id)
    cand_review = rt._candidate_review_file(chapter, candidate_id)

    if not cand_draft.exists():
        return 0.0, {}

    rt._safe_unlink(cand_review)
    rc = rt.run_script(
        "reviewer.py",
        "--chapter",
        str(chapter),
        "--chapter-file",
        str(cand_draft),
        "--review-file",
        str(cand_review),
    )
    if rc != 0 or not cand_review.exists():
        rt.log(f"[Coordinator] 第{chapter}章 Polisher 候选{candidate_id} 评分失败")
        return 0.0, {}

    review_data = rt._load_json_file(cand_review)
    score = rt._score_from_review(review_data)
    rt.log(f"[Coordinator] 第{chapter}章 Polisher 候选{candidate_id} 评分: {score}")
    return score, review_data


def _try_polish_race(rt, chapter: int, round_no: int, attempt: int, reviews: list[dict]) -> bool:
    race_cfg = rt.CONFIG.get("polisher", {}).get("race", {})
    candidates = int(race_cfg.get("candidates", 3) or 3)
    max_workers = int(race_cfg.get("max_workers", 3) or 3)
    stop_on_first_pass = bool(race_cfg.get("stop_on_first_pass", False))
    base_temp = float(rt.CONFIG.get("polisher", {}).get("temperature", 0.2))
    temps = [round(max(0.0, min(1.0, base_temp + (i - candidates // 2) * 0.05)), 2) for i in range(1, candidates + 1)]

    current_review = rt._load_json_file(rt._review_file(chapter))
    current_score = rt._score_from_review(current_review)
    best_score = rt._load_best_score(chapter)
    if current_score >= best_score:
        rt._save_best_draft(chapter, current_score, rt._draft_file(chapter))
        best_score = current_score

    main_draft = rt._draft_file(chapter)
    main_review = rt._review_file(chapter)
    rt.log(f"[Coordinator] 第{chapter}章评分 {current_score}，触发 Polisher 赛马模式（{candidates} 候选并行精修）")

    polished_ids: list[int] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_run_polish_only, rt, chapter, i, temps[i - 1]): i
            for i in range(1, candidates + 1)
        }
        for future in futures:
            i = futures[future]
            try:
                ok = future.result()
            except Exception as exc:
                rt.log(f"[Coordinator] 第{chapter}章 Polisher 候选{i} 任务异常: {exc}")
                ok = False
            if ok:
                polished_ids.append(i)

    if not polished_ids:
        rt.log(f"[Coordinator] 第{chapter}章 Polisher 赛马模式无候选生成成功，回退到最佳草稿")
        rt._restore_best_draft(chapter)
        return False

    results: list[tuple[int, float, dict]] = []
    for i in polished_ids:
        score, review_data = _review_candidate(rt, chapter, i)
        if not review_data:
            continue
        results.append((i, score, review_data))
        reviews.append(rt._review_feedback(review_data, chapter, "draft", round_no, attempt, label=f"polish_race_{i}"))
        if stop_on_first_pass and score >= draft_min_score(rt, chapter):
            break

    if not results:
        rt.log(f"[Coordinator] 第{chapter}章 Polisher 赛马模式无可用评分，回退到最佳草稿")
        rt._restore_best_draft(chapter)
        return False

    best_cand_id, best_cand_score, _ = max(results, key=lambda x: x[1])
    cand_draft = rt._candidate_draft_file(chapter, best_cand_id)
    cand_review = rt._candidate_review_file(chapter, best_cand_id)
    if cand_draft.exists():
        _atomic_copy_text(cand_draft, main_draft)
    if cand_review.exists():
        _atomic_copy_text(cand_review, main_review)

    rt.log(f"[Coordinator] 第{chapter}章 Polisher 赛马模式最优候选: {best_cand_id}，评分 {best_cand_score}")

    if best_cand_score > best_score:
        rt._save_best_draft(chapter, best_cand_score, main_draft)
        best_score = best_cand_score

    for i in range(1, candidates + 1):
        rt._safe_unlink(rt._candidate_draft_file(chapter, i))
        rt._safe_unlink(rt._candidate_review_file(chapter, i))

    if draft_gate_passed(rt, chapter):
        return True

    if best_cand_score <= best_score:
        rt.log(f"[Coordinator] 第{chapter}章 Polisher 赛马模式最优候选 {best_cand_score} 分未超过历史最佳 {best_score} 分，回退到最佳草稿")
        restored_score = rt._restore_best_draft(chapter)
        if restored_score >= draft_min_score(rt, chapter):
            return promote_draft_to_final(rt, chapter)
        return False

    return False


def _try_polish_pass(rt, chapter: int, round_no: int, attempt: int, reviews: list[dict]) -> bool:
    enable_polisher = bool(rt.CONFIG.get("polisher", {}).get("enabled", True))
    if not enable_polisher:
        return False
    polish_threshold = float(rt.CONFIG.get("polisher", {}).get("threshold", 8.0))
    current_review = rt._load_json_file(rt._review_file(chapter))
    current_score = rt._score_from_review(current_review)
    if current_score < polish_threshold:
        return False

    race_cfg = rt.CONFIG.get("polisher", {}).get("race", {})
    if race_cfg.get("enabled", False):
        return _try_polish_race(rt, chapter, round_no, attempt, reviews)

    max_polish_attempts = int(rt.CONFIG.get("polisher", {}).get("max_attempts", 3) or 3)
    best_score = rt._load_best_score(chapter)
    if current_score >= best_score:
        rt._save_best_draft(chapter, current_score, rt._draft_file(chapter))
        best_score = current_score

    last_score = current_score
    for polish_attempt in range(1, max_polish_attempts + 1):
        rt.log(f"[Coordinator] 第{chapter}章评分 {last_score}，触发 Polisher 精修（第{polish_attempt}次）")
        if rt.run_script("polisher.py", "--chapter", str(chapter)) != 0:
            restored_score = rt._restore_best_draft(chapter)
            if restored_score >= draft_min_score(rt, chapter):
                return promote_draft_to_final(rt, chapter)
            return False
        rt._safe_unlink(rt._review_file(chapter))
        if rt.run_script("reviewer.py", "--chapter", str(chapter)) != 0:
            rt._restore_best_draft(chapter)
            return False
        polish_review = rt._load_json_file(rt._review_file(chapter))
        reviews.append(rt._review_feedback(polish_review, chapter, "draft", round_no, attempt, label=f"polish_{polish_attempt}"))
        if draft_gate_passed(rt, chapter):
            return True
        new_score = rt._score_from_review(polish_review)
        if new_score > last_score and new_score > best_score:
            rt._save_best_draft(chapter, new_score, rt._draft_file(chapter))
            best_score = new_score
        if new_score <= last_score:
            rt.log(f"[Coordinator] 第{chapter}章 Polisher 后分数未提升（{last_score} -> {new_score}），回退到最佳草稿 {best_score} 分")
            restored_score = rt._restore_best_draft(chapter)
            if restored_score >= draft_min_score(rt, chapter):
                return promote_draft_to_final(rt, chapter)
            return False
        last_score = new_score
    return False


def process_draft_gate(rt, chapter: int) -> bool:
    if rt._draft_file(chapter).exists() and draft_gate_passed(rt, chapter):
        return promote_draft_to_final(rt, chapter)

    if draft_race_config(rt)["enabled"]:
        return _process_draft_gate_race(rt, chapter)

    max_rounds = int(rt.CONFIG.get("coordinator", {}).get("draft_analysis_rounds", 3) or 3)
    attempts_per_round = int(rt.CONFIG.get("coordinator", {}).get("draft_attempts_per_round", 3) or 3)
    enable_polisher = bool(rt.CONFIG.get("polisher", {}).get("enabled", True))
    polish_threshold = float(rt.CONFIG.get("polisher", {}).get("threshold", 8.0))
    reviews: list[dict] = []
    feedback_file = None

    for round_no in range(1, max_rounds + 1):
        if reviews:
            feedback_file = rt._write_gate_feedback(chapter, "draft", reviews, round_no)
            rt.log(f"[Coordinator] 第{chapter}章初稿进入第{round_no}轮原因调整: {feedback_file}")

        if enable_polisher and rt._draft_file(chapter).exists() and rt._review_file(chapter).exists():
            existing_review = rt._load_json_file(rt._review_file(chapter))
            existing_score = rt._score_from_review(existing_review)
            if polish_threshold <= existing_score < draft_min_score(rt, chapter):
                rt.log(f"[Coordinator] 第{chapter}章已有 {existing_score} 分底稿，跳过 writer 重写，直接 Polisher 精修")
                if _try_polish_pass(rt, chapter, round_no, 1, reviews):
                    return promote_draft_to_final(rt, chapter)

        for attempt in range(1, attempts_per_round + 1):
            rt.log(f"[Coordinator] 第{chapter}章初稿生成/审查 {round_no}.{attempt}")
            writer_args = ["--chapter", str(chapter)]
            if feedback_file:
                writer_args += ["--review-feedback", str(feedback_file)]
            if rt.run_script("writer.py", *writer_args) != 0:
                reviews.append({"chapter": chapter, "status": "writer_failed"})
                feedback_file = rt._write_gate_feedback(chapter, "draft", reviews, round_no)
                continue
            rt._safe_unlink(rt._review_file(chapter))
            if rt.run_script("reviewer.py", "--chapter", str(chapter)) != 0:
                reviews.append({"chapter": chapter, "status": "reviewer_failed"})
                continue

            review_data = rt._load_json_file(rt._review_file(chapter))
            current_score = rt._score_from_review(review_data)
            reviews.append(rt._review_feedback(review_data, chapter, "draft", round_no, attempt))
            feedback_file = rt._write_gate_feedback(chapter, "draft", reviews, round_no)

            best_score = rt._load_best_score(chapter)
            if current_score >= best_score:
                rt._save_best_draft(chapter, current_score, rt._draft_file(chapter))

            if draft_gate_passed(rt, chapter):
                return promote_draft_to_final(rt, chapter)

            if enable_polisher and current_score >= polish_threshold:
                if _try_polish_pass(rt, chapter, round_no, attempt, reviews):
                    return promote_draft_to_final(rt, chapter)

    best_score = rt._load_best_score(chapter)
    if best_score >= draft_min_score(rt, chapter):
        rt.log(f"[Coordinator] 第{chapter}章最终回退到历史最佳草稿 {best_score} 分并通过")
        rt._restore_best_draft(chapter)
        return promote_draft_to_final(rt, chapter)

    report = rt._write_failure_report(chapter, "draft", reviews)
    rt._push_gate_failure(chapter, "初稿审查", report)
    return False
