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
from core.json_repair import parse_score as _parse_score, strip_json_markdown as _strip_json_markdown
from core.review_ai_client import ReviewAIError, call_review_ai
from core.ai_flavor_detector import detect_ai_flavor, evaluate_ai_gate
from core.review_quality import (evaluate_review_quality_gate, review_quality_settings,
                                content_sha256, read_manuscript, review_matches_text)
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

    仅供诊断。关键词命中不能证明人情味，未命中也不能作为拒绝理由。
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


def detect_scene_realization(
    chapter_content: str,
    chapter_outline: dict,
    *,
    min_rate: float = 0.75,
) -> dict:
    """本地检测正文是否兑现大纲 scenes 的感官锚点、潜台词和出口钩子。

    仅供诊断，不按该比例降分或触发改写。全章关键词匹配不代表逐场语义兑现。
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
        "min_rate": min_rate,
        "needs_attention": rate < min_rate,
    }


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def call_llm(system_prompt: str, user_prompt: str, max_tokens: int = 4096, temperature: float = 0.3) -> str:
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


def _limited_item_list(value, limit: int) -> list:
    if not isinstance(value, list):
        return []
    return [item for item in value if item is not None][:limit]


def _normalize_review_tasks(review_data: dict, max_repair_tasks: int) -> None:
    review_data["strengths"] = _limited_item_list(review_data.get("strengths"), 2)
    for field in ("weaknesses", "suggestions", "continuity_issues", "edits"):
        review_data[field] = _limited_item_list(review_data.get(field), max_repair_tasks)
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


def _build_repair_tasks(review_data: dict, gate_reasons: list[str], limit: int) -> list[str]:
    """将审稿意见和确定性门禁原因整理为 Writer 可执行的有限任务集。"""
    tasks: list[str] = []
    for field in ("suggestions", "continuity_issues", "weaknesses"):
        values = review_data.get(field)
        if not isinstance(values, list):
            continue
        for value in values:
            text = str(value or "").strip()
            if text and text not in tasks:
                tasks.append(text)
    for reason in gate_reasons:
        text = str(reason or "").strip()
        if text and text not in tasks:
            tasks.append(text)
    return tasks[:limit]


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
    force: bool = False,
) -> dict:
    chapter_file = Path(chapter_file_override) if chapter_file_override else CHAPTERS_DIR / f"chapter_{chapter_number:04d}.txt"
    review_file = Path(review_file_override) if review_file_override else REVIEWS_DIR / f"chapter_{chapter_number:04d}_review.json"

    if not chapter_file.exists():
        log(f"[Reviewer] 第{chapter_number}章文件不存在")
        return {"status": "no_file"}

    chapter_content = read_manuscript(chapter_file)
    snapshot_sha256 = content_sha256(chapter_content)
    if review_file.exists() and not force:
        configured_min_score = review_quality_settings(CONFIG)["review_min_score"]
        _, status, _, ok = load_review_status(review_file, configured_min_score)
        cached = load_json(review_file)
        if (ok and cached.get("chapter_number") == chapter_number
                and review_matches_text(cached, chapter_content)
                and evaluate_review_quality_gate(cached, cached.get("local_analysis", {}), CONFIG)["passed"]):
            log(f"[Reviewer] 第{chapter_number}章已有有效审查报告，跳过")
            with open(review_file, "r", encoding="utf-8") as f:
                return json.load(f)
        log(f"[Reviewer] 第{chapter_number}章审查报告无效（{status}），重新审查")

    local_analysis = analyze_chapter_text(chapter_content)
    try:
        ai_flavor = detect_ai_flavor(chapter_content, project=NOVELS_DIR)
    except Exception as exc:
        ai_flavor = {"error": str(exc)}
    if not isinstance(ai_flavor, dict):
        ai_flavor = {"error": "AI检测器未返回有效对象"}
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
    review_settings = review_quality_settings(CONFIG)
    local_analysis["scene_realization_detection"] = detect_scene_realization(
        chapter_content,
        chapter_outline,
        min_rate=review_settings["scene_realization_min_rate"],
    )

    # Evidence must come from the full chapter; samples can hide a setup or payoff.
    content_sample = chapter_content
    for key in ("human_warmth_detection", "origin_fact_reference_detection",
                "relationship_obligation_detection", "scene_realization_detection"):
        local_analysis[key]["advisory_only"] = True
    local_analysis["ai_flavor_detection"]["advisory_only"] = False

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
    review_min_score = review_settings["review_min_score"]
    max_repair_tasks = review_settings["max_repair_tasks"]
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
            "continuity_issues、summary、edits。未通过时必须给出可定位的 edits。\n"
        )

    system = f"""你是一位中文小说编辑，审查《{book_title}》的正文。
{genre_text}
{retry_contract}
根据本章承担的功能评价因果、人物、语言和阅读体验；日常、过渡、安静收尾都可获得高分。
不要把悬念密度、反转、打脸、性格反差、潜台词或物象数量当作所有章节的配额。
评分描述当前文本的完成度，不是 AI 来源概率。保留作者口吻和有意留白。
10分为完成度极高，9分为完成度高且表达鲜明，8分为基本有效但有具体可改进问题，
7分及以下须有影响理解、可信度或阅读的实际问题。不得仅因没有强钩子、生活词汇或微表情扣分。
本地词频、句式、关系和场景匹配均为弱线索；必须读上下文验证，不能直接作为失败理由。
只指出有文本证据的最关键问题；不存在问题时允许空数组，不为了凑9分虚构不足。
只输出紧凑的合法 JSON，不要 Markdown。"""

    prompt = f"""审查第{chapter_number}章。保持现有评分字段用于报告兼容，但按章节功能理解它们：
