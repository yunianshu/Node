from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.workflow_state import list_outline_chapters


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _items(value: Any, limit: int = 7) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()][:limit]
    text = str(value or "").strip()
    return [text] if text else []


def _chapter_contract(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "chapter": int(item.get("chapter_number", 0) or 0),
        "title": str(item.get("title", ""))[:60],
        "time_progression": str(item.get("time_progression", ""))[:160],
        "location": str(item.get("location", ""))[:120],
        "characters": _items(item.get("characters_involved"), 12),
        "summary": str(item.get("summary", ""))[:220],
        "key_events": _items(item.get("key_events"), 4),
        "foreshadowing": str(item.get("foreshadowing", ""))[:140],
        "power_progression": str(item.get("power_progression", ""))[:120],
        "hook": str(item.get("chapter_hook", ""))[:140],
    }


def _volume_context(project: Path, start: int, end: int) -> Any:
    volumes = _load(project / "volume_outline.json")
    if not isinstance(volumes, list):
        return None
    relevant = []
    for item in volumes:
        if not isinstance(item, dict):
            continue
        try:
            volume_start = int(item.get("start_chapter", 0) or 0)
            volume_end = int(item.get("end_chapter", 0) or 0)
        except (TypeError, ValueError):
            continue
        if volume_start <= end and volume_end >= start:
            relevant.append(item)
    return relevant or None


def format_outline_constraints(
    project: Path,
    start: int,
    end: int,
    *,
    max_chars: int = 7000,
) -> str:
    """Build constraints from the current outline files.

    The old implementation read maintenance ledgers that could describe an
    earlier outline revision. During repair that fed deleted events back into
    the model. Reading chapter files directly keeps every candidate anchored to
    the latest accepted state.
    """
    chapters = [
        item
        for item in list_outline_chapters(project)
        if isinstance(item, dict) and int(item.get("chapter_number", 0) or 0) > 0
    ]
    if not chapters:
        payload = {
            "range": f"{start}-{end}",
            "volume_plan": _volume_context(project, start, end),
            "continuity_rules": [
                "每个不可逆事件只发生一次：死亡、被捕、身份揭露、关键证据取得、公开直播不得重复。",
                "后一章开场的人物、地点、伤势、道具和时间必须承接前一章结尾。",
                "角色真实身份和阵营只能按既定揭露节奏扩展，不得改写为互相排斥的新身份。",
                "同一场景、追逐、营救、对峙或证据公开流程不得换标题后重复使用。",
            ],
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:max_chars]

    nearby = [
        _chapter_contract(item)
        for item in chapters
        if start - 3 <= int(item.get("chapter_number", 0) or 0) <= end + 3
        and not start <= int(item.get("chapter_number", 0) or 0) <= end
    ]

    payload = {
        "range": f"{start}-{end}",
        "volume_plan": _volume_context(project, start, end),
        "continuity_rules": [
            "以最早已发生章节为事实锚点；后章不得推翻前章已确认的人物身份、生死、伤势、地点、时间和证据状态。",
            "每个不可逆事件只发生一次：死亡、被捕、身份揭露、关键证据取得、公开直播不得重复。",
            "后一章开场状态必须等于前一章结尾状态；跨时段或跨地点必须明确交代经过。",
            "同一场景、追逐、营救、对峙、直播或取证流程不得换标题后重复使用。",
            "伏笔一旦明确回收，不得在后章重新当成未知信息；新伏笔必须安排后续承接。",
        ],
        "nearby_accepted_chapters": nearby,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:max_chars]
