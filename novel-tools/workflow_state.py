#!/usr/bin/env python3
"""小说生成工作流状态与质量门。"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable

from novel_config import load_config

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
DEFAULT_QUALITY_RULES = {
    "min_chapter_words": MIN_CHAPTER_WORDS,
    "max_chapter_words": MAX_CHAPTER_WORDS,
    "warn_min_chapter_words": WARN_MIN_CHAPTER_WORDS,
    "warn_max_chapter_words": WARN_MAX_CHAPTER_WORDS,
    "hard_fail_min_chapter_words": HARD_FAIL_MIN_CHAPTER_WORDS,
    "min_paragraphs": MIN_PARAGRAPHS,
    "max_duplicate_paragraph_ratio": MAX_DUPLICATE_PARAGRAPH_RATIO,
    "max_similar_paragraph_ratio": MAX_SIMILAR_PARAGRAPH_RATIO,
    "similar_paragraph_threshold": SIMILAR_PARAGRAPH_THRESHOLD,
    "title_required": False,
    "title_prefixes": ["第"],
    "title_keywords": ["章", "节", "回"],
}
CACHE_FILE_NAME = ".workflow_status_cache.json"


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
    draft_issues: list[str] | None = None
    review_issues: list[str] | None = None
    final_issues: list[str] | None = None
    updated_at: str = ""


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def is_valid_chapter_text(text: str) -> bool:
    return analyze_chapter_text(text)[2]


def load_quality_rules(base_dir: Path | None = None) -> dict:
    rules = dict(DEFAULT_QUALITY_RULES)
    if base_dir is None:
        return rules
    config = load_config(base_dir)
    quality = config.get("quality", {})
    if isinstance(quality, dict):
        rules.update({key: value for key, value in quality.items() if value is not None})
    return rules


def _non_empty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _has_chapter_title(lines: list[str], rules: dict | None = None) -> bool:
    if not lines:
        return False
    first = lines[0]
    rules = rules or DEFAULT_QUALITY_RULES
    prefixes = tuple(str(item) for item in rules.get("title_prefixes", ["第"]))
    keywords = tuple(str(item) for item in rules.get("title_keywords", ["章", "节", "回"]))
    head = first[:40].lstrip("# 　")
    return bool(head.startswith(prefixes) and any(keyword in head for keyword in keywords))


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


def analyze_chapter_text(text: str, exists: bool = True, rules: dict | None = None) -> tuple[int, str, bool, list[str]]:
    if not exists:
        return 0, "missing", False, ["missing"]
    rules = rules or DEFAULT_QUALITY_RULES
    stripped = text.strip()
    length = len(stripped)
    lines = _non_empty_lines(stripped)
    issues: list[str] = []

    min_words = int(rules.get("min_chapter_words", MIN_CHAPTER_WORDS))
    max_words = int(rules.get("max_chapter_words", MAX_CHAPTER_WORDS))
    hard_fail_min = int(rules.get("hard_fail_min_chapter_words", HARD_FAIL_MIN_CHAPTER_WORDS))
    min_paragraphs = int(rules.get("min_paragraphs", MIN_PARAGRAPHS))
    duplicate_ratio = float(rules.get("max_duplicate_paragraph_ratio", MAX_DUPLICATE_PARAGRAPH_RATIO))
    similar_ratio = float(rules.get("max_similar_paragraph_ratio", MAX_SIMILAR_PARAGRAPH_RATIO))

    if length < min_words:
        issues.append("length_too_short")
    elif length > max_words:
        issues.append("length_too_long")

    notes: list[str] = []
    if bool(rules.get("title_required", False)) and not _has_chapter_title(lines, rules):
        notes.append("missing_title")
    if len(lines) < min_paragraphs:
        notes.append("paragraphs_too_few")
    if stripped and not stripped.endswith(VALID_ENDINGS):
        issues.append("ending_maybe_truncated")
    for phrase in FORBIDDEN_PHRASES:
        if phrase in stripped:
            issues.append("forbidden_text")
            break
    if _duplicate_paragraph_ratio(lines) > duplicate_ratio:
        issues.append("duplicate_paragraphs")
    if _similar_paragraph_ratio(lines) > similar_ratio:
        issues.append("similar_paragraphs")

    if not issues:
        return length, "ok", True, notes
    if length < hard_fail_min or any(
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


def grade_chapter_words(length: int, exists: bool = True, rules: dict | None = None) -> str:
    rules = rules or DEFAULT_QUALITY_RULES
    if not exists:
        return "missing"
    if int(rules.get("min_chapter_words", MIN_CHAPTER_WORDS)) <= length <= int(rules.get("max_chapter_words", MAX_CHAPTER_WORDS)):
        return "ok"
    if length < int(rules.get("hard_fail_min_chapter_words", HARD_FAIL_MIN_CHAPTER_WORDS)):
        return "hard_fail"
    if int(rules.get("warn_min_chapter_words", WARN_MIN_CHAPTER_WORDS)) <= length <= int(rules.get("warn_max_chapter_words", WARN_MAX_CHAPTER_WORDS)):
        return "warn"
    return "hard_fail"


def load_text_quality(path: Path, rules: dict | None = None) -> tuple[bool, int, str, bool, list[str]]:
    if not path.exists():
        return False, 0, "missing", False, ["missing"]
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return True, 0, "hard_fail", False, ["read_error"]
    length, grade, ok, issues = analyze_chapter_text(text, rules=rules)
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


def scan_one_chapter(base_dir: Path, chapter: int, rules: dict | None = None) -> ChapterStatus:
    rules = rules or load_quality_rules(base_dir)
    draft_file = base_dir / "chapters" / "draft" / f"chapter_{chapter:04d}.txt"
    final_file = base_dir / "chapters" / "final" / f"chapter_{chapter:04d}.txt"
    review_file = base_dir / "reviews" / f"chapter_{chapter:04d}_review.json"

    draft_exists, draft_words, draft_grade, draft_ok, draft_issues = load_text_quality(draft_file, rules)
    final_exists, final_words, final_grade, final_length_ok, final_issues = load_text_quality(final_file, rules)
    review_exists, review_status, review_score, review_ok = load_review_status(review_file)
    final_ok = final_exists and final_length_ok and review_ok

    failed_reason = ""
    if draft_exists and not draft_ok:
        failed_reason = "draft_" + ",".join(draft_issues)
    if review_exists and not review_ok:
        failed_reason = review_status
    review_issues = [] if review_ok else [review_status]
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
        draft_issues=draft_issues,
        review_issues=review_issues,
        final_issues=final_issues if final_exists else ["missing"],
        updated_at=now_text(),
    )


def _chapter_inputs(base_dir: Path, chapter: int) -> dict:
    paths = {
        "draft": base_dir / "chapters" / "draft" / f"chapter_{chapter:04d}.txt",
        "final": base_dir / "chapters" / "final" / f"chapter_{chapter:04d}.txt",
        "review": base_dir / "reviews" / f"chapter_{chapter:04d}_review.json",
    }
    result = {}
    for name, path in paths.items():
        if path.exists():
            stat = path.stat()
            result[name] = {"mtime": stat.st_mtime, "size": stat.st_size}
        else:
            result[name] = {"mtime": 0, "size": 0}
    return result


def _status_from_dict(data: dict) -> ChapterStatus:
    valid = set(ChapterStatus.__dataclass_fields__.keys())
    return ChapterStatus(**{key: value for key, value in data.items() if key in valid})


def scan_chapter_status(base_dir: Path, start: int, end: int, use_cache: bool = False) -> Dict[int, ChapterStatus]:
    rules = load_quality_rules(base_dir)
    if not use_cache:
        return {chapter: scan_one_chapter(base_dir, chapter, rules) for chapter in range(start, end + 1)}

    cache_file = base_dir / CACHE_FILE_NAME
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {"chapters": {}}
    except Exception:
        cache = {"chapters": {}}
    chapters = cache.setdefault("chapters", {})
    statuses: dict[int, ChapterStatus] = {}
    changed = False
    for chapter in range(start, end + 1):
        key = f"{chapter:04d}"
        inputs = _chapter_inputs(base_dir, chapter)
        cached = chapters.get(key)
        if cached and cached.get("inputs") == inputs:
            statuses[chapter] = _status_from_dict(cached["status"])
            continue
        status = scan_one_chapter(base_dir, chapter, rules)
        statuses[chapter] = status
        chapters[key] = {"inputs": inputs, "status": asdict(status)}
        changed = True
    if changed:
        atomic_write_json(cache_file, cache)
    return statuses


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
