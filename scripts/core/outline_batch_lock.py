from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from core.workflow_state import atomic_write_json, report_path


def lock_path(project: Path) -> Path:
    return report_path(project, "outline_batch_locks.json")


def load_locks(project: Path) -> dict[str, Any]:
    path = lock_path(project)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"batches": {}}
    except Exception as exc:
        return {"status": "active", "batches": {}}


def batch_range(chapter: int, batch_size: int = 25) -> tuple[int, int]:
    start = ((chapter - 1) // batch_size) * batch_size + 1
    return start, start + batch_size - 1


def is_chapter_locked(project: Path, chapter: int, batch_size: int = 25) -> bool:
    start, end = batch_range(chapter, batch_size)
    item = load_locks(project).get("batches", {}).get(f"{start}-{end}", {})
    return item.get("locked") is True


def lock_batch(
    project: Path,
    start: int,
    end: int,
    *,
    score: float,
    report: str,
) -> None:
    data = load_locks(project)
    batches = data.setdefault("batches", {})
    batches[f"{start}-{end}"] = {
        "locked": True,
        "score": score,
        "report": report,
        "locked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    atomic_write_json(lock_path(project), data)


def unlock_chapters(project: Path, chapters: list[int], batch_size: int = 25) -> list[str]:
    data = load_locks(project)
    batches = data.setdefault("batches", {})
    unlocked: list[str] = []
    for chapter in chapters:
        start, end = batch_range(chapter, batch_size)
        key = f"{start}-{end}"
        item = batches.get(key)
        if isinstance(item, dict) and item.get("locked") is True:
            item["locked"] = False
            item["unlocked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            unlocked.append(key)
    atomic_write_json(lock_path(project), data)
    return sorted(set(unlocked))
