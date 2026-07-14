#!/usr/bin/env python3
"""
Reviewer Agent - 审查Agent
负责审查章节质量并输出评分报告
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
import sys
import time
from pathlib import Path

from core.novel_config import (
    configure_stdio,
    extract_origin_fact_clauses,
    extract_origin_fact_terms,
    load_config,
    load_origin_materials,
    resolve_project_dir,
)
from core.review_ai_client import ReviewAIError, call_review_ai
from core.ai_flavor_detector import detect_ai_flavor
# 微信推送已禁用，改由 coordinator 统一推送进度
# from core.push_notifier import push_stage_complete
from core.workflow_state import (
    load_outline_chapter, load_review_status, review_dir,
    scan_chapter_status, write_status_file, highest_contiguous, report_path,
)
from core.outline_quality_gate import cast_name_set, clean_char_name

configure_stdio()

NOVELS_DIR = None
CHAPTERS_DIR = None
CHARACTERS_FILE = None
WORLD_FILE = None
REVIEWS_DIR = None
LOG_FILE = None
CONFIG = None
ORIGIN_MATERIALS = ""


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, CHAPTERS_DIR, CHARACTERS_FILE, WORLD_FILE, REVIEWS_DIR, LOG_FILE, CONFIG, ORIGIN_MATERIALS
    NOVELS_DIR = Path(project_dir).resolve()
    CHAPTERS_DIR = NOVELS_DIR / "chapters" / "draft"
    CHARACTERS_FILE = NOVELS_DIR / "characters.json"
    WORLD_FILE = NOVELS_DIR / "world.json"
    REVIEWS_DIR = review_dir(NOVELS_DIR)
    LOG_FILE = NOVELS_DIR / "logs" / "reviewer.log"
    CONFIG = load_config(NOVELS_DIR)
    review_cfg = CONFIG.get("reviewer", {})
    ORIGIN_MATERIALS = load_origin_materials(
        NOVELS_DIR,
        max_chars=int(review_cfg.get("origin_max_chars", 4000) or 4000),
    )


def analyze_chapter_text(chapter_content: str) -> dict:
    text = chapter_content.strip()
    length = len(text)
    paragraph_count = len([p for p in text.splitlines() if p.strip()])
    dialogue_count = text.count("\u201c") + text.count('"')
    issues = []

    quality = CONFIG.get("quality", {}) if CONFIG else {}
    min_words = int(quality.get("min_chapter_words", 5000))
    max_words = int(quality.get("max_chapter_words", 12000))
    hard_fail_min = int(quality.get("hard_fail_min_chapter_words", 3000))
    warn_min = int(quality.get("warn_min_chapter_words", min_words - 200))
    warn_max = int(quality.get("warn_max_chapter_words", max_words + 3000))

    if length < warn_min:
        issues.append(f"字数低于{warn_min}字，当前{length}字")
    if length > warn_max:
        issues.append(f"字数超过{warn_max}字，当前{length}字")
    if paragraph_count < 20:
        issues.append(f"段落数量偏少，当前{paragraph_count}段")
    if dialogue_count < 4:
        issues.append("对话标记偏少，可能缺少角色互动")
    return {
        "word_count": length,
        "word_count_ok": min_words <= length <= max_words,
        "paragraph_count": paragraph_count,
        "dialogue_marker_count": dialogue_count,
        "issues": issues,
        "local_ok": not issues,
    }


def _keyword_hits(text: str, keywords: tuple[str, ...]) -> list[str]:
    return [keyword for keyword in keywords if keyword and keyword in text]


def _anchor_terms(anchor: str) -> list[str]:
    terms: list[str] = []
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", str(anchor or "")):
        if chunk in {"生活压力", "关系牵挂", "潜台词", "生活物件", "本章", "具体"}:
            continue
        if len(chunk) <= 6:
            terms.append(chunk)
            continue
        for size in (4, 3, 2):
            for index in range(0, max(0, len(chunk) - size + 1), size):
                terms.append(chunk[index:index + size])
    seen: list[str] = []
    for term in terms:
        if term and term not in seen:
            seen.append(term)
    return seen[:12]


def detect_origin_fact_reference(chapter_content: str, origin_materials: str) -> dict:
    """Detect whether a chapter touches concrete terms from origin/facts.

    Non-blocking evidence: not every chapter must cite origin facts, but this
    lets Reviewer and later gates distinguish fact adherence from style mimicry.
    """
    terms = extract_origin_fact_terms(origin_materials, limit=40)
    clauses = extract_origin_fact_clauses(origin_materials, limit=20)
    if not terms and not clauses:
        return {
            "required": False,
            "matched_terms": [],
            "missing_terms_sample": [],
            "matched_fact_clauses": [],
            "missing_fact_clauses_sample": [],
            "reference_hit_rate": None,
            "needs_attention": False,
        }
    text = chapter_content or ""
    matched = [term for term in terms if term in text]
    hit_rate = round(len(matched) / len(terms), 3) if terms else 0.0
    matched_clauses = []
    missing_clauses = []
    for clause in clauses:
        clause_terms = [
            term for term in extract_origin_fact_terms(f"## origin/facts\n{clause}", limit=12)
            if len(term) >= 2
        ]
        clause_terms = list(dict.fromkeys(clause_terms))
        clause_hits = [term for term in clause_terms if term in text]
        required_hits = 1 if len(clause_terms) <= 2 else 2
        if clause_hits and (len(clause_hits) >= required_hits or clause in text):
            matched_clauses.append({"clause": clause, "matched_terms": clause_hits[:8]})
        else:
            missing_clauses.append({"clause": clause, "expected_terms": clause_terms[:8]})
    nominal_only_hit = bool(matched) and bool(clauses) and not matched_clauses
    return {
        "required": True,
        "fact_terms_sample": terms[:20],
        "matched_terms": matched[:20],
        "missing_terms_sample": [term for term in terms if term not in matched][:20],
        "fact_clauses_sample": clauses[:8],
        "matched_fact_clauses": matched_clauses[:8],
        "missing_fact_clauses_sample": missing_clauses[:8],
        "reference_hit_rate": hit_rate,
        "nominal_only_hit": nominal_only_hit,
        "needs_attention": len(matched) == 0 or nominal_only_hit,
    }


def detect_human_warmth(chapter_content: str, chapter_outline: dict) -> dict:
    """本地启发式检查：正文是否兑现了 human_anchor 与基本人情味元素。

    这是保守门禁，不评价文笔，只挡明显"只有事件没有人"的章节。
    """
    text = chapter_content or ""
    anchor = str(chapter_outline.get("human_anchor", "")).strip() if isinstance(chapter_outline, dict) else ""
    livelihood_keywords = (
        "饭", "菜", "粥", "面", "茶", "烟", "酒", "药", "病", "医院", "诊所", "房租", "租金",
        "工资", "工钱", "欠", "债", "账", "钱", "银行卡", "手机", "电量", "楼道", "门口",
        "工位", "班", "老板", "同事", "邻居", "家里", "厨房", "旧衣", "袖口", "伤口",
    )
    relationship_keywords = (
        "妈", "娘", "爹", "爸", "父亲", "母亲", "孩子", "女儿", "儿子", "妻子", "丈夫",
        "兄弟", "姐姐", "妹妹", "师父", "徒弟", "朋友", "同事", "邻居", "恩", "亏欠",
        "照顾", "等你", "别告诉", "别怕", "对不起", "谢谢", "沉默", "没说", "欲言又止",
    )
    object_keywords = (
        "碗", "杯", "筷", "门", "灯", "伞", "钥匙", "纸条", "照片", "旧", "裂", "磨白",
        "袖口", "手心", "指节", "汗", "药味", "饭盒", "手机", "屏幕", "账单", "零钱",
    )
    dialogue_markers = text.count("\u201c") + text.count('"') + text.count("：")

    livelihood_hits = _keyword_hits(text, livelihood_keywords)
    relationship_hits = _keyword_hits(text, relationship_keywords)
    object_hits = _keyword_hits(text, object_keywords)
    anchor_terms = _anchor_terms(anchor)
    anchor_hits = [term for term in anchor_terms if term in text]
    category_hit_count = len(livelihood_hits) + len(relationship_hits) + len(object_hits)

    score = 10
    issues: list[str] = []
    anchor_soft_match = bool(anchor_hits) and category_hit_count >= 4
    if anchor and not anchor_soft_match and len(anchor_hits) < max(1, min(3, len(anchor_terms) // 3)):
        score -= 3
        issues.append("正文未充分兑现大纲 human_anchor")
    if len(livelihood_hits) < 2:
        score -= 2
        issues.append("缺少具体生活压力或日常处境细节")
    if len(relationship_hits) < 2:
        score -= 2
        issues.append("缺少关系牵挂、亏欠或人的反应")
    if len(object_hits) < 2:
        score -= 1
        issues.append("缺少可触摸的生活物件或身体细节")
    if dialogue_markers < 4:
        score -= 1
        issues.append("对话互动偏少，潜台词承载不足")

    score = max(0, min(10, score))
    return {
        "human_warmth_score": score,
        "passed": score >= 7,
        "issues": issues,
        "anchor": anchor[:160],
        "anchor_terms": anchor_terms,
        "anchor_hits": anchor_hits[:8],
        "livelihood_hits": livelihood_hits[:10],
        "relationship_hits": relationship_hits[:10],
        "object_hits": object_hits[:10],
        "category_hit_count": category_hit_count,
        "dialogue_markers": dialogue_markers,
    }


def _relationship_terms(value: str) -> list[str]:
    terms: list[str] = []
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", str(value or "")):
        if chunk in {"后续压力", "未说出口", "误会", "隐瞒", "亏欠", "人情", "承诺", "照料行为"}:
            continue
        if len(chunk) <= 5:
            terms.append(chunk)
        else:
            for size in (4, 3, 2):
                for index in range(0, max(0, len(chunk) - size + 1), size):
                    terms.append(chunk[index:index + size])
    seen: list[str] = []
    for term in terms:
        if term and term not in seen:
            seen.append(term)
    return seen[:12]


def detect_relationship_obligation(chapter_content: str, chapter_number: int) -> dict:
    """Check whether the Writer's selected relationship obligation has basic textual evidence."""
    if not NOVELS_DIR or chapter_number <= 1:
        return {"required": False, "passed": True, "reason": "无上一章关系状态"}
    try:
        from core.relationship_state import latest_relationships_before, select_relationship_obligation
        prev = latest_relationships_before(NOVELS_DIR, chapter_number)
        obligation = select_relationship_obligation(prev)
    except Exception as exc:
        return {"required": False, "passed": True, "reason": f"关系任务读取失败: {exc}"}
    if not obligation:
        return {"required": False, "passed": True, "reason": "无未解决关系任务"}

    text = chapter_content or ""
    pair = str(obligation.get("pair", "")).strip()
    names = [part.strip() for part in re.split(r"->|→|/|、|和", pair) if part.strip()]
    name_hits = [name for name in names if name and name in text]
    pressure_text = "；".join(str(v) for v in (obligation.get("pressure_fields") or {}).values())
    pressure_terms = _relationship_terms(pressure_text)
    pressure_hits = [term for term in pressure_terms if term in text]
    subtext_hits = _keyword_hits(text, (
        "没说", "沉默", "欲言又止", "别告诉", "对不起", "谢谢", "别怕", "算了",
        "问", "追问", "低声", "停了一下", "没看", "移开目光",
    ))
    action_hits = _keyword_hits(text, (
        "递", "扶", "护", "挡", "还", "补", "等", "送", "替", "拉住", "放下",
        "收起", "塞给", "攥住", "避开", "回头",
    ))
    outcome_hits = _keyword_hits(text, (
        "亏欠", "误会", "和解", "原谅", "答应", "承诺", "更沉", "松了口气",
        "不再", "终于", "仍然", "欠", "还清",
    ))

    score = 0
    issues: list[str] = []
    if len(name_hits) >= min(2, max(1, len(names))):
        score += 3
    else:
        issues.append("关系任务对象在正文中出现不足")
    if len(pressure_hits) >= 2:
        score += 3
    else:
        issues.append("未解决关系压力缺少正文关键词回声")
    if subtext_hits:
        score += 2
    else:
        issues.append("缺少承载关系压力的潜台词对白")
    if action_hits:
        score += 1
    else:
        issues.append("缺少照料、回避、补偿或保护动作")
    if outcome_hits:
        score += 1
    else:
        issues.append("缺少关系变化结果")

    return {
        "required": True,
        "passed": score >= 6,
        "score": score,
        "pair": pair,
        "pressure": str(obligation.get("pressure", ""))[:220],
        "name_hits": name_hits,
        "pressure_terms": pressure_terms,
        "pressure_hits": pressure_hits[:8],
        "subtext_hits": subtext_hits[:8],
        "action_hits": action_hits[:8],
        "outcome_hits": outcome_hits[:8],
        "issues": issues,
    }