hook_strength 评价结尾是否合适，suspense_density 评价信息安排是否合适，
payoff_intensity 评价本章的阅读收获；安静的认识变化、后果消化也算收获。
不适用的维度不应因缺少相应桥段扣分。

## 本章大纲（核心事件与事实需要遵守，修辞和感官提示允许不同表达）
{json.dumps(chapter_outline, ensure_ascii=False, indent=2) if chapter_outline else '未找到大纲'}
## 角色设定
{json.dumps(characters, ensure_ascii=False, indent=2)}
## origin 原始参考素材
{ORIGIN_MATERIALS or '（无）'}
## 正文全文
{content_sample}
## 本地检查
{json.dumps(local_analysis, ensure_ascii=False, indent=2)}

重点检查：人物行为与认知依据、核心事件因果、时间/地点/伤势/资源连续性、
设定冲突、严重重复和妨碍理解的语句。风格意见须引用具体原文并说明实际阅读影响。
缺少某个词、某种物件、反转或潜台词不能证明缺少人情味或场景。
字数范围 {min_words}-{max_words}；角色实际缺失和字数违规属于本地硬检查。
总分低于 {review_min_score} 或有实际硬伤时给“需修改”，并定位需要修订的补丁；
达标且无硬伤时给“通过”。修改应解决具体问题，不追求统一的“更生动、更紧张”。
edits、weaknesses、suggestions、continuity_issues 最多 {max_repair_tasks} 条，strengths 最多两条，可以为空；每条编辑引用原稿唯一的 old/after 锚点。

