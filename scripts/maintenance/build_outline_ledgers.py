from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


CHAPTER_PATTERN = re.compile(r"chapter_(\d{4})\.json$")
TIME_PATTERN = re.compile(r"(?:凌晨|上午|中午|下午|傍晚|晚上)?\s*\d{1,2}[:：]\d{2}")
SECOND_PATTERN = re.compile(r"\d+(?:\.\d+)?\s*(?:秒|s)", re.IGNORECASE)
LOCATION_KEYS = ("location", "地点", "位置", "场景", "场景地点")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def chapter_number(path: Path) -> int:
    match = CHAPTER_PATTERN.search(path.name)
    if not match:
        raise ValueError(f"invalid chapter outline filename: {path.name}")
    return int(match.group(1))


def text_value(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            return "；".join(str(item).strip() for item in value if str(item).strip())
    return ""


def discover_character_names(project: Path) -> list[str]:
    characters_path = project / "characters.json"
    if not characters_path.exists():
        return []
    data = load_json(characters_path)
    names: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"name", "姓名", "角色名"} and isinstance(item, str):
                    names.add(item.strip())
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)
    return sorted(name for name in names if name)


def extract_location(data: dict[str, Any], summary: str) -> str:
    for key in LOCATION_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    match = re.search(r"(?:在|抵达|进入|返回)([^，。；]{2,18})", summary)
    return match.group(1).strip() if match else ""


def build_ledgers(project: Path) -> dict[str, Any]:
    outline_dir = project / "chapters" / "outline"
    paths = sorted(outline_dir.glob("chapter_*.json"), key=chapter_number)
    if not paths:
        raise FileNotFoundError(f"no outlines found under {outline_dir}")

    character_names = discover_character_names(project)
    character_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    power_events: list[dict[str, Any]] = []
    timeline_events: list[dict[str, Any]] = []
    foreshadowing_events: list[dict[str, Any]] = []
    location_events: list[dict[str, Any]] = []
    titles: dict[str, list[int]] = defaultdict(list)
    phrase_counts: Counter[str] = Counter()
    risky_phrases = (
        "首次对话",
        "第一次对话",
        "首次接触",
        "第一次接触",
        "首次激活",
        "第一次激活",
        "签订契约",
        "完成契约",
    )

    for path in paths:
        number = chapter_number(path)
        data = load_json(path)
        title = text_value(data, "title", "章节标题")
        summary = text_value(data, "summary", "概要", "chapter_summary")
        power = text_value(data, "power_progression", "能力进展", "能力成长")
        foreshadowing = text_value(data, "foreshadowing", "伏笔")
        hook = text_value(data, "chapter_hook", "hook", "章末钩子")
        key_events = text_value(data, "key_events", "关键事件")
        time_progression = text_value(data, "time_progression", "时间推进")
        combined = "；".join(
            part
            for part in (
                title,
                summary,
                key_events,
                power,
                foreshadowing,
                hook,
                time_progression,
            )
            if part
        )

        titles[title].append(number)
        for phrase in risky_phrases:
            if phrase in combined:
                phrase_counts[phrase] += 1

        for name in character_names:
            if name in combined:
                character_events[name].append(
                    {
                        "chapter": number,
                        "title": title,
                        "context": summary[:240],
                    }
                )

        if power:
            power_events.append(
                {
                    "chapter": number,
                    "title": title,
                    "progression": power,
                    "duration_markers": SECOND_PATTERN.findall(power),
                }
            )

        times = [item.replace("：", ":").replace(" ", "") for item in TIME_PATTERN.findall(combined)]
        location = extract_location(data, summary)
        timeline_events.append(
            {
                "chapter": number,
                "title": title,
                "time_markers": list(dict.fromkeys(times)),
                "location": location,
            }
        )
        location_events.append(
            {
                "chapter": number,
                "title": title,
                "location": location,
            }
        )
        if foreshadowing:
            foreshadowing_events.append(
                {
                    "chapter": number,
                    "title": title,
                    "setup": foreshadowing,
                    "status": "unverified",
                }
            )

    duplicate_titles = [
        {"title": title, "chapters": chapters}
        for title, chapters in titles.items()
        if title and len(chapters) > 1
    ]
    repeated_phrases = [
        {"phrase": phrase, "occurrences": count}
        for phrase, count in phrase_counts.most_common()
        if count > 1
    ]

    output_dir = project / "reports" / "outline_reconstruction" / "ledgers"
    ledgers = {
        "character_state.json": {
            "source_chapters": len(paths),
            "characters": character_events,
            "canonical_constraints": [],
        },
        "power_progression.json": {
            "source_chapters": len(paths),
            "events": power_events,
            "validation_rules": [
                "首次激活、首次接触、首次稳定使用只能各出现一次",
                "持续时间必须按阶段单调演进，回退需给出明确代价或原因",
            ],
        },
        "timeline.json": {
            "source_chapters": len(paths),
            "events": timeline_events,
            "validation_rules": [
                "同日章节时间不得无解释倒退",
                "跨地点移动必须预留合理时间或明确使用特殊能力",
            ],
        },
        "foreshadowing_ledger.json": {
            "source_chapters": len(paths),
            "events": foreshadowing_events,
            "status_note": "基线仅登记埋设点，修订时补充回收章节与状态。",
        },
        "location_ledger.json": {
            "source_chapters": len(paths),
            "events": location_events,
        },
        "repetition_blacklist.json": {
            "source_chapters": len(paths),
            "duplicate_titles": duplicate_titles,
            "repeated_risky_phrases": repeated_phrases,
            "hard_conflicts": [],
        },
    }
    for filename, data in ledgers.items():
        write_json(output_dir / filename, data)
    return {"chapters": len(paths), "output_dir": str(output_dir), "files": list(ledgers)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build whole-book outline reconstruction ledgers.")
    parser.add_argument("--project", required=True, type=Path)
    args = parser.parse_args()
    result = build_ledgers(args.project.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
