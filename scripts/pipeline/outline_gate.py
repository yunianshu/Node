#!/usr/bin/env python3
"""Outline quality-gate orchestration used by Coordinator and outline lanes."""
from __future__ import annotations

import re
import sys
import threading
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
    outline_file = outline_chapter_path(rt.NOVELS_DIR, chapter)
    review_file = rt._outline_review_file(chapter)
    if outline_file.exists() and review_file.exists():
        try:
            if review_file.stat().st_mtime + 0.5 < outline_file.stat().st_mtime:
                return False
        except OSError:
            return False
    min_score = float(rt.CONFIG.get("outline_reviewer", {}).get("min_score", 8.5))
    _, _, _, ok = load_outline_review_status(
        review_file,
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
    repair_feedback: Path | None = None,
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
        if repair_feedback is not None:
            args += ["--repair-feedback", str(repair_feedback)]
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
        if repair_feedback is not None:
            reviewer_args += ["--repair-feedback", str(repair_feedback)]
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


def _valid_feedback_score(review: dict) -> float | None:
    status = str(review.get("status", "")).strip().lower()
    if status and status != "completed":
        return None
    try:
        return float(review.get("overall_score"))
    except (TypeError, ValueError):
        return None


def _select_candidate_feedback_reviews(reviews: list[dict], limit: int = 2) -> list[dict]:
    valid = [
        (index, score, review)
        for index, review in enumerate(reviews)
        if isinstance(review, dict)
        and (score := _valid_feedback_score(review)) is not None
    ]
    if not valid:
        return [review for review in reviews[-limit:] if isinstance(review, dict)]

    best = max(valid, key=lambda item: (item[1], item[0]))
    latest = valid[-1]
    selected = [best]
    if latest[0] != best[0] and limit > 1:
        selected.append(latest)
    return [item[2] for item in sorted(selected, key=lambda item: item[0])][-limit:]


def _write_outline_feedback(
    rt,
    chapter: int,
    reviews: list[dict],
    round_no: int,
    *,
    base_feedback: Path | None = None,
) -> Path:
    """Write retry feedback without discarding book-review constraints."""
    feedback_file = rt._write_gate_feedback(chapter, "outline", reviews, round_no)
    if base_feedback is None:
        return feedback_file

    base_payload = rt._load_json_file(base_feedback)
    generated_payload = rt._load_json_file(feedback_file)
    key = str(chapter)
    base_item = base_payload.get(key) if isinstance(base_payload, dict) else None
    generated_item = generated_payload.get(key) if isinstance(generated_payload, dict) else None
    if not isinstance(base_item, dict) or not isinstance(generated_item, dict):
        return feedback_file

    merged = dict(base_item)
    base_analysis = base_item.get("failure_analysis")
    generated_analysis = generated_item.get("failure_analysis")
    base_analysis = base_analysis if isinstance(base_analysis, dict) else {}
    generated_analysis = generated_analysis if isinstance(generated_analysis, dict) else {}

    def unique_values(*groups) -> list:
        values = []
        for group in groups:
            if not isinstance(group, list):
                continue
            for value in group:
                if value not in values:
                    values.append(value)
        return values

    merged["analysis_round"] = round_no
    merged["failure_analysis"] = {
        **generated_analysis,
        "attempts": generated_analysis.get("attempts", len(reviews)),
        "likely_reasons": unique_values(
            base_analysis.get("likely_reasons"),
            generated_analysis.get("likely_reasons"),
        )[:12],
        "adjustments": unique_values(
            base_analysis.get("adjustments"),
            generated_analysis.get("adjustments"),
        )[:12],
    }

    base_reviews = base_item.get("reviews")
    base_reviews = base_reviews if isinstance(base_reviews, list) else []
    # Keep the book-review directive, the highest-scoring actionable candidate,
    # and the latest valid candidate. This prevents later low-score or invalid
    # reports from discarding the best cumulative repair base.
    merged["reviews"] = base_reviews[-1:] + _select_candidate_feedback_reviews(reviews)
    atomic_write_json(feedback_file, {key: merged})
    return feedback_file



def process_outline_gate(rt, chapter: int, *, push_on_failure: bool = True, initial_feedback: Path | None = None) -> bool:
    if is_chapter_locked(rt.NOVELS_DIR, chapter):
        if initial_feedback is None:
            rt.log(f"[Coordinator] 第{chapter}章所在25章批次已锁定，跳过修订")
            return outline_gate_passed(rt, chapter)
        unlocked = unlock_chapters(rt.NOVELS_DIR, [chapter])
        rt.log(f"[Coordinator] 应用修订反馈前解锁批次: {unlocked}")

    if initial_feedback is None and outline_chapter_path(rt.NOVELS_DIR, chapter).exists() and outline_gate_passed(rt, chapter):
        rt.log(f"[Coordinator] 第{chapter}章大纲和大纲审已通过，跳过")
        return True

    max_rounds = int(rt.CONFIG.get("coordinator", {}).get("outline_analysis_rounds", 3) or 3)
    attempts_per_round = int(rt.CONFIG.get("coordinator", {}).get("outline_attempts_per_round", 3) or 3)
    reviews: list[dict] = []
    feedback_file: Path | None = initial_feedback
    base_feedback: Path | None = initial_feedback

    for round_no in range(1, max_rounds + 1):
        if reviews:
            feedback_file = _write_outline_feedback(
                rt,
                chapter,
                reviews,
                round_no,
                base_feedback=base_feedback,
            )
            rt.log(f"[Coordinator] 第{chapter}章大纲进入第{round_no}轮原因调整: {feedback_file}")

        for attempt in range(1, attempts_per_round + 1):
            rt.log(f"[Coordinator] 第{chapter}章大纲生成/初审 {round_no}.{attempt}")
            existing_review = rt._load_json_file(rt._outline_review_file(chapter))
            if existing_review and base_feedback is None:
                reviews.append(rt._review_feedback(existing_review, chapter, "outline", round_no, attempt))
                feedback_file = _write_outline_feedback(
                    rt,
                    chapter,
                    reviews,
                    round_no,
                    base_feedback=base_feedback,
                )
                rt.log(f"[Coordinator] 第{chapter}章读取现有大纲审查意见，反馈给本次修订: {feedback_file}")
            rt._drop_text_artifacts(chapter, reason="大纲正在修订")
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
                feedback_file = _write_outline_feedback(
                    rt,
                    chapter,
                    reviews,
                    round_no,
                    base_feedback=base_feedback,
                )
                rt.log(f"[Coordinator] 第{chapter}章大纲生成失败，下一次修订将使用反馈: {feedback_file}")
                continue

            rc, review_data = run_outline_review_rounds(
                rt,
                chapter,
                outline_chapter_path(rt.NOVELS_DIR, chapter),
                rt._outline_review_file(chapter),
                repair_feedback=feedback_file,
            )
            if rc != 0:
                reviews.append({
                    "chapter": chapter,
                    "status": "outline_reviewer_failed",
                    "weaknesses": ["大纲审查器执行失败"],
                    "suggestions": ["重新生成大纲并确保结构完整、剧情冲突明确、伏笔和能力进展具体"],
                })
                feedback_file = _write_outline_feedback(
                    rt,
                    chapter,
                    reviews,
                    round_no,
                    base_feedback=base_feedback,
                )
                rt.log(f"[Coordinator] 第{chapter}章大纲审查失败，下一次修订将使用反馈: {feedback_file}")
                continue

            reviews.append(rt._review_feedback(review_data, chapter, "outline", round_no, attempt))
            if outline_gate_passed(rt, chapter):
                build_ledgers(rt.NOVELS_DIR)
                rt.log(f"[Coordinator] 第{chapter}章大纲初审通过")
                return True
            if review_data.get("review_budget", {}).get("exhausted") is True:
                rt.log(f"[Coordinator] 第{chapter}章审查预算耗尽，停止继续盲目修订")
                report = rt._write_failure_report(chapter, "outline", reviews)
                if push_on_failure:
                    rt._push_gate_failure(chapter, "大纲初审", report)
                return False
            feedback_file = _write_outline_feedback(
                rt,
                chapter,
                reviews,
                round_no,
                base_feedback=base_feedback,
            )
            rt.log(f"[Coordinator] 第{chapter}章大纲未通过，下一次修订将使用反馈: {feedback_file}")

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
    rt.log(f"[Coordinator] 第{chapter}章审查指向第{overlap_chapter}章存在重叠，已先排除后章，后续将修订第{overlap_chapter}章大纲")
    return overlap_chapter


def ensure_outline_lookahead(rt, chapter: int, end: int, lookahead: int) -> tuple[bool, int | None]:
    start_chapter, end_chapter = outline_lookahead_window(chapter, end, lookahead)
    rt.log(f"[Coordinator] 写第{chapter}章前，确保第{start_chapter}-{end_chapter}章大纲已通过")
    for outline_chapter in range(start_chapter, end_chapter + 1):
        if not process_outline_gate(rt, outline_chapter, push_on_failure=False):
            return False, outline_chapter
    return True, None


def _global_outline_feedback(
    rt,
    chapter: int,
    issues: list[dict],
    round_no: int,
    *,
    source: str = "outline_book_review",
    path_prefix: str = "outline_book_feedback",
    summary: str = "整本大纲总审要求修复跨章结构问题",
) -> Path:
    relevant = [issue for issue in issues if isinstance(issue, dict)]
    repair_items = []
    for index, item in enumerate(relevant, start=1):
        problem = str(item.get("detail", "")).strip()
        acceptance = str(item.get("suggestion", "")).strip() or problem
        if not problem and not acceptance:
            continue
        repair_items.append({
            "id": f"R{index}",
            "severity": item.get("severity", "major"),
            "chapters": item.get("chapters", []),
            "category": item.get("category", ""),
            "problem": problem[:500],
            "evidence": str(item.get("evidence", "")).strip()[:400],
            "acceptance": acceptance[:500],
        })
    reasons = [
        f"{item['id']}: {item['problem']}"
        for item in repair_items
        if item.get("problem")
    ]
    suggestions = [
        f"{item['id']}: {item['acceptance']}"
        for item in repair_items
        if item.get("acceptance")
    ]
    anchor_numbers = sorted({
        value
        for issue in relevant
        for value in issue.get("chapters", [])
        if isinstance(value, int) and value < chapter
    })
    fact_anchors = []
    for anchor in anchor_numbers:
        data = rt._load_json_file(outline_chapter_path(rt.NOVELS_DIR, anchor))
        if not data:
            continue
        fact_anchors.append({
            "chapter": anchor,
            "role": "earlier_fact_anchor",
            "title": data.get("title", ""),
            "time_progression": data.get("time_progression", ""),
            "location": data.get("location", ""),
            "characters_involved": data.get("characters_involved", []),
            "summary": str(data.get("summary", ""))[:260],
            "key_events": data.get("key_events", [])[:5] if isinstance(data.get("key_events"), list) else data.get("key_events", ""),
            "foreshadowing": str(data.get("foreshadowing", ""))[:180],
            "chapter_hook": str(data.get("chapter_hook", ""))[:180],
        })
    payload = {
        str(chapter): {
            "chapter": chapter,
            "gate": "outline_book_review",
            "source": source,
            "analysis_round": round_no,
            "repair_policy": (
                "最早章节是已确认事实锚点。本章只能兼容锚点，"
                "不得通过改写、否定或重复锚点事件来消除冲突。"
                "逐条完成 repair_items；每条必须有明确剧情事件闭环。"
            ),
            "fact_anchors": fact_anchors,
            "repair_items": repair_items[:8],
            "failure_analysis": {
                "attempts": 0,
                "issue_count": len(relevant),
                "likely_reasons": reasons[:8],
                "adjustments": suggestions[:8] or reasons[:8],
            },
            "reviews": [{
                "overall_score": None,
                "verdict": "需重写",
                "weaknesses": reasons[:6],
                "suggestions": suggestions[:6],
                "continuity_issues": reasons[:3],
                "summary": summary,
            }],
        }
    }
    path = rt.LOGS_DIR / f"{path_prefix}_ch{chapter:04d}_round{round_no}.json"
    atomic_write_json(path, payload)
    return path


def _target_chapters_from_text(*parts: str, total: int) -> list[int]:
    chapters: list[int] = []
    for text in parts:
        for match in re.findall(r"(?:第\s*|ch(?:apter)?\s*)(\d+)\s*章?", str(text), flags=re.I):
            chapter = int(match)
            if 1 <= chapter <= total and chapter not in chapters:
                chapters.append(chapter)
    return sorted(chapters)


def _relationship_repair_issues_from_book_review(report: dict, total: int, limit: int) -> dict[int, list[dict]]:
    targets = report.get("relationship_repair_targets")
    if not isinstance(targets, list):
        return {}
    assigned: dict[int, list[dict]] = {}
    for target in targets:
        if not isinstance(target, dict):
            continue
        try:
            chapter = int(target.get("target_chapter"))
        except (TypeError, ValueError):
            continue
        if not 1 <= chapter <= total:
            continue
        pair = str(target.get("pair", "")).strip()
        problem = str(target.get("problem", "")).strip()
        evidence = str(target.get("evidence", "")).strip()
        acceptance = str(target.get("acceptance", "")).strip()
        if not problem and not acceptance:
            continue
        chapters = _target_chapters_from_text(problem, evidence, acceptance, total=total)
        if chapter not in chapters:
            chapters.append(chapter)
        chapters = sorted(chapters)
        assigned.setdefault(chapter, []).append({
            "severity": "major",
            "category": "relationship_repair",
            "chapters": chapters,
            "detail": f"{pair}: {problem}" if pair else problem,
            "evidence": evidence,
            "suggestion": acceptance,
        })
        if len(assigned) >= limit:
            break
    return {chapter: items[:1] for chapter, items in sorted(assigned.items())}


def _human_warmth_repair_issues_from_local_scan(local_scan: dict, total: int, limit: int) -> dict[int, list[dict]]:
    issues = local_scan.get("issues")
    if not isinstance(issues, list):
        return {}
    assigned: dict[int, list[dict]] = {}
    for issue in issues:
        if not isinstance(issue, dict) or issue.get("category") != "human_warmth_streak":
            continue
        chapters = sorted({
            value
            for value in issue.get("chapters", [])
            if isinstance(value, int) and 1 <= value <= total
        })
        if not chapters:
            continue
        # Keep the first chapter as the observed fact anchor; repair the later
        # chapter where the pattern should be broken with a concrete human beat.
        target = chapters[-1] if len(chapters) >= 2 else chapters[0]
        detail = str(issue.get("detail", "")).strip()
        evidence = str(issue.get("evidence", "")).strip()
        suggestion = (
            "本章必须补一个能打断连续空泛感的烟火气场景：至少包含可触摸生活物件、"
            "一句带问句或试探意味的对白、一个配角主动选择；这些元素必须推动剧情或关系变化，"
            "不能只作为环境描写。"
        )
        assigned.setdefault(target, []).append({
            "severity": issue.get("severity", "major"),
            "category": "human_warmth_streak_repair",
            "chapters": chapters,
            "detail": detail or "连续多章缺少烟火气触点",
            "evidence": evidence,
            "suggestion": suggestion,
        })
        if len(assigned) >= limit:
            break
    return {chapter: items[:1] for chapter, items in sorted(assigned.items())}


def repair_book_relationship_targets(rt, round_no: int = 1) -> list[int]:
    """Apply final book-review relationship repair targets to chapter outlines.

    Returns the chapters whose outlines were repaired and whose text artifacts
    should be regenerated on the next Coordinator pass.
    """
    report = rt._load_json_file(report_path(rt.NOVELS_DIR, "book_review") / "final_book_review.json")
    if not report:
        return []
    total = int(rt.CONFIG["total_chapters"])
    cfg = rt.CONFIG.get("book_reviewer", {}) if isinstance(rt.CONFIG.get("book_reviewer"), dict) else {}
    default_limit = 10
    limit = int(cfg.get("max_relationship_repair_chapters", default_limit) or default_limit)
    assigned = _relationship_repair_issues_from_book_review(report, total=total, limit=max(1, limit))
    targets = sorted(assigned)
    if not targets:
        return []

    unlocked = unlock_chapters(rt.NOVELS_DIR, targets)
    if unlocked:
        rt.log(f"[Coordinator] 关系线终审修复前已解锁批次: {unlocked}")
    rt.log(f"[Coordinator] 终审关系线问题回灌到大纲门: {targets}")
    repaired: list[int] = []
    for chapter in targets:
        feedback = _global_outline_feedback(
            rt,
            chapter,
            assigned.get(chapter, []),
            round_no,
            source="book_relationship_review",
            path_prefix="book_relationship_feedback",
            summary="整本终审要求修复人物关系欠账和人情味回声",
        )
        if not process_outline_gate(rt, chapter, push_on_failure=False, initial_feedback=feedback):
            rt.log(f"[Coordinator] 第{chapter}章应用关系线终审反馈后仍未通过逐章大纲门")
            break
        rt._drop_text_artifacts(chapter, reason="整本终审关系线修复目标已回灌大纲，需重写正文")
        repaired.append(chapter)
    return repaired


def repair_book_human_warmth_streaks(rt, round_no: int = 1) -> list[int]:
    """Apply final local-scan human warmth streak issues to chapter outlines."""
    local_scan = rt._load_json_file(report_path(rt.NOVELS_DIR, "book_review") / "local_full_scan.json")
    if not local_scan:
        return []
    total = int(rt.CONFIG["total_chapters"])
    cfg = rt.CONFIG.get("book_reviewer", {}) if isinstance(rt.CONFIG.get("book_reviewer"), dict) else {}
    default_limit = 10
    limit = int(cfg.get("max_human_warmth_repair_chapters", default_limit) or default_limit)
    assigned = _human_warmth_repair_issues_from_local_scan(local_scan, total=total, limit=max(1, limit))
    targets = sorted(assigned)
    if not targets:
        return []

    unlocked = unlock_chapters(rt.NOVELS_DIR, targets)
    if unlocked:
        rt.log(f"[Coordinator] 烟火气连续性修复前已解锁批次: {unlocked}")
    rt.log(f"[Coordinator] 终审烟火气连续性问题回灌到大纲门: {targets}")
    repaired: list[int] = []
    for chapter in targets:
        feedback = _global_outline_feedback(
            rt,
            chapter,
            assigned.get(chapter, []),
            round_no,
            source="book_human_warmth_scan",
            path_prefix="book_human_warmth_feedback",
            summary="整本终审本地扫描要求修复连续缺少烟火气和配角主动选择的问题",
        )
        if not process_outline_gate(rt, chapter, push_on_failure=False, initial_feedback=feedback):
            rt.log(f"[Coordinator] 第{chapter}章应用烟火气连续性反馈后仍未通过逐章大纲门")
            break
        rt._drop_text_artifacts(chapter, reason="整本终审烟火气连续性修复目标已回灌大纲，需重写正文")
        repaired.append(chapter)
    return repaired
