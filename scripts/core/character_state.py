#!/usr/bin/env python3
"""人物状态追踪：跨章一致性保障。

每章正文生成后，抽取所有角色的当前状态快照（生死/位置/关系/伤势/掌握信息），
存为 character_states/chapter_XXXX.json。writer 生成下一章前注入此快照，
从根源消除"老周第7章中枪→第8章还在呼吸→第10章才确认死亡"这类跨章矛盾。

状态文件位置：projects/<book>/chapters/character_states/chapter_XXXX.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS_ROOT = Path(__file__).resolve().parent.parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from core.json_repair import fix_inner_quotes, fix_truncated_json
from core.llm_client import LLMError, call_llm
from core.workflow_state import atomic_write_json


def state_dir(project: Path) -> Path:
    return project / "chapters" / "character_states"


def state_file(project: Path, chapter: int) -> Path:
    return state_dir(project) / f"chapter_{chapter:04d}.json"


def empty_states() -> dict:
    return {"characters": {}, "chapter": 0}


def load_state(project: Path, chapter: int) -> dict:
    f = state_file(project, chapter)
    if not f.exists():
        return empty_states()
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and "characters" in data else empty_states()
    except Exception as exc:
        return empty_states()


def latest_state_before(project: Path, chapter: int) -> dict:
    """返回第 chapter 章之前最近的角色状态快照（用于注入 writer）。"""
    for ch in range(chapter - 1, 0, -1):
        st = load_state(project, ch)
        if st.get("characters"):
            return st
    return empty_states()


def format_state_for_prompt(states: dict) -> str:
    """把角色状态快照格式化为可注入 writer prompt 的文本。无数据返回空串。"""
    chars = states.get("characters", {})
    if not chars:
        return ""
    lines = ["## 【人物当前状态】（截至上一章，本章必须严格遵守，不得自相矛盾）"]
    for name, info in chars.items():
        parts = []
        for field in ("status", "location", "injury", "relationship", "knows", "note"):
            val = info.get(field) if isinstance(info, dict) else None
            if val:
                label = {"status": "状态", "location": "位置", "injury": "伤势",
                         "relationship": "关系", "knows": "已知信息", "note": "备注"}[field]
                parts.append(f"{label}:{val}")
        if parts:
            lines.append(f"- {name}：" + "；".join(parts))
    lines.append("以上角色状态是前文已确立的事实，本章不得与之矛盾（如某人已死则本章不得再让其说话行动）。")
    return "\n".join(lines)


def extract_character_states(project: Path, config: dict, chapter: int,
                             text: str, characters_meta: dict | None = None) -> dict:
    """从一章正文抽取角色状态快照。用 LLM，返回 {chapter, characters}。"""
    chars_meta = characters_meta or {}
    known_names = []
    if isinstance(chars_meta.get("characters"), list):
        known_names = [c.get("name", "") for c in chars_meta["characters"] if isinstance(c, dict)]
    elif isinstance(chars_meta.get("protagonist"), dict):
        known_names.append(chars_meta["protagonist"].get("name", ""))

    names_hint = "、".join(n for n in known_names if n) or "（自动识别文中出现的角色）"
    prompt = f"""分析以下小说第{chapter}章正文，提取【本章结束时】所有重要角色的当前状态。
只提取本章确实出现的角色。对每个角色给出：status(生死/在场状态)、location(位置)、injury(伤势，无则留空)、relationship(与主角关系或关键关系变化)、knows(本章新掌握的关键信息)、note(其他影响后续的重要状态)。

已知主要角色：{names_hint}

只输出紧凑JSON，不要Markdown：
{{"characters": {{
  "角色名": {{"status": "活着/垂死/死亡/失踪/在场/离场", "location": "当前位置", "injury": "伤势描述或空", "relationship": "关系描述", "knows": "本章新知道的关键信息", "note": "其他重要状态"}}
}}}}

第{chapter}章正文：
{text[:6000]}"""
    cfg = config.get("character_state", {})
    try:
        raw = call_llm(
            config,
            project,
            "character_state",
            "你是小说连续性校验专家。精准提取角色状态，只基于正文事实，不臆测。",
            prompt,
            max_tokens=int(cfg.get("max_tokens", 2048)),
            temperature=float(cfg.get("temperature", 0.1)),
            retries=int(cfg.get("retries", 2)),
            retry_delay=float(cfg.get("retry_delay", 5.0)),
            timeout=int(cfg.get("timeout_seconds", 120)),
            raw_name=f"char_state_ch{chapter:04d}",
        )
    except LLMError as exc:
        return {"chapter": chapter, "characters": {}, "error": str(exc)}

    parsed = _parse_json(raw)
    chars = parsed.get("characters", parsed) if isinstance(parsed, dict) else {}
    if not isinstance(chars, dict):
        chars = {}
    result = {"chapter": chapter, "characters": chars}
    state_dir(project).mkdir(parents=True, exist_ok=True)
    atomic_write_json(state_file(project, chapter), result)
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
        except Exception as exc:
            continue
    return {}


def _selftest() -> None:
    states = {
        "characters": {
            "老周": {"status": "垂死", "injury": "腹部中枪，失血过多",
                     "location": "废弃仓库", "knows": "U盘第三层密码"},
            "林深": {"status": "在场", "location": "废弃仓库",
                     "relationship": "记者，老周的联络人"},
        }
    }
    text = format_state_for_prompt(states)
    assert "人物当前状态" in text
    assert "老周" in text and "垂死" in text and "腹部中枪" in text
    assert format_state_for_prompt(empty_states()) == ""
    assert latest_state_before(Path("nonexistent_xyz"), 5).get("characters") == {}
    print("character_state selftest OK")


if __name__ == "__main__":
    _selftest()
