#!/usr/bin/env python3
"""Draft quality-gate orchestration used by Coordinator."""
from __future__ import annotations

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


def final_ready(rt, chapter: int) -> bool:
    return scan_one_chapter(rt.NOVELS_DIR, chapter).final_ok


def _try_polish_pass(rt, chapter: int, round_no: int, attempt: int, reviews: list[dict]) -> bool:
    enable_polisher = bool(rt.CONFIG.get("polisher", {}).get("enabled", True))
    if not enable_polisher:
        return False
    polish_threshold = float(rt.CONFIG.get("polisher", {}).get("threshold", 8.0))
    current_review = rt._load_json_file(rt._review_file(chapter))
    current_score = rt._score_from_review(current_review)
    if current_score < polish_threshold:
        return False

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
