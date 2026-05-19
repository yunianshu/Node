#!/usr/bin/env python3
"""小说生成工作流状态与质量门。"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable

MIN_CHAPTER_WORDS = 4500
MAX_CHAPTER_WORDS = 5500
WARN_MIN_CHAPTER_WORDS = 4300
WARN_MAX_CHAPTER_WORDS = 5800
HARD_FAIL_MIN_CHAPTER_WORDS = 3000
MIN_EXISTING_BYTES = 1000
MIN_PARAGRAPHS = 20
MAX_DUPLICATE_PARAGRAPH_RATIO = 0.25
MAX_SIMILAR_PARAGRAPH_RATIO = 0.20
SIMILAR_PARAGRAPH_THRESHOLD = 0.88
VALID_ENDINGS = tuple("。！？.!?」”’）)")
FORBIDDEN_PHRASES = (
    "无法生成",
    "抱歉",
    "作为AI",
    "未完待续",
    "内容省略",
    "此处省略",
    "TODO",
)
COMPLETED_REVIEW_REQUIRED_FIELDS = {
    "overall_score": (int, float),
    "verdict": str,
    "scores": dict,
    "summary": str,
}


@dataclass
class ChapterStatus:
    chapter: int
    draft_exists: bool = False
    draft_words: int = 0
    draft_grade: str = "missing"
    draft_ok: bool = False
    review_exists: bool = False
    review_status: str = "missing"
    review_score: float | None = None
    review_ok: bool = False
    final_exists: bool = False
    final_words: int = 0
    final_grade: str = "missing"
    final_ok: bool = False
    failed_reason: str = ""
    updated_at: str = ""


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def is_valid_chapter_text(text: str) -> bool:
    return analyze_chapter_text(text)[2]


def _non_empty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _has_chapter_title(lines: list[str]) -> bool:
    if not lines:
        return False
    first = lines[0]
    return first.startswith("第") and "章" in first[:16]


def _duplicate_paragraph_ratio(lines: list[str]) -> float:
    paragraphs = [line for line in lines if len(line) >= 20]
    if not paragraphs:
        return 0.0
    counts: dict[str, int] = {}
    for paragraph in paragraphs:
        counts[paragraph] = counts.get(paragraph, 0) + 1
    duplicate_count = sum(count - 1 for count in counts.values() if count > 1)
    return duplicate_count / len(paragraphs)


def _char_ngrams(text: str, size: int = 2) -> set[str]:
    compact = "".join(text.split())
    if len(compact) < size:
        return set()
    return {compact[index:index + size] for index in range(len(compact) - size + 1)}


def _is_similarity_candidate(line: str) -> bool:
    if len(line) < 40:
        return False
    compact = "".join(line.split())
    return bool(compact) and len(set(compact)) / len(compact) >= 0.12


def _jaccard_similarity(left: str, right: str) -> float:
    left_grams = _char_ngrams(left)
    right_grams = _char_ngrams(right)
    if not left_grams or not right_grams:
        return 0.0
    return len(left_grams & right_grams) / len(left_grams | right_grams)


def _similar_paragraph_ratio(lines: list[str]) -> float:
    paragraphs = [line for line in lines if _is_similarity_candidate(line)]
    if len(paragraphs) < 2:
        return 0.0
    similar_pairs = 0
    for index, paragraph in enumerate(paragraphs[:-1]):
        for other in paragraphs[index + 1:index + 4]:
            if _jaccard_similarity(paragraph, other) >= SIMILAR_PARAGRAPH_THRESHOLD:
                similar_pairs += 1
                break
    return similar_pairs / max(1, len(paragraphs) - 1)


def analyze_chapter_text(text: str, exists: bool = True) -> tuple[int, str, bool, list[str]]:
    if not exists:
        return 0, "missing", False, ["missing"]
    stripped = text.strip()
    length = len(stripped)
    lines = _non_empty_lines(stripped)
    issues: list[str] = []

    if length < MIN_CHAPTER_WORDS:
        issues.append("length_too_short")
    elif length > MAX_CHAPTER_WORDS:
        issues.append("length_too_long")

    notes: list[str] = []
    if not _has_chapter_title(lines):
        notes.append("missing_title")
    if len(lines) < MIN_PARAGRAPHS:
        notes.append("paragraphs_too_few")
    if stripped and not stripped.endswith(VALID_ENDINGS):
        issues.append("ending_maybe_truncated")
    for phrase in FORBIDDEN_PHRASES:
        if phrase in stripped:
            issues.append("forbidden_text")
            break
    if _duplicate_paragraph_ratio(lines) > MAX_DUPLICATE_PARAGRAPH_RATIO:
        issues.append("duplicate_paragraphs")
    if _similar_paragraph_ratio(lines) > MAX_SIMILAR_PARAGRAPH_RATIO:
        issues.append("similar_paragraphs")

    if not issues:
        return length, "ok", True, []
    if length < HARD_FAIL_MIN_CHAPTER_WORDS or any(
        issue in issues
        for issue in ("ending_maybe_truncated", "forbidden_text", "duplicate_paragraphs", "similar_paragraphs")
    ):
        return length, "hard_fail", False, issues
    return length, "warn", False, issues + notes


def read_text_length(path: Path) -> tuple[bool, int, bool]:
    if not path.exists():
        return False, 0, False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return True, 0, False
    length, _, ok, _ = analyze_chapter_text(text)
    return True, length, ok


def grade_chapter_words(length: int, exists: bool = True) -> str:
    if not exists:
        return "missing"
    if MIN_CHAPTER_WORDS <= length <= MAX_CHAPTER_WORDS:
        return "ok"
    if length < HARD_FAIL_MIN_CHAPTER_WORDS:
        return "hard_fail"
    if WARN_MIN_CHAPTER_WORDS <= length <= WARN_MAX_CHAPTER_WORDS:
        return "warn"
    return "hard_fail"


def load_text_quality(path: Path) -> tuple[bool, int, str, bool, list[str]]:
    if not path.exists():
        return False, 0, "missing", False, ["missing"]
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return True, 0, "hard_fail", False, ["read_error"]
    length, grade, ok, issues = analyze_chapter_text(text)
    return True, length, grade, ok, issues


def load_review_status(path: Path) -> tuple[bool, str, float | None, bool]:
    if not path.exists():
        return False, "missing", None, False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return True, "invalid_json", None, False

    status = data.get("status", "")
    score = data.get("overall_score")
    score_value = score if isinstance(score, (int, float)) else None
    verdict = data.get("verdict", "")
    schema_errors = validate_review_schema(data)
    if schema_errors:
        return True, "schema_" + schema_errors[0], score_value, False
    ok = status == "completed" and verdict != "需重写" and (score_value is None or score_value >= 7)
    return True, status or "unknown", score_value, ok


def validate_review_schema(data: dict) -> list[str]:
    if not isinstance(data.get("status"), str) or not data.get("status"):
        return ["missing_status"]
    if data.get("status") != "completed":
        return []
    errors: list[str] = []
    for field, expected_type in COMPLETED_REVIEW_REQUIRED_FIELDS.items():
        value = data.get(field)
        if value is None:
            errors.append(f"missing_{field}")
        elif not isinstance(value, expected_type):
            errors.append(f"invalid_{field}")
    return errors


def scan_one_chapter(base_dir: Path, chapter: int) -> ChapterStatus:
    draft_file = base_dir / "chapters" / "draft" / f"chapter_{chapter:04d}.txt"
    final_file = base_dir / "chapters" / "final" / f"chapter_{chapter:04d}.txt"
    review_file = base_dir / "reviews" / f"chapter_{chapter:04d}_review.json"

    draft_exists, draft_words, draft_grade, draft_ok, draft_issues = load_text_quality(draft_file)
    final_exists, final_words, final_grade, final_length_ok, final_issues = load_text_quality(final_file)
    review_exists, review_status, review_score, review_ok = load_review_status(review_file)
    final_ok = final_exists and final_length_ok and review_ok

    failed_reason = ""
    if draft_exists and not draft_ok:
        failed_reason = "draft_" + ",".join(draft_issues)
    if review_exists and not review_ok:
        failed_reason = review_status
    if final_exists and not final_length_ok:
        failed_reason = "final_" + ",".join(final_issues)

    return ChapterStatus(
        chapter=chapter,
        draft_exists=draft_exists,
        draft_words=draft_words,
        draft_grade=draft_grade,
        draft_ok=draft_ok,
        review_exists=review_exists,
        review_status=review_status,
        review_score=review_score,
        review_ok=review_ok,
        final_exists=final_exists,
        final_words=final_words,
        final_grade=final_grade,
        final_ok=final_ok,
        failed_reason=failed_reason,
        updated_at=now_text(),
    )


def scan_chapter_status(base_dir: Path, start: int, end: int) -> Dict[int, ChapterStatus]:
    return {chapter: scan_one_chapter(base_dir, chapter) for chapter in range(start, end + 1)}


def highest_contiguous(statuses: Dict[int, ChapterStatus], start: int, attr: str) -> int:
    last = start - 1
    for chapter in range(start, max(statuses) + 1 if statuses else start):
        status = statuses.get(chapter)
        if not status or not getattr(status, attr):
            break
        last = chapter
    return last


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def write_status_file(base_dir: Path, statuses: Iterable[ChapterStatus]) -> None:
    data = {f"{s.chapter:04d}": asdict(s) for s in statuses}
    atomic_write_json(base_dir / "chapter_status.json", data)
