#!/usr/bin/env python3
"""关系欠账追踪：跨章保留人情味与潜台词。

每章正文通过后，抽取角色之间的关系变化、亏欠、误会、承诺、照料行为和未说出口的话，
存为 relationship_states/chapter_XXXX.json。writer 生成下一章前注入最近快照，
让故事的烟火气不是单章装饰，而是能跨章延续的关系压力。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS_ROOT = Path(__file__).resolve().parent.parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from core.json_repair import fix_inner_quotes, fix_truncated_json
from core.mmx_client import MmxError, call_mmx
from core.workflow_state import atomic_write_json


def relationship_dir(project: Path) -> Path:
    return project / "chapters" / "relationship_states"


def relationship_file(project: Path, chapter: int) -> Path:
    return relationship_dir(project) / f"chapter_{chapter:04d}.json"


def empty_relationships() -> dict:
    return {"chapter": 0, "relationships": []}


def load_relationships(project: Path, chapter: int) -> dict:
    f = relationship_file(project, chapter)
    if not f.exists():
        return empty_relationships()
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and "relationships" in data else empty_relationships()
    except Exception:
        return empty_relationships()


def latest_relationships_before(project: Path, chapter: int) -> dict:
    """返回第 chapter 章之前最近的关系欠账快照。"""
    for ch in range(chapter - 1, 0, -1):
        data = load_relationships(project, ch)
        if data.get("relationships"):
            return data
    return empty_relationships()


def format_relationships_for_prompt(data: dict) -> str:
    """把关系欠账快照格式化为可注入 writer prompt 的文本。"""
    rels = data.get("relationships", [])
    if not isinstance(rels, list) or not rels:
        return ""
    lines = ["## 【关系欠账与人情味连续性】（截至上一章，本章要延续人的反应）"]
    for item in rels[:10]:
        if not isinstance(item, dict):
            continue
        pair = str(item.get("pair", "")).strip()
        if not pair:
            a = str(item.get("from", "")).strip()
            b = str(item.get("to", "")).strip()
            pair = f"{a}->{b}" if a or b else "未命名关系"
        parts = []
        for field, label in (
            ("change", "变化"),
            ("debt", "亏欠/人情"),
            ("misunderstanding", "误会/隐瞒"),
            ("promise", "承诺"),
            ("care_action", "照料行为"),
            ("unsaid", "未说出口"),
            ("next_pressure", "后续压力"),
        ):
            val = str(item.get(field, "")).strip()
            if val:
                parts.append(f"{label}:{val}")
        if parts:
            lines.append(f"- {pair}：" + "；".join(parts))
    if len(lines) == 1:
        return ""
    lines.append("本章写冲突和爽点时，必须让至少一条关系欠账产生回声：有人更亏欠、释怀、误会加深或改变看法。")
    return "\n".join(lines)


def select_relationship_obligation(data: dict) -> dict:
    """Select the highest-pressure unresolved relationship obligation."""
    rels = data.get("relationships", []) if isinstance(data, dict) else []
    if not isinstance(rels, list):
        return {}
    candidates: list[tuple[int, dict]] = []
    for item in rels:
        if not isinstance(item, dict) or _is_resolved(item):
            continue
        score = 0
        for field, weight in (
            ("next_pressure", 5),
            ("unsaid", 4),
            ("misunderstanding", 4),
            ("debt", 3),
            ("promise", 3),
            ("care_action", 2),
            ("change", 1),
        ):
            if str(item.get(field, "")).strip():
                score += weight
        if item.get("carried_over"):
            score += 3
        if score:
            candidates.append((score, item))
    if not candidates:
        return {}
    _, item = sorted(candidates, key=lambda pair: pair[0], reverse=True)[0]
    pair_name = str(item.get("pair", "")).strip()
    if not pair_name:
        a = str(item.get("from", "")).strip()
        b = str(item.get("to", "")).strip()
        pair_name = f"{a}->{b}" if a or b else "一组关键关系"
    pressure_parts = []
    pressure_fields = {}
    for field, label in (
        ("next_pressure", "后续压力"),
        ("unsaid", "未说出口"),
        ("misunderstanding", "误会/隐瞒"),
        ("debt", "亏欠/人情"),
        ("promise", "承诺"),
        ("care_action", "照料行为"),
    ):
        value = str(item.get(field, "")).strip()
        if value:
            pressure_fields[field] = value
            pressure_parts.append(f"{label}:{value}")
    pressure = "；".join(pressure_parts)[:260]
    return {
        "pair": pair_name,
        "pressure": pressure,
        "pressure_fields": pressure_fields,
        "source": item,
    }


def relationship_obligation_for_prompt(data: dict) -> str:
    """Select one open relationship pressure and turn it into a hard task."""
    obligation = select_relationship_obligation(data)
    if not obligation:
        return ""
    return (
        "## 【本章必须兑现的关系任务】\n"
        f"- 关系对象：{obligation['pair']}\n"
        f"- 未解决压力：{obligation['pressure']}\n"
        "- 正文必须让这条关系在一个具体场景中发生可见变化：至少写出一句带潜台词的对白、一个照料/回避/补偿动作，"
        "以及事件结束后双方关系是更亏欠、误会加深、短暂释怀还是立下新承诺。不得只在内心旁白中提及。"
    )


def _relationship_key(item: dict) -> str:
    pair = str(item.get("pair", "")).strip()
    if pair:
        return pair
    a = str(item.get("from", "")).strip()
    b = str(item.get("to", "")).strip()
    return f"{a}->{b}" if a or b else ""


def _is_resolved(item: dict) -> bool:
    if item.get("resolved") is True:
        return True
    text = "；".join(str(item.get(field, "")) for field in ("change", "next_pressure", "note"))
    return any(word in text for word in ("已解决", "已释怀", "误会解除", "还清", "和解"))


def _has_open_pressure(item: dict) -> bool:
    if _is_resolved(item):
        return False
    for field in ("debt", "misunderstanding", "promise", "unsaid", "next_pressure"):
        if str(item.get(field, "")).strip():
            return True
    return False


def merge_relationships(prev: dict, current_relationships: list, chapter: int, *, limit: int = 12) -> list[dict]:
    """合并当前抽取与上一章未解决关系欠账。

    当前章抽取优先；上一章仍有未解决压力且未被当前章同 pair 覆盖的项目自动延续。
    """
    merged: list[dict] = []
    seen: set[str] = set()
    if not isinstance(current_relationships, list):
        current_relationships = []
    for raw in current_relationships:
        if not isinstance(raw, dict):
            continue
        item = {k: v for k, v in raw.items() if v not in ("", None, [], {})}
        key = _relationship_key(item)
        if not key:
            continue
        item.setdefault("chapter", chapter)
        item["carried_over"] = False
        merged.append(item)
        seen.add(key)

    prev_rels = prev.get("relationships", []) if isinstance(prev, dict) else []
    if isinstance(prev_rels, list):
        for raw in prev_rels:
            if not isinstance(raw, dict) or not _has_open_pressure(raw):
                continue
            key = _relationship_key(raw)
            if not key or key in seen:
                continue
            item = {k: v for k, v in raw.items() if v not in ("", None, [], {})}
            item["carried_over"] = True
            item["carried_from_chapter"] = int(raw.get("chapter", prev.get("chapter", 0)) or 0)
            item["chapter"] = chapter
            if "next_pressure" in item:
                item["next_pressure"] = str(item["next_pressure"])[:160]
            merged.append(item)
            seen.add(key)
            if len(merged) >= limit:
                break
    return merged[:limit]


def extract_relationship_states(
    project: Path,
    config: dict,
    chapter: int,
    text: str,
    characters_meta: dict | None = None,
) -> dict:
    """从一章正文抽取关系欠账。用 LLM，返回 {chapter, relationships}。"""
    chars_meta = characters_meta or {}
    known_names: list[str] = []

    def walk(value) -> None:
        if isinstance(value, dict):
            name = str(value.get("name", "")).strip()
            if name:
                known_names.append(name)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(chars_meta)
    names_hint = "、".join(dict.fromkeys(known_names[:20])) or "（自动识别文中出现的角色）"
    prev = latest_relationships_before(project, chapter)
    prev_text = format_relationships_for_prompt(prev) or "（无）"

    prompt = f"""分析以下小说第{chapter}章正文，提取【本章结束时】仍会影响后续章节的关系欠账。
