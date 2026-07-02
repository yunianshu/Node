#!/usr/bin/env python3
"""
Outliner Agent - 单章大纲生成Agent
负责按章节范围生成大纲，并拆分为 chapters/outline/chapter_XXXX.json。
"""

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

import argparse
import json
import math
import os
import re
import time

from core.mmx_client import MmxError, call_mmx as call_mmx_client
from core.novel_config import build_origin_fact_directive, load_config, load_origin_materials, resolve_project_dir
from core.outline_constraints import format_outline_constraints
from core.outline_memory import build_outline_memory, format_outline_memory
from core.foreshadowing_ledger import (
    dangling_threads as ledger_dangling,
    load_ledger,
    parse_foreshadowing_field,
    register_thread,
    resolve_thread,
    claim_thread,
    save_ledger,
)
from core.outline_quality_gate import clean_char_name
from core.workflow_state import list_outline_chapters, write_outline_chapters, outline_dir
from core.edit_diff import EditApplyError, apply_json_field_edit

NOVELS_DIR = None
WORLD_FILE = None
CHARACTERS_FILE = None
CONFIG = None
NOVEL_PREMISE = ""
ORIGIN_MATERIALS = ""


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, WORLD_FILE, CHARACTERS_FILE, CONFIG, NOVEL_PREMISE, ORIGIN_MATERIALS
    NOVELS_DIR = Path(project_dir).resolve()
    WORLD_FILE = NOVELS_DIR / "world.json"
    CHARACTERS_FILE = NOVELS_DIR / "characters.json"
    CONFIG = load_config(NOVELS_DIR)
    ORIGIN_MATERIALS = load_origin_materials(NOVELS_DIR, max_chars=3000)
    total = CONFIG["total_chapters"]

    premise_file = NOVELS_DIR / "premise.txt"
    if premise_file.exists():
        NOVEL_PREMISE = premise_file.read_text(encoding="utf-8").replace("{total_chapters}", str(total))
    else:
        NOVEL_PREMISE = f"请围绕既有世界观和角色档案，规划一部长篇小说，全书共{total}章。"


def call_mmx(system_prompt: str, user_prompt: str, max_tokens: int = 8192, temperature: float = 0.5) -> str:
    try:
        cfg = CONFIG.get("outliner", {})
        fallback = CONFIG.get("writer", {})
        return call_mmx_client(
            system_prompt,
            user_prompt,
            model=CONFIG["model"],
            mmx_path=CONFIG["mmx_path"],
            max_tokens=cfg.get("max_tokens", max_tokens),
            temperature=cfg.get("temperature", temperature),
            retries=cfg.get("max_retries", cfg.get("retries", fallback.get("max_retries", 3))),
            retry_delay=cfg.get("retry_delay", fallback.get("retry_delay", 5.0)),
            log_dir=NOVELS_DIR / "logs" / "raw_responses",
            raw_name="outliner",
            qps=CONFIG["api_qps"],
            rate_state_dir=NOVELS_DIR / "logs" / "rate_limit",
        )
    except MmxError as e:
        print(f"[ERROR] mmx调用失败: {e}", file=sys.stderr)
        return ""


def _ledger_resolve_directive(batch_start: int) -> str:
    """生成第 batch_start 章应回收的伏笔清单（注入 prompt）。无则返回空串。"""
    if not NOVELS_DIR:
        return ""
    try:
        ledger = load_ledger(NOVELS_DIR)
        due = ledger_dangling(ledger, as_of_chapter=batch_start)
    except Exception:
        return ""
    if not due:
        return ""
    lines = ["## 【本章必须回收的伏笔】（强制要求，未回收视为不合格）"]
    for t in due[:8]:
        lines.append(
            f"- {t.get('id')}（第{t.get('planted_at')}章埋下，类别{t.get('category')}，"
            f"优先级{t.get('priority')}）：{t.get('setup', '')}"
        )
    lines.append("本章 foreshadowing 字段必须用 [收]FXXX 回收说明 格式回收至少一条。"
                 "埋设新伏笔用 [埋]描述（FXXX）格式。")
    return "\n".join(lines)


def _update_ledger_from_new_chapters(chapters: list) -> None:
    """写大纲后解析新章节 foreshadowing 字段，增量更新伏笔台账。"""
    if not NOVELS_DIR or not chapters:
        return
    try:
        ledger = load_ledger(NOVELS_DIR)
    except Exception:
        return
    tc = int(CONFIG.get("total_chapters", 0)) if CONFIG else 0
    if tc:
        ledger["total_chapters"] = tc
    changed = False
    for ch in chapters:
        num = int(ch.get("chapter_number", 0) or 0) if isinstance(ch, dict) else 0
        if num <= 0:
            continue
        parsed = parse_foreshadowing_field(ch.get("foreshadowing", ""))
        for tid, setup in parsed["planted"]:
            if tid and any(t.get("id") == tid for t in ledger["threads"]):
                continue
            register_thread(ledger, planted_at=num, setup=setup or f"第{num}章伏笔",
                            total_chapters=tc, thread_id=tid or None)
            changed = True
        for tid, note in parsed["resolved"]:
            # 大纲层只标 claimed（声称回收）；正文 writer 兑现后才升 resolved。
            # 这样终审能区分"大纲写了回收但正文没写"的假回收。
            if tid and claim_thread(ledger, tid, claimed_at=num, resolution=note):
                changed = True
            elif tid and resolve_thread(ledger, tid, resolved_at=num, resolution=note):
                changed = True
    if changed:
        save_ledger(NOVELS_DIR, ledger)


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _normalized_character_prompt_data(value):
    """Build a prompt-only character view with clean canonical names."""
    if isinstance(value, list):
        return [_normalized_character_prompt_data(item) for item in value]
    if not isinstance(value, dict):
        return value

    result = {
        key: _normalized_character_prompt_data(child)
        for key, child in value.items()
    }
    if "name" not in value:
        return result

    raw_name = str(value.get("name", "")).strip()
    canonical = clean_char_name(raw_name)
    if canonical:
        result["name"] = canonical

    aliases = value.get("aliases")
    normalized_aliases = []
    if isinstance(aliases, str):
        aliases = [aliases]
    if isinstance(aliases, list):
        normalized_aliases.extend(
            str(alias).strip()
            for alias in aliases
            if str(alias).strip()
        )
    normalized_aliases.extend(
        part.strip()
        for part in re.findall(r"[（(]([^）)]*)[）)]", raw_name)
        if part.strip()
    )
    result["aliases"] = [
        alias
        for alias in dict.fromkeys(normalized_aliases)
        if alias != canonical
    ]
    return result