def detect_scene_realization(chapter_content: str, chapter_outline: dict) -> dict:
    """本地检测正文是否兑现大纲 scenes 的感官锚点、潜台词和出口钩子。

    非硬门禁：用 bigram 软匹配避免误杀同义改写。scene_realization_rate < 0.5 时
    由上层降 human_warmth 分并触发重写轮深化。
    """
    if not isinstance(chapter_outline, dict):
        return {"required": False, "rate": None, "scenes": []}
    scenes = chapter_outline.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        return {"required": False, "rate": None, "scenes": []}
    text = chapter_content or ""
    subtext_markers = ("没说", "沉默", "欲言又止", "别告诉", "对不起", "谢谢", "别怕", "算了", "低声", "移开目光")
    realized_count = 0
    scene_results = []
    for scene_idx, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            continue
        anchor = str(scene.get("sensory_anchor", "")).strip()
        anchor_terms = _anchor_terms(anchor)
        anchor_hits = [t for t in anchor_terms if t in text]
        subtext = str(scene.get("subtext_beat", "")).strip()
        subtext_terms = _anchor_terms(subtext)
        subtext_hits = [t for t in subtext_terms if t in text]
        subtext_marker_hits = [m for m in subtext_markers if m in text]
        exit_hook = str(scene.get("exit_hook", "")).strip()
        exit_terms = _anchor_terms(exit_hook)
        exit_hits = [t for t in exit_terms if t in text]
        anchor_ok = len(anchor_hits) >= max(1, min(2, len(anchor_terms) // 3)) if anchor_terms else False
        subtext_ok = bool(subtext_hits) or bool(subtext_marker_hits)
        exit_ok = bool(exit_hits)
        realized = anchor_ok and (subtext_ok or exit_ok)
        if realized:
            realized_count += 1
        scene_results.append({
            "index": scene_idx,
            "position": str(scene.get("position", "")),
            "realized": realized,
            "anchor_hits": anchor_hits[:6],
            "subtext_hits": subtext_hits[:4],
            "exit_hits": exit_hits[:4],
        })
    rate = round(realized_count / len(scene_results), 3) if scene_results else 0.0
    return {
        "required": True,
        "rate": rate,
        "realized_count": realized_count,
        "total_scenes": len(scene_results),
        "scenes": scene_results,
        "needs_attention": rate < 0.5,
    }


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def call_mmx(system_prompt: str, user_prompt: str, max_tokens: int = 4096, temperature: float = 0.3) -> str:
    try:
        cfg = CONFIG.get("reviewer", {})
        fallback = CONFIG.get("writer", {})
        return call_review_ai(
            CONFIG,
            NOVELS_DIR,
            "reviewer",
            system_prompt,
            user_prompt,
            max_tokens=int(cfg.get("max_tokens", max_tokens) or max_tokens),
            temperature=float(cfg.get("temperature", temperature)),
            raw_name="reviewer",
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


def _parse_score(val):
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        val = val.strip()
        if "/" in val:
            num = val.split("/")[0].strip()
            try:
                return float(num)
            except ValueError:
                pass
        try:
            return float(val)
        except ValueError:
            pass
    return val


def _extract_json_text(content: str) -> str:
    if "```json" in content:
        return content.split("```json", 1)[1].split("```", 1)[0].strip()
    if "```" in content:
        return content.split("```", 1)[1].split("```", 1)[0].strip()
    return content.strip()


def _extract_string_field(content: str, field: str) -> str:
    match = re.search(rf'"{re.escape(field)}"\s*:\s*"([^"]*)"', content)
    return match.group(1).strip() if match else ""


def _extract_score_field(content: str, field: str):
    match = re.search(rf'"{re.escape(field)}"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?', content)
    return _parse_score(match.group(1)) if match else None


def _extract_array_items(content: str, field: str, limit: int = 3) -> list[str]:
    match = re.search(rf'"{re.escape(field)}"\s*:\s*\[(.*?)\]', content, re.S)
    if not match:
        return []
    items = re.findall(r'"([^"]+)"', match.group(1))
    return [item[:120] for item in items[:limit]]


def _partial_review_from_raw(chapter_number: int, content: str, local_analysis: dict) -> dict:
    score = _extract_score_field(content, "overall_score")
    verdict = _extract_string_field(content, "verdict") or "需修改"
    return {
        "chapter_number": chapter_number,
        "status": "parse_error",
        "reported_overall_score": score,
        "reported_verdict": verdict,
        "verdict": "需修改",
        "partial_strengths": _extract_array_items(content, "strengths"),
        "partial_weaknesses": _extract_array_items(content, "weaknesses"),
        "partial_suggestions": _extract_array_items(content, "suggestions"),
        "partial_continuity_issues": _extract_array_items(content, "continuity_issues"),
        "raw_response": content[:3000],
        "local_analysis": local_analysis,
    }


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


def _character_presence_issues(
    chapter_content: str, chapter_outline: dict, characters: dict
) -> dict:
    """确定性跨章一致性检查（series-bible 姓名核对）：

    大纲 characters_involved 中、且在 characters.json 规范名册里的角色，
    其规范名（或已登记别名）是否在正文中出现。缺失往往意味着称呼漂移
    （同一角色被换成未绑定的称呼，如 周德茂→只用"周社长"）或角色被遗忘——
    这是 world_consistency 扣分的主因之一。

    只核对"规范名册内"的角色，避免大纲/名册称呼不一致造成的误报。
    """
    text = str(chapter_content or "")

    def name_forms(value) -> set[str]:
        raw = str(value or "").strip()
        forms: set[str] = set()
        if not raw:
            return forms
        forms.add(raw)
        cleaned = clean_char_name(raw)
        if cleaned:
            forms.add(cleaned)
        for part in re.findall(r"[（(]([^）)]*)[）)]", raw):
            alias = str(part).strip()
            if alias:
                forms.add(alias)
        return forms

    roster: dict[str, set[str]] = {}

    def add_character(value: dict) -> None:
        forms = name_forms(value.get("name"))
        aliases = value.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        if isinstance(aliases, list):
            for alias in aliases:
                forms.update(name_forms(alias))
        if not forms:
            return
        for form in forms:
            roster[form] = forms

    def walk(value) -> None:
        if isinstance(value, dict):
            if "name" in value:
                add_character(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(characters)
    cast_names = cast_name_set(characters)
    involved = chapter_outline.get("characters_involved", []) if isinstance(chapter_outline, dict) else []
    if isinstance(involved, str):
        involved = [involved]
    absent: list[str] = []
    checked = 0
    for who in involved:
        raw_who = str(who).strip()
        if not raw_who:
            continue
        who_key = clean_char_name(raw_who)
        forms = roster.get(raw_who) or roster.get(who_key)
        if forms is None and who_key in cast_names:
            forms = {raw_who, who_key}
        if not forms:
            continue
        checked += 1
        if not any(form and form in text for form in forms):
            absent.append(who_key or raw_who)
    return {
        "checked": checked,
        "absent": absent,
        "note": (
            "characters_involved 中规范角色在正文未出现（可能称呼漂移/被遗忘）"
            if absent else "ok"
        ),
    }


def review_chapter(
    chapter_number: int,
    chapter_file_override: str | Path | None = None,
    review_file_override: str | Path | None = None,
    _semantic_attempt: int = 0,
    _semantic_errors: list[str] | None = None,
) -> dict:
    chapter_file = Path(chapter_file_override) if chapter_file_override else CHAPTERS_DIR / f"chapter_{chapter_number:04d}.txt"
    review_file = Path(review_file_override) if review_file_override else REVIEWS_DIR / f"chapter_{chapter_number:04d}_review.json"

    if not chapter_file.exists():
        log(f"[Reviewer] 第{chapter_number}章文件不存在")
        return {"status": "no_file"}

    if review_file.exists():
        configured_min_score = float(CONFIG.get("reviewer", {}).get("min_score", 8.5))
        _, status, _, ok = load_review_status(review_file, configured_min_score)
        if ok:
            log(f"[Reviewer] 第{chapter_number}章已有有效审查报告，跳过")
            with open(review_file, "r", encoding="utf-8") as f:
                return json.load(f)
        log(f"[Reviewer] 第{chapter_number}章审查报告无效（{status}），重新审查")

    with open(chapter_file, "r", encoding="utf-8") as f:
        chapter_content = f.read()
    local_analysis = analyze_chapter_text(chapter_content)
    ai_flavor = detect_ai_flavor(chapter_content, project=NOVELS_DIR)
    local_analysis["ai_flavor_detection"] = ai_flavor

    chapter_outline = load_outline_chapter(NOVELS_DIR, chapter_number)

    characters = load_json(CHARACTERS_FILE)
    local_analysis["character_presence"] = _character_presence_issues(
        chapter_content, chapter_outline, characters
    )
    local_analysis["human_warmth_detection"] = detect_human_warmth(
        chapter_content, chapter_outline
    )
    local_analysis["origin_fact_reference_detection"] = detect_origin_fact_reference(
        chapter_content, ORIGIN_MATERIALS
    )
    local_analysis["relationship_obligation_detection"] = detect_relationship_obligation(
        chapter_content, chapter_number
    )
    local_analysis["scene_realization_detection"] = detect_scene_realization(
        chapter_content, chapter_outline
    )

    content_sample = chapter_content[:1500]
    mid_start = max(0, len(chapter_content) // 2 - 500)
    content_sample += "\n\n[中间部分...]\n\n" + chapter_content[mid_start:mid_start + 800]
    content_sample += "\n\n[结尾部分...]\n\n" + chapter_content[-800:]

    world = load_json(WORLD_FILE)
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

    quality = CONFIG.get("quality", {})
    review_min_score = float(CONFIG.get("reviewer", {}).get("min_score", 8.5))
    min_words = int(quality.get("min_chapter_words", 5000))
    max_words = int(quality.get("max_chapter_words", 12000))
    warn_min = int(quality.get("warn_min_chapter_words", min_words - 200))
    warn_max = int(quality.get("warn_max_chapter_words", max_words + 3000))

    retry_contract = ""
    if _semantic_errors:
        retry_contract = (
            "\n## 上次输出无效，本次必须纠正\n"
            "上次缺陷：" + "；".join(_semantic_errors) + "\n"
            "本次必须返回完整 JSON；不得省略 strengths、weaknesses、suggestions、"
            "continuity_issues、summary、edits。未通过时只给出一条最关键可定位 edits。\n"
        )

    system = f"""你是一位拥有20年经验的资深网络小说编辑，同时也是一位苛刻的"神作猎手"。
你的任务不是列出所有问题，而是找出当前最阻碍本章达标的唯一问题，并给出唯一一条可执行修改。
本书是《{book_title}》。
{genre_text}
{retry_contract}

## 【9分神作评分标准】
- 10分：传世级神作。每一句都在推动剧情或揭示人物，章末钩子让人失眠，读完之后心跳加速，无法停止思考。
- 9-9.9分：优秀神作。悬念密集，情感冲击强烈，信息新鲜，读完立刻想打开下一章。只存在极小的可改进空间。
- 8-8.9分：良好但不够惊艳。有可读性，但存在套路化倾向、悬念不足、情感平淡或信息重复等问题。低于{review_min_score}分，必须重写。
- 7-7.9分：平庸。有明显水文、套路、OOC或逻辑问题，读者很可能中途弃书。
- 低于7分：不合格，存在严重质量问题。

【你的审查哲学】
- 不要给"辛苦分"。写得多、写得顺不等于写得好。
- 不要放过"还行"——"还行"就是失败的委婉说法。
- 重点关注：读者读完这章后，会不会立刻想读下一章？如果不会，只指出最关键的一处原因。
- 如果你给不出9分以上，必须在 weaknesses 中只写一条"距离9分的最大差距"。

输出必须是合法的紧凑JSON，不要使用Markdown代码块，不要输出JSON之外的任何文字。
审查意见要短而具体，整份JSON尽量控制在1500个中文字符以内。"""

    prompt = f"""请审查以下第{chapter_number}章的内容。

## 章节大纲
{json.dumps(chapter_outline, ensure_ascii=False, indent=2) if chapter_outline else '未找到大纲'}

## 角色设定
{json.dumps(characters, ensure_ascii=False, indent=2)[:1000]}

## origin/ 原始参考素材
{ORIGIN_MATERIALS or "（无）"}

## 章节内容（节选）
{content_sample}

## 章节字数
{len(chapter_content)}字

## 本地全文检查
{json.dumps(local_analysis, ensure_ascii=False, indent=2)}

请只输出以下JSON格式的审查报告。所有数组都只能有1条，每条不超过80字；edits只能有1条：
{{
  "chapter_number": {chapter_number},
  "overall_score": "请给出0-10的客观评分。9分意味着'非常想读下一章'，10分意味着'震撼到说不出话'。不要给辛苦分",
  "verdict": "通过/需修改/需重写。注意：如果 overall_score >= {review_min_score}，verdict 必须写'通过'；只有低于{review_min_score}分才写'需重写'或'需修改'",
  "scores": {{
    "writing_quality": "文笔流畅度（0-10）",
    "plot_coherence": "剧情连贯性（0-10）",
    "character_consistency": "人物一致性（0-10）",
    "character_voice": "人物辨识度（0-10。遮住角色名字能否认出是谁？主角是否有鲜明性格锋芒、价值取向、标志动作或说话腔调？是否有性格反差让人物立体？不同角色台词是否可分辨？）",
    "causal_logic": "因果逻辑（0-10。每个转折/破局是否可追溯到前文原因？是否禁止巧合解围？信息传递是否闭环？动机-行为-结果是否自洽？时间/伤情/资源是否连续？）",
    "scene_description": "场景氛围感（0-10。不是画面多清晰，而是氛围多压迫/诡异/震撼）",
    "dialogue_quality": "对话质量（0-10。是否有潜台词？是否推动情节？是否避免了解说员式对话？）",
    "outline_adherence": "大纲遵循度（0-10）",
    "pacing": "节奏把控（0-10。是否存在平铺直叙超过1500字？中段是否有小高潮？）",
    "emotional_impact": "情感冲击力（0-10。是否触及角色核心恐惧/欲望？是否有刺点？）",
    "human_warmth": "烟火气与人情味（0-10。是否有具体生活压力、关系牵挂、潜台词和人的反应？）",
    "hook_strength": "章末钩子强度（0-10。最后200字是否让人心跳加速、必须读下一章？）",
    "suspense_density": "悬念密度（0-10。每800-1200字是否有新信息/冲突升级/意外转折？）",
    "information_freshness": "信息新鲜度（0-10。是否带来至少一个此前从未出现过的新元素？有无重复已知信息？）",
    "content_richness": "内容丰富度/层次感（0-10。外部事件之外，是否同时推进人物关系、生活压力、秘密代价或世界规则现场化？）",
    "anti_cliche": "反套路程度（0-10。是否存在标准升级流/打怪流/解谜流模板？是否有意外和不可预测性？）",
    "payoff_intensity": "爽点与反差强度（0-10。每2000-3000字是否有明确爽点？是否有充分的压抑→爆发反差？打脸/扮猪吃虎是否写到位而非无脑碾压？本章读起来爽不爽、有没有让人拍腿的瞬间？）",
    "read_desire": "读下去的欲望（0-10。假设你是第一次读的读者，读完这章后有多想立刻打开下一章？）",
    "ai_flavor": "去AI味程度（0-10。越高越自然。参考 local_analysis.ai_flavor_detection 的本地证据）",
    "world_consistency": "世界观/设定一致性（0-10。本章涉及的力量体系/势力关系/地理/经济/规则是否与世界观设定自洽？有无设定矛盾或吃书？）"
  }},
  "word_count_check": {{
    "actual": {len(chapter_content)},
    "target": {min_words},
    "status": "达标/偏短/偏长"
  }},
  "primary_issue": "当前最关键的唯一问题，80字以内",
  "primary_suggestion": "针对primary_issue的唯一修改建议，80字以内",
  "strengths": ["唯一优点，80字以内"],
  "weaknesses": ["唯一不足，80字以内。如果给分低于9分，必须写出'距离9分的最大差距'"],
  "suggestions": ["唯一具体修改建议，80字以内"],
  "continuity_issues": ["唯一连续性问题；没有则空数组"],
  "summary": "总体评价，80字以内。如果评分低于9分，用一句话回答：'本章最致命的短板是什么？'",
  "edits": [
    {{
      "type": "replace",
      "old": "正文中需要被替换的原文片段（30-200字，必须精确可定位）",
      "new": "替换后的文本"
    }},
    {{
      "type": "insert",
      "after": "原文锚点片段（插入位置）",
      "text": "要插入的新内容"
    }},
    {{
      "type": "delete",
      "old": "要删除的原文片段"
    }}
  ]
}}

【9分神作核心审查清单】
请你在给出评分前按以下核心项快速自检；如存在多个问题，只输出最影响通过的一项：
1. 章末最后200字是否包含一个让人心跳加速的强力钩子（危机升级/信息反转/情感爆点）？
2. 本章是否有至少一个让读者心头一紧的"刺点"细节（反常动作、未说出口的话、突然沉默）？
3. 每800-1200字是否至少有一次有效推进（新信息、冲突升级、意外转折、人物关系质变）？
4. 本章是否给读者带来至少一个"此前从未出现过的新元素"？
5. 心理描写是否展现了真实的情感波动，而不是代码化/分析化的流水账？
6. 冲突是否触及角色核心恐惧或核心欲望，而非表层利害计算？
7. 本章是否有具体生活压力或人际牵挂参与剧情，而不是只有宏大危机和任务推进？
8. 关键对白是否有潜台词，配角是否有自己的难处、善意、恐惧或小算盘？
9. 爽点/破局后是否有人产生真实反应、关系变化或亏欠回声？
10. 本章是否兑现大纲 content_layers，除外部事件外至少还有一层关系、生活压力、秘密代价或世界规则现场化发生真实变化？
11. 是否出现AI指纹：高频1-3字短句断句、身后/身前/一步又一步式对称收尾、抽象概念堆叠、地图打卡、散文诗式重复段式？
12. 是否存在形式感压过叙事的问题：多个段落只是同一瞬间的物象变奏，而没有新的行动、阻碍、选择、反应或信息变化？
13. 本章是否有一个可复述的不可逆动作，让读者看见人物真的“往前挪了半寸”？
14. 若本章有群像/多线协作，是否完成“从重奏到独步”的转折：群像压力最终收束为主角自己的判断、行动和代价？
15. 关键证据/信息/信物的传递链是否清楚：谁拿到、如何转交、接收者如何理解、风险在哪里、最后如何使用？
16. 反派在露出破绽后是否有更冷的应对（规则、程序、威胁、交易、嫁祸），而不是只脸色一变或发怒？
17. 跨地点/跨时间转场是否有声音、光、脚步、物件、时辰或伤口变化作为桥接，避免读者脑补关键链路？
18. 反派标志物或贯穿意象是否照出了反派内层、旧事、软肋或破绽，而不是只作为“某人的灯笼/刀/戒指”存在？
19. 旧签押、旧证词、旧物证逼到反派时，是否有半拍身体裂隙（目光移开、指节发白、喉咙动、张口又咽回、灯柄轻响等）再接冷处理？
20. 章末关键道具/证据是否在前文有可见预埋动作（制作、拓印、藏匿、转手、瞥见），避免“作者需要它现在出现”？
21. 墨印/拓片/副本/录音备份等复制型证据是否写出了制作动作，而非结尾突然出现？
22. 父辈/亲缘/旧痕线索是否在章末关键动作中被微小回扣，而不是中途放下？
23. 结尾关键动作后是否有1-2个现场微反应形成余韵（反派停顿、旁人吸气、同伴松手、标志物光线变化），而不是动作一落就截断？
24. 是否有作者旁注式总结（“读者能看见”“这一章往前挪”“权力最怕的是”等）替代现场动作？
25. 是否存在套路化描写（标准战斗模板、标准解谜流程、配角当解说员）？
26. 如果我是第一次读这本书的读者，读完这章后会不会立刻想打开下一章？
27. 本章是否按大纲 scenes 逐场兑现：每个场景的 sensory_anchor（可触摸物象）、subtext_beat（潜台词）、exit_hook（出口钩子）是否在正文中落地？兑现率过低视为内容单薄。
28. 【人物辨识度】遮住名字能否认出谁在说话、谁在行动？主角是否有"只有他会这么干"的鲜明性格锋芒、价值取向或标志反应？是否有性格反差让人物从扁平变立体？不同角色台词是否可分辨？
29. 【因果逻辑】每个转折/破局是否可追溯到前文某个人物选择、信息或伏笔？是否禁止"恰好/刚好/凑巧"替主角解决问题？角色掌握的信息是否有合理来源？动机-行为-结果是否自洽？时间/伤情/资源是否连续承接？
30. 【爽点与反差】本章爽点密度够吗（每2000-3000字一个）？是否有"压抑→爆发"的反差设计？打脸/扮猪吃虎是写到位了（铺垫足、反转有因果）还是无脑碾压？整章读下来有没有让人想拍腿叫好的瞬间，还是只有铺垫和憋屈？

要求：
1. 评分要冷酷客观。不要给辛苦分，不要给"还行"分。9分意味着"非常想读下一章"，8分意味着"看完了，还行"。
2. 重点审查：人物辨识度、因果逻辑、爽点与反差、悬念密度、情感冲击、烟火气与人情味、内容丰富度、信息新鲜度、反套路程度、读下去的欲望。这些维度比文笔更重要。**人物辨识度、因果逻辑或爽点与反差任一低于8分，overall_score 不得给到9分及以上；任一低于7分直接判"需修改"。**
3. 剧情推进是否自然，有无逻辑漏洞或突兀转折
4. 对话是否有潜台词，是否符合角色身份，是否避免了解说员式长篇大论
5. 必须给出具体的修改建议，不能泛泛而谈，且只给最关键1条
6. 如果 origin/ 中存在素材，必须检查正文是否参考并遵守原始素材；与素材冲突需列入 weaknesses 或 continuity_issues
7. 如低于{review_min_score}分必须标记为"需重写"
8. 字数不足{warn_min}或超过{warn_max}要标记字数问题
9. 必须输出合法JSON，不要Markdown，不要长篇解释
10. **必须输出 edits 数组**：如果 verdict 不是"通过"，只能给出1条最关键、最可定位的 edit ops（replace/insert/delete），用于定点修改而不是全文重写。edit 的 old/after 字段必须引用原文真实片段，长度 30-200 字。
11. 若 local_analysis.ai_flavor_detection.ai_flavor_score < 7，verdict 不得为"通过"，只挑最严重的一处AI味问题在 edits 中定点重写。
12. 若 local_analysis.ai_flavor_detection.issues 出现 short_sentence_fragmentation、symmetric_anchor_ending、abstract_concept_pileup、formal_refrain_stagnation、repeated_authorial_judgment、authorial_aside 或 static_lyrical_scene，verdict 不得为"通过"，必须优先定点重写对应段落。
13. 若 local_analysis.character_presence.absent 非空，verdict 不得为"通过"，只挑最关键缺失角色在 continuity_issues 和 edits 中处理。
14. 若 local_analysis.human_warmth_detection.passed=false，verdict 不得为"通过"，必须优先修正文中未兑现 human_anchor、缺少生活压力或关系牵挂的问题。
15. 若 local_analysis.relationship_obligation_detection.required=true 且 passed=false，verdict 不得为"通过"，必须优先修复上一章延续下来的关系任务：让对应人物、压力、潜台词对白、照料/回避/补偿动作和关系结果进入正文。
16. 若 local_analysis.origin_fact_reference_detection.needs_attention=true，说明正文没有命中 origin/facts 事实素材；这不是单独硬门槛，但应优先在 weaknesses/suggestions 中指出，避免只模仿 style 风格而不遵守事实。
17. 若 local_analysis.scene_realization_detection.needs_attention=true，verdict 不得为"通过"，必须在 weaknesses/suggestions 中指出哪些 scene 未兑现，并在 edits 中定点补 sensory_anchor 或潜台词对白。"""

    log(f"[Reviewer] 正在审查第{chapter_number}章...")
    start_time = time.time()
    content = call_mmx(system, prompt, max_tokens=4096, temperature=0.3)
    elapsed = time.time() - start_time
    log(f"[Reviewer] 第{chapter_number}章审查 API 调用耗时 {elapsed:.1f}s")

    if not content:
        log(f"[Reviewer] 第{chapter_number}章审查失败")
        review_data = {
            "chapter_number": chapter_number,
            "status": "failed",
            "local_analysis": local_analysis,
        }
        review_file.parent.mkdir(parents=True, exist_ok=True)
        with open(review_file, "w", encoding="utf-8") as f:
            json.dump(review_data, f, ensure_ascii=False, indent=2)
        return review_data

    try:
        content = _extract_json_text(content)
        review_data = json.loads(content)
        review_data["status"] = "completed"
        review_data["local_analysis"] = local_analysis

        # 将 overall_score 统一转为 float，避免字符串类型导致 schema 校验失败
        review_data["overall_score"] = _parse_score(review_data.get("overall_score"))
        # 同时将 scores 子项也转为 float
        scores = review_data.get("scores")
        if isinstance(scores, dict):
            for k, v in list(scores.items()):
                scores[k] = _parse_score(v)
        _normalize_single_action_review(review_data)

        contract_errors: list[str] = []
        score = review_data.get("overall_score")
        verdict = review_data.get("verdict")
        if review_data.get("chapter_number") != chapter_number:
            contract_errors.append("chapter_number 缺失或不匹配")
        if not isinstance(score, (int, float)) or not 0 <= float(score) <= 10:
            contract_errors.append("overall_score 缺失、不是数字或超出0-10")
        if verdict not in {"通过", "需修改", "需重写"}:
            contract_errors.append("verdict 缺失或非法")
        if not isinstance(review_data.get("scores"), dict):
            contract_errors.append("scores 缺失或不是对象")
        for field in ("strengths", "weaknesses", "suggestions", "continuity_issues", "edits"):
            if not isinstance(review_data.get(field), list):
                contract_errors.append(f"{field} 缺失或不是数组")
        if not isinstance(review_data.get("summary"), str) or not review_data["summary"].strip():
            contract_errors.append("summary 缺失")

        hard_gate_reasons: list[str] = []
        if not local_analysis.get("word_count_ok", False):
            hard_gate_reasons.append("正文字数未通过本地范围检查")
        ai_flavor_score = (local_analysis.get("ai_flavor_detection") or {}).get("ai_flavor_score")
        if isinstance(ai_flavor_score, (int, float)) and ai_flavor_score < 7:
            hard_gate_reasons.append("本地去AI味评分低于7")
        ai_issues = (local_analysis.get("ai_flavor_detection") or {}).get("issues")
        if isinstance(ai_issues, list):
            fingerprint_types = {
                "short_sentence_fragmentation": "短句断句AI指纹",
                "symmetric_anchor_ending": "对称式章节结尾AI指纹",
                "abstract_concept_pileup": "抽象概念堆叠AI指纹",
                "formal_refrain_stagnation": "散文诗式重复段式",
                "repeated_authorial_judgment": "重复作者判断句",
                "authorial_aside": "作者旁注式总结",
                "static_lyrical_scene": "静态意象堆叠导致叙事停滞",
            }
            found = [
                fingerprint_types.get(str(item.get("type")))
                for item in ai_issues
                if isinstance(item, dict) and str(item.get("type")) in fingerprint_types
            ]
            if found:
                hard_gate_reasons.append("本地AI指纹检测未通过：" + "、".join(dict.fromkeys(found)))
        absent = (local_analysis.get("character_presence") or {}).get("absent")
        if isinstance(absent, list) and absent:
            hard_gate_reasons.append("大纲要求角色在正文缺失：" + "、".join(str(item) for item in absent[:3]))
        human_warmth = local_analysis.get("human_warmth_detection") or {}
        if isinstance(human_warmth, dict) and human_warmth.get("passed") is False:
            issues = human_warmth.get("issues")
            if isinstance(issues, list) and issues:
                hard_gate_reasons.append("本地人情味检测未通过：" + "、".join(str(item) for item in issues[:3]))
            else:
                hard_gate_reasons.append("本地人情味检测未通过")
        relationship_obligation = local_analysis.get("relationship_obligation_detection") or {}
        if (
            isinstance(relationship_obligation, dict)
            and relationship_obligation.get("required") is True
            and relationship_obligation.get("passed") is False
        ):
            issues = relationship_obligation.get("issues")
            pair = str(relationship_obligation.get("pair", "")).strip()
            prefix = f"关系任务未兑现({pair})" if pair else "关系任务未兑现"
            if isinstance(issues, list) and issues:
                hard_gate_reasons.append(prefix + "：" + "、".join(str(item) for item in issues[:3]))
            else:
                hard_gate_reasons.append(prefix)
        scene_realization = local_analysis.get("scene_realization_detection") or {}
        if (
            isinstance(scene_realization, dict)
            and scene_realization.get("required") is True
            and scene_realization.get("needs_attention") is True
        ):
            scene_rate = scene_realization.get("rate")
            hard_gate_reasons.append(
                f"本地场景兑现率过低({scene_rate})：scenes 的 sensory_anchor/subtext_beat/exit_hook 未充分落地正文"
            )

        if isinstance(score, (int, float)):
            if hard_gate_reasons and score >= review_min_score:
                review_data["reported_overall_score"] = score
                review_data["overall_score"] = round(review_min_score - 0.1, 2)
                score = review_data["overall_score"]
                review_data["verdict"] = "需修改"
                verdict = "需修改"
            elif score >= review_min_score:
                review_data["verdict"] = "通过"
                verdict = "通过"
            elif verdict == "通过":
                review_data["verdict"] = "需修改"
                verdict = "需修改"

        if hard_gate_reasons:
            weaknesses = review_data.get("weaknesses")
            weakness_text = "；".join(hard_gate_reasons)
            if isinstance(weaknesses, list) and weakness_text not in weaknesses:
                weaknesses[:] = [weakness_text]

        score = review_data.get("overall_score")
        if isinstance(score, (int, float)) and score < 9:
            weaknesses = review_data.get("weaknesses")
            if not isinstance(weaknesses, list) or not weaknesses:
                contract_errors.append("低于9分但未说明具体 weaknesses")

        edits = review_data.get("edits")
        if verdict != "通过":
            if not isinstance(review_data.get("suggestions"), list) or not review_data["suggestions"]:
                contract_errors.append("未通过但没有可执行 suggestions")
            if not isinstance(edits, list) or not edits:
                contract_errors.append("未通过但没有可定位 edits")

        if isinstance(edits, list):
            review_data["edits"] = edits[:1]
            edits = review_data["edits"]
            valid_edit_count = 0
            for index, edit in enumerate(edits):
                if not isinstance(edit, dict):
                    contract_errors.append(f"edits[{index}] 不是对象")
                    continue
                edit_type = edit.get("type")
                anchor = edit.get("after") if edit_type == "insert" else edit.get("old")
                replacement = edit.get("text") if edit_type == "insert" else edit.get("new")
                if edit_type not in {"replace", "insert", "delete"}:
                    contract_errors.append(f"edits[{index}] type 非法")
                    continue
                if not isinstance(anchor, str) or not anchor.strip() or anchor not in chapter_content:
                    contract_errors.append(f"edits[{index}] 原文锚点无法定位")
                    continue
                if edit_type != "delete" and (not isinstance(replacement, str) or not replacement.strip()):
                    contract_errors.append(f"edits[{index}] 修改后文本缺失")
                    continue
                valid_edit_count += 1
            if verdict != "通过" and valid_edit_count == 0:
                contract_errors.append("未通过但没有任何可应用的 edit")

        if contract_errors:
            semantic_retries = max(
                0,
                int(CONFIG.get("reviewer", {}).get("semantic_retries", 1) or 0),
            )
            if _semantic_attempt < semantic_retries:
                log(
                    f"[Reviewer] 第{chapter_number}章审查报告契约不完整，"
                    f"原候选重审 {_semantic_attempt + 1}/{semantic_retries}: "
                    + "；".join(contract_errors)
                )
                return review_chapter(
                    chapter_number,
                    chapter_file_override,
                    review_file_override,
                    _semantic_attempt=_semantic_attempt + 1,
                    _semantic_errors=contract_errors,
                )
            review_data["reported_overall_score"] = review_data.get(
                "reported_overall_score",
                review_data.get("overall_score"),
            )
            # M3 等模型偶发输出字段不全（edit 锚点不命中原文、漏 suggestions/weaknesses 等）
            # 触发契约失败，但其给出的分数仍有效。salvage 真实分数，避免整候选作废、压低通过率。
            # 本地硬门禁失败时镜像正常路径降分到 min_score-0.1，防止带硬伤的章节因 salvage 误过。
            _reported = review_data.get("reported_overall_score")
            if isinstance(_reported, (int, float)) and 0 <= float(_reported) <= 10:
                _salvaged = round(float(_reported), 2)
                if hard_gate_reasons and _salvaged >= review_min_score:
                    _salvaged = round(review_min_score - 0.1, 2)
                review_data["overall_score"] = _salvaged
                review_data["status"] = "invalid_review_salvaged"
            else:
                review_data["overall_score"] = None
                review_data["status"] = "invalid_review"
            review_data["verdict"] = "需修改"
            review_data["review_contract_errors"] = contract_errors
    except Exception as e:
        log(f"[Reviewer] JSON解析失败: {e}")
        semantic_retries = max(
            0,
            int(CONFIG.get("reviewer", {}).get("semantic_retries", 1) or 0),
        )
        if _semantic_attempt < semantic_retries:
            log(
                f"[Reviewer] 第{chapter_number}章审查响应解析失败，"
                f"原候选重审 {_semantic_attempt + 1}/{semantic_retries}"
            )
            return review_chapter(
                chapter_number,
                chapter_file_override,
                review_file_override,
                _semantic_attempt=_semantic_attempt + 1,
                _semantic_errors=["响应不是可解析的完整 JSON 对象"],
            )
        review_data = _partial_review_from_raw(chapter_number, content, local_analysis)

    review_file.parent.mkdir(parents=True, exist_ok=True)
    with open(review_file, "w", encoding="utf-8") as f:
        json.dump(review_data, f, ensure_ascii=False, indent=2)

    overall = review_data.get("overall_score", "N/A")
    verdict = review_data.get("verdict", "N/A")
    edits = review_data.get("edits", [])
    edit_count = len(edits) if isinstance(edits, list) else 0
    log(f"[Reviewer] 第{chapter_number}章审查完成，评分: {overall}， verdict: {verdict}， edits: {edit_count}")
    return review_data


def _refresh_status(start: int, end: int) -> None:
    """扫描处理过的章节，增量更新 chapter_status.json 和 progress.json"""
    try:
        log(f"[Reviewer] 刷新状态文件 ({start}-{end})...")
        statuses = scan_chapter_status(NOVELS_DIR, start, end, use_cache=False)
        write_status_file(NOVELS_DIR, statuses.values())

        progress_file = report_path(NOVELS_DIR, "progress.json")
        if progress_file.exists():
            with open(progress_file, "r", encoding="utf-8") as f:
                progress = json.load(f)
        else:
            progress = {
                "planner_done": True,
                "last_generated_chapter": 0,
                "last_reviewed_chapter": 0,
                "failed_chapters": [],
            }

        all_statuses = scan_chapter_status(NOVELS_DIR, 1, CONFIG["total_chapters"], use_cache=False)
        progress["last_reviewed_chapter"] = highest_contiguous(all_statuses, 1, "review_ok")
        progress_file.parent.mkdir(parents=True, exist_ok=True)
        with open(progress_file, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)

        log(f"[Reviewer] 状态刷新完成，last_reviewed_chapter={progress['last_reviewed_chapter']}")
    except Exception as e:
        log(f"[Reviewer] 状态刷新失败: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-p", type=str,
                        default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录（默认从环境变量 NOVEL_PROJECT_DIR 读取）")
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=10, help="结束章节")
    parser.add_argument("--chapter", type=int, default=0, help="只审查某一章")
    parser.add_argument("--final", action="store_true", help="审查终稿（final 目录）而非草稿")
    parser.add_argument("--chapter-file", type=str, default="", help="候选模式：审查指定正文文件")
    parser.add_argument("--review-file", type=str, default="", help="候选模式：审查报告写入指定文件")
    args = parser.parse_args()

    try:
        project = resolve_project_dir(args.project)
    except ValueError as exc:
        print(f"错误: {exc}")
        sys.exit(1)

    init_project(project)

    global CHAPTERS_DIR, REVIEWS_DIR
    if args.final:
        CHAPTERS_DIR = NOVELS_DIR / "chapters" / "final"
        REVIEWS_DIR = NOVELS_DIR / "chapters" / "review_final"

    print("=" * 60)
    print("Reviewer Agent 启动")
    print(f"项目: {NOVELS_DIR}")
    if args.final:
        print("模式: 审查终稿")
    print("=" * 60)

    CHAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    total = 1 if args.chapter > 0 else (args.end - args.start + 1)
    failed = 0
    if args.chapter > 0:
        result = review_chapter(
            args.chapter,
            chapter_file_override=args.chapter_file or None,
            review_file_override=args.review_file or None,
        )
        if result.get("status") in ("failed", "no_file", "parse_error"):
            failed += 1
        if not (args.chapter_file or args.review_file):
            _refresh_status(args.chapter, args.chapter)
    else:
        for ch in range(args.start, args.end + 1):
            result = review_chapter(ch)
            if result.get("status") in ("failed", "no_file", "parse_error"):
                failed += 1
            time.sleep(1)
        _refresh_status(args.start, args.end)

    # 获取书名并发送推送
    title = "本小说"
    world_file = NOVELS_DIR / "world.json"
    if world_file.exists():
        try:
            with open(world_file, "r", encoding="utf-8") as f:
                title = json.load(f).get("title", title)
        except Exception:
            pass

    start_ch = args.chapter if args.chapter > 0 else args.start
    end_ch = args.chapter if args.chapter > 0 else args.end
    # 微信推送已禁用，改由 coordinator 统一推送进度
    # push_stage_complete(
    #     config=CONFIG,
    #     title=title,
    #     stage="审查",
    #     start_chapter=start_ch,
    #     end_chapter=end_ch,
    #     processed=total - failed,
    #     failed=failed,
    # )
    log(f"[Reviewer] 完成 {total - failed} 章，失败 {failed} 章")

    log("[Reviewer] 全部完成")


if __name__ == "__main__":
    main()
