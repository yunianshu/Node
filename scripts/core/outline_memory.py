#!/usr/bin/env python3
"""Build compact long-range memory from per-chapter outline files."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.workflow_state import atomic_write_json, list_outline_chapters, report_path


def _items(value: Any, limit: int = 3) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()][:limit]
    if isinstance(value, dict):
        return [f"{key}: {item}" for key, item in list(value.items())[:limit]]
    text = str(value or "").strip()
    return [text] if text else []


def build_outline_memory(
    project: Path,
    before_chapter: int,
    *,
    milestone_size: int = 25,
    recent_chapters: int = 8,
) -> dict:
    chapters = [
        item
        for item in list_outline_chapters(project)
        if 0 < int(item.get("chapter_number", 0) or 0) < before_chapter
    ]
    milestones = []
    for offset in range(0, len(chapters), max(1, milestone_size)):
        group = chapters[offset:offset + max(1, milestone_size)]
        if not group:
            continue
        milestones.append({
            "range": f"{group[0]['chapter_number']}-{group[-1]['chapter_number']}",
            "start": str(group[0].get("summary", ""))[:180],
            "end": str(group[-1].get("summary", ""))[:220],
            # 中段浓缩：抽取组内 power/伏笔/地点，避免中段细节全丢导致注水腰
            "power_progression": _items(group[-1].get("power_progression"), 2),
            "group_foreshadowing": [
                {"ch": item.get("chapter_number"), "f": str(item.get("foreshadowing", ""))[:120]}
                for item in group
                if str(item.get("foreshadowing", "")).strip()
            ][:4],
            "key_events_tail": _items(group[-1].get("key_events"), 3),
            "locations": list(dict.fromkeys(
                str(item.get("location", "")).strip()
                for item in group
                if str(item.get("location", "")).strip()
            ))[-5:],
        })

    recent = []
    open_threads = []
    character_states: dict[str, int] = {}
    for item in chapters:
        chapter = int(item.get("chapter_number", 0) or 0)
        for name in _items(item.get("characters_involved"), 20):
            character_states[name] = chapter
        for thread in _items(item.get("foreshadowing"), 4):
            open_threads.append({"chapter": chapter, "thread": thread[:180]})

    for item in chapters[-max(1, recent_chapters):]:
        recent.append({
            "chapter": item.get("chapter_number"),
            "title": item.get("title", ""),
            "summary": str(item.get("summary", ""))[:260],
            "hook": str(item.get("chapter_hook", ""))[:160],
            "power_progression": str(item.get("power_progression", ""))[:160],
        })

    memory = {
        "status": "derived",
        "source": "chapters/outline/chapter_XXXX.json",
        "before_chapter": before_chapter,
        "covered_chapters": len(chapters),
        "milestones": milestones[-12:],
        "recent_chapters": recent,
        "recent_foreshadowing": open_threads[-30:],
        "character_last_seen": dict(
            sorted(character_states.items(), key=lambda item: item[1], reverse=True)[:30]
        ),
    }
    atomic_write_json(report_path(project, "outline_memory.json"), memory)
    return memory


def format_outline_memory(memory: dict, max_chars: int = 7000) -> str:
    if not memory or not memory.get("covered_chapters"):
        return "（暂无前序大纲记忆）"
    return json.dumps(memory, ensure_ascii=False, separators=(",", ":"))[:max_chars]