def _story_architecture_context(
    batch_start: int,
    batch_end: int,
    *,
    book_repair: bool = False,
) -> str:
    """Return the whole-book structure that must constrain chapter design."""
    world = _load_json(WORLD_FILE) if WORLD_FILE else {}
    volume_path = NOVELS_DIR / "volume_outline.json" if NOVELS_DIR else None
    volumes = []
    if volume_path and volume_path.exists():
        raw = _load_json(volume_path)
        if isinstance(raw, list):
            volumes = raw
        elif isinstance(raw, dict):
            values = raw.get("volumes")
            if isinstance(values, list):
                volumes = values

    relevant_volumes = []
    for item in volumes:
        if not isinstance(item, dict):
            continue
        try:
            start = int(item.get("start_chapter", 0) or 0)
            end = int(item.get("end_chapter", 0) or 0)
        except (TypeError, ValueError):
            continue
        if start <= batch_end and end >= batch_start:
            if book_repair and item.get("source_basis") == "existing_outlines":
                relevant_volumes.append({
                    key: item.get(key)
                    for key in (
                        "volume",
                        "title",
                        "start_chapter",
                        "end_chapter",
                        "theme",
                        "core_conflict",
                        "act_role",
                        "emotion_curve",
                    )
                })
            else:
                relevant_volumes.append(item)

    payload = {
        "total_chapters": int(CONFIG.get("total_chapters", 0) or 0) if CONFIG else 0,
        "overall_arc": world.get("overall_arc", ""),
        "three_act_structure": world.get("three_act_structure", {}),
        "themes": world.get("themes", []),
        "current_volume_plan": relevant_volumes,
        "volume_plan_policy": (
            "当前正在修复整本审查冲突。existing_outlines来源卷纲的章级事件摘要属于旧版归纳，"
            "只保留主题和结构方向；审查反馈中的最早事实锚点与相邻章节状态优先。"
            if book_repair
            else "卷纲是单章设计的上位结构约束。"
        ),
        "sensory_palette": (world.get("quality_bible") or {}).get("sensory_palette")
        if isinstance(world.get("quality_bible"), dict)
        else None,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _strip_json_markdown(content: str) -> str:
    if "```json" in content:
        return content.split("```json", 1)[1].split("```", 1)[0].strip()
    if "```" in content:
        return content.split("```", 1)[1].split("```", 1)[0].strip()
    return content.strip()


def _fix_inner_quotes(text: str) -> str:
    """状态机：识别 JSON 字符串边界，将字符串内未转义的 " 替换为单引号。"""
    out = []
    i = 0
    in_string = False
    n = len(text)
    while i < n:
        ch = text[i]
        if not in_string:
            if ch == '"':
                in_string = True
                out.append(ch)
            else:
                out.append(ch)
        else:
            if ch == "\\":
                out.append(ch)
                if i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
            elif ch == '"':
                j = i + 1
                while j < n and text[j] in " \t\n\r":
                    j += 1
                next_ch = text[j] if j < n else ""
                if next_ch in (":", ",", "}", "]", ""):
                    in_string = False
                    out.append(ch)
                else:
                    out.append("'")
            else:
                out.append(ch)
        i += 1
    return "".join(out)


def _fix_unclosed_array_fields(text: str) -> str:
    """修复常见的数组字段少写 ]，下一字段名被塞进数组的问题。"""
    array_next_fields = {
        "characters_involved": {"location", "mood", "key_events"},
        "key_events": {"foreshadowing", "power_progression", "word_count_target"},
        "tension_points": {"story_beat", "chapter_goal", "payoff_design"},
    }

    def repair_one(source: str, field: str, next_fields: set[str]) -> str:
        pattern = re.compile(rf'"{re.escape(field)}"\s*:\s*\[')
        pos = 0
        out = source
        while True:
            match = pattern.search(out, pos)
            if not match:
                return out
            i = match.end()
            depth = 1
            in_string = False
            escape = False
            while i < len(out):
                ch = out[i]
                if in_string:
                    if escape:
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == '"':
                        in_string = False
                    i += 1
                    continue
                if ch == '"':
                    in_string = True
                elif ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        break
                elif ch == "," and depth == 1:
                    next_match = re.match(r'\s*"([^"]+)"\s*:', out[i + 1:])
                    if next_match and next_match.group(1) in next_fields:
                        out = out[:i] + "]" + out[i:]
                        break
                elif ch == "}" and depth == 1:
                    break
                i += 1
            pos = i + 1

    repaired = text
    for field, next_fields in array_next_fields.items():
        repaired = repair_one(repaired, field, next_fields)
    return repaired


def _safe_parse_outline(text: str, depth: int = 0) -> dict | None:
    """尝试多种方式解析大纲 JSON。"""
    def coerce_outline(data) -> dict | None:
        if isinstance(data, dict) and isinstance(data.get("chapters"), list):
            return data
        if isinstance(data, list):
            return {"chapters": data}
        if depth >= 2 or not isinstance(data, dict):
            return None
        for key in ("content", "text", "response", "output", "raw_response"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                nested = _safe_parse_outline(value, depth + 1)
                if nested is not None:
                    return nested
            if isinstance(value, dict):
                nested = coerce_outline(value)
                if nested is not None:
                    return nested
        return None

    text = _strip_json_markdown(text)
    parse_variants = [
        text,
        _fix_inner_quotes(text),
        _fix_unclosed_array_fields(text),
        _fix_inner_quotes(_fix_unclosed_array_fields(text)),
    ]
    for variant in parse_variants:
        try:
            parsed = coerce_outline(json.loads(variant))
            if parsed is not None:
                return parsed
        except Exception:
            pass
    # 3. 截断到最后一个完整的 }
    for end_marker in ('"\n    }\n  ]\n}', '"\n    }\n  ]', '"\n    }', '"\n}'):
        idx = text.rfind(end_marker)
        if idx != -1:
            # 找到包裹的右大括号
            end = text.find("}", idx) + 1
            candidate = text[:end]
            for variant in (
                candidate,
                _fix_inner_quotes(candidate),
                _fix_unclosed_array_fields(candidate),
                _fix_inner_quotes(_fix_unclosed_array_fields(candidate)),
            ):
                try:
                    parsed = coerce_outline(json.loads(variant))
                    if parsed is not None:
                        return parsed
                except Exception:
                    pass
    return None


def _flatten_project_text(*values, limit: int = 12000) -> str:
    parts: list[str] = []

    def walk(value) -> None:
        if len(" ".join(parts)) >= limit:
            return
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif value is not None:
            text = str(value).strip()
            if text:
                parts.append(text)

    for value in values:
        walk(value)
    return " ".join(parts)[:limit]


def _story_payoff_profile(world: dict | None = None) -> dict:
    """Return genre-aware payoff wording so prompts do not force every book into battle/upgrading.

    多标签题材识别：一本混合题材的书（如悬疑+修仙）不再被第一个命中的关键词锁死，
    而是按命中数加权，给出融合的回报链 prompt，避免错配（修仙书被要求写"证据到手"）。
    """
    world = world or _load_json(WORLD_FILE) if WORLD_FILE else {}
    text = _flatten_project_text(world, NOVEL_PREMISE).lower()
    genre_keywords = {
        "suspense": ("悬疑", "案件", "调查", "记者", "证据", "真相", "追踪", "警方", "犯罪", "谜", "反转", "都市"),
        "cultivation": ("修仙", "武道", "玄幻", "境界", "灵气", "真气", "宗门", "神通", "法宝", "妖兽", "飞升", "修炼"),
    }
    scores = {
        genre: sum(1 for kw in kws if kw in text)
        for genre, kws in genre_keywords.items()
    }
    is_suspense = scores["suspense"] > 0
    is_cultivation = scores["cultivation"] > 0

    # 混合题材：两条回报链融合，提示模型按场景选用
    if is_suspense and is_cultivation:
        return {
            "label": "阅读回报（悬疑+成长双线）",
            "chain": "期待→压迫/阻碍→线索反转+能力压制→代价兑现/局势推进",
            "field_hint": "期待→阻碍→反转（线索或能力）→兑现/推进式阅读回报，15字以上",
            "requirement": (
                "payoff_design 必须写清读者期待如何被压迫、关键线索如何反转、主角如何用判断/"
                "行动/代价（可含能力发挥）换来阶段性推进；悬疑线优先真相推进，成长线优先能力代价。"
            ),
            "extra": (
                "本书含悬疑与成长双线：悬疑线回报可以是真相推进、证据到手、关系破局；"
                "成长线回报可以是能力突破的代价与代价后的主动权；禁止硬塞单一模板。"
            ),
        }
    if is_suspense:
        return {
            "label": "阅读回报",
            "chain": "期待→压迫/阻碍→线索反转→代价兑现/局势推进",
            "field_hint": "期待→阻碍→反转→兑现/推进式阅读回报，15字以上",
            "requirement": (
                "payoff_design 必须写清读者期待如何被压迫、关键线索如何反转、主角如何用判断/"
                "行动/代价换来阶段性推进；不要求战力碾压或强敌轻视。"
            ),
            "extra": (
                "本书按悬疑/现实向逻辑处理：回报可以是真相推进、证据到手、关系破局、"
                "逃出生天或代价后的主动权变化，禁止硬塞升级打脸桥段。"
            ),
        }
    if is_cultivation:
        return {
            "label": "爽点链",
            "chain": "期待→压制→反转→兑现",
            "field_hint": "期待→压制→反转→兑现式爽点链，15字以上",
            "requirement": (
                "payoff_design 必须写清期待、受挫、反转和兑现；爽点来自主角判断、能力、"
                "资源或协作的真实发挥，不能靠巧合。"
            ),
            "extra": "升级或战斗只在符合世界观时使用，仍需服务人物目标和代价。",
        }
    return {
        "label": "阅读回报",
        "chain": "期待→阻碍→反转→兑现",
        "field_hint": "期待→阻碍→反转→兑现式阅读回报，15字以上",
        "requirement": (
            "payoff_design 必须写清读者期待、主角遇到的阻碍、局势反转和阶段性兑现；"
            "回报来自人物选择、行动、资源或关系变化。"
        ),
        "extra": "按本书题材选择合适的回报形式，不要套用固定升级、打脸或解谜模板。",
    }


def _character_brief(item: dict) -> dict:
    name = str(item.get("name", "")).strip()
    if not name:
        return {}
    fields = []
    for key in (
        "identity",
        "occupation",
        "role",
        "description",
        "motivation",
        "character_arc",
        "relationship_with_protagonist",
        "life_profile",
        "language_fingerprint",
    ):
        value = item.get(key)
        if isinstance(value, (list, tuple)):
            value = "、".join(str(v) for v in value[:3])
        elif isinstance(value, dict):
            value = "；".join(f"{k}:{v}" for k, v in list(value.items())[:3])
        text = str(value or "").strip()
        if text:
            limit = 140 if key == "life_profile" else 80
            fields.append(f"{key}:{text[:limit]}")
    aliases = item.get("aliases")
    if isinstance(aliases, list) and aliases:
        fields.append("aliases:" + "、".join(str(a) for a in aliases[:4]))
    return {"name": name, "brief": "；".join(fields)[:360]}


def _collect_character_briefs(value, *, limit: int = 10) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()

    def walk(item) -> None:
        if len(result) >= limit:
            return
        if isinstance(item, dict):
            brief = _character_brief(item)
            if brief and brief["name"] not in seen:
                seen.add(brief["name"])
                result.append(brief)
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return result


def _normalize_volume_plan(value) -> list[dict]:
    if isinstance(value, dict):
        value = value.get("volumes")
    return value if isinstance(value, list) else []


def _stamp_volume_plan_metadata(volumes: list[dict], source_basis: str) -> list[dict]:
    """Attach system-owned schema metadata without discarding model semantics."""
    for volume in volumes:
        if not isinstance(volume, dict):
            continue
        volume["plan_version"] = 2
        volume["source_basis"] = source_basis
    return volumes


def _validate_volume_plan(volumes: list[dict], total_chapters: int) -> list[str]:
    issues: list[str] = []
    if not volumes:
        return ["volumes 为空"]
    normalized: list[tuple[int, int, dict]] = []
    for index, volume in enumerate(volumes):
        if not isinstance(volume, dict):
            issues.append(f"volumes[{index}] 不是对象")
            continue
        try:
            start = int(volume.get("start_chapter"))
            end = int(volume.get("end_chapter"))
        except (TypeError, ValueError):
            issues.append(f"volumes[{index}] 起止章节无效")
            continue
        if start <= 0 or end < start:
            issues.append(f"volumes[{index}] 起止范围无效")
        if int(volume.get("plan_version", 0) or 0) < 2:
            issues.append(f"volumes[{index}] plan_version 过旧")
        if volume.get("source_basis") not in {"existing_outlines", "world_and_characters"}:
            issues.append(f"volumes[{index}] source_basis 缺失或非法")
        for field in (
            "title",
            "theme",
            "core_conflict",
            "act_role",
            "opening_state",
            "climax",
            "end_state",
            "volume_hook",
            "emotion_curve",
        ):
            if not str(volume.get(field, "")).strip():
                issues.append(f"volumes[{index}] 缺少 {field}")
        turning_points = volume.get("turning_points")
        if not isinstance(turning_points, list) or not turning_points:
            issues.append(f"volumes[{index}] turning_points 为空")
        normalized.append((start, end, volume))

    normalized.sort(key=lambda item: item[0])
    expected = 1
    for start, end, _ in normalized:
        if start != expected:
            issues.append(f"分卷范围不连续：期望从第{expected}章开始，实际为第{start}章")
        expected = end + 1
    if expected - 1 != total_chapters:
        issues.append(f"分卷未精确覆盖1-{total_chapters}章，实际结束于第{expected - 1}章")
    return list(dict.fromkeys(issues))


def _fallback_volume_plan(
    total_chapters: int,
    chapters_per_volume: int,
    world: dict,
    source_basis: str = "world_and_characters",
) -> list[dict]:
    themes = world.get("themes") if isinstance(world.get("themes"), list) else []
    overall_arc = str(world.get("overall_arc", "") or "主角围绕核心目标持续升级冲突并承担代价")
    volumes: list[dict] = []
    count = max(1, math.ceil(total_chapters / chapters_per_volume))
    for index in range(count):
        start = index * chapters_per_volume + 1
        end = min(total_chapters, (index + 1) * chapters_per_volume)
        ratio = (index + 0.5) / count
        if count == 1:
            act_role = "完整三幕：建立危机、升级反转、完成高潮与阶段收束"
        else:
            act_role = "第一幕：建立目标与不可逆危机" if ratio <= 1 / 3 else (
                "第二幕：升级阻碍、反转认知并累积代价" if ratio <= 2 / 3
                else "第三幕：兑现伏笔、完成高潮与阶段收束"
            )
        theme = str(themes[index % len(themes)]) if themes else "选择、代价与成长"
        midpoint = start + (end - start) // 2
        volumes.append({
            "plan_version": 2,
            "source_basis": source_basis,
            "volume": index + 1,
            "title": f"第{index + 1}卷",
            "start_chapter": start,
            "end_chapter": end,
            "theme": theme,
            "core_conflict": overall_arc[:300],
            "act_role": act_role,
            "opening_state": "承接前卷最终人物、地点、时间、伤势、证据和关系状态",
            "turning_points": [
                {"chapter": start, "function": "建立本卷目标与失败后果"},
                {"chapter": midpoint, "function": "中段反转，改变原行动方案"},
                {"chapter": end, "function": "卷高潮与不可逆代价"},
            ],
            "climax": "核心矛盾在本卷末形成不可逆结果",
            "climax_chapter": end,
            "end_state": "明确记录本卷结束后的角色、线索、能力、关系与未决危机",
            "volume_hook": "以新危机、信息反转或代价开启下一卷",
            "emotion_curve": "建立期待→阻碍升级→短暂兑现→反转受挫→高潮与代价",
        })
    return volumes


def _existing_outline_anchor_context(max_chars: int = 14000) -> tuple[str, str]:
    chapters = list_outline_chapters(NOVELS_DIR)
    if not chapters:
        return "world_and_characters", ""

    chapters = sorted(
        (item for item in chapters if isinstance(item, dict)),
        key=lambda item: int(item.get("chapter_number", 0) or 0),
    )
    if len(chapters) > 60:
        selected = chapters[:10] + chapters[-10:]
        stride = max(1, len(chapters) // 40)
        selected.extend(chapters[index] for index in range(10, len(chapters) - 10, stride))
        chapters = sorted(
            {int(item.get("chapter_number", 0) or 0): item for item in selected}.values(),
            key=lambda item: int(item.get("chapter_number", 0) or 0),
        )

    compact = []
    for item in chapters:
        compact.append({
            "chapter": item.get("chapter_number"),
            "title": item.get("title"),
            "time_progression": item.get("time_progression"),
            "location": item.get("location"),
            "characters_involved": item.get("characters_involved"),
            "summary": str(item.get("summary", ""))[:240],
            "chapter_hook": str(item.get("chapter_hook", ""))[:160],
        })
    return (
        "existing_outlines",
        json.dumps(compact, ensure_ascii=False, separators=(",", ":"))[:max_chars],
    )


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            # Access denied still proves that the PID exists.
            return ctypes.get_last_error() == 5
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_volume_lock(lock_file: Path, stale_seconds: float = 600.0):
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            return fd
        except FileExistsError:
            owner_pid = 0
            try:
                owner_pid = int(lock_file.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                pass
            try:
                age = time.time() - lock_file.stat().st_mtime
            except FileNotFoundError:
                continue
            owner_alive = _process_is_alive(owner_pid)
            invalid_lock_grace = min(2.0, max(0.1, stale_seconds))
            if (
                (owner_pid > 0 and not owner_alive)
                or (owner_pid <= 0 and age > invalid_lock_grace)
            ):
                lock_file.unlink(missing_ok=True)
                continue
            time.sleep(0.1)


def _release_volume_lock(lock_file: Path, fd) -> None:
    os.close(fd)
    try:
        owner_pid = int(lock_file.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        owner_pid = 0
    if owner_pid in {0, os.getpid()}:
        lock_file.unlink(missing_ok=True)


def ensure_volume_outline(*, force: bool = False) -> bool:
    """Ensure Outliner owns a valid whole-book volume plan before chapter design."""
    volume_file = NOVELS_DIR / "volume_outline.json"
    total = int(CONFIG.get("total_chapters", 0) or 0)
    existing = _normalize_volume_plan(_load_json(volume_file))
    if not force and total > 0 and not _validate_volume_plan(existing, total):
        return True

    lock_file = NOVELS_DIR / "logs" / "volume_outline.lock"
    lock_fd = _acquire_volume_lock(lock_file, stale_seconds=600.0)
    try:
        existing = _normalize_volume_plan(_load_json(volume_file))
        if not force and total > 0 and not _validate_volume_plan(existing, total):
            return True

        world = _load_json(WORLD_FILE)
        characters = _normalized_character_prompt_data(_load_json(CHARACTERS_FILE))
        configured_size = int(CONFIG.get("outliner", {}).get("volume_chapters", 25) or 25)
        chapters_per_volume = max(5, min(configured_size, max(5, total)))
        volume_count = max(1, math.ceil(total / chapters_per_volume))
        character_briefs = _collect_character_briefs(characters, limit=12)
        source_basis, outline_anchor_context = _existing_outline_anchor_context()

        prompt = f"""你是长篇中文网络小说的总纲设计师。请在生成任何单章大纲前，先输出覆盖全书的分卷结构。

故事前提：
{NOVEL_PREMISE[:1600]}

世界观：
{json.dumps(world, ensure_ascii=False, separators=(",", ":"))[:5000]}

核心角色：
{json.dumps(character_briefs, ensure_ascii=False, separators=(",", ":"))[:3000]}

origin/ 原始参考素材：
{ORIGIN_MATERIALS or "（无）"}

已存在单章大纲事实锚点：
{outline_anchor_context or "（无，当前是新项目首次规划）"}

全书共{total}章，计划约{volume_count}卷，每卷约{chapters_per_volume}章。
只输出合法JSON对象：
{{
  "volumes": [
    {{
      "plan_version": 2,
      "source_basis": "{source_basis}",
      "volume": 1,
      "title": "卷名",
      "start_chapter": 1,
      "end_chapter": {min(total, chapters_per_volume)},
      "theme": "本卷人物主题",
      "core_conflict": "本卷核心矛盾及失败代价",
      "act_role": "本卷在全书三幕结构中的功能",
      "opening_state": "本卷开始时人物、地点、关系、能力和主线状态",
      "turning_points": [
        {{"chapter": 1, "function": "本卷关键节拍及不可逆变化"}}
      ],
      "climax": "本卷高潮的具体事件与代价",
      "climax_chapter": {min(total, chapters_per_volume)},
      "end_state": "本卷结束后的确定事实状态",
      "volume_hook": "下一卷必须承接的危机或反转",
      "emotion_curve": "本卷情绪曲线"
    }}
  ]
}}

硬约束：
1. 分卷必须从第1章开始、连续无重叠无空档，并精确覆盖到第{total}章。
2. turning_points 必须给出具体章节号，至少覆盖卷首目标、中段反转、卷末高潮。
3. 身份揭露、死亡、被捕、关键证据取得、营救和公开直播等不可逆事件只能规划一次。
4. 每卷 opening_state 必须承接前卷 end_state；时间、伤势、阵营、道具和证据归属不得复位。
5. 高潮与情绪曲线要分层，不能每卷重复同一种追逐、对峙、取证或营救动作链。
6. 角色与设定必须来自 world.json、characters.json 或 origin/，不得临时发明主角团核心成员。
7. 如果“已存在单章大纲事实锚点”非空，这些章节是不可改写事实；卷级规划只能归纳其结构，
   不得改名、改时间、改人物状态、改事件结果，也不得新增与它们冲突的剧情。
"""
        system = (
            "你负责长篇小说卷级结构。输出必须是合法紧凑JSON，"
            "分卷范围必须连续覆盖全书，并为单章Outliner提供不可逆状态锚点。"
        )
        volumes: list[dict] = []
        last_error = ""
        semantic_retries = max(
            0,
            int(CONFIG.get("outliner", {}).get("semantic_retries", 1) or 0),
        )
        retry_hint = ""
        for attempt in range(semantic_retries + 1):
            try:
                content = call_mmx(
                    system,
                    prompt + retry_hint,
                    max_tokens=8192,
                    temperature=0.2,
                )
                parsed = json.loads(_strip_json_markdown(content)) if content else {}
                volumes = _stamp_volume_plan_metadata(
                    _normalize_volume_plan(parsed),
                    source_basis,
                )
                issues = _validate_volume_plan(volumes, total)
                if not issues:
                    break
                last_error = "；".join(issues[:8])
            except Exception as exc:
                last_error = str(exc)
            volumes = []
            if attempt < semantic_retries:
                print(
                    f"[Outliner] 卷级规划契约不完整，原任务重试 "
                    f"{attempt + 1}/{semantic_retries}: {last_error}"
                )
                retry_hint = (
                    "\n\n## 上次输出无效，本次必须从头输出完整JSON\n"
                    f"校验问题：{last_error}\n"
                    "不得省略任何卷级字段，分卷范围必须连续覆盖全部章节。"
                )

        if not volumes:
            print(
                "[Outliner] 卷级规划生成或校验失败，使用确定性兜底: "
                f"{last_error or '未知错误'}"
            )
            volumes = _fallback_volume_plan(
                total,
                chapters_per_volume,
                world,
                source_basis=source_basis,
            )

        temp_file = volume_file.with_name(f"{volume_file.name}.{os.getpid()}.tmp")
        payload = {
            "schema_version": 2,
            "generated_by": "outliner",
            "source_basis": source_basis,
            "volumes": volumes,
        }
        temp_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_file.replace(volume_file)
        print(f"[Outliner] 卷级规划已就绪 -> {volume_file}（{len(volumes)}卷）")
        return True
    finally:
        _release_volume_lock(lock_file, lock_fd)


REQUIRED_CHAPTER_FIELDS = (
    "chapter_number",
    "title",
    "summary",
    "characters_involved",
    "location",
    "mood",
    "key_events",
    "foreshadowing",
    "power_progression",
    "word_count_target",
    "chapter_hook",
    "emotional_arc",
    "tension_points",
    # 救猫咪结构 + 网文增强维度（结构功能/目标赌注/爽点链）
    "story_beat",
    "chapter_goal",
    "payoff_design",
    "human_anchor",
    "content_layers",
    # 推荐维度（对抗/时间/主线）
    "main_antagonist",
    "time_progression",
    "main_arc_link",
    "scenes",
)

# Save the Cat 15 拍 + 网文扩展节拍。story_beat 必须取以下值之一。
# 依据 https://reedsy.com/blog/guide/story-structure/save-the-cat-beat-sheet/
VALID_STORY_BEATS = {
    "opening_image", "theme_stated", "setup", "catalyst", "debate",
    "break_into_two", "b_story", "fun_and_games", "midpoint",
    "bad_guys_close_in", "all_is_lost", "dark_night", "break_into_three",
    "finale", "final_image", "rising_action", "transition",
}

STORY_BEAT_ALIASES = {
    "dark_night_of_the_soul": "dark_night",
    "dark_moment": "dark_night",
    "dark-night": "dark_night",
    "all is lost": "all_is_lost",
    "all-is-lost": "all_is_lost",
    "bad_guys_close": "bad_guys_close_in",
    "bad-guys-close-in": "bad_guys_close_in",
    "break_into_2": "break_into_two",
    "break_into_3": "break_into_three",
    "fun_and_game": "fun_and_games",
    "opening": "opening_image",
    "final": "finale",
}


def _normalize_story_beat(value: object) -> str:
    text = str(value or "").strip()
    if text in VALID_STORY_BEATS:
        return text
    key = text.lower().replace(" ", "_").replace("-", "_")
    return STORY_BEAT_ALIASES.get(key, text)

PLACEHOLDER_TEXTS = {
    "章节标题",
    "核心事件摘要",
    "核心事件摘要（150-250字）",
    "200字详细摘要",
    "角色名1",
    "角色名2",
    "场景地点",
    "情感基调",
    "事件1",
    "事件2",
    "埋下的伏笔",
    "实力变化说明",
    "章末钩子",
    "情绪曲线",
    "张力节点",
    # 新字段占位文本，防模型照抄
    "结构功能",
    "章节目标",
    "爽点设计",
    "内容层次",
    "主要对抗",
    "时间推进",
    "主线关联",
    "故事节拍",
}


PLACEHOLDER_PATTERNS = (
    re.compile(r"此处(写|填|补|描述)"),
    re.compile(r"在此(处)?(写|填|补|描述)"),
    re.compile(r"请(在此)?(写|填|补|描述)"),
    re.compile(r"第[一二三四五六七八九十\d]+(件事|个事件|项)"),
    re.compile(r"\.{3,}|…{1,}"),  # 省略号占位
    re.compile(r"待(补充|填写|完善)"),
    re.compile(r"具体(剧情|内容|事件)(描述)?"),  # "具体剧情"本身是占位
)


def _has_placeholder(value) -> bool:
    if isinstance(value, str):
        text = value.strip()
        if not text or text in PLACEHOLDER_TEXTS or "示例" in text or "占位" in text:
            return True
        # 模式匹配：捕获"此处写标题""第一件事"等变体占位
        if any(pattern.search(text) for pattern in PLACEHOLDER_PATTERNS):
            return True
        return False
    if isinstance(value, list):
        return any(_has_placeholder(item) for item in value)
    return False


def _split_compound_list_item(value: object) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    numbered = [
        item.strip("；;，, \t\r\n")
        for item in re.split(r"(?=[①②③④⑤⑥⑦⑧⑨⑩])", text)
        if item.strip("；;，, \t\r\n")
    ]
    if len(numbered) >= 3:
        return numbered
    punct = [
        item.strip("；;，, \t\r\n")
        for item in re.split(r"[；;\n]", text)
        if item.strip("；;，, \t\r\n")
    ]
    return punct if len(punct) >= 3 else []


def _flatten_outline_list_items(items: list) -> list[str]:
    flattened: list[str] = []
    for item in items:
        if isinstance(item, list):
            flattened.extend(_flatten_outline_list_items(item))
            continue
        text = str(item or "").strip()
        if text:
            flattened.append(text)
    return flattened


def _validate_chapter_outline(chapter: dict, expected_number: int | None = None) -> list[str]:
    issues = []
    if not isinstance(chapter, dict):
        return ["章节对象不是JSON object"]

    for field in REQUIRED_CHAPTER_FIELDS:
        if field not in chapter:
            issues.append(f"缺少字段 {field}")

    chapter_number = chapter.get("chapter_number")
    if expected_number is not None and chapter_number != expected_number:
        issues.append(f"chapter_number应为{expected_number}，实际为{chapter_number}")

    summary = str(chapter.get("summary", "")).strip()
    if len(summary) < 120:
        issues.append("summary少于120字")
    if _has_placeholder(summary):
        issues.append("summary仍是占位文本")

    characters = chapter.get("characters_involved")
    if not isinstance(characters, list) or len([item for item in characters if str(item).strip()]) < 1:
        issues.append("characters_involved必须至少包含1个角色")
    elif _has_placeholder(characters):
        issues.append("characters_involved包含占位文本")

    key_events = chapter.get("key_events")
    if isinstance(key_events, str):
        parts = _split_compound_list_item(key_events)
        if parts:
            chapter["key_events"] = parts
            key_events = parts
    elif isinstance(key_events, list):
        if any(isinstance(item, list) for item in key_events):
            chapter["key_events"] = _flatten_outline_list_items(key_events)
            key_events = chapter["key_events"]
        nonempty_events = [item for item in key_events if str(item).strip()]
        if len(nonempty_events) == 1:
            parts = _split_compound_list_item(nonempty_events[0])
            if parts:
                chapter["key_events"] = parts
                key_events = parts
    event_items = [item for item in key_events if str(item).strip()] if isinstance(key_events, list) else []
    if not isinstance(key_events, list) or len(event_items) < 3:
        issues.append("key_events必须至少包含3个具体事件")
    elif len(event_items) > 9:
        issues.append("key_events不得超过9个，需合并重复动作并保留5-7个核心事件")
    elif any(len(str(item).strip()) > 140 for item in event_items):
        issues.append("key_events单条不得超过140字，需压缩为可执行事件")
    elif _has_placeholder(key_events):
        issues.append("key_events包含占位文本")

    for field in ("title", "location", "mood", "foreshadowing", "power_progression", "chapter_hook", "emotional_arc"):
        value = str(chapter.get(field, "")).strip()
        if len(value) < 2:
            issues.append(f"{field}不能为空")
        if _has_placeholder(value):
            issues.append(f"{field}仍是占位文本")

    # story_beat 枚举硬校验（防模型乱填结构功能标签）
    story_beat = _normalize_story_beat(chapter.get("story_beat", ""))
    if story_beat != str(chapter.get("story_beat", "")).strip():
        chapter["story_beat"] = story_beat
    if story_beat not in VALID_STORY_BEATS:
        issues.append(f"story_beat 必须是有效节拍值之一（如 catalyst/midpoint/all_is_lost/finale 等），当前为：{story_beat}")

    payoff_profile = _story_payoff_profile()
    # 核心增强字段：目标赌注 + 阅读回报，要求>=15字（与 summary 校验风格一致）
    for field in ("chapter_goal", "payoff_design"):
        value = str(chapter.get(field, "")).strip()
        if len(value) < 15:
            issues.append(
                f"{field}不少于15字（chapter_goal 写清主角想要什么+失败后果；"
                f"payoff_design 写清{payoff_profile['chain']}）"
            )
        if _has_placeholder(value):
            issues.append(f"{field}仍是占位文本")

    human_anchor = str(chapter.get("human_anchor", "")).strip()
    if len(human_anchor) < 25:
        issues.append("human_anchor不少于25字，需写清生活压力、关系牵挂、潜台词或生活物件")
    if _has_placeholder(human_anchor):
        issues.append("human_anchor仍是占位文本")

    content_layers = chapter.get("content_layers")
    if isinstance(content_layers, str):
        parts = [item.strip() for item in re.split(r"[；;\n、]", content_layers) if item.strip()]
        if parts:
            chapter["content_layers"] = parts
            content_layers = parts
    layer_items = [str(item).strip() for item in content_layers if str(item).strip()] if isinstance(content_layers, list) else []
    if len(layer_items) < 2:
        issues.append("content_layers至少包含2层内容：外部事件推进 + 关系/生活压力/秘密代价/世界规则现场化等")
    elif _has_placeholder(content_layers):
        issues.append("content_layers包含占位文本")

    # 推荐字段：仅校验非空（对抗/时间/主线）
    for field in ("main_antagonist", "time_progression", "main_arc_link"):
        value = str(chapter.get(field, "")).strip()
        if len(value) < 2:
            issues.append(f"{field}不能为空")
        if _has_placeholder(value):
            issues.append(f"{field}仍是占位文本")

    tension_points = chapter.get("tension_points")
    if isinstance(tension_points, str):
        parts = [item.strip() for item in re.split(r"[；;\n、]", tension_points) if item.strip()]
        if parts:
            chapter["tension_points"] = parts
            tension_points = parts
    elif isinstance(tension_points, list):
        if any(isinstance(item, list) for item in tension_points):
            chapter["tension_points"] = _flatten_outline_list_items(tension_points)
            tension_points = chapter["tension_points"]
        nonempty_tensions = [item for item in tension_points if str(item).strip()]
        if len(nonempty_tensions) == 1:
            parts = [
                item.strip()
                for item in re.split(r"[；;\n]|(?=【(?:前段|中段|后段)】)", str(nonempty_tensions[0]))
                if item.strip()
            ]
            if len(parts) >= 3:
                chapter["tension_points"] = parts
                tension_points = parts
    if (
        (not isinstance(tension_points, list) or len([item for item in tension_points if str(item).strip()]) < 3)
        and isinstance(key_events, list)
        and len(key_events) >= 3
    ):
        chapter["tension_points"] = [
            "前段：" + str(key_events[0])[:80],
            "中段：" + str(key_events[len(key_events) // 2])[:80],
            "后段：" + str(key_events[-1])[:80],
        ]
        tension_points = chapter["tension_points"]
    if not isinstance(tension_points, list) or len([item for item in tension_points if str(item).strip()]) < 3:
        issues.append("tension_points必须至少包含3个张力节点")
    elif _has_placeholder(tension_points):
        issues.append("tension_points包含占位文本")

    try:
        target = int(chapter.get("word_count_target", 0))
        if target < 3000:
            issues.append("word_count_target低于3000")
    except (TypeError, ValueError):
        issues.append("word_count_target必须是数字")

    # scenes 场景级设计校验（向后兼容：旧章节缺 scenes 由 REQUIRED_CHAPTER_FIELDS 已报"缺少字段"，
    # 这里校验字段质量）
    scenes = chapter.get("scenes")
    if isinstance(scenes, str):
        issues.append("scenes 必须是数组，不能是字符串")
    elif not isinstance(scenes, list):
        issues.append("scenes 缺失或不是数组")
    else:
        if len(scenes) < 3 or len(scenes) > 5:
            issues.append("scenes 必须为3-5个场景")
        valid_positions = {"opening", "middle", "climax", "closing"}
        scene_anchors: list[str] = []
        for scene_idx, scene in enumerate(scenes):
            if not isinstance(scene, dict):
                issues.append(f"scenes[{scene_idx}] 不是对象")
                continue
            for scene_field in ("position", "objective", "conflict", "sensory_anchor", "subtext_beat", "exit_hook"):
                scene_val = str(scene.get(scene_field, "")).strip()
                if len(scene_val) < 4:
                    issues.append(f"scenes[{scene_idx}].{scene_field} 过短或为空")
                if _has_placeholder(scene_val):
                    issues.append(f"scenes[{scene_idx}].{scene_field} 是占位文本")
            scene_pos = str(scene.get("position", "")).strip()
            if scene_pos and scene_pos not in valid_positions:
                issues.append(f"scenes[{scene_idx}].position 必须是 {','.join(sorted(valid_positions))} 之一")
            scene_anchor = str(scene.get("sensory_anchor", "")).strip()
            if scene_anchor:
                scene_anchors.append(scene_anchor)
        if scene_anchors and len(scene_anchors) != len(set(scene_anchors)):
            issues.append("scenes 的 sensory_anchor 存在重复，每个场景物象必须独立")

    return issues


def _validate_outline_batch(chapters: list, batch_start: int, batch_end: int) -> None:
    if not isinstance(chapters, list):
        raise ValueError("chapters不是数组")
    expected_count = batch_end - batch_start + 1
    if len(chapters) != expected_count:
        raise ValueError(f"章节数量不匹配，期望{expected_count}章，实际{len(chapters)}章")

    issues = []
    for offset, chapter in enumerate(chapters):
        expected_number = batch_start + offset
        chapter_issues = _validate_chapter_outline(chapter, expected_number)
        if chapter_issues:
            issues.append(f"第{expected_number}章: {'; '.join(chapter_issues)}")
    if issues:
        raise ValueError("大纲结构不完整: " + " | ".join(issues[:5]))


def _outline_quality_contract() -> str:
    min_score = float(CONFIG.get("outline_reviewer", {}).get("min_score", 8.5))
    payoff_profile = _story_payoff_profile()
    return f"""## 【高质量单章大纲契约】（必须满足，否则视为不合格）

### 【读下去设计·最高优先】（大纲决定读者留存，违反即判"普通"）
- 【开篇钩子】本章 key_events 的第一个事件、以及 summary 开头，必须是抓人的开篇——主角正卷入冲突/撞见反常/危机当头。**禁止"介绍设定/铺垫背景/平淡日常"开头**。设计前先想："读者翻开本章第一段，会看到什么有张力的画面？"
- 【{payoff_profile['label']}必释放】{payoff_profile['requirement']}
- 【题材适配】{payoff_profile['extra']}
- 【赌注升级】chapter_goal 的赌注必须具体可感、比上一章更高，失败后果触及主角核心利益。

- 大纲审查目标分必须达到 {min_score:g} 分及以上；低于该分数视为不合格，需要重写。
- 必须顺接前章结尾的人物状态、地点、时间和危机，不能跳场景、跳时间、跳动机。
- 输出前快速自检：钩子是否强力？情绪是否起伏？张力节点是否≥3个？是否反套路？人物动机是否合理？

### 【章末钩子·强制要求】
- chapter_hook 字段必须明确写出本章最后200字要落地的强力钩子，且必须是以下三种之一：
  1. 危机升级钩子：主角或核心人物突然陷入更大危险
  2. 信息反转钩子：抛出颠覆前文认知的关键信息
  3. 情感爆点钩子：人物关系发生剧烈撕裂或质变
- 禁止以"平静收尾、总结现状、铺垫过渡"作为章末钩子。
- 禁止对称式安全锁收尾：不得设计"身后……身前……""朝某方向迈步""一步，又一步""未知的路"等机械锚点句式。钩子必须来自具体现场：未完成动作、反常反应、物件暴露或关系裂变。

### 【情绪曲线·强制要求】
- emotional_arc 字段必须描述本章的情绪变化轨迹，例如："压抑→紧张→短暂希望→绝望反转"。
- 禁止一整章都是同一种情绪（如全程紧张或全程平淡）。

### 【烟火气与人情味·强制要求】
- human_anchor 字段必须写成本章的人情味锚点：具体生活压力 + 关系牵挂/亏欠 + 一句潜台词或一个生活物件，不能少于25字。
- 本章必须设计至少一个贴近日常生计、家庭/邻里/同事关系、旧情分、亏欠、照料、面子或尊严的具体压力点，不能只有宏大危机、系统任务或抽象利益。
- 至少一个关键事件必须让人物在"完成目标"与"照顾某个人/守住某段关系/保住体面"之间产生取舍；没有关系代价的胜利不算高质量回报。
- 对白设计必须预留潜台词：人物不能把动机、背景和感情全说透，应有一句绕开真话、顾左右而言他或欲言又止的瞬间。
- 场景必须有可触摸的生活细节：饭菜气味、旧物、账单、工位、楼道、雨棚、手上伤口、手机电量等，服务人物处境，不得堆砌风景。

### 【张力节点·强制要求】
- tension_points 字段必须列出至少3个"让人无法停止阅读"的关键时刻，标注它们在章节中的大致位置（前/中/后）。
- 每个张力节点必须是：新信息曝光、冲突升级、意外转折、或人物关系质变。

### 【反套路·强制要求】
- 不得与前后章节核心事件重复；若发现重复，必须重新设计本章独有冲突和阅读回报。
- 禁止套路化设计：禁止"遇敌→分析→升级→打赢"的标准战斗流程，禁止"发现问题→查资料→解决"的标准解谜流程。
- 每章必须产生有效的新进展：新信息、关系变化、风险升级、目标推进或旧伏笔回收至少一项；不强制新增人物或地点。

### 【内容丰富度·强制要求】
- 必须从 world.quality_bible 中选择至少一条内容密度规则或禁用套路落地到本章设计；不能只满足通用模板。
- 本章至少包含两类内容层：外部事件推进 + 人物关系/生活压力/秘密代价/世界规则现场化中的一类。只有单线打斗、单线调查或单线赶路视为单薄。
- 场景不得只是地点名，必须写出一个会影响人物选择的具体物件、制度、账目、伤势、天气后果、职业流程或生活噪声。
- 如果 characters.relationship_matrix 中存在本章人物关系，本章必须推进其中一个 hidden_debt、pressure_trigger 或 payoff_direction；没有关系推进时，必须在 human_anchor 中说明原因和替代的人情味压力。
- 若本章采用群像/多线协作，必须设计“从重奏到独步”的结构：前中段让多方行动形成压力或铺路，中后段收束为主角自己的判断、行动和代价，不能让主角只旁观配角推进。
- 关键证据、信物、药包、账页、钥匙、录音等必须在 key_events 或 payoff_design 中写出传递链：起点、转交动作、接收者理解方式、风险、最终用途。
- main_antagonist/payoff_design 必须写出反派遇到破绽后的应对方式：搬规矩、拖程序、威胁、交易、嫁祸或冷处理，不要只写反派发怒。
- 如果反派有标志性物件或意象（灯笼、刀、戒指、手套、烟、车等），必须规划一次“物象反照内层”：该物件照出、碰到或遮住反派不愿面对的旧事、软肋、签押、伤痕或破绽。
- 旧签押、旧证词、旧物证或熟人指认逼到反派时，必须规划一个极短身体裂隙（目光移开、指节发白、喉咙动、张口又咽回、手套按疤、灯柄轻响等），再让反派用规矩/程序/威胁冷处理，避免只有“猛地站起/脸色变了”。
- 章末要使用的关键物/证据/拓印/录音/信物必须在 key_events 前段预埋制作、藏匿、转交或被角色瞥见的动作，避免结尾突然冒出。
- 若章末出现墨印、拓片、副本、录音备份等复制型证据，key_events 必须提前写出复制动作：蘸墨、按压、拓下、晾干、夹入、换袋或藏入夹层。
- 前文若出现亲缘旧痕、父亲字迹、旧称呼、手把手教过的动作等情感线索，chapter_hook 或 payoff_design 必须设计一个极小回扣，让字形、手势、触感或旧称呼在章末关键动作里返回。
- chapter_hook 不得只停在主角动作本身；关键动作之后要设计1-2个短现场反应作为余韵，例如反派停顿、灯光偏移、旁人吸气、同伴松手或账本合上。
- 多地点或跨时间段必须在 summary/key_events 中给出转场桥：声音、灯光、脚步、物件到达、传话延迟、时辰变化、伤口变化等，防止关键行动链跳步。
- 禁止"打卡地图"：新地点不能只为拿道具、升境界或过副本服务；location/summary/key_events 必须体现当地风土人情、制度规则、生计结构、普通人压力或文化差异中的至少两项。
- 禁止"抽象概念堆叠"：foreshadowing、power_progression、payoff_design 不能只写道意、本源、法则、共鸣、境界变化；必须说明它在身体、器物、环境、关系或现实成本上造成的具体后果。

### 【场景级设计·强制要求】（scenes 数组）
- 必须输出 3-5 个 scene 对象，每个含 position(opening/middle/climax/closing)、objective(本场景主角具体目标)、conflict(阻碍+对手+赌注)、sensory_anchor(本场景专属可触摸物象/气味/声音/身体细节，取自 quality_bible.sensory_palette 对应类别)、subtext_beat(一句潜台词或未说出口的话)、exit_hook(本场景如何推向下一场景的不可逆动作或信息落点)。
- 每个 scene 的 sensory_anchor 必须互不重复，且至少一个 scene 落地本章 human_anchor 的生活压力/关系牵挂。
- sensory_anchor 必须从 world.quality_bible.sensory_palette 五类(indoor/outdoor/body/object/sound_smell)中取材，不能写空泛形容词。
- scenes 与 key_events 互补：key_events 是高层事件链，scenes 是场景级执行蓝图；Writer 会按 scenes 逐场兑现。

### 【基础要求】
- summary 必须写具体剧情链路：起因、冲突、转折、结果、章末钩子，不得写模板话。
- key_events 至少 5 条，按发生顺序列出，每条必须包含行动、阻碍和结果。
- foreshadowing 必须包含本章埋下或回收的具体伏笔，不能只写抽象评价。
- power_progression 必须说明主角能力、资源、关系、情报或目标的具体变化。
- 人物动机必须可执行、可理解，不能为了剧情强行行动。
- 人物动机必须落到具体关系与具体处境：为谁、欠谁、怕谁失望、怕失去什么、眼前要解决哪件生活难题。
- 每章必须有冲突升级和阅读回报，回报来自主角判断、能力、资源、关系或协作的实际发挥。
- 回报应触及角色核心欲望、恐惧或当前阶段目标，不能只有表层事件堆叠。

### 【金手指·建议】（power_progression）
- power_progression 如果涉及主角金手指/外挂使用，建议体现有限制（代价/门槛）、有逻辑（自洽）、有成长（进化）、有融合（与世界观绑定）。

### 【设计硬门槛】
- 五项硬门槛必须在章级尺度成立：明确欲望、产生真实代价的承诺或选择、中段改变行动方案、一个可复述的不可逆动作、章末正在发生的强钩子。若是群像章节，还必须额外完成“群像铺压→主角独步”的转折。

### 【章节标题·建议】（title）
- title 建议使用以下五种方法之一设计，避免平淡的"第X章"：
  事件概括/人物对白/悬念暗示/情绪渲染/诗词化用，2-8字简洁有力。

### 【结构功能·强制要求】（story_beat）
- story_beat 必须从节拍枚举中选取，且必须呼应本章在全卷/全书中的结构位置。
- 关键节拍判据：catalyst（催化事件）打破日常、midpoint（中点）让赌注升级且主角从被动转主动、all_is_lost（谷底）制造最低点、finale（高潮）兑现主线承诺。
- 禁止给中段连续多章都标 transition/rising_action 而无任何 catalyst/midpoint 式转折——那是"注水腰"的信号。

### 【闭环角色·强制要求】（防人物漂移，world_consistency 主因）
- characters_involved 必须只含"角色设定/世界观"中已登记角色的规范名，每个名字是纯名字（如"周德茂""苏雯"）；禁止带括号注释（如"苏雯（记者）"）、"类别："前缀（如"反派：高建军"）或整句描述/群体名（如"受害者家属""神秘追踪者"）。
- 不得临时生造未登记的新命名角色。若剧情确需新压力来源，优先使用已登记角色或机构名称写在剧情字段中，不要把临时人名放进 characters_involved。
- 同一角色全章只用其规范名或已登记别名，不得换用未绑定的新称呼。

### 【视角与多线·建议】
- 默认以主角视角叙事；如需切换 POV，在 summary 中标明"[POV:角色名]"。
- 多线叙事时，main_arc_link 说明本章事件如何与主线交汇。

### 【章节目标与赌注·强制要求】（chapter_goal）
- chapter_goal 必须写清：①主角本章具体想要什么（可执行的目标）；②失败的后果是什么（赌注）。
- 目标必须是"本章可推进、可部分达成或可受挫"的，不能是全书级宏大目标（如"成为最强"）。
- 赌注必须触及角色核心利益（生存/关系/目标/秘密），不能只是无关痛痒的得失。

### 【阅读回报·强制要求】（payoff_design）
- payoff_design 必须描述完整的{payoff_profile['label']}：{payoff_profile['chain']}。
- 回报必须来自主角的判断、行动、能力、资源、关系或协作的真实发挥，禁止靠巧合或天降外挂硬赢。
- 回报应触及角色核心欲望或恐惧，而非只有表层事件堆叠。
- 回报核心是"获取后的利用"：如果本章主角获得了新能力/新资源/新信息，建议 payoff_design 说明主角如何将其利用起来，而非"得到即高潮"。"""


def _feedback_attempt_count(review_feedback_data: dict, chapter_no: int) -> int:
    item = review_feedback_data.get(str(chapter_no))
    if not isinstance(item, dict):
        return 0
    analysis = item.get("failure_analysis")
    if isinstance(analysis, dict):
        try:
            return int(analysis.get("attempts") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _feedback_hard_constraints(review_feedback_data: dict, batch_start: int, batch_end: int) -> list[str]:
    constraints: list[str] = [
        (
            f"本次只允许输出第{batch_start}章到第{batch_end}章；"
            "反馈中出现的其它章节号只作跨章上下文，不得把 chapter_number 改成其它章节。"
        )
    ]
    for key, value in review_feedback_data.items():
        if not isinstance(value, dict):
            continue
        chapter_no = value.get("chapter")
        if not chapter_no or not (batch_start <= int(chapter_no) <= batch_end):
            continue
        policy = str(value.get("repair_policy", "")).strip()
        if policy:
            constraints.append(policy)
        fact_anchors = value.get("fact_anchors")
        if isinstance(fact_anchors, list):
            for anchor in fact_anchors:
                if not isinstance(anchor, dict):
                    continue
                anchor_chapter = anchor.get("chapter")
                time_progression = str(anchor.get("time_progression", "")).strip()
                location = str(anchor.get("location", "")).strip()
                constraints.append(
                    f"第{anchor_chapter}章是已确认前序事实锚点；"
                    f"时间={time_progression or '未标注'}；地点={location or '未标注'}。"
                    "本章必须从该状态单调向后推进，后续旧大纲不得反向覆盖此前事实。"
                )
        analysis = value.get("failure_analysis") if isinstance(value.get("failure_analysis"), dict) else {}
        if value.get("gate") == "outline_book_review":
            repair_items = value.get("repair_items", [])
            if isinstance(repair_items, list) and repair_items:
                item = repair_items[0]
                if isinstance(item, dict):
                    problem = str(item.get("problem", "")).strip()
                    acceptance = str(item.get("acceptance", "")).strip()
                    evidence = str(item.get("evidence", "")).strip()
                    constraints.append(
                        f"单条修复项：问题={problem or evidence or '未说明'}；"
                        f"验收={acceptance or '本章只修复该问题并保持前后章连续'}。"
                    )
                else:
                    constraints.append(f"单条修复项：{str(item).strip()}")
            else:
                values = analysis.get("adjustments", []) or analysis.get("likely_reasons", [])
                if isinstance(values, list) and values:
                    constraints.append(str(values[0]).strip())
        reviews = value.get("reviews") if isinstance(value.get("reviews"), list) else []
        # Candidate-specific feedback follows immutable book-review anchors.
        # It may refine the repair, but must not reverse established chronology.
        for review in reversed(reviews[-3:]):
            if not isinstance(review, dict):
                continue
            status = str(review.get("status", "")).strip().lower()
            if status and status != "completed":
                continue
            try:
                float(review.get("overall_score"))
            except (TypeError, ValueError):
                continue
            for field in ("continuity_issues", "suggestions", "weaknesses"):
                values = review.get(field, [])
                if isinstance(values, list) and values:
                    first = str(values[0]).strip()
                    if first:
                        constraints.append(first)
                    break
        if value.get("gate") != "outline_book_review":
            for field in ("adjustments", "likely_reasons"):
                values = analysis.get(field, [])
                if isinstance(values, list) and values:
                    first = str(values[0]).strip()
                    if first:
                        constraints.append(first)
                    break
    return list(dict.fromkeys(constraints))[:5]


def _best_valid_feedback_review(value: dict, require_edits: bool = False) -> dict:
    reviews = value.get("reviews") if isinstance(value.get("reviews"), list) else []
    candidates: list[tuple[int, float, dict]] = []
    for index, review in enumerate(reviews):
        if not isinstance(review, dict):
            continue
        status = str(review.get("status", "")).strip().lower()
        if status and status != "completed":
            continue
        try:
            score = float(review.get("overall_score"))
        except (TypeError, ValueError):
            continue
        edits = review.get("edits")
        if require_edits and (not isinstance(edits, list) or not edits):
            continue
        candidates.append((index, score, review))
    if not candidates:
        return {}
    return max(candidates, key=lambda item: (item[1], item[0]))[2]


def _compact_review_feedback(review_feedback_data: dict, batch_start: int, batch_end: int) -> dict:
    compact: dict[str, dict] = {}
    for key, value in review_feedback_data.items():
        if not isinstance(value, dict):
            continue
        chapter_no = value.get("chapter")
        if not chapter_no or not (batch_start <= int(chapter_no) <= batch_end):
            continue

        analysis = value.get("failure_analysis") if isinstance(value.get("failure_analysis"), dict) else {}
        latest = _best_valid_feedback_review(value)
        compact[key] = {
            "chapter": chapter_no,
            "repair_policy": value.get("repair_policy"),
            "repair_items": list(value.get("repair_items", []))[:1],
            "fact_anchors": list(value.get("fact_anchors", []))[:5],
            "best_score": analysis.get("best_score"),
            "must_fix": list(analysis.get("likely_reasons", []))[:1],
            "required_adjustments": list(analysis.get("adjustments", []))[:1],
            "latest_verdict": latest.get("verdict"),
            "latest_score": latest.get("overall_score"),
            "latest_weaknesses": list(latest.get("weaknesses", []))[:1],
            "latest_suggestions": list(latest.get("suggestions", []))[:1],
            "latest_continuity_issues": list(latest.get("continuity_issues", []))[:1],
        }
    return compact


def _get_direct_outline_edits(review_feedback_data: dict, chapter_no: int) -> list[dict]:
    """从 review_feedback 中提取指定章节的字段级 edits。

    早期只取 edits[:1] 丢弃了其余修复点，导致关键问题（如 key_events）未修就回退到
    模型重生成。改为返回全部 edits，但排除高风险的 key_events 整体替换（仍保留单条）。
    """
    for key, value in review_feedback_data.items():
        if not isinstance(value, dict):
            continue
        if int(value.get("chapter", 0)) != chapter_no:
            continue
        latest = _best_valid_feedback_review(value, require_edits=True)
        edits = latest.get("edits")
        if isinstance(edits, list) and edits:
            # 应用全部 edits，但每条校验；最多 5 条防失控
            return list(edits)[:5]
    return []


def _get_direct_outline_source(review_feedback_data: dict, chapter_no: int) -> Path | None:
    """Use the highest-scoring reviewed candidate as the cumulative edit base."""
    for value in review_feedback_data.values():
        if not isinstance(value, dict):
            continue
        if int(value.get("chapter", 0) or 0) != chapter_no:
            continue
        review = _best_valid_feedback_review(value, require_edits=True)
        candidate = review.get("candidate_file")
        if candidate:
            path = Path(str(candidate))
            if path.exists():
                return path
    return None


def _chapter_signature(chapter_data: dict) -> tuple[str, str]:
    """提取章节的内容指纹：summary + 拼接的 key_events。

    用于相似度对比，忽略 title/time_progression 等可变字段，聚焦核心剧情。
    """
    summary = str(chapter_data.get("summary", "")).strip()
    events = chapter_data.get("key_events", [])
    if isinstance(events, list):
        events_text = " ".join(str(e) for e in events)
    else:
        events_text = str(events or "")
    return summary, events_text


def _bigram_set(text: str) -> set[str]:
    import re as _re
    compact = _re.sub(r"[\W_]+", "", str(text), flags=_re.UNICODE)
    return {compact[i:i + 2] for i in range(max(0, len(compact) - 1))}


def _similarity(a: str, b: str) -> float:
    sa, sb = _bigram_set(a), _bigram_set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _find_duplicate_candidate(candidate_file: Path, chapter_data: dict) -> Path | None:
    """检测候选是否与历史候选实质雷同（summary/key_events 相似度≥0.8）。

    早期实现用整章 JSON 字节级精确匹配，模型改个标点就绕过；改为内容相似度检测，
    让 race 竞速真正选到不同的候选。
    """
    candidate_root = candidate_file.parent.parent
    new_summary, new_events = _chapter_signature(chapter_data)
    for path in candidate_root.glob("round*_attempt*/candidate_*.json"):
        if path == candidate_file or path.name.endswith("_review.json"):
            continue
        try:
            existing = _load_json(path)
        except Exception:
            continue
        ex_summary, ex_events = _chapter_signature(existing)
        # summary 或 key_events 任一高度相似即判雷同
        if _similarity(new_summary, ex_summary) >= 0.8 or _similarity(new_events, ex_events) >= 0.8:
            return path
    return None


def _pacing_guard_violations(outline_chapters: list, batch_end: int, *, max_run: int = 4) -> list[dict]:
    """全局节奏守卫：检测写入新章节后是否产生连续注水段。

    单章 story_beat 校验只能挡"标错值"，挡不住"内容注水但标 transition"。
    本函数在写入后统计连续 transition/rising_action/setup 章数，超过阈值即报违规，
    触发该批次重新生成。治"注水腰"——整本 pacing_curve 低分的根因。

    只检查截至 batch_end 的已存在章节，复用 outline_quality_gate.detect_beat_runs 逻辑。
    """
    from core.outline_quality_gate import detect_beat_runs, normalize_beat
    flat_beats = []
    for ch in outline_chapters:
        if not isinstance(ch, dict):
            continue
        num = int(ch.get("chapter_number", 0) or 0)
        if num <= 0 or num > batch_end:
            continue
        beat = str(ch.get("story_beat", "")).strip()
        if beat:
            flat_beats.append((num, beat))
    flat_beats.sort(key=lambda x: x[0])
    # 注水 beat 集合：连续多章这类无转折节拍即注水腰
    flat_beat_set = {"transition", "rising_action", "setup", "fun_and_games", "b_story"}
    issues: list[dict] = []
    # 自检连续注水 beat（detect_beat_runs 检测任意 beat，这里更严格针对注水 beat）
    norm = [(ch, normalize_beat(b)) for ch, b in flat_beats]
    i = 0
    while i < len(norm):
        j = i
        while (
            j + 1 < len(norm)
            and norm[j + 1][0] == norm[j][0] + 1
            and norm[j + 1][1] == norm[i][1]
            and norm[i][1] in flat_beat_set
        ):
            j += 1
        run_len = j - i + 1
        if run_len > max_run and norm[i][1] in flat_beat_set:
            chs = [norm[k][0] for k in range(i, j + 1)]
            issues.append({
                "type": "sagging_middle",
                "chapters": chs,
                "beat": norm[i][1],
                "run_length": run_len,
                "evidence": f"第{chs[0]}-{chs[-1]}章连续{run_len}章注水beat'{norm[i][1]}'，无转折，构成注水腰",
            })
        i = j + 1
    # 同时用 detect_beat_runs 兜底任意 beat 的连续塌陷
    issues.extend(detect_beat_runs(flat_beats, max_consecutive=max_run))
    # 去重
    seen = set()
    deduped = []
    for item in issues:
        key = (item.get("type"), tuple(item.get("chapters", [])))
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


def _volume_alignment_violations(
    new_chapters: list[dict], batch_start: int, batch_end: int
) -> list[str]:
    """卷纲衔接校验：单章 story_beat 须服从所在卷的 turning_points 设计。

    早期只校验卷纲自身完整性，不校验单章是否服从卷纲——卷纲说 ch50 是 midpoint，
    但 ch50 写成 transition 也能过，卷纲成摆设。本函数读取 volume_outline，
    对落在 turning_points/climax_chapter 的章节，校验其 story_beat 是否为转折类。
    """
    if not NOVELS_DIR:
        return []
    volume_path = NOVELS_DIR / "volume_outline.json"
    if not volume_path.exists():
        return []
    volumes = _load_json(volume_path)
    if isinstance(volumes, dict):
        volumes = volumes.get("volumes", [])
    if not isinstance(volumes, list):
        return []

    # 转折类 beat：卷纲的关键节点应该是这些
    turning_beats = {
        "catalyst", "midpoint", "all_is_lost", "dark_night",
        "break_into_two", "break_into_three", "finale",
    }
    issues: list[str] = []
    for ch in new_chapters:
        if not isinstance(ch, dict):
            continue
        num = int(ch.get("chapter_number", 0) or 0)
        beat = str(ch.get("story_beat", "")).strip()
        if not num or not beat:
            continue
        # 找到该章所在卷
        for vol in volumes:
            if not isinstance(vol, dict):
                continue
            try:
                vs = int(vol.get("start_chapter", 0) or 0)
                ve = int(vol.get("end_chapter", 0) or 0)
            except (TypeError, ValueError):
                continue
            if not (vs <= num <= ve):
                continue
            # 该章是否是卷纲指定的转折点
            is_turning = False
            tps = vol.get("turning_points", [])
            if isinstance(tps, list):
                for tp in tps:
                    if isinstance(tp, dict) and int(tp.get("chapter", 0) or 0) == num:
                        is_turning = True
                        break
            climax_ch = int(vol.get("climax_chapter", 0) or 0)
            if climax_ch == num:
                is_turning = True
            if is_turning and beat not in turning_beats:
                issues.append(
                    f"第{num}章是卷纲指定的转折点，story_beat 应为转折类"
                    f"（catalyst/midpoint/all_is_lost/finale 等），实际为 '{beat}'"
                )
            break
    return issues


def _context_lines(outline: dict, batch_start: int, batch_end: int, rescue: bool = False) -> str:
    if not outline.get("chapters"):
        return ""
    before = [
        ch for ch in outline["chapters"]
        if isinstance(ch.get("chapter_number"), int) and ch.get("chapter_number", 0) < batch_start
    ]
    after = [
        ch for ch in outline["chapters"]
        if isinstance(ch.get("chapter_number"), int) and ch.get("chapter_number", 0) > batch_end
    ]
    before.sort(key=lambda x: x.get("chapter_number", 0))
    after.sort(key=lambda x: x.get("chapter_number", 0))
    limit = 4 if rescue else 5
    items = list(before[-limit:]) + list(after[:limit])

    # 动态补充远端关键事实锚点：±3 章窗口看不到更早的不可逆事件（死亡/身份揭露），
    # 导致跨卷身份/生死被后章推翻。把更早章节中含伏笔埋设[埋]或能力重大变化的章拉进来。
    def _is_fact_anchor(ch: dict) -> bool:
        foreshadow = str(ch.get("foreshadowing", ""))
        power = str(ch.get("power_progression", ""))
        return "[埋]" in foreshadow or bool(foreshadow.strip() and ("F0" in foreshadow or "F1" in foreshadow)) or len(power) > 30

    existing_nums = {ch.get("chapter_number") for ch in items}
    extra_far: list[dict] = []
    for ch in before[:-limit] if len(before) > limit else []:
        if ch.get("chapter_number") in existing_nums:
            continue
        if _is_fact_anchor(ch):
            extra_far.append(ch)
    # 远端锚点最多补 3 个（取最近的几个关键事实章），避免 prompt 膨胀
    for ch in extra_far[-3:]:
        if ch.get("chapter_number") not in existing_nums:
            items.append(ch)
            existing_nums.add(ch.get("chapter_number"))
    items.sort(key=lambda x: x.get("chapter_number", 0))

    if not items:
        return ""

    title = "前后章事实锚点（用于衔接，不得照抄、推翻或重复）"
    lines = [f"\n{title}："]
    for ch in items:
        lines.append(
            f"第{ch.get('chapter_number')}章《{ch.get('title')}》："
            f"时间={str(ch.get('time_progression', ''))[:100]}；"
            f"地点={str(ch.get('location', ''))[:80]}；"
            f"摘要={str(ch.get('summary', ''))[:260]}；"
            f"关键事件={json.dumps(ch.get('key_events', []), ensure_ascii=False)[:500]}；"
            f"能力/资源状态={str(ch.get('power_progression', ''))[:140]}；"
            f"伏笔状态={str(ch.get('foreshadowing', ''))[:140]}；"
            f"章末状态={str(ch.get('chapter_hook', ''))[:180]}"
        )
    return "\n".join(lines) + "\n"


def _build_rescue_prompt(
    batch_start: int,
    batch_end: int,
    world_json: str,
    chars_json: str,
    prev_context: str,
    review_feedback_data: dict,
    long_memory: str,
    ledger_constraints: str,
) -> str:
    constraints = _feedback_hard_constraints(review_feedback_data, batch_start, batch_end)
    constraints_text = "\n".join(f"- {item}" for item in constraints) or "- 修复上一轮所有审查问题，保持前后章连续。"
    premise = NOVEL_PREMISE[:1000]
    characters = (
        _normalized_character_prompt_data(_load_json(CHARACTERS_FILE))
        if CHARACTERS_FILE
        else {}
    )
    protagonist = characters.get("protagonist", {}) if isinstance(characters, dict) else {}
    sample_name = protagonist.get("name") if isinstance(protagonist, dict) else ""
    sample_name = sample_name or "主角"
    payoff_profile = _story_payoff_profile()
    architecture = _story_architecture_context(
        batch_start,
        batch_end,
        book_repair=any(
            isinstance(value, dict) and value.get("gate") == "outline_book_review"
            for value in review_feedback_data.values()
        ),
    )
    return f"""你正在修复第{batch_start}章到第{batch_end}章的大纲卡点。目标不是扩写，而是输出短小、闭合、可审查通过的JSON。

世界观摘要：
{world_json}

角色摘要：
{chars_json}

故事前提摘要：
{premise}

全书结构与当前分卷规划：
{architecture}

前序全局记忆：
{long_memory}

全书结构化台账硬约束：
{ledger_constraints}

{prev_context}
必须修复的硬约束：
{constraints_text}

{_outline_quality_contract()}

卡章救援规则：
- 若同一问题已重复出现，不得沿用上一版的事件顺序、场景组织和反转方式，必须重构冲突链。
- 前一章结尾状态是本章开场硬起点，后一章开场状态是本章结尾硬终点；时间、地点、人物伤势和道具状态必须闭合。
- 不得用重复发生的灾难、重复倒计时或另一轮纯对话对峙冒充新转折。
- 最新 continuity_issues 的优先级高于早期泛化建议；冲突时以最新意见和前后章事实为准。

输出要求：
- 只输出合法JSON，不要Markdown代码块，不要解释文字。
- 只生成第{batch_start}章到第{batch_end}章，共{batch_end - batch_start + 1}个章节对象。
- chapters 数组中每个对象的 chapter_number 必须严格落在 {batch_start}-{batch_end} 内；不得照抄反馈里的其它章节号。
- 字段必须完整：chapter_number/title/summary/characters_involved/location/mood/key_events/foreshadowing/power_progression/word_count_target/chapter_hook/emotional_arc/tension_points/story_beat/chapter_goal/payoff_design/human_anchor/content_layers/main_antagonist/time_progression/main_arc_link。
- story_beat 必须取枚举值：opening_image/theme_stated/setup/catalyst/debate/break_into_two/b_story/fun_and_games/midpoint/bad_guys_close_in/all_is_lost/dark_night/break_into_three/finale/final_image/rising_action/transition。
- summary 控制在150-220字，key_events 只写5-6条，每条不超过70字。
- chapter_goal 写清主角想要什么+失败后果；payoff_design 写清题材适配的期待→阻碍/压迫→反转→兑现/推进。
- 不允许尾随逗号，不允许注释，不允许省略号，不允许占位文本。

JSON结构：
{{
  "chapters": [
    {{
      "chapter_number": {batch_start},
      "title": "具体章节名",
      "summary": "150-220字具体剧情摘要，必须顺接前章并给下一章留下接口",
      "characters_involved": ["{sample_name}"],
      "location": "具体地点",
      "mood": "具体情绪基调",
      "key_events": ["事件1", "事件2", "事件3", "事件4", "事件5"],
      "foreshadowing": "具体伏笔",
      "power_progression": "具体能力、资源、关系或情报进展",
      "word_count_target": 5000,
      "chapter_hook": "最后200字落地的危机升级、信息反转或情感爆点",
      "emotional_arc": "压抑→紧张→短暂希望→反转",
      "tension_points": ["前段张力节点", "中段张力节点", "后段张力节点"],
      "story_beat": "从枚举值选取，须呼应本章结构位置（如 catalyst/midpoint/all_is_lost/finale）",
      "chapter_goal": "主角本章具体想要什么+失败的后果（赌注），15字以上",
      "payoff_design": "{payoff_profile['field_hint']}",
      "human_anchor": "本章烟火气锚点：具体生活压力、关系牵挂、潜台词或生活物件，25字以上",
      "content_layers": ["外部事件推进层：本章现场行动和可见结果", "人物关系/生活压力层：谁与谁的压力、亏欠或潜台词被推进"],
      "main_antagonist": "本章主要对抗力量（人或势力或困境）",
      "time_progression": "本章相对前章的时间推进（如次日清晨/三天后/同一夜）",
      "main_arc_link": "本章如何推进全书主线（如揭示主线新线索/达成阶段目标）"
    }}
  ]
}}"""


def generate_outline_range(
    start: int,
    end: int,
    outline_file: Path = None,
    fill_gaps: bool = False,
    chapter: int = None,
    review_feedback: Path = None,
    rescue: bool = False,
    candidate_file: Path = None,
):
    batch_size = 5
    if outline_file is not None:
        print("[Outliner] --outline-file 已废弃并被忽略；大纲统一写入 chapters/outline/chapter_XXXX.json")
    failures = []

    world = _load_json(WORLD_FILE)
    characters = _normalized_character_prompt_data(_load_json(CHARACTERS_FILE))
    
    # 精简世界观设定，避免请求过大导致 API 超时
    world_summary = {
        "title": world.get("title", ""),
        "world_name": world.get("world_name", ""),
        "world_description": world.get("world_description", "")[:800],
        "quality_bible": world.get("quality_bible", {}),
        "power_system": {
            "name": world.get("power_system", {}).get("name", ""),
            "description": world.get("power_system", {}).get("description", "")[:500],
            "levels": world.get("power_system", {}).get("levels", []),
        },
        "factions": world.get("factions", [])[:6],
        "key_locations": world.get("key_locations", [])[:6],
        "rules": world.get("rules", []),
    }
    # 注入八维世界观建构（如果 planner 已生成）
    world_building = world.get("world_building")
    if world_building:
        world_summary["world_building"] = world_building
    world_json = json.dumps(world_summary, ensure_ascii=False, indent=2)
    
    # 精简角色设定，兼容新版嵌套 characters.json。
    chars_summary = {"_meta": characters.get("_meta", {})}
    protagonist = characters.get("protagonist", {})
    chars_summary["protagonist"] = {
        "name": protagonist.get("name", ""),
        "identity": protagonist.get("identity", protagonist.get("occupation", "")),
        "occupation": protagonist.get("occupation", ""),
        "motivation": protagonist.get("motivation", ""),
        "growth_path": protagonist.get("growth_path", []),
        "character_arc": protagonist.get("character_arc", ""),
        "signature_ability": protagonist.get("signature_ability", protagonist.get("abilities", "")),
    }
    chars_summary["key_characters"] = _collect_character_briefs(characters, limit=12)
    if isinstance(characters.get("relationship_matrix"), list):
        chars_summary["relationship_matrix"] = characters.get("relationship_matrix", [])[:8]
    if isinstance(characters.get("casting_plan"), dict):
        chars_summary["casting_plan"] = characters.get("casting_plan", {})
    chars_json = json.dumps(chars_summary, ensure_ascii=False, indent=2)

    # 读取审查意见
    review_feedback_data = {}
    if review_feedback and review_feedback.exists():
        try:
            review_feedback_data = json.loads(review_feedback.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[Outliner] 审查意见文件读取失败: {e}")

    # 单章模式：强制只生成指定章节
    if chapter is not None:
        start = chapter
        end = chapter

    outline = {"chapters": list_outline_chapters(NOVELS_DIR)}
    covered = {
        item.get("chapter_number")
        for item in outline["chapters"]
        if start <= item.get("chapter_number", 0) <= end
    }
    if not fill_gaps and not chapter and len(covered) == end - start + 1:
        print(f"[Outliner] 单章大纲范围{start}-{end}已覆盖，跳过")
        return

    # 计算实际需要生成的章节范围
    if candidate_file and chapter:
        batch_ranges = [(chapter, chapter)]
    elif chapter:
        batch_ranges = [(chapter, chapter)]
    elif fill_gaps:
        existing_chapters = {item.get("chapter_number") for item in outline.get("chapters", [])}
        missing = [ch for ch in range(start, end + 1) if ch not in existing_chapters]
        if not missing:
            print(f"[Outliner] 范围内无缺失章节，跳过")
            return
        # 将缺失章节分组为连续的批次
        batch_ranges = []
        batch_s = missing[0]
        batch_e = missing[0]
        for ch in missing[1:]:
            if ch == batch_e + 1:
                batch_e = ch
            else:
                batch_ranges.append((batch_s, batch_e))
                batch_s = ch
                batch_e = ch
        batch_ranges.append((batch_s, batch_e))
    else:
        batch_ranges = [(s, min(s + batch_size - 1, end)) for s in range(start, end + 1, batch_size)]

    system = """你是一位顶级中文网络小说大纲设计师。
你需要设计详细的大纲，每章包含标题、核心事件、涉及角色、场景、情感基调、伏笔、能力/事业进展。
严格按照 premise 中描述的故事设定和主角设定来设计大纲。
即使只生成单章，也必须输出可供正文写作的完整章节设计，不能输出模板占位词。
输出必须是合法的JSON格式。"""

    for batch_start, batch_end in batch_ranges:
        # === 方案B：单章字段级增量修改优先 ===
        if (
            chapter is not None
            and batch_start == batch_end == chapter
            and review_feedback_data
        ):
            direct_edits = _get_direct_outline_edits(review_feedback_data, chapter)
            if direct_edits:
                official_file = outline_dir(NOVELS_DIR) / f"chapter_{chapter:04d}.json"
                source_file = _get_direct_outline_source(review_feedback_data, chapter) or official_file
                if source_file.exists():
                    try:
                        chapter_data = _load_json(source_file)
                        for edit in direct_edits:
                            apply_json_field_edit(chapter_data, edit)
                        issues = _validate_chapter_outline(chapter_data, expected_number=chapter)
                        if not issues:
                            if candidate_file:
                                duplicate = _find_duplicate_candidate(candidate_file, chapter_data)
                                if duplicate:
                                    print(
                                        f"[Outliner] 字段级 edits 产生重复候选 "
                                        f"{duplicate.name}，回退到模型重构"
                                    )
                                else:
                                    candidate_file.parent.mkdir(parents=True, exist_ok=True)
                                    candidate_file.write_text(
                                        json.dumps(chapter_data, ensure_ascii=False, indent=2),
                                        encoding="utf-8",
                                    )
                                    print(
                                        f"[Outliner] 第{chapter}章通过字段级 edits 生成候选 -> "
                                        f"{candidate_file}（基线: {source_file.name}）"
                                    )
                                    continue
                            else:
                                write_outline_chapters(
                                    NOVELS_DIR,
                                    {"chapters": [chapter_data]},
                                    skip_existing=False,
                                )
                                print(f"[Outliner] 第{chapter}章通过字段级 edits 直接修复")
                                continue
                        else:
                            safe_edits = [
                                edit for edit in direct_edits
                                if isinstance(edit, dict) and edit.get("field") != "key_events"
                            ]
                            safe_data = _load_json(source_file)
                            if safe_edits and len(safe_edits) < len(direct_edits):
                                for edit in safe_edits:
                                    apply_json_field_edit(safe_data, edit)
                                safe_issues = _validate_chapter_outline(
                                    safe_data,
                                    expected_number=chapter,
                                )
                                duplicate = (
                                    _find_duplicate_candidate(candidate_file, safe_data)
                                    if candidate_file and not safe_issues
                                    else None
                                )
                                if not safe_issues and not duplicate:
                                    if candidate_file:
                                        candidate_file.parent.mkdir(parents=True, exist_ok=True)
                                        candidate_file.write_text(
                                            json.dumps(safe_data, ensure_ascii=False, indent=2),
                                            encoding="utf-8",
                                        )
                                    else:
                                        write_outline_chapters(
                                            NOVELS_DIR,
                                            {"chapters": [safe_data]},
                                            skip_existing=False,
                                        )
                                    print(
                                        f"[Outliner] 整批 edits 校验失败，已保留 "
                                        f"{len(safe_edits)} 条非 key_events 安全修改"
                                    )
                                    continue
                            print(f"[Outliner] 字段级 edits 修复后校验失败: {'; '.join(issues[:3])}，回退到模型生成")
                    except (EditApplyError, Exception) as e:
                        print(f"[Outliner] 字段级 edits apply 失败: {e}，回退到模型生成")

        print(f"[Outliner] 正在生成第 {batch_start}-{batch_end} 章大纲...")
        memory_cfg = CONFIG.get("outline_memory", {})
        long_memory = format_outline_memory(
            build_outline_memory(
                NOVELS_DIR,
                batch_start,
                milestone_size=int(memory_cfg.get("milestone_size", 25) or 25),
                recent_chapters=int(memory_cfg.get("recent_chapters", 8) or 8),
            ),
            max_chars=int(memory_cfg.get("max_prompt_chars", 7000) or 7000),
        )
        ledger_constraints = format_outline_constraints(
            NOVELS_DIR,
            batch_start,
            batch_end,
            max_chars=int(memory_cfg.get("ledger_max_prompt_chars", 7000) or 7000),
        )

        rescue_mode = rescue or (
            chapter is not None
            and batch_start == batch_end
            and _feedback_attempt_count(review_feedback_data, batch_start) >= 3
        )

        prev_context = _context_lines(outline, batch_start, batch_end, rescue=rescue_mode)

        review_section = ""
        if review_feedback_data:
            batch_feedback = _compact_review_feedback(review_feedback_data, batch_start, batch_end)
            if batch_feedback:
                review_section = f"""\n上一轮大纲审查反馈（请特别注意并改进以下问题；其中提到的其它章节号只作上下文，本次输出 chapter_number 必须是 {batch_start}）：\n{json.dumps(batch_feedback, ensure_ascii=False, indent=2)}\n"""

        prompt_premise = NOVEL_PREMISE
        prompt_origin = ORIGIN_MATERIALS or "（无）"
        if chapter is not None:
            prompt_premise = NOVEL_PREMISE[:1800]
            prompt_origin = ORIGIN_MATERIALS[:800] if ORIGIN_MATERIALS else "（无）"

        if rescue_mode:
            print(f"[Outliner] 第 {batch_start}-{batch_end} 章启用卡章救援模式")
            prompt = _build_rescue_prompt(
                batch_start,
                batch_end,
                world_json,
                chars_json,
                prev_context,
                review_feedback_data,
                long_memory,
                ledger_constraints,
            )
        else:
            ledger_resolve_directive = _ledger_resolve_directive(batch_start)
            book_repair = any(
                isinstance(value, dict) and value.get("gate") == "outline_book_review"
                for value in review_feedback_data.values()
            )
            architecture = _story_architecture_context(
                batch_start,
                batch_end,
                book_repair=book_repair,
            )
            fact_directive = build_origin_fact_directive(prompt_origin, limit=8)
            prompt = f"""你正在为一部追求9分神作的中文网文设计单章大纲。下面是本次要生成的章节范围、世界观、角色设定和参考素材。

## ⚠️ 硬门槛（必须优先满足，否则视为不合格）
{_outline_quality_contract()}

## 世界观设定
{world_json}

## 角色设定
{chars_json}

## 故事前提
{prompt_premise}

## 全书结构与当前分卷规划
正常生成时这是所有章节的上位约束。整本审查修复时，若卷纲来源为 existing_outlines，
其中章级事件属于待修旧版摘要；必须以审查反馈的最早事实锚点和相邻章节状态为准：
{architecture}

## origin/ 原始参考素材
{prompt_origin}

{fact_directive}
## 前序大纲全局压缩记忆
以下记忆由此前全部单章大纲派生，用于保持长期主线、角色、能力和伏笔连续；不得机械重复其中事件：
{long_memory}

## 全书结构化台账硬约束
以下内容来自人物、能力、时间、地点、伏笔和重复事件台账；不得违反：
{ledger_constraints}

{prev_context}
{review_section}

请输出以下JSON结构。所有字段都是必填；不得输出“章节标题”“200字详细摘要”“事件1”等占位文本。
story_beat 必须取枚举值之一：opening_image/theme_stated/setup/catalyst/debate/break_into_two/b_story/fun_and_games/midpoint/bad_guys_close_in/all_is_lost/dark_night/break_into_three/finale/final_image/rising_action/transition：
{{
  "chapters": [
    {{
      "chapter_number": {batch_start},
      "title": "章节标题",
      "summary": "150-220字具体摘要：写清起因、冲突、转折、结果和本章结尾钩子",
      "characters_involved": ["角色名1", "角色名2"],
      "location": "场景地点",
      "mood": "情感基调",
      "key_events": ["5-7个具体事件，按发生顺序列出，每条不超过90字"],
      "foreshadowing": "本章埋下或回收的具体伏笔",
      "power_progression": "本章主角能力、资源、关系或事业进展",
      "word_count_target": 5000,
      "chapter_hook": "最后200字落地的危机升级、信息反转或情感爆点",
      "emotional_arc": "压抑→紧张→短暂希望→反转",
      "tension_points": ["前段张力节点", "中段张力节点", "后段张力节点"],
      "story_beat": "枚举值，须呼应本章在全卷结构中的位置（如卷首catalyst、卷中midpoint、卷末finale）",
      "chapter_goal": "主角本章具体想要什么+失败的后果（赌注），15字以上",
      "payoff_design": "{_story_payoff_profile()['field_hint']}",
      "human_anchor": "本章烟火气锚点：具体生活压力、关系牵挂、潜台词或生活物件，25字以上",
      "content_layers": ["外部事件推进层：本章现场行动和可见结果", "人物关系/生活压力层：谁与谁的压力、亏欠或潜台词被推进"],
      "main_antagonist": "本章主要对抗力量（具体人或势力或困境）",
      "time_progression": "本章相对前章的时间推进（如次日清晨/三天后/同一夜）",
      "main_arc_link": "本章如何推进全书主线（如揭示主线新线索/达成阶段目标）"
    }}
  ]
}}
{ledger_resolve_directive}
## 【配角弧线规划】（强制要求）
为每个本章涉及的有名配角落实弧线推进：若该配角正处于转折/高潮/收束节点，本章 key_events 必须包含其弧线推进事件，禁止配角出场后连续多章消失或沦为背景板。

要求：
1. 每章必须有独特的核心事件，不能流水账；key_events 必须5-7条，不能为空，单条不超过90字
2. story_beat 必须与本章实际剧情结构相符，且要考虑全卷/全书节奏曲线——催化事件(catalyst)、中点(midpoint)、谷底(all_is_lost)、高潮(finale)等关键节拍要落在合理位置，禁止中段连续多章 transition 造成注水腰
3. chapter_goal 必须是本章可推进的具体目标，不能是全书级宏大目标；必须写清失败后果（赌注）
4. payoff_design 必须设计完整阅读回报链：{_story_payoff_profile()['chain']}，回报来自主角判断、行动、资源、关系或能力的真实发挥
5. human_anchor 必须具体说明本章的人情味锚点，至少包含生活压力、关系牵挂、潜台词或生活物件中的三类
6. content_layers 必须至少两条，明确本章除了外部事件推进之外，还推进了人物关系、生活压力、秘密代价或世界规则现场化中的哪一层
7. 本章必须设计一个可复述的不可逆动作：签下/撕毁/交出/藏起/公开/背叛/救下/放弃/承认/误伤/暴露等，不能只用氛围和意象表达“往前挪”
8. 若本章有群像/多线协作，必须写出“从重奏到独步”：配角如何铺路或施压，主角最终如何独自做出不可逆动作
9. 关键证据/信息/信物必须有清楚传递链，不能只写“传出去/大家知道了”；接收者必须有可理解暗语或现场反应
10. 反派遇到证据、质问或民意压力时必须有冷处理策略（规矩、程序、威胁、交易、嫁祸），不能只写发怒
11. 反派标志物/贯穿意象必须有一次反照内层或旧事的设计，不能只做随身道具
12. 旧案证据逼到反派时必须设计一个半拍身体裂隙，再接冷处理策略
13. 章末关键道具/证据必须在前文预埋一次制作、藏匿、转交或瞥见的动作
14. 墨印/拓片/副本/录音备份等复制型证据必须提前写出复制动作，不能结尾突然出现
15. 父辈/亲缘/旧痕线索必须在章末关键动作中有微小回扣
16. 章末关键动作后必须设计1-2个现场反应形成余韵，不能动作一落就截断
17. 跨地点、跨时辰、并行动作必须有转场桥，保证读者知道同一时间各线如何咬合
18. 情节要有起伏，有高潮有低谷，有符合本书题材的阶段性回报
19. 主角的能力、资源、关系、情报或目标要有具体变化，不能原地踏步
20. 伏笔要前后呼应，与前一批大纲自然衔接
21. main_antagonist 和 payoff_design 要体现明确阻碍、压力来源和阶段性兑现；不要硬塞强敌轻视或战力碾压桥段
22. 探索不同场景时要展现环境差异和世界多样性
23. 地图切换必须有因果和代价：若本章进入新地点，必须写出当地风土人情/制度规则/生计结构/文化差异，不能只把地点当通关清单
24. 抽象概念必须落地：修炼、规则、科技或神秘体系变化必须转化为身体代价、器物变化、环境后果、旁人反应或关系成本
25. 如果 origin/ 中存在素材，必须参考其中的设定、人物关系、历史事件和风格约束，不能与其冲突；若存在 origin/facts，本章 key_events、summary 或 human_anchor 必须落地 1-2 条事实线索
26. summary 必须是具体剧情摘要，不能写“200字详细摘要”等占位内容
27. foreshadowing 和 power_progression 必须有具体内容，不能缺失或留空
28. 必须输出合法JSON，总共{batch_end - batch_start + 1}个章节对象，chapter_number 必须严格落在 {batch_start}-{batch_end} 内
29. 单章大纲整体保持紧凑，避免长段解释；必须优先保证JSON闭合和所有必填字段完整
30. 若本批次包含多章，必须把它们设计成连续状态机：前章章末的人物、地点、时间、伤势、证据和关系状态，必须原样成为后章开场事实
31. 死亡、被捕、身份揭露、关键证据取得、营救成功和公开直播均属于不可逆事件，同一事件全书只能发生一次
32. 每章必须有独占的核心场景和核心动作链；不得把同一追逐、对峙、取证、营救或直播拆成两章重复叙述"""

        import time as _time
        semantic_retries = max(
            0,
            int(CONFIG.get("outliner", {}).get("semantic_retries", 1) or 0),
        )
        batch_succeeded = False
        batch_error = "empty_response"
        retry_hint = ""
        for semantic_attempt in range(semantic_retries + 1):
            attempt_prompt = prompt + retry_hint
            start_time = _time.time()
            content = call_mmx(
                system,
                attempt_prompt,
                max_tokens=4096 if rescue_mode else 8192,
                temperature=0.25 if rescue_mode else 0.5,
            )
            elapsed = _time.time() - start_time
            print(
                f"[Outliner] 第 {batch_start}-{batch_end} 章大纲生成 API 调用耗时 "
                f"{elapsed:.1f}s"
            )
            if not content:
                batch_error = "empty_response"
            else:
                try:
                    stripped = _strip_json_markdown(content)
                    try:
                        from core.json_repair import repair_latin1_gbk_mojibake
                        stripped = repair_latin1_gbk_mojibake(stripped)
                    except Exception:
                        pass
                    try:
                        from core.json_repair import fix_truncated_json
                        stripped = fix_truncated_json(stripped)
                    except Exception:
                        pass
                    batch_outline = _safe_parse_outline(stripped)
                    if batch_outline is None:
                        raise ValueError("所有 JSON 解析策略均失败")
                    new_chapters = batch_outline.get("chapters", [])
                    _validate_outline_batch(new_chapters, batch_start, batch_end)
                    if candidate_file:
                        duplicate = _find_duplicate_candidate(candidate_file, new_chapters[0])
                        if duplicate:
                            raise ValueError(
                                f"候选内容与历史候选重复: {duplicate}"
                            )
                    print(f"[Outliner] 第 {batch_start}-{batch_end} 章大纲已生成（{len(new_chapters)}章）")
                    if candidate_file:
                        candidate_file.parent.mkdir(parents=True, exist_ok=True)
                        candidate_file.write_text(
                            json.dumps(new_chapters[0], ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        print(f"[Outliner] 候选大纲已保存 -> {candidate_file}")
                    else:
                        outline["chapters"].extend(new_chapters)
                        skip_existing = fill_gaps and chapter is None
                        write_outline_chapters(
                            NOVELS_DIR,
                            {"chapters": new_chapters},
                            skip_existing=skip_existing,
                        )
                        try:
                            _update_ledger_from_new_chapters(new_chapters)
                        except Exception as _le:
                            print(f"[Outliner] 台账更新失败（不影响大纲）: {_le}")
                        # 卷纲衔接校验：转折点章节的 story_beat 须为转折类
                        align_issues = _volume_alignment_violations(new_chapters, batch_start, batch_end)
                        if align_issues:
                            detail = "; ".join(align_issues[:3])
                            print(f"[Outliner] ⚠️ 卷纲衔接校验失败：{detail}")
                            for ch in new_chapters:
                                num = int(ch.get("chapter_number", 0) or 0)
                                if num:
                                    f = outline_dir(NOVELS_DIR) / f"chapter_{num:04d}.json"
                                    if f.exists():
                                        f.unlink()
                            raise ValueError(f"卷纲衔接：转折点章节 story_beat 与卷纲冲突。{detail}")
                        # G-节奏守卫：写入后检测是否产生连续注水段（>4章同注水beat）。
                        # 单章生成时 chapter 模式下跨章上下文不足，跳过；仅批次/连续生成时检查。
                        if chapter is None and len(new_chapters) > 1:
                            try:
                                pacing_issues = _pacing_guard_violations(
                                    list_outline_chapters(NOVELS_DIR), batch_end, max_run=4
                                )
                                if pacing_issues:
                                    detail = "; ".join(
                                        item.get("evidence", "")[:120] for item in pacing_issues[:3]
                                    )
                                    print(f"[Outliner] ⚠️ 节奏守卫触发：{detail}")
                                    # 回滚本批次新章节，强制下一轮语义重试时带反馈重生成
                                    for ch in new_chapters:
                                        num = int(ch.get("chapter_number", 0) or 0)
                                        if num:
                                            f = outline_dir(NOVELS_DIR) / f"chapter_{num:04d}.json"
                                            if f.exists():
                                                f.unlink()
                                    raise ValueError(f"节奏守卫：检测到注水腰，需重新设计转折节拍。{detail}")
                            except ValueError:
                                raise
                            except Exception as _pe:
                                print(f"[Outliner] 节奏守卫检查异常（忽略）: {_pe}")
                    batch_succeeded = True
                    break
                except Exception as e:
                    batch_error = str(e)
                    print(f"[Outliner] 第 {batch_start}-{batch_end} 章解析失败: {e}")
                    raw_file = NOVELS_DIR / "logs" / f"outline_batch_{batch_start:04d}.raw"
                    raw_file.parent.mkdir(parents=True, exist_ok=True)
                    raw_file.write_text(content, encoding="utf-8")

            if semantic_attempt < semantic_retries:
                print(
                    f"[Outliner] 第 {batch_start}-{batch_end} 章结构不完整，"
                    f"原批次语义重试 {semantic_attempt + 1}/{semantic_retries}"
                )
                retry_hint = f"""

## 上次输出无效，本次必须纠正
校验错误：{batch_error[:1200]}
请从头返回完整合法 JSON。每个章节对象必须包含示例中的全部字段，不得在 main_arc_link 之前提前结束；
不得解释、不得省略字段、不得只返回修补片段。"""

        if not batch_succeeded:
            print(f"[Outliner] 第 {batch_start}-{batch_end} 章大纲生成失败")
            failures.append((batch_start, batch_end, batch_error))

    generated_count = end - start + 1 - len(failures)
    if candidate_file:
        print(f"[Outliner] 候选请求范围 {start}-{end} 已处理，本次成功 {max(0, generated_count)} 章")
    else:
        print(
            f"[Outliner] 本次请求范围 {start}-{end} 已处理，"
            f"本次成功 {max(0, generated_count)} 章，当前项目累计大纲 {len(outline['chapters'])} 章"
        )
    if failures:
        print(f"[Outliner] 失败批次: {failures}")
        return False
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-p", type=str, default=os.getenv("NOVEL_PROJECT_DIR", ""), help="小说项目目录")
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=0, help="结束章节")
    parser.add_argument("--outline-file", type=str, default="", help="兼容参数；已废弃，大纲统一写入单章文件")
    parser.add_argument("--fill-gaps", action="store_true", help="只生成缺失的章节，跳过已存在的")
    parser.add_argument("--chapter", type=int, default=0, help="只生成指定单章的大纲")
    parser.add_argument("--review-feedback", type=str, default="", help="大纲审查意见JSON文件路径，用于指导改进")
    parser.add_argument("--rescue", action="store_true", help="启用单章卡点救援提示，压缩上下文并强制短JSON输出")
    parser.add_argument("--candidate-file", type=str, default="", help="候选大纲输出文件；启用后不写入正式大纲目录")
    parser.add_argument("--volume-only", action="store_true", help="仅生成或校验 volume_outline.json")
    parser.add_argument("--force-volume", action="store_true", help="忽略现有卷纲并重新生成")
    args = parser.parse_args()

    try:
        project = resolve_project_dir(args.project)
    except ValueError as exc:
        print(f"错误: {exc}")
        sys.exit(1)

    init_project(project)
    end = args.end or CONFIG["total_chapters"]

    print("=" * 60)
    if args.chapter:
        print(f"Outliner Agent 启动 - 单章: 第{args.chapter}章")
    elif args.fill_gaps:
        print(f"Outliner Agent 启动 - 填补空缺: 第{args.start}章到第{end}章")
    else:
        print(f"Outliner Agent 启动 - 范围: 第{args.start}章到第{end}章")
    print(f"项目: {NOVELS_DIR}")
    print("=" * 60)

    NOVELS_DIR.mkdir(parents=True, exist_ok=True)
    if not ensure_volume_outline(force=args.force_volume):
        print("[Outliner] 卷级规划未就绪")
        sys.exit(1)
    if args.volume_only:
        print("[Outliner] 卷级规划检查完成")
        return
    outline_file = Path(args.outline_file) if args.outline_file else None
    candidate_file = Path(args.candidate_file) if args.candidate_file else None
    if candidate_file and args.chapter <= 0:
        print("错误: --candidate-file 必须与 --chapter 一起使用")
        sys.exit(1)
    ok = generate_outline_range(
        args.start, end, outline_file,
        fill_gaps=args.fill_gaps,
        chapter=args.chapter if args.chapter > 0 else None,
        review_feedback=Path(args.review_feedback) if args.review_feedback else None,
        rescue=args.rescue,
        candidate_file=candidate_file,
    )

    if not ok:
        print("[Outliner] 存在失败批次")
        sys.exit(1)
    print("[Outliner] 全部完成")


if __name__ == "__main__":
    main()
