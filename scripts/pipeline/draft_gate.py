#!/usr/bin/env python3
"""Draft quality-gate orchestration used by Coordinator."""
from __future__ import annotations

from pathlib import Path

from core.workflow_state import load_review_status, read_text_length, scan_one_chapter, analyze_chapter_text, load_quality_rules, atomic_write_json
from core.review_quality import (review_quality_settings, read_manuscript, review_matches_text,
                                evaluate_review_quality_gate)
from core.ai_flavor_detector import check_text_ai_gate


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    tmp.replace(path)


def _atomic_copy_text(source: Path, target: Path) -> None:
    _atomic_write_text(target, source.read_text(encoding="utf-8", errors="ignore"))


def draft_min_score(rt, chapter: int | None = None) -> float:
    """返回正文通过门禁的最低评分。

    G10 黄金三章专项门禁：第1-3章使用更高阈值（golden_chapter_min_score），
    因为前3章决定全书追读率。后续章节使用标准 min_score。
    """
    settings = review_quality_settings(rt.CONFIG)
    base = settings["review_min_score"]
    if chapter is not None and chapter <= 3:
        golden = settings["golden_chapter_min_score"]
        return max(base, golden)
    return base


def draft_gate_passed(rt, chapter: int) -> bool:
    _, _, _, ok = load_review_status(rt._review_file(chapter), draft_min_score(rt, chapter))
    source = rt._draft_file(chapter)
    if not ok or not source.exists():
        return False
    data = rt._load_json_file(rt._review_file(chapter))
    return (data.get("status") == "completed" and data.get("verdict") == "通过"
            and review_matches_text(data, read_manuscript(source))
            and evaluate_review_quality_gate(data, data.get("local_analysis", {}), rt.CONFIG)["passed"])


def promote_draft_to_final(rt, chapter: int) -> bool:
    source = rt._draft_file(chapter)
    target = rt._final_file(chapter)
    if not source.exists():
        return False
    text = read_manuscript(source)
    if not draft_gate_passed(rt, chapter):
        return False
    review = rt._load_json_file(rt._review_file(chapter))
    if not review_matches_text(review, text):
        return False
    if not analyze_chapter_text(text, rules=load_quality_rules(rt.NOVELS_DIR))[2]:
        return False
    gate = check_text_ai_gate(text, project=rt.NOVELS_DIR)
    if not gate["passed"]:
        rt.log(f"[Coordinator] 第{chapter}章发布前AI硬门失败：{gate['reasons']}")
        return False
    previous = read_manuscript(target) if target.exists() else None
    _atomic_write_text(target, text)
    post_gate = check_text_ai_gate(read_manuscript(target), project=rt.NOVELS_DIR)
    if not post_gate["passed"]:
        record_final_gate_failure(rt, chapter, post_gate["reasons"])
        if previous is not None:
            _atomic_write_text(target, previous)
        else:
            target.unlink()
        rt.log(f"[Coordinator] 第{chapter}章落盘后AI复检失败，停止流程")
        return False
    rt.log(f"[Coordinator] 第{chapter}章初稿审查通过，已写入终稿 -> {target}")
    return True


def final_ready(rt, chapter: int) -> bool:
    return scan_one_chapter(rt.NOVELS_DIR, chapter).final_ok


def record_final_gate_failure(rt, chapter: int, reasons: list[str]) -> None:
    """持久化失败，后续扫描不能因一次偶然检测恢复就推进完成进度。"""
    review = rt._load_json_file(rt._review_file(chapter))
    if not review:
        return
    gate = review.get("quality_gate")
    gate = gate if isinstance(gate, dict) else {}
    gate["passed"] = False
    gate["reasons"] = list(dict.fromkeys([*gate.get("reasons", []), *reasons]))
    review["quality_gate"] = gate
    review["verdict"] = "需修改"
    atomic_write_json(rt._review_file(chapter), review)


def _review_current(rt, chapter: int, reviews: list[dict], round_no: int, attempt: int,
                    label: str = "") -> bool:
    rc = rt.run_script("reviewer.py", "--chapter", str(chapter), "--force")
    data = rt._load_json_file(rt._review_file(chapter))
    feedback = rt._review_feedback(data, chapter, "draft", round_no, attempt, label=label)
    feedback["execution_failed"] = rc != 0
    feedback["reviewer_exit_code"] = rc
    if rc != 0:
        feedback["report_status"] = data.get("status", "missing")
        feedback["status"] = "reviewer_failed"
    reviews.append(feedback)
    rt._write_gate_feedback(chapter, "draft", reviews, round_no)
    return rc == 0 and draft_gate_passed(rt, chapter)