重点只提取正文中确实发生的：信任变化、亏欠/人情、误会/隐瞒、承诺、照料行为、未说出口的话、下一章可继续发酵的关系压力。
不要抽取纯设定关系，不要臆测正文没有写出的情绪。

已知主要角色：{names_hint}

上一章关系欠账参考：
{prev_text}

只输出紧凑JSON，不要Markdown：
{{"relationships": [
  {{"pair": "角色A->角色B", "change": "关系变化", "debt": "亏欠或人情", "misunderstanding": "误会或隐瞒", "promise": "承诺", "care_action": "照料行为", "unsaid": "未说出口的话", "next_pressure": "后续可延续的关系压力", "resolved": false}}
]}}

第{chapter}章正文：
{text[:6000]}"""
    cfg = config.get("relationship_state", {})
    try:
        raw = call_mmx(
            "你是小说关系线连续性编辑。只基于正文事实，抽取会影响后文的人情债与潜台词。",
            prompt,
            model=config["model"],
            mmx_path=config["mmx_path"],
            max_tokens=int(cfg.get("max_tokens", 2048)),
            temperature=float(cfg.get("temperature", 0.1)),
            retries=int(cfg.get("retries", 2)),
            retry_delay=float(cfg.get("retry_delay", 5.0)),
            timeout=int(cfg.get("timeout_seconds", 120)),
            log_dir=project / "logs" / "raw_responses",
            raw_name=f"relationship_state_ch{chapter:04d}",
            qps=float(config.get("api_qps", 5.0)),
            rate_state_dir=project / "logs" / "rate_limit",
        )
    except MmxError as exc:
        return {"chapter": chapter, "relationships": [], "error": str(exc)}

    parsed = _parse_json(raw)
    rels = parsed.get("relationships", []) if isinstance(parsed, dict) else []
    if not isinstance(rels, list):
        rels = []
    merged = merge_relationships(prev, rels, chapter, limit=12)
    result = {"chapter": chapter, "relationships": merged}
    relationship_dir(project).mkdir(parents=True, exist_ok=True)
    atomic_write_json(relationship_file(project, chapter), result)
    return result


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()
    for cand in (text, fix_inner_quotes(text), fix_truncated_json(text)):
        try:
            data = json.loads(cand)
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return {}


def _selftest() -> None:
    data = {
        "chapter": 3,
        "relationships": [
            {
                "pair": "林深->母亲",
                "debt": "隐瞒药费催款",
                "unsaid": "不敢说自己已经没钱",
                "next_pressure": "下一章母亲发现账单会加深冲突",
            }
        ],
    }
    text = format_relationships_for_prompt(data)
    assert "关系欠账" in text and "林深->母亲" in text and "药费" in text
    merged = merge_relationships(
        data,
        [{"pair": "林深->苏雯", "promise": "答应帮她找回录音"}],
        4,
    )
    assert len(merged) == 2
    assert any(item.get("pair") == "林深->母亲" and item.get("carried_over") for item in merged)
    resolved_prev = {
        "chapter": 3,
        "relationships": [{"pair": "林深->母亲", "debt": "药费", "resolved": True}],
    }
    assert merge_relationships(resolved_prev, [], 4) == []
    assert format_relationships_for_prompt(empty_relationships()) == ""
    obligation = relationship_obligation_for_prompt({
        "relationships": [
            {"pair": "林深->母亲", "debt": "药费", "resolved": True},
            {
                "pair": "林深->苏雯",
                "unsaid": "不敢承认自己需要帮忙",
                "next_pressure": "下一章她会追问伤口来源",
                "carried_over": True,
            },
        ]
    })
    assert "本章必须兑现的关系任务" in obligation and "林深->苏雯" in obligation and "潜台词" in obligation
    selected = select_relationship_obligation({
        "relationships": [
            {"pair": "林深->母亲", "debt": "药费", "resolved": True},
            {"pair": "林深->苏雯", "unsaid": "不敢承认自己需要帮忙", "carried_over": True},
        ]
    })
    assert selected["pair"] == "林深->苏雯" and "unsaid" in selected["pressure_fields"]
    assert latest_relationships_before(Path("nonexistent_xyz"), 5).get("relationships") == []
    print("relationship_state selftest OK")


if __name__ == "__main__":
    _selftest()
