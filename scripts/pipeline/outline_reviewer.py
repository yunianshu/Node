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


def call_llm(system_prompt: str, user_prompt: str, max_tokens: int = 4096, temperature: float = 0.3) -> str:
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

    min_score = float(CONFIG.get("outline_reviewer", {}).get("min_score", 8.5))

    retry_contract = ""
    if _semantic_errors:
        retry_contract = (
            "\n## 上次输出无效，本次必须纠正\n"
            "上次缺陷：" + "；".join(_semantic_errors) + "\n"
            "本次必须返回完整 JSON，尤其不得省略 strengths、weaknesses、suggestions、"
            "continuity_issues、summary、edits。未通过时只给出1条最关键edit。\n"
        )

    system = f"""你是一位中文小说编辑，评估《{book_title}》的大纲是否可执行、人物可信、前后连贯。
{genre_text}
{retry_contract}
按章节在全书中的作用评分。过渡、日常、后果消化和安静结束也可获得高分；
不按反转、物象、潜台词、爽点或情绪变化的数量打分，不要求每章成为高潮。
9分表示设计完成度高，8分表示基本有效但有具体可改进处；不为追求9分虚构问题。
只输出紧凑合法 JSON，指出最关键的、有事实证据的问题；无问题时允许空数组。"""

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
  "overall_score": "请给出0-10的客观评分。按本章功能、人物与因果完成度评价，不以悬念密度为统一标准",
  "verdict": "通过/需修改/需重写。注意：如果 overall_score >= {min_score}，verdict 必须写'通过'；只有低于{min_score}分才写'需重写'或'需修改'",
  "primary_issue": "当前最关键的唯一问题，80字以内",
  "primary_suggestion": "针对primary_issue的唯一修改建议，80字以内",
  "weaknesses": ["唯一不足，80字以内。无实际问题时可为空数组"],
  "suggestions": ["唯一具体修改建议，80字以内"],
  "continuity_issues": ["唯一衔接问题；没有则空数组"],
  "repair_feedback_checks": [{{"id": "R1", "passed": true, "evidence": "本章中实际完成修复的具体事件"}}],
  "summary": "总体评价，80字以内。按本章功能评价完成度，不虚构短板",
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
    "scene_design": {{"passed": true, "evidence": "scenes 非空，位置与具体目标有效，场景数量和修辞随需要，60字以内"}}{repair_gate_schema}
  }}
}}

审查原则：
1. 硬设计门只包括 core_desire（具体目标）与 scene_design（可执行场景），以及显式修复任务。
2. irreversible_choice/midpoint_reversal/human_warmth/content_richness/strong_hook 为观察项，不适用时说明即可，不因没有反转、生活词汇或强钩子扣分。
3. scenes 至少一个，position/objective 有效即可；其他场景字段按需，可空，可自然重复物件。
4. tension_points 可空，单一情绪、单个场景、日常相处或自然收束均可成立。
5. 人物认知、时间、地点、伤势、资源、身份与关键事件需和前后章一致。已发生的不可逆事件不能无理由再次发生。
6. 最早事实作为锚点：由后章引入的矛盾应修后章，不为此压低本章分数。
7. 遵守 origin 事实，但未复述某个词或某条素材不等于违背事实。
8. 低于 {min_score} 或有实际硬伤时给“需修改”，并给一条 field/action/value 补丁；其余字段保持。
9. story_beat 标签是结构描述，不因连续相同自动判为注水；只有具体内容重复才需要修订。
10. 若有 repair_feedback_checks，逐条给出本章是否完成显式修复任务的证据。"""

    log(f"[OutlineReviewer] 正在审查第{chapter_number}章大纲...")
    start_time = time.time()
    content = call_llm(system, prompt, max_tokens=4096, temperature=0.3)
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


    required_gates = ("core_desire", "scene_design")
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
        if beat_flat_issues:
            review_data["beat_distribution_issue"] = beat_flat_issues[0]
            # 相同节拍不等同于内容重复，不能据此改标签、压分或强制改写。
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
                    "value": "非空场景数组，每项有有效position与具体objective，其他修辞字段按需",
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
        if isinstance(score, (int, float)) and score < min_score:
            weaknesses = review_data.get("weaknesses")
            if not isinstance(weaknesses, list) or not weaknesses:
                contract_errors.append("未达通过阈值但未说明具体 weaknesses")
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