def _restore_and_review(rt, chapter: int, reviews: list[dict], round_no: int, attempt: int) -> bool:
    """历史分数只用于选稿，恢复正文后必须重新审查。"""
    if rt._restore_best_draft(chapter) <= 0:
        return False
    return _review_current(rt, chapter, reviews, round_no, attempt, label="best_restored")


def _try_polish_pass(rt, chapter: int, round_no: int, attempt: int, reviews: list[dict]) -> bool:
    enable_polisher = bool(rt.CONFIG.get("polisher", {}).get("enabled", True))
    if not enable_polisher:
        return False
    polish_threshold = float(rt.CONFIG.get("polisher", {}).get("threshold", 8.0))
    current_review = rt._load_json_file(rt._review_file(chapter))
    current_score = rt._score_from_review(current_review)
    if current_review.get("status") != "completed" or current_score < polish_threshold:
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
            return _restore_and_review(rt, chapter, reviews, round_no, attempt)
        if _review_current(rt, chapter, reviews, round_no, attempt, label=f"polish_{polish_attempt}"):
            return True
        polish_review = rt._load_json_file(rt._review_file(chapter))
        if polish_review.get("status") != "completed":
            return _restore_and_review(rt, chapter, reviews, round_no, attempt)
        new_score = rt._score_from_review(polish_review)
        if new_score > last_score and new_score > best_score:
            rt._save_best_draft(chapter, new_score, rt._draft_file(chapter))
            best_score = new_score
        if new_score <= last_score:
            rt.log(f"[Coordinator] 第{chapter}章 Polisher 后分数未提升（{last_score} -> {new_score}），回退到最佳草稿 {best_score} 分")
            return _restore_and_review(rt, chapter, reviews, round_no, attempt)
        last_score = new_score
    return _restore_and_review(rt, chapter, reviews, round_no, attempt)


def process_draft_gate(rt, chapter: int) -> bool:
    if rt._draft_file(chapter).exists() and draft_gate_passed(rt, chapter):
        return promote_draft_to_final(rt, chapter)

    max_rounds = int(rt.CONFIG.get("coordinator", {}).get("draft_analysis_rounds", 2) or 2)
    attempts_per_round = int(rt.CONFIG.get("coordinator", {}).get("draft_attempts_per_round", 1) or 1)
    enable_polisher = bool(rt.CONFIG.get("polisher", {}).get("enabled", True))
    polish_threshold = float(rt.CONFIG.get("polisher", {}).get("threshold", 8.0))
    reviews: list[dict] = []
    feedback_file = None
    if rt._draft_file(chapter).exists():
        if _review_current(rt, chapter, reviews, 1, 0, label="existing_draft"):
            return promote_draft_to_final(rt, chapter)
        feedback_file = rt._write_gate_feedback(chapter, "draft", reviews, 1)

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
            passed = _review_current(rt, chapter, reviews, round_no, attempt)
            feedback_file = rt._write_gate_feedback(chapter, "draft", reviews, round_no)
            if reviews[-1]["execution_failed"]:
                continue

            review_data = rt._load_json_file(rt._review_file(chapter))
            current_score = rt._score_from_review(review_data)

            best_score = rt._load_best_score(chapter)
            if review_data.get("status") == "completed" and current_score >= best_score:
                rt._save_best_draft(chapter, current_score, rt._draft_file(chapter))

            if passed:
                return promote_draft_to_final(rt, chapter)

            if enable_polisher and current_score >= polish_threshold:
                if _try_polish_pass(rt, chapter, round_no, attempt, reviews):
                    return promote_draft_to_final(rt, chapter)

    best_score = rt._load_best_score(chapter)
    if best_score > 0 and _restore_and_review(rt, chapter, reviews, max_rounds, attempts_per_round):
        return promote_draft_to_final(rt, chapter)

    report = rt._write_failure_report(chapter, "draft", reviews)
    rt._push_gate_failure(chapter, "初稿审查", report)
    return False
