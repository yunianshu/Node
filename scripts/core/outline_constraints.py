from __future__ import annotations

import json
from pathlib import Path
from typing import Any


LEDGER_NAMES = (
    "character_state.json",
    "power_progression.json",
    "timeline.json",
    "foreshadowing_ledger.json",
    "location_ledger.json",
    "repetition_blacklist.json",
)


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def format_outline_constraints(
    project: Path,
    start: int,
    end: int,
    *,
    max_chars: int = 7000,
) -> str:
    ledger_dir = project / "reports" / "outline_reconstruction" / "ledgers"
    data = {name: _load(ledger_dir / name) for name in LEDGER_NAMES}
    if not any(data.values()):
        return "（暂无结构化台账；必须以world.json和characters.json为准）"

    def nearby(name: str, margin: int) -> list[dict]:
        return [
            item for item in data[name].get("events", [])
            if start - margin <= int(item.get("chapter", 0) or 0) <= end + margin
        ]

    conflicts = [
        item for item in data["repetition_blacklist.json"].get("hard_conflicts", [])
        if any(start - 5 <= int(chapter) <= end + 5 for chapter in item.get("chapters", []))
    ]
    payload = {
        "range": f"{start}-{end}",
        "hard_character_constraints": data["character_state.json"].get("canonical_constraints", []),
        "power_rules": data["power_progression.json"].get("validation_rules", []),
        "repetition_blacklist": {
            "hard_conflicts": conflicts,
            "risky_phrases": data["repetition_blacklist.json"].get("repeated_risky_phrases", [])[:20],
        },
        "nearby_power_events": nearby("power_progression.json", 5)[-12:],
        "timeline_rules": data["timeline.json"].get("validation_rules", []),
        "nearby_timeline": nearby("timeline.json", 3),
        "nearby_locations": nearby("location_ledger.json", 3),
        "open_foreshadowing": [
            item for item in data["foreshadowing_ledger.json"].get("events", [])
            if int(item.get("chapter", 0) or 0) <= end
            and item.get("status", "unverified") != "resolved"
        ][-20:],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:max_chars]
