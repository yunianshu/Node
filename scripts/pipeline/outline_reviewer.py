#!/usr/bin/env python3
"""
Outline Reviewer Agent - 单章大纲审查Agent
负责审查单章大纲质量，确保达到初稿生成标准。
"""

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

import argparse
import json
import os
import re
import time
from pathlib import Path

from core.novel_config import (
    configure_stdio,
    load_config,
    load_origin_materials,
    resolve_project_dir,
)
from core.review_ai_client import ReviewAIError, call_review_ai
from core.workflow_state import (
    load_outline_chapter,
    load_outline_review_status,
    outline_dir,
    outline_review_dir,
)
from core.outline_quality_gate import (
    cast_name_set,
    clean_char_name,
    detect_adjacent_event_repetition,
    detect_beat_runs,
    detect_cast_violations,
    detect_scene_density_issues,
)
from core.json_repair import fix_inner_quotes, fix_truncated_json, parse_score as _parse_score, strip_json_markdown as _strip_json_markdown

configure_stdio()

NOVELS_DIR = None
OUTLINE_REVIEW_DIR = None
LOG_FILE = None
CONFIG = None
ORIGIN_MATERIALS = ""

VALID_STORY_BEATS = {
    "opening_image",
    "theme_stated",
    "setup",
    "catalyst",
    "debate",
    "break_into_two",
    "b_story",
    "fun_and_games",
    "midpoint",
    "bad_guys_close_in",
    "all_is_lost",
    "dark_night",
    "break_into_three",
    "finale",
    "final_image",
    "rising_action",
    "transition",
}


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, OUTLINE_REVIEW_DIR, LOG_FILE, CONFIG, ORIGIN_MATERIALS
    NOVELS_DIR = Path(project_dir).resolve()
    OUTLINE_REVIEW_DIR = outline_review_dir(NOVELS_DIR)
    LOG_FILE = NOVELS_DIR / "logs" / "outline_reviewer.log"
    CONFIG = load_config(NOVELS_DIR)
    review_cfg = CONFIG.get("outline_reviewer", {})
    ORIGIN_MATERIALS = load_origin_materials(
        NOVELS_DIR,
        max_chars=int(review_cfg.get("origin_max_chars", 4000) or 4000),
    )


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def call_mmx(system_prompt: str, user_prompt: str, max_tokens: int = 4096, temperature: float = 0.3) -> str:
    try:
        cfg = CONFIG.get("outline_reviewer", {})
        fallback = CONFIG.get("writer", {})
        return call_review_ai(
            CONFIG,
            NOVELS_DIR,
            "outline_reviewer",
            system_prompt,
            user_prompt,
            max_tokens=int(cfg.get("max_tokens", max_tokens) or max_tokens),
            temperature=float(cfg.get("temperature", temperature)),
            raw_name="outline_reviewer",
            fallback_retries=int(fallback.get("max_retries", 3)),
            fallback_retry_delay=float(fallback.get("retry_delay", 5.0)),
        )
    except ReviewAIError as e:
        log(f"[ERROR] 审查AI调用失败: {e}")
        return ""


def load_json(filepath: Path) -> dict:
    if not filepath.exists():
        return {}
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


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


def _payoff_review_profile(world: dict, outline: dict) -> dict:
    text = _flatten_project_text(world, outline).lower()
    if any(key in text for key in ("悬疑", "案件", "调查", "记者", "证据", "真相", "追踪", "警方", "犯罪", "谜", "都市")):
        return {
            "label": "阅读回报",
            "chain": "期待→压迫/阻碍→线索反转→代价兑现/局势推进",
            "score_desc": "payoff_design是否有完整的期待、阻碍、反转和阶段性兑现？回报是否来自主角判断/行动/证据推进而非巧合？",
            "check": "payoff_design是否包含题材适配的阅读回报链（期待→阻碍/压迫→反转→兑现/推进）？若该字段缺失或空泛，需在weaknesses指出。",
            "design": "阅读回报是否到位：是否有期待感、压迫或阻碍、信息/局势反转、阶段性兑现，且是否触及角色内核",
        }
    return {
        "label": "阅读回报",
        "chain": "期待→阻碍/压制→反转→兑现",
        "score_desc": "payoff_design是否有完整的期待、阻碍/压制、反转和兑现？回报是否来自主角真实发挥而非巧合？",
        "check": "payoff_design是否包含完整阅读回报链（期待→阻碍/压制→反转→兑现）？若该字段缺失或空泛，需在weaknesses指出。",
        "design": "阅读回报是否到位：是否有期待感、阻碍、反转、兑现等要素，且是否触及角色内核",
    }


def _current_chapter_continuity_failures(chapter: int, issues: object) -> list[str]:
    if not isinstance(issues, list):
        return []
    hard_markers = (
        "矛盾", "冲突", "重复", "断裂", "倒退", "重演", "推翻",
        "无法承接", "同一时间", "生死状态", "证据归属",
    )
    previous_markers = ("前章", "上一章", "前序", "此前章节")
    later_markers = ("后章", "下一章", "后续章节", "后续既定")
    failures = []
    for value in issues:
        text = str(value or "").strip()
        if not text or not any(marker in text for marker in hard_markers):
            continue
        referenced = {
            int(match)
            for match in re.findall(r"(?:第\s*|ch(?:apter)?\s*)(\d+)\s*章?", text, flags=re.I)
        }
        if any(number < chapter for number in referenced):
            failures.append(text)
            continue
        if any(marker in text for marker in previous_markers):
            failures.append(text)
            continue
        if referenced or any(marker in text for marker in later_markers):
            continue
        failures.append(text)
    return failures


def _load_repair_requirements(feedback_file: Path | None, chapter: int) -> list[dict]:
    if feedback_file is None or not feedback_file.exists():
        return []
    try:
        payload = load_json(feedback_file)
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    item = payload.get(str(chapter)) if isinstance(payload, dict) else None
    if not isinstance(item, dict) or item.get("gate") != "outline_book_review":
        return []
    repair_items = item.get("repair_items")
    if isinstance(repair_items, list):
        requirements = []
        for index, repair_item in enumerate(repair_items):
            if not isinstance(repair_item, dict):
                continue
            problem = str(repair_item.get("problem", "")).strip()
            acceptance = str(repair_item.get("acceptance", "")).strip()
            if not problem and not acceptance:
                continue
            requirements.append({
                "id": str(repair_item.get("id") or f"R{index + 1}"),
                "problem": problem,
                "acceptance": acceptance,
                "evidence": str(repair_item.get("evidence", "")).strip(),
                "chapters": repair_item.get("chapters", []),
                "category": repair_item.get("category", ""),
            })
        if requirements:
            return requirements[:8]
    analysis = item.get("failure_analysis")
    analysis = analysis if isinstance(analysis, dict) else {}
    reasons = analysis.get("likely_reasons")
    adjustments = analysis.get("adjustments")
    reasons = reasons if isinstance(reasons, list) else []
    adjustments = adjustments if isinstance(adjustments, list) else []
    requirements = []
    for index in range(max(len(reasons), len(adjustments))):
        problem = str(reasons[index] if index < len(reasons) else "").strip()
        acceptance = str(adjustments[index] if index < len(adjustments) else "").strip()
        if not problem and not acceptance:
            continue
        requirements.append({
            "id": f"R{len(requirements) + 1}",
            "problem": problem,
            "acceptance": acceptance,
        })
    return requirements[:8]


