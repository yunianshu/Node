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
MAX_CHAPTER_WORDS = 8000
WARN_MIN_CHAPTER_WORDS = 4300
WARN_MAX_CHAPTER_WORDS = 8500
HARD_FAIL_MIN_CHAPTER_WORDS = 3000
MIN_EXISTING_BYTES = 1000


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
    length = len(text.strip())
    return MIN_CHAPTER_WORDS <= length <= MAX_CHAPTER_WORDS


def read_text_length(path: Path) -> tuple[bool, int, bool]:
    if not path.exists():
        return False, 0, False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return True, 0, False
    length = len(text.strip())
    return True, length, MIN_CHAPTER_WORDS <= length <= MAX_CHAPTER_WORDS


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
    ok = status == "completed" and verdict != "需重写" and (score_value is None or score_value >= 7)
    return True, status or "unknown", score_value, ok


def scan_one_chapter(base_dir: Path, chapter: int) -> ChapterStatus:
    draft_file = base_dir / "chapters" / "draft" / f"chapter_{chapter:04d}.txt"
    final_file = base_dir / "chapters" / "final" / f"chapter_{chapter:04d}.txt"
    review_file = base_dir / "reviews" / f"chapter_{chapter:04d}_review.json"

    draft_exists, draft_words, draft_ok = read_text_length(draft_file)
    final_exists, final_words, final_length_ok = read_text_length(final_file)
    draft_grade = grade_chapter_words(draft_words, draft_exists)
    final_grade = grade_chapter_words(final_words, final_exists)
    review_exists, review_status, review_score, review_ok = load_review_status(review_file)
    final_ok = final_exists and final_length_ok and review_ok

    failed_reason = ""
    if draft_exists and not draft_ok:
        failed_reason = "draft_length_invalid"
    if review_exists and not review_ok:
        failed_reason = review_status
    if final_exists and not final_length_ok:
        failed_reason = "final_length_invalid"

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