输出格式：
{{
  "chapter_number": {chapter_number},
  "overall_score": 8.5,
  "verdict": "通过/需修改",
  "scores": {{
    "writing_quality": 8.5, "plot_coherence": 8.5, "character_consistency": 8.5,
    "character_voice": 8.5, "causal_logic": 8.5, "scene_description": 8.5,
    "dialogue_quality": 8.5, "outline_adherence": 8.5, "pacing": 8.5,
    "emotional_impact": 8.5, "human_warmth": 8.5, "hook_strength": 8.5,
    "suspense_density": 8.5, "information_freshness": 8.5, "content_richness": 8.5,
    "anti_cliche": 8.5, "payoff_intensity": 8.5, "read_desire": 8.5,
    "ai_flavor": 8.5, "world_consistency": 8.5
  }},
  "word_count_check": {{"actual": {len(chapter_content)}, "target": {min_words}, "status": "达标/偏短/偏长"}},
  "primary_issue": "问题或空字符串", "primary_suggestion": "建议或空字符串",
  "strengths": [], "weaknesses": [], "suggestions": [], "continuity_issues": [],
  "summary": "基于本章功能的简短评价",
  "edits": []
}}
数值仅示意，请独立评分。若需修订，将 edits 填为下列一种：
{{"type":"replace","old":"精确原文","new":"修正文本"}}，
{{"type":"insert","after":"精确原文锚点","text":"新增文本"}}，
{{"type":"delete","old":"精确原文"}}。
若无法从全文确认问题，不应给出编辑。"""

    log(f"[Reviewer] 正在审查第{chapter_number}章...")
    start_time = time.time()
    try:
        content = call_llm(system, prompt, max_tokens=4096, temperature=0.3)
    except Exception as exc:
        log(f"[Reviewer] 审查调用失败：{exc}")
        content = ""
    elapsed = time.time() - start_time
    log(f"[Reviewer] 第{chapter_number}章审查 API 调用耗时 {elapsed:.1f}s")

    if not content:
        log(f"[Reviewer] 第{chapter_number}章审查失败")
        review_data = {
            "chapter_number": chapter_number,
            "status": "failed",
            "content_sha256": snapshot_sha256,
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
        _normalize_review_tasks(review_data, max_repair_tasks)

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
        absent = (local_analysis.get("character_presence") or {}).get("absent")
        if isinstance(absent, list) and absent:
            hard_gate_reasons.append("大纲要求角色在正文缺失：" + "、".join(str(item) for item in absent[:3]))
        hard_gate_reasons.extend(evaluate_ai_gate(ai_flavor)["reasons"])

        quality_gate = evaluate_review_quality_gate(
            review_data,
            local_analysis,
            CONFIG,
            base_reasons=hard_gate_reasons,
        )
        if isinstance(score, (int, float)):
            if not quality_gate["passed"] or verdict in {"需修改", "需重写"}:
                review_data["reported_overall_score"] = score
                review_data["overall_score"] = min(score, round(review_min_score - 0.1, 2))
                score = review_data["overall_score"]
                review_data["verdict"] = "需修改"
                verdict = "需修改"
            elif verdict == "通过":
                if score < review_min_score:
                    review_data["verdict"] = "需修改"
                    verdict = "需修改"

        if not quality_gate["passed"]:
            weaknesses = review_data.get("weaknesses")
            if isinstance(weaknesses, list):
                for reason in quality_gate["reasons"]:
                    if reason not in weaknesses:
                        weaknesses.append(reason)
                review_data["weaknesses"] = weaknesses[:max_repair_tasks]

        score = review_data.get("overall_score")
        if isinstance(score, (int, float)) and score < review_min_score:
            weaknesses = review_data.get("weaknesses")
            if not isinstance(weaknesses, list) or not weaknesses:
                contract_errors.append("未达通过阈值但未说明具体 weaknesses")

        edits = review_data.get("edits")
        if verdict != "通过":
            if not isinstance(review_data.get("suggestions"), list) or not review_data["suggestions"]:
                contract_errors.append("未通过但没有可执行 suggestions")
            if not isinstance(edits, list) or not edits:
                contract_errors.append("未通过但没有可定位 edits")

        if isinstance(edits, list):
            review_data["edits"] = edits[:max_repair_tasks]
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
            quality_gate["passed"] = False
            quality_gate["reasons"] = list(dict.fromkeys([
                *quality_gate["reasons"],
                *(f"审稿报告契约错误：{item}" for item in contract_errors),
            ]))
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
                    force=True,
                )
            review_data["reported_overall_score"] = review_data.get(
                "reported_overall_score",
                review_data.get("overall_score"),
            )
            # 契约不完整的审稿不能作为质量依据，保留 reported 分数仅用于排障。
            review_data["overall_score"] = None
            review_data["status"] = "invalid_review"
            review_data["verdict"] = "需修改"
            review_data["review_contract_errors"] = contract_errors
        review_data["quality_gate"] = quality_gate
        review_data["repair_tasks"] = _build_repair_tasks(
            review_data,
            quality_gate["reasons"],
            max_repair_tasks,
        )
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
                force=True,
            )
        review_data = _partial_review_from_raw(chapter_number, content, local_analysis)

    review_data["content_sha256"] = snapshot_sha256
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
    parser.add_argument("--force", action="store_true", help="强制重审当前正文，禁止复用缓存")
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
            force=args.force,
        )
        if result.get("status") != "completed":
            failed += 1
        if not (args.chapter_file or args.review_file):
            _refresh_status(args.chapter, args.chapter)
    else:
        for ch in range(args.start, args.end + 1):
            result = review_chapter(ch, force=args.force)
            if result.get("status") != "completed":
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
        except Exception as exc:
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
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