def _repair_id_sort_key(value: str) -> tuple[int, str]:
    match = re.search(r"\d+", value)
    return (int(match.group(0)) if match else 9999, value)


def _single_item_list(value) -> list:
    if not isinstance(value, list):
        return []
    return value[:1]


def _normalize_single_action_review(review_data: dict) -> None:
    for field in ("strengths", "weaknesses", "suggestions", "continuity_issues", "edits"):
        review_data[field] = _single_item_list(review_data.get(field))
    primary_issue = ""
    for field in ("weaknesses", "continuity_issues", "suggestions"):
        values = review_data.get(field)
        if isinstance(values, list) and values:
            primary_issue = str(values[0]).strip()
            if primary_issue:
                break
    if primary_issue:
        review_data["primary_issue"] = primary_issue
    if isinstance(review_data.get("suggestions"), list) and review_data["suggestions"]:
        review_data["primary_suggestion"] = str(review_data["suggestions"][0]).strip()


def review_outline(
    chapter_number: int,
    outline_file_override: Path | None = None,
    review_file_override: Path | None = None,
    context_outline_dir: Path | None = None,
    repair_feedback_file: Path | None = None,
    _semantic_attempt: int = 0,
    _semantic_errors: list[str] | None = None,
) -> dict:
    outline_file = outline_file_override or outline_dir(NOVELS_DIR) / f"chapter_{chapter_number:04d}.json"
    review_file = review_file_override or OUTLINE_REVIEW_DIR / f"chapter_{chapter_number:04d}_review.json"

    if not outline_file.exists():
        log(f"[OutlineReviewer] 第{chapter_number}章大纲文件不存在")
        return {"status": "no_file"}

    if review_file.exists():
        try:
            existing = json.loads(review_file.read_text(encoding="utf-8"))
            min_score = float(CONFIG.get("outline_reviewer", {}).get("min_score", 8.5))
            _, status, score, ok = load_outline_review_status(review_file, min_score)
            if ok and repair_feedback_file is None:
                log(f"[OutlineReviewer] 第{chapter_number}章大纲已有达标审查报告（{score}分），跳过")
                return existing
            if ok and repair_feedback_file is not None:
                log(
                    f"[OutlineReviewer] 第{chapter_number}章已有达标审查报告，"
                    "但本次带整本修复反馈，必须重新验收"
                )
            else:
                log(
                    f"[OutlineReviewer] 第{chapter_number}章现有审查未达门槛"
                    f"（status={status}, score={score}, min={min_score:g}），重新审查"
                )
        except Exception as exc:
            pass

    outline = load_json(outline_file)

    # 加载前后章节作为上下文
    def load_context_outline(number: int) -> dict:
        if number <= 0:
            return {}
        if context_outline_dir is None:
            return load_outline_chapter(NOVELS_DIR, number)
        return load_json(context_outline_dir / f"chapter_{number:04d}.json")

    context_window = max(
        1,
        int(CONFIG.get("outline_reviewer", {}).get("context_window", 5) or 5),
    )
    context_outlines = {
        number: load_context_outline(number)
        for number in range(
            max(1, chapter_number - context_window),
            min(int(CONFIG.get("total_chapters", chapter_number)), chapter_number + context_window) + 1,
        )
        if number != chapter_number
    }
    context_outlines = {
        number: item
        for number, item in context_outlines.items()
        if isinstance(item, dict) and item
    }
    prev_outline = context_outlines.get(chapter_number - 1, {})
    next_outline = context_outlines.get(chapter_number + 1, {})

    # 跨章 story_beat 分布检查（注水腰根因）：单章审查看不到连续多章同 beat。
    # 取 N-2..N+2 的 story_beat，找出涉及本章的平推段。
    beat_window = []
    for offset, fallback in ((-2, None), (-1, prev_outline), (0, outline), (1, next_outline), (2, None)):
        nch = chapter_number + offset
        ob = fallback if fallback is not None else load_context_outline(nch)
        beat = ob.get("story_beat") if isinstance(ob, dict) else None
        if beat:
            beat_window.append((nch, beat))
    beat_flat_issues = [
        item
        for item in detect_beat_runs(beat_window)
        if item.get("chapters")
        and chapter_number == max(item.get("chapters", []))
    ]
    adjacent_repetition_issue = detect_adjacent_event_repetition(prev_outline, outline)
    scene_density_issue = detect_scene_density_issues(outline)
    repair_requirements = _load_repair_requirements(repair_feedback_file, chapter_number)

    world = load_json(NOVELS_DIR / "world.json")
    characters = load_json(NOVELS_DIR / "characters.json")

    # 闭环角色校验（人物漂移根因）：出场角色必须前期锁定在角色档案或世界观。
    cast_violations = detect_cast_violations(
        outline.get("characters_involved", []), cast_name_set({"characters": characters, "world": world})
    )

    book_title = world.get("title", "本小说")
    world_desc = world.get("world_description", "")[:300]
    themes = world.get("themes", [])
    power_system = world.get("power_system", {})
    power_name = power_system.get("name", "")
    power_desc = power_system.get("description", "")[:200]

    genre_hints = []
    if themes:
        genre_hints.append(f"核心主题：{'; '.join(themes[:3])}")
    if power_name:
        genre_hints.append(f"力量体系：{power_name}（{power_desc}）")
    if world_desc:
        genre_hints.append(f"世界观：{world_desc}")
    genre_text = "\n".join(genre_hints) if genre_hints else "请根据世界观和角色设定判断题材类型。"
    payoff_profile = _payoff_review_profile(world, outline)

    min_score = float(CONFIG.get("outline_reviewer", {}).get("min_score", 8.5))

    retry_contract = ""
    if _semantic_errors:
        retry_contract = (
            "\n## 上次输出无效，本次必须纠正\n"
            "上次缺陷：" + "；".join(_semantic_errors) + "\n"
            "本次必须返回完整 JSON，尤其不得省略 strengths、weaknesses、suggestions、"
            "continuity_issues、summary、edits。未通过时只给出1条最关键edit。\n"
        )

    system = f"""你是一位拥有20年经验的资深网络小说总编，同时也是一位苛刻的"神作猎手"。
你的任务是判断单章大纲能否稳定支撑高质量正文，并找出当前最阻止它达标的唯一关键原因。
本书是《{book_title}》。
{genre_text}
{retry_contract}

## 【高质量单章大纲评分标准】
- 10分：大纲足以支撑传世级神作。悬念密集，情感冲击强烈，信息新鲜，章末钩子让人失眠，Writer据此必能写出让人欲罢不能的章节。
- 9-9.9分：优秀大纲。悬念设计到位，情绪曲线清晰，反套路，有新鲜感，足以支撑9分正文。
- 8-8.9分：良好到优秀。结构完整、有明确冲突和钩子；达到{min_score}分且六项设计门通过即可进入后续全书审查。
- 7-7.9分：平庸大纲。有明显套路、重复、动机牵强或缺乏钩子的问题。
- 低于7分：不合格，存在严重设计缺陷。

【你的审查哲学】
- 不要给"辛苦分"。字段全不等于设计好。
- 不要给字段完整性辛苦分，但也不要把每章都强行要求成卷终高潮。
- 重点关注：这个大纲能否让 Writer 写出一章让人"读完立刻想打开下一章"的内容？
- 如果你给不出9分以上，必须在 weaknesses 中只写一条"距离9分的最大差距"。

输出必须是合法的紧凑JSON，不要使用Markdown代码块，不要输出JSON之外的任何文字。
审查意见要短而具体，整份JSON尽量控制在3500个中文字符以内；不得为压缩长度省略任何必填字段。"""

    context_parts = []
    for number, item in sorted(context_outlines.items()):
        relation = "前序事实锚点" if number < chapter_number else "后续既定接口"
        context_parts.append(f"""## {relation}（第{number}章）
标题：{item.get('title', 'N/A')}
时间推进：{item.get('time_progression', 'N/A')}
地点：{item.get('location', 'N/A')}
涉及角色：{item.get('characters_involved', [])}
摘要：{str(item.get('summary', 'N/A'))[:360]}
关键事件：{json.dumps(item.get('key_events', []), ensure_ascii=False)[:650]}
伏笔：{str(item.get('foreshadowing', ''))[:240]}
章末状态：{str(item.get('chapter_hook', ''))[:240]}""")
    context_text = "\n\n".join(context_parts)
    volume_outline = load_json(NOVELS_DIR / "volume_outline.json")
    repair_section = ""
    repair_gate_schema = ""
    if repair_requirements:
        repair_section = f"""\n## 本次整本审查修复验收单（硬门槛）
{json.dumps(repair_requirements, ensure_ascii=False, indent=2)}

必须逐项检查 R1..R{len(repair_requirements)} 是否已在待审大纲中真正落地。仅改换地点、措辞或结果，
却没有补齐验收单要求的原因、过程、人物状态或时间过渡，必须判定该项未闭环。\n"""
        repair_gate_schema = """,
    "repair_feedback_closed": {"passed": true, "evidence": "R1..Rn逐项闭环的简短证据"}"""

    prompt = f"""请审查以下第{chapter_number}章的单章大纲。虽然输出是一份单章报告，但必须以给出的前后{context_window}章为事实链做跨章一致性检查。

## 世界观与角色设定
世界观：{json.dumps(world, ensure_ascii=False, indent=2)[:1400]}
角色：{json.dumps(characters, ensure_ascii=False, indent=2)[:1800]}

## 分卷规划
{json.dumps(volume_outline, ensure_ascii=False, indent=2)[:1800]}

## origin/ 原始参考素材
{ORIGIN_MATERIALS or "（无）"}

{context_text}
{repair_section}

## 待审查大纲（第{chapter_number}章）
{json.dumps(outline, ensure_ascii=False, indent=2)}

请只输出以下JSON格式的审查报告。所有数组都只能有1条，每条不超过80字；edits只能有1条：
{{
  "chapter_number": {chapter_number},
  "overall_score": "请给出0-10的客观评分。9分意味着Writer据此必能写出让人欲罢不能的章节。不要给辛苦分",
  "verdict": "通过/需修改/需重写。注意：如果 overall_score >= {min_score}，verdict 必须写'通过'；只有低于{min_score}分才写'需重写'或'需修改'",
  "primary_issue": "当前最关键的唯一问题，80字以内",
  "primary_suggestion": "针对primary_issue的唯一修改建议，80字以内",
  "weaknesses": ["唯一不足，80字以内。如果给分低于9分，必须写出距离9分的最大差距"],
  "suggestions": ["唯一具体修改建议，80字以内"],
  "continuity_issues": ["唯一衔接问题；没有则空数组"],
  "repair_feedback_checks": [{{"id": "R1", "passed": true, "evidence": "本章中实际完成修复的具体事件"}}],
  "summary": "总体评价，80字以内。如果评分低于9分，用一句话回答：本章大纲最致命的短板是什么？",
  "edits": [
    {{
      "field": "summary",
      "action": "replace",
      "value": "修改后的具体剧情摘要"
    }},
    {{
      "field": "key_events",
      "action": "replace_index",
      "index": 0,
      "value": "修改后的关键事件"
    }},
    {{
      "field": "chapter_hook",
      "action": "replace",
      "value": "修改后的章末钩子"
    }},
    {{
      "field": "human_anchor",
      "action": "replace",
      "value": "修改后的烟火气锚点：生活压力、关系牵挂、潜台词或生活物件"
    }}
  ],
  "strengths": ["优点1，80字以内"],
  "design_gates": {{
    "core_desire": {{"passed": true, "evidence": "主角本章具体想得到或保护什么，60字以内"}},
    "irreversible_choice": {{"passed": true, "evidence": "本章不可撤销的选择、损失或暴露，60字以内"}},
    "midpoint_reversal": {{"passed": true, "evidence": "中段如何改变原行动方案，60字以内"}},
    "human_warmth": {{"passed": true, "evidence": "本章具体生活压力、关系牵挂或潜台词如何参与剧情，60字以内"}},
    "content_richness": {{"passed": true, "evidence": "本章除外部事件外，至少哪一层关系/生活/秘密/规则内容被推进，60字以内"}},
    "strong_hook": {{"passed": true, "evidence": "章末正在发生的具体危机或反转，60字以内"}},
    "scene_design": {{"passed": true, "evidence": "scenes 数组3-5个、五字段齐全、sensory_anchor不重复且取自分类库、human_anchor落进至少一个场景，60字以内"}}{repair_gate_schema}
  }}
}}

【9分神作大纲核心审查清单】
请在给出评分前按以下核心项快速自检。六项 design_gates 是硬门槛；其余项目用于综合评分。若存在多个问题，只输出最影响通过的一项：
1. chapter_hook字段是否明确写出了一个让人心跳加速的强力钩子（危机升级/信息反转/情感爆点）？
2. chapter_hook是否禁止了平静收尾、总结现状、铺垫过渡？
3. emotional_arc是否描述了清晰的情绪起伏（如压抑→紧张→希望→绝望），而非全程单一情绪？
4. tension_points是否至少包含3个有效的让人无法停止阅读的关键时刻？
5. 本章是否带来有效的新进展（新信息、新关系变化、新风险或旧伏笔回收），不要求每章强行新增人物或地点？
6. 是否存在套路化设计（标准战斗流程、标准解谜流程、配角当解说员）？
7. 本章回报是否触及角色核心欲望、恐惧或明确阶段目标，而非只有表层事件堆叠？
8. human_anchor 是否具体写出了生活压力、关系牵挂、潜台词或生活物件，且这些内容会参与剧情推进？
9. 本章是否有具体生活压力、关系牵挂、旧情分、亏欠、照料、面子或尊严参与剧情推进？
10. content_layers 是否至少包含两层，并且不是复述同一个外部事件？是否明确推进了关系、生活压力、秘密代价或世界规则现场化？
11. 是否有打卡地图风险：地点只为拿道具/升境界/过副本服务，缺少风土人情、制度、生计和文化差异？
12. 是否有抽象概念堆叠风险：道意、本源、法则、共鸣、境界等概念没有身体/器物/环境/关系后果？
13. chapter_hook 是否避开“身后……身前……”“一步又一步”“未知的路”等机械对称收尾？
14. 是否避免散文诗式复沓：不是用十几段同一物象聚焦/同一作者判断替代事件推进？
15. 是否设计了一个可复述的不可逆动作，而不是只用氛围说明人物“往前挪”？
16. 若是群像/多线章节，是否设计了“从重奏到独步”：群像铺压或递火，最终收束成主角自己的独立选择？
17. 关键证据/信息/信物的传递链是否清楚：起点、转交、接收者理解方式、风险、最终用途是否都能复述？
18. 反派是否有层次：面对破绽时会用规矩、程序、威胁、交易、嫁祸或冷处理稳住场面，而不是只变脸或发怒？
19. 跨地点/跨时辰/并行动作是否有明确转场桥，避免Writer写成突兀跳切？
20. 反派标志物或贯穿意象是否被设计成反照反派内层、旧事、软肋或破绽，而不是只做道具？
21. 旧证据逼到反派时是否设计了一个半拍身体裂隙，再接规矩/程序/威胁等冷处理？
22. 章末关键道具/证据是否在前文预埋了制作、拓印、藏匿、转手或瞥见动作？
23. 墨印/拓片/副本/录音备份等复制型证据是否提前设计了复制动作？
24. 父辈/亲缘/旧痕线索是否在 chapter_hook 或 payoff_design 中有章末微回扣？
25. chapter_hook 是否包含关键动作后的1-2个现场微反应，形成余韵而不是动作一落就截断？
26. 是否设计了至少一句有潜台词的对白或欲言又止的瞬间，而不是把动机和情绪全说透？
27. 如果Writer严格按这个大纲写，能否产出一章让人读完立刻想打开下一章的内容？
28. story_beat是否名实相符？标注的结构功能（如catalyst/midpoint/all_is_lost/finale）是否在剧情中真正兑现？与全卷节奏曲线是否衔接？
29. chapter_goal是否是本章可推进的具体目标（非全书口号）？失败后果是否触及核心利益？
30. {payoff_profile['check']}
31. 与上下文各章相比，是否重复了同一核心场景、追逐、对峙、取证、营救或直播动作链？
32. 人物身份、阵营、生死、伤势、被捕/获救状态是否与前后章一致？不可逆事件是否只发生一次？
33. 时间是否单调推进？跨日、等待、移动和地点切换是否有明确过渡？
34. 本章新埋伏笔在后续接口中是否有承接；前章已回收信息是否被错误地再次当成未知？
35. 问题归属遵循“最早事实为锚点”：若冲突由后章推翻前章事实造成，只在 continuity_issues 中指出应修改的后章，不得因此压低本章分数或判本章不通过。
36. 【人物辨识度】大纲是否为每个有戏份角色设计了体现性格的动作/抉择/台词方向？主角的关键抉择是否"只有他会这么干"？是否设计了至少一处性格反差作为人物弧线一步？不同角色是否预留了可分辨的说话方式差异？
37. 【因果链】本章转折/破局是否都可追溯到前文原因（人物选择/信息/物件/伏笔）？是否禁止"恰好/凑巧"破局？主角回报是否来自自身判断/能力/资源/布局而非天降？信息传递是否闭环？时间/伤势/资源是否与前章一致承接？

要求：
1. 评分要客观可复现。9分代表单章设计突出；达到{min_score}分且六项设计门通过，代表足以进入全书层级审查。
2. 重点审查：人物辨识度、因果链、悬念密度、钩子强度、情绪曲线、烟火气与人情味、内容层次、不可逆动作、地图真实感、概念落地、信息新鲜度、反套路程度、结构功能合理性、目标赌注、{payoff_profile['label']}。这些维度比字段完整性更重要。
3. 剧情是否有真正的冲突和转折，而非流水账
4. {payoff_profile['design']}
5. 人物动机是否合理，是否与角色设定一致
6. 与前后章的衔接是否自然，伏笔是否呼应
7. 场景使用是否有效；单章允许集中在一个地点，但不能重复做同样的事或缺少状态变化
8. 力量体系是否自洽，实力成长是否有合理铺垫
9. 信息是否足够详细，Writer能否据此写出{outline.get('word_count_target', 5000)}字高质量正文
10. 如果origin/中存在素材，必须检查大纲是否参考并遵守原始素材；与素材冲突需列入weaknesses或continuity_issues
11. 如低于{min_score}分必须标记为需重写
12. 必须输出合法JSON，不要Markdown，不要长篇解释
13. 任一由本章引入或应由本章承担修复责任的 critical 跨章矛盾（重复不可逆事件、身份/生死冲突、时间倒退、相邻章核心动作链重复）都必须判定为不通过；若责任在更晚章节，本章保留为事实锚点并正常评分
14. **必须输出 edits 数组**：如果 verdict 不是"通过"，只能给出1条最关键字段级 edit（field/action/value），让 Outliner 定点修改 JSON 而不是整章重生成。action 可选 replace/append/replace_index/delete_index。小问题优先改 chapter_hook、key_events、human_anchor、summary 等字段。
15. 如果 edit 修改 story_beat，value 只能取以下枚举之一：opening_image/theme_stated/setup/catalyst/debate/break_into_two/b_story/fun_and_games/midpoint/bad_guys_close_in/all_is_lost/dark_night/break_into_three/finale/final_image/rising_action/transition。不得发明 impossible_choice 等新标签。"""

    log(f"[OutlineReviewer] 正在审查第{chapter_number}章大纲...")
    start_time = time.time()
    content = call_mmx(system, prompt, max_tokens=4096, temperature=0.3)
    elapsed = time.time() - start_time
    log(f"[OutlineReviewer] 第{chapter_number}章大纲审查 API 调用耗时 {elapsed:.1f}s")

    if not content:
        log(f"[OutlineReviewer] 第{chapter_number}章大纲审查失败")
        review_data = {
            "chapter_number": chapter_number,
            "status": "failed",
        }
        review_file.parent.mkdir(parents=True, exist_ok=True)
        with open(review_file, "w", encoding="utf-8") as f:
            json.dump(review_data, f, ensure_ascii=False, indent=2)
        return review_data


    required_gates = (
        "core_desire",
        "irreversible_choice",
        "midpoint_reversal",
        "human_warmth",
        "content_richness",
        "strong_hook",
        "scene_design",
    )
    if repair_requirements:
        required_gates += ("repair_feedback_closed",)

    def _extract_object_after_key(text: str, key: str) -> str | None:
        match = re.search(rf'"{re.escape(key)}"\s*:', text)
        if not match:
            return None
        start = text.find("{", match.end())
        if start < 0:
            return None
        stack: list[str] = []
        in_string = False
        escape = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                stack.append("}")
            elif ch == "}":
                if not stack:
                    return None
                stack.pop()
                if not stack:
                    return text[start:idx + 1]
        return None

    def _extract_scores_from_partial(text: str) -> dict:
        scores_obj = _extract_object_after_key(text, "scores")
        if scores_obj:
            for variant in (scores_obj, fix_inner_quotes(scores_obj), fix_truncated_json(scores_obj)):
                try:
                    scores = json.loads(variant)
                except Exception as exc:
                    continue
                if isinstance(scores, dict):
                    return scores
        match = re.search(r'"scores"\s*:\s*\{(?P<body>.*?)(?:\}\s*,\s*"design_gates"|$)', text, re.S)
        if not match:
            return {}
        body = match.group("body")
        scores: dict[str, float | str] = {}
        for key, val in re.findall(r'"([^"]+)"\s*:\s*("?[-+]?\d+(?:\.\d+)?"?)', body):
            scores[key] = _parse_score(val.strip('"'))
        return scores

    def _partial_review_from_truncated(text: str) -> dict | None:
        cleaned = _strip_json_markdown(text)
        start = cleaned.find("{")
        if start >= 0:
            cleaned = cleaned[start:]
        if '"overall_score"' not in cleaned and '"scores"' not in cleaned:
            return None

        review: dict = {
            "chapter_number": chapter_number,
            "parse_recovered": "truncated_response",
            "suggestions": ["审查响应被截断，已保留可解析评分；缺失设计门证据需重新确认。"],
            "continuity_issues": [],
            "edits": [],
        }
        chapter_match = re.search(r'"chapter_number"\s*:\s*(\d+)', cleaned)
        if chapter_match:
            review["chapter_number"] = int(chapter_match.group(1))

        score_match = re.search(r'"overall_score"\s*:\s*("?[-+]?\d+(?:\.\d+)?"?)', cleaned)
        if score_match:
            review["overall_score"] = _parse_score(score_match.group(1).strip('"'))

        verdict_match = re.search(r'"verdict"\s*:\s*"([^"]+)"', cleaned)
        if verdict_match:
            review["verdict"] = verdict_match.group(1)

        scores = _extract_scores_from_partial(cleaned)
        if scores:
            review["scores"] = scores

        design_gates: dict[str, dict] = {}
        for gate_name in required_gates:
            gate_obj = _extract_object_after_key(cleaned, gate_name)
            if not gate_obj:
                continue
            for variant in (gate_obj, fix_inner_quotes(gate_obj)):
                try:
                    gate_data = json.loads(variant)
                except Exception as exc:
                    continue
                if isinstance(gate_data, dict):
                    design_gates[gate_name] = gate_data
                    break
        if design_gates:
            review["design_gates"] = design_gates
        if "overall_score" not in review and not scores:
            return None
        return review

    def _json_candidates(text: str) -> list[str]:
        cleaned = _strip_json_markdown(text)
        candidates = [cleaned]
        start = cleaned.find("{")
        if start >= 0:
            candidates.append(cleaned[start:])
            end = cleaned.rfind("}")
            if end > start:
                candidates.append(cleaned[start:end + 1])
        unique: list[str] = []
        for candidate in candidates:
            if candidate and candidate not in unique:
                unique.append(candidate)
        return unique

    def _loads_json_object(text: str) -> dict:
        errors: list[str] = []
        for candidate in _json_candidates(text):
            variants = [
                candidate,
                fix_inner_quotes(candidate),
                fix_truncated_json(candidate),
                fix_inner_quotes(fix_truncated_json(candidate)),
            ]
            for variant in variants:
                try:
                    data = json.loads(variant)
                except Exception as exc:
                    errors.append(str(exc))
                    try:
                        start = variant.find("{")
                        if start >= 0:
                            data, _ = json.JSONDecoder().raw_decode(variant[start:])
                        else:
                            continue
                    except Exception as raw_exc:
                        errors.append(str(raw_exc))
                        continue
                if isinstance(data, dict):
                    return data
                errors.append("review response is not a JSON object")
        partial = _partial_review_from_truncated(text)
        if partial:
            return partial
        raise ValueError(errors[-1] if errors else "response contains no JSON object")

    try:
        review_data = _loads_json_object(content)
        review_data["status"] = "completed"
        # 将 overall_score 统一转为 float
        review_data["overall_score"] = _parse_score(review_data.get("overall_score"))
        # 将 scores 子项也转为 float
        scores = review_data.get("scores")
        if isinstance(scores, dict):
            for k, v in list(scores.items()):
                scores[k] = _parse_score(v)
        _normalize_single_action_review(review_data)
        design_gates = review_data.get("design_gates")
        gate_results = []
        if isinstance(design_gates, dict):
            normalized_gates = {
                str(key).replace(" ", "").replace("-", "_"): value
                for key, value in design_gates.items()
            }
            gate_aliases = {
                "core_design": "core_desire",
                "core_goal": "core_desire",
                "irreversible_decision": "irreversible_choice",
                "midpoint": "midpoint_reversal",
                "humanity": "human_warmth",
                "human_touch": "human_warmth",
                "human_warmth_gate": "human_warmth",
                "strong_chapter_hook": "strong_hook",
            }
            for alias, canonical in gate_aliases.items():
                if canonical not in normalized_gates and alias in normalized_gates:
                    normalized_gates[canonical] = normalized_gates[alias]
            for gate_name in required_gates:
                gate = normalized_gates.get(gate_name)
                passed = isinstance(gate, dict) and gate.get("passed") is True
                evidence = gate.get("evidence", "") if isinstance(gate, dict) else ""
                gate_results.append(passed and isinstance(evidence, str) and bool(evidence.strip()))
            review_data["design_gates"] = normalized_gates
        design_gate_passed = len(gate_results) == len(required_gates) and all(gate_results)
        review_data["design_gate_passed"] = design_gate_passed
        if not design_gate_passed:
            score = review_data.get("overall_score")
            if isinstance(score, (int, float)) and score >= 9.0:
                review_data["overall_score"] = 8.8
            review_data["verdict"] = "需修改"

        def _ensure_review_list(field: str) -> list:
            value = review_data.get(field)
            if not isinstance(value, list):
                value = []
                review_data[field] = value
            return value

        repair_feedback_closed = True
        repair_feedback_failed_ids: list[str] = []
        repair_feedback_contract_errors: list[str] = []
        if repair_requirements:
            required_repair_ids = {
                str(item.get("id", "")).strip()
                for item in repair_requirements
                if str(item.get("id", "")).strip()
            }
            repair_checks = review_data.get("repair_feedback_checks")
            if not isinstance(repair_checks, list):
                repair_checks = []
                repair_feedback_contract_errors.append("repair_feedback_checks 缺失或不是数组")
            review_data["repair_feedback_checks"] = repair_checks
            passed_repair_ids: set[str] = set()
            seen_repair_ids: set[str] = set()
            for check in repair_checks:
                if not isinstance(check, dict):
                    continue
                check_id = str(check.get("id", "")).strip()
                if check_id:
                    seen_repair_ids.add(check_id)
                evidence = str(check.get("evidence", "")).strip()
                if check_id in required_repair_ids and check.get("passed") is True and evidence:
                    passed_repair_ids.add(check_id)
            missing_check_ids = required_repair_ids - seen_repair_ids
            if missing_check_ids:
                repair_feedback_contract_errors.append(
                    "repair_feedback_checks 未覆盖整本反馈项: "
                    + "、".join(sorted(missing_check_ids, key=_repair_id_sort_key))
                )
            repair_feedback_failed_ids = sorted(
                required_repair_ids - passed_repair_ids,
                key=_repair_id_sort_key,
            )
            normalized_design_gates = review_data.get("design_gates")
            repair_gate = (
                normalized_design_gates.get("repair_feedback_closed")
                if isinstance(normalized_design_gates, dict)
                else None
            )
            repair_gate_ok = (
                isinstance(repair_gate, dict)
                and repair_gate.get("passed") is True
                and bool(str(repair_gate.get("evidence", "")).strip())
            )
            repair_feedback_closed = not repair_feedback_failed_ids and repair_gate_ok
            review_data["repair_feedback_requirements"] = repair_requirements
            review_data["repair_feedback_closed"] = repair_feedback_closed
            review_data["repair_feedback_failed_ids"] = repair_feedback_failed_ids
            review_data["repair_feedback_gate_passed"] = repair_gate_ok
            if not repair_feedback_closed:
                failed_text = "、".join(repair_feedback_failed_ids) or "repair_feedback_closed"
                evidence = f"整本大纲反馈未逐项闭环：{failed_text}"
                _ensure_review_list("continuity_issues")[:] = [evidence]
                _ensure_review_list("weaknesses")[:] = [evidence]
                _ensure_review_list("suggestions")[:] = [
                    "按整本反馈验收单补齐原因、过程、人物状态和时间过渡；未闭环前不得进入正文。"
                ]
                edits = _ensure_review_list("edits")
                if isinstance(edits, list) and not edits:
                    edits.append({
                        "field": "summary",
                        "action": "replace",
                        "value": (
                            str(outline.get("summary", "")).strip()
                            + "（需按整本大纲反馈补齐跨章过渡、角色状态与因果闭环。）"
                        )[:900],
                    })
                score = review_data.get("overall_score")
                if isinstance(score, (int, float)):
                    review_data["overall_score"] = round(min(score, min_score - 0.1), 2)
                review_data["verdict"] = "需修改"
        # 跨章节奏守卫：本章若身处连续同 beat 的注水腰段，强制需修改并压分，
        # 让 Outliner 带反馈重生成本章、换用不同转折 beat。
        if beat_flat_issues:
            fi = beat_flat_issues[0]
            review_data.setdefault("continuity_issues", [])[:] = [fi["evidence"]]
            review_data.setdefault("weaknesses", [])[:] = [fi["evidence"]]
            review_data.setdefault("suggestions", [])[:] = [fi["suggestion"]]
            current_beat = str(outline.get("story_beat", "")).strip().lower()
            replacement_beat = {
                "setup": "catalyst",
                "catalyst": "break_into_two",
                "debate": "break_into_two",
                "break_into_two": "b_story",
                "b_story": "fun_and_games",
                "fun_and_games": "midpoint",
                "rising_action": "midpoint",
                "transition": "rising_action",
                "midpoint": "bad_guys_close_in",
                "bad_guys_close_in": "all_is_lost",
                "all_is_lost": "dark_night",
                "dark_night": "break_into_three",
                "break_into_three": "finale",
                "finale": "final_image",
            }.get(current_beat, "rising_action")
            edits = review_data.setdefault("edits", [])
            if isinstance(edits, list) and not any(
                isinstance(edit, dict) and edit.get("field") == "story_beat"
                for edit in edits
            ):
                edits[:] = [{
                    "field": "story_beat",
                    "action": "replace",
                    "value": replacement_beat,
                }]
            score = review_data.get("overall_score")
            if isinstance(score, (int, float)):
                review_data["overall_score"] = round(min(score, min_score - 0.1), 2)
            review_data["verdict"] = "需修改"
            review_data["beat_distribution_issue"] = fi
        if scene_density_issue:
            scene_evi = "；".join(scene_density_issue["issues"])
            review_data.setdefault("continuity_issues", [])[:] = [scene_evi]
            review_data.setdefault("weaknesses", [])[:] = [scene_evi]
            review_data.setdefault("suggestions", [])[:] = [scene_density_issue["suggestion"]]
            scene_edits = review_data.setdefault("edits", [])
            if isinstance(scene_edits, list) and not any(
                isinstance(edit, dict) and edit.get("field") == "scenes"
                for edit in scene_edits
            ):
                scene_edits[:] = [{
                    "field": "scenes",
                    "action": "replace",
                    "value": "3-5个场景对象，每个含position/objective/conflict/sensory_anchor/subtext_beat/exit_hook",
                }]
            score = review_data.get("overall_score")
            if isinstance(score, (int, float)):
                review_data["overall_score"] = round(min(score, min_score - 0.1), 2)
            review_data["verdict"] = "需修改"
            review_data["scene_density_issue"] = scene_density_issue
        if adjacent_repetition_issue:
            evidence = adjacent_repetition_issue["evidence"]
            suggestion = adjacent_repetition_issue["suggestion"]
            review_data.setdefault("continuity_issues", [])[:] = [evidence]
            review_data.setdefault("weaknesses", [])[:] = [evidence]
            review_data.setdefault("suggestions", [])[:] = [suggestion]
            edits = review_data.setdefault("edits", [])
            has_key_event_edit = isinstance(edits, list) and any(
                isinstance(edit, dict) and edit.get("field") == "key_events"
                for edit in edits
            )
            if isinstance(edits, list) and not has_key_event_edit:
                for match in sorted(
                    adjacent_repetition_issue["matches"],
                    key=lambda item: item["current_index"],
                    reverse=True,
                ):
                    edits[:] = [{
                        "field": "key_events",
                        "action": "delete_index",
                        "index": match["current_index"],
                    }]
                    break
            score = review_data.get("overall_score")
            if isinstance(score, (int, float)):
                review_data["overall_score"] = round(min(score, min_score - 0.1), 2)
            review_data["verdict"] = "需修改"
            review_data["adjacent_event_repetition"] = adjacent_repetition_issue
        continuity_hard_failures = _current_chapter_continuity_failures(
            chapter_number,
            review_data.get("continuity_issues"),
        )
        if continuity_hard_failures:
            score = review_data.get("overall_score")
            if isinstance(score, (int, float)):
                review_data["overall_score"] = round(min(score, min_score - 0.1), 2)
            review_data["verdict"] = "需修改"
            review_data["continuity_hard_failures"] = continuity_hard_failures[:4]
        # 闭环角色守卫：出场角色未在前期 roster 锁定（带括号注释/临时生造/未登记别名），
        # 强制需修改，让 Outliner 改用已登记角色或机构名，避免临时人名漂移。
        if cast_violations["polluted"] or cast_violations["unregistered"]:
            parts = []
            if cast_violations["polluted"]:
                parts.append("非规范写法(去括号注释/'类别：'前缀)：" + "、".join(cast_violations["polluted"]))
            if cast_violations["unregistered"]:
                parts.append("未登记角色/群体：" + "、".join(cast_violations["unregistered"]))
            evi = "；".join(parts)
            sug = ("characters_involved 必须只含 characters.json 或 world.json 已登记角色的规范名"
                   "（无括号注释/整句描述）；若未登记，改用已登记角色或只作为背景机构写入剧情，"
                   "不要把临时人名放进 characters_involved。")
            review_data.setdefault("continuity_issues", [])[:] = ["人物未闭环锁定：" + evi]
            review_data.setdefault("suggestions", [])[:] = [sug]
            valid_names = cast_name_set({"characters": characters, "world": world})
            normalized_cast = []
            for raw_name in outline.get("characters_involved", []):
                name = clean_char_name(raw_name)
                if name and name in valid_names and name not in normalized_cast:
                    normalized_cast.append(name)
            if normalized_cast:
                edits = review_data.setdefault("edits", [])
                if isinstance(edits, list) and not any(
                    isinstance(edit, dict) and edit.get("field") == "characters_involved"
                    for edit in edits
                ):
                    edits[:] = [{
                        "field": "characters_involved",
                        "action": "replace",
                        "value": normalized_cast,
                    }]
            score = review_data.get("overall_score")
            if isinstance(score, (int, float)):
                review_data["overall_score"] = round(min(score, min_score - 0.1), 2)
            review_data["verdict"] = "需修改"
            review_data["cast_violation"] = cast_violations

        score = review_data.get("overall_score")
        hard_gate_ok = (
            design_gate_passed
            and repair_feedback_closed
            and not beat_flat_issues
            and not adjacent_repetition_issue
            and not scene_density_issue
            and not continuity_hard_failures
            and not (cast_violations["polluted"] or cast_violations["unregistered"])
        )
        if isinstance(score, (int, float)) and score >= min_score and hard_gate_ok:
            review_data["verdict"] = "通过"
        elif isinstance(score, (int, float)) and score < min_score and review_data.get("verdict") == "通过":
            review_data["verdict"] = "需修改"

        if not isinstance(review_data.get("strengths"), list):
            review_data["strengths"] = []
        if not isinstance(review_data.get("continuity_issues"), list):
            review_data["continuity_issues"] = []
        if not isinstance(review_data.get("weaknesses"), list):
            derived = review_data["continuity_issues"][:3]
            if not derived and isinstance(review_data.get("suggestions"), list):
                derived = [str(item) for item in review_data["suggestions"][:3]]
            review_data["weaknesses"] = derived
        if not isinstance(review_data.get("suggestions"), list):
            review_data["suggestions"] = [
                f"修复：{item}" for item in review_data["weaknesses"][:3]
            ]
        if not isinstance(review_data.get("edits"), list):
            review_data["edits"] = []
        if not isinstance(review_data.get("summary"), str) or not review_data.get("summary", "").strip():
            summary_sources = review_data["weaknesses"] or review_data["suggestions"]
            review_data["summary"] = "；".join(str(item) for item in summary_sources[:2])[:300]
        _normalize_single_action_review(review_data)

        contract_errors: list[str] = []
        contract_errors.extend(repair_feedback_contract_errors)
        expected_lists = (
            "strengths",
            "weaknesses",
            "suggestions",
            "continuity_issues",
            "edits",
        )
        if review_data.get("chapter_number") != chapter_number:
            contract_errors.append("chapter_number 缺失或不匹配")
        if not isinstance(review_data.get("overall_score"), (int, float)):
            contract_errors.append("overall_score 缺失或不是数字")
        if review_data.get("verdict") not in {"通过", "需修改", "需重写"}:
            contract_errors.append("verdict 缺失或非法")
        if not isinstance(review_data.get("scores"), dict):
            review_data["scores"] = {}
        for field in expected_lists:
            if not isinstance(review_data.get(field), list):
                contract_errors.append(f"{field} 缺失或不是数组")
        if not isinstance(review_data.get("summary"), str) or not review_data["summary"].strip():
            contract_errors.append("summary 缺失")

        score = review_data.get("overall_score")
        if isinstance(score, (int, float)) and score < 9.0:
            weaknesses = review_data.get("weaknesses")
            if not isinstance(weaknesses, list) or not weaknesses:
                contract_errors.append("低于9分但未说明具体 weaknesses")
        if review_data.get("verdict") != "通过":
            suggestions = review_data.get("suggestions")
            edits = review_data.get("edits")
            if not isinstance(suggestions, list) or not suggestions:
                contract_errors.append("未通过但没有可执行 suggestions")
            if not isinstance(edits, list) or not edits:
                contract_errors.append("未通过但没有字段级 edits")
        edits = review_data.get("edits")
        if isinstance(edits, list):
            for index, edit in enumerate(edits):
                if not isinstance(edit, dict):
                    contract_errors.append(f"edits[{index}] 不是对象")
                    continue
                action = edit.get("action")
                if action not in {"replace", "append", "replace_index", "delete_index"}:
                    contract_errors.append(f"edits[{index}] action 非法")
                if edit.get("field") == "story_beat":
                    beat = str(edit.get("value", "")).strip().lower()
                    if action != "replace" or beat not in VALID_STORY_BEATS:
                        contract_errors.append(
                            f"edits[{index}] story_beat 必须 replace 为合法枚举值"
                        )

        if contract_errors:
            semantic_retries = max(
                0,
                int(CONFIG.get("outline_reviewer", {}).get("semantic_retries", 1) or 0),
            )
            if _semantic_attempt < semantic_retries:
                log(
                    f"[OutlineReviewer] 第{chapter_number}章审核报告契约不完整，"
                    f"原候选重审 {_semantic_attempt + 1}/{semantic_retries}: "
                    + "；".join(contract_errors)
                )
                return review_outline(
                    chapter_number,
                    outline_file_override,
                    review_file_override,
                    context_outline_dir,
                    repair_feedback_file,
                    _semantic_attempt=_semantic_attempt + 1,
                    _semantic_errors=contract_errors,
                )
            review_data["reported_overall_score"] = review_data.get("overall_score")
            review_data["overall_score"] = None
            review_data["verdict"] = "需修改"
            review_data["status"] = "invalid_review"
            review_data["review_contract_errors"] = contract_errors
    except Exception as e:
        log(f"[OutlineReviewer] JSON解析失败: {e}")
        semantic_retries = max(
            0,
            int(CONFIG.get("outline_reviewer", {}).get("semantic_retries", 1) or 0),
        )
        if _semantic_attempt < semantic_retries:
            log(
                f"[OutlineReviewer] 第{chapter_number}章审核响应解析失败，"
                f"原候选重审 {_semantic_attempt + 1}/{semantic_retries}"
            )
            return review_outline(
                chapter_number,
                outline_file_override,
                review_file_override,
                context_outline_dir,
                repair_feedback_file,
                _semantic_attempt=_semantic_attempt + 1,
                _semantic_errors=["响应不是可解析的完整 JSON 对象"],
            )
        review_data = {
            "chapter_number": chapter_number,
            "status": "parse_error",
            "raw_response": content,
        }

    review_file.parent.mkdir(parents=True, exist_ok=True)
    with open(review_file, "w", encoding="utf-8") as f:
        json.dump(review_data, f, ensure_ascii=False, indent=2)

    overall = review_data.get("overall_score", "N/A")
    verdict = review_data.get("verdict", "N/A")
    edits = review_data.get("edits", [])
    edit_count = len(edits) if isinstance(edits, list) else 0
    log(f"[OutlineReviewer] 第{chapter_number}章大纲审查完成，评分: {overall}，verdict: {verdict}， edits: {edit_count}")
    return review_data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-p", type=str,
                        default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录")
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=10, help="结束章节")
    parser.add_argument("--chapter", type=int, default=0, help="只审查某一章")
    parser.add_argument("--outline-file", type=str, default="", help="候选大纲文件；启用后审查该文件而非正式大纲")
    parser.add_argument("--review-file", type=str, default="", help="候选审查输出文件")
    parser.add_argument("--outline-dir", type=str, default="", help="候选大纲目录；批量审查时同时作为前后章上下文")
    parser.add_argument("--review-dir", type=str, default="", help="候选审查报告输出目录")
    parser.add_argument("--repair-feedback", type=str, default="", help="整本大纲总审回灌到本章的修复验收反馈")
    args = parser.parse_args()

    try:
        project = resolve_project_dir(args.project)
    except ValueError as exc:
        print(f"错误: {exc}")
        sys.exit(1)

    init_project(project)

    print("=" * 60)
    print("Outline Reviewer Agent 启动")
    print(f"项目: {NOVELS_DIR}")
    print("=" * 60)

    OUTLINE_REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    total = 1 if args.chapter > 0 else (args.end - args.start + 1)
    failed = 0
    candidate_outline_dir = Path(args.outline_dir).resolve() if args.outline_dir else None
    candidate_review_dir = Path(args.review_dir).resolve() if args.review_dir else None
    repair_feedback = Path(args.repair_feedback).resolve() if args.repair_feedback else None
    if args.chapter > 0:
        outline_override = Path(args.outline_file) if args.outline_file else None
        if outline_override is None and candidate_outline_dir is not None:
            outline_override = candidate_outline_dir / f"chapter_{args.chapter:04d}.json"
        review_override = Path(args.review_file) if args.review_file else None
        if review_override is None and candidate_review_dir is not None:
            review_override = candidate_review_dir / f"chapter_{args.chapter:04d}_review.json"
        result = review_outline(
            args.chapter,
            outline_override,
            review_override,
            candidate_outline_dir,
            repair_feedback,
        )
        if result.get("status") in ("failed", "no_file", "parse_error", "invalid_review"):
            failed += 1
    else:
        for ch in range(args.start, args.end + 1):
            outline_override = (
                candidate_outline_dir / f"chapter_{ch:04d}.json"
                if candidate_outline_dir is not None
                else None
            )
            review_override = (
                candidate_review_dir / f"chapter_{ch:04d}_review.json"
                if candidate_review_dir is not None
                else None
            )
            result = review_outline(
                ch,
                outline_override,
                review_override,
                candidate_outline_dir,
                repair_feedback,
            )
            if result.get("status") in ("failed", "no_file", "parse_error", "invalid_review"):
                failed += 1
            time.sleep(1)

    log(f"[OutlineReviewer] 完成 {total - failed} 章，失败 {failed} 章")
    log("[OutlineReviewer] 全部完成")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
