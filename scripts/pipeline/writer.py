#!/usr/bin/env python3
"""
Writer Agent - 内容生成Agent
负责根据大纲生成具体章节内容，保存为txt文件
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
from typing import Any

from core.llm_client import LLMError, call_llm as _call_section_llm
from core.json_repair import repair_latin1_gbk_mojibake as _repair_latin1_gbk_mojibake
from core.novel_config import (
    configure_stdio,
    load_config,
    load_origin_materials,
    resolve_project_dir,
)
from core.workflow_state import load_outline_chapter, review_dir
from core.workflow_state import FORBIDDEN_PHRASES, VALID_ENDINGS, is_valid_chapter_text, read_text_length
from core.review_quality import review_quality_settings
from core.edit_diff import (
    EditApplyError,
    apply_reviewed_edits,
    build_edit_prompt,
    parse_edit_ops,
)

configure_stdio()

NOVELS_DIR = None
CHAPTERS_DIR = None
WORLD_FILE = None
CHARACTERS_FILE = None
LOG_FILE = None
CONFIG = None
ORIGIN_MATERIALS = ""
CANDIDATE_MODE = False
NOVEL_PREMISE = ""


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, CHAPTERS_DIR, WORLD_FILE, CHARACTERS_FILE, LOG_FILE, CONFIG, ORIGIN_MATERIALS, NOVEL_PREMISE
    NOVELS_DIR = Path(project_dir).resolve()
    CHAPTERS_DIR = NOVELS_DIR / "chapters" / "draft"
    WORLD_FILE = NOVELS_DIR / "world.json"
    CHARACTERS_FILE = NOVELS_DIR / "characters.json"
    LOG_FILE = NOVELS_DIR / "logs" / "writer.log"
    CONFIG = load_config(NOVELS_DIR)
    premise_file = NOVELS_DIR / "premise.txt"
    NOVEL_PREMISE = premise_file.read_text(encoding="utf-8") if premise_file.exists() else ""
    # 限制 origin 素材长度，避免 prompt 过长导致 API 超时（reviewer/outline_reviewer 都有此限制）
    origin_max = int(CONFIG.get("writer", {}).get("origin_max_chars", 1200) or 1200)
    ORIGIN_MATERIALS = load_origin_materials(NOVELS_DIR, max_chars=origin_max)


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def call_llm(system_prompt: str, user_prompt: str, max_tokens: int = 8192, temperature: float = 0.7) -> str:
    try:
        writer_cfg = CONFIG.get("writer", {})
        return _call_section_llm(
            CONFIG,
            NOVELS_DIR,
            "writer",
            system_prompt,
            user_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            raw_name="writer",
            retries=int(writer_cfg.get("candidate_retries", 0) if CANDIDATE_MODE else writer_cfg.get("max_retries", 3)),
            retry_delay=float(writer_cfg.get("retry_delay", 5.0)),
            timeout=int(writer_cfg.get("candidate_timeout_seconds", 240) if CANDIDATE_MODE else writer_cfg.get("timeout_seconds", 300)),
        )
    except LLMError as e:
        log(f"[ERROR] LLM调用失败: {e}")
        return ""


def load_json(filepath: Path) -> dict:
    if not filepath.exists():
        return {}
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


_GENRE_KEYWORDS = {
    "都市悬疑/社会派": ["悬疑", "案件", "调查", "记者", "证据", "真相", "追踪", "警方", "犯罪", "谜", "反转", "都市", "档案", "举报", "旧案"],
    "东方玄幻/仙侠": ["修仙", "武道", "真气", "灵气", "境界", "斗气", "魔法", "飞升", "宗门", "法宝", "神通", "筑基", "金丹", "元婴", "渡劫", "仙人", "神魔", "妖兽", "灵根", "天劫", "修炼", "炼气", "悟道", "儒道", "剑修", "魔教", "仙宫", "圣地"],
    "都市重生/职场": ["重生", "都市", "现代", "职场", "校园", "商战", "创业", "房价", "互联网", "移动互联网", "智能手机", "时代", "金钱", "银行卡", "股票", "投资", "公司", "上班", "打工", "商业", "电商", "地产", "金融", "中年", "青年", "生活", "婚姻", "家庭"],
    "灵异恐怖": ["鬼", "灵异", "恐怖", "诡异", "尸体", "死亡", "诅咒", "惊悚", "阴间", "黄泉", "冥界", "怨灵", "厉鬼", "驱鬼", "驭鬼", "复苏", "僵尸", "邪祟", "阴气", "灵魂"],
    "科幻未来": ["星际", "飞船", "机甲", "基因", "未来", "太空", "人工智能", "AI", "机器人", "量子", "宇宙", "星球", "外星", "末世", "丧尸", "核战", "科技"],
    "历史架空": ["古代", "王朝", "皇帝", "科举", "诸侯", "架空", "宫廷", "权谋", "宦官", "将士", "兵马", "江山", "天下", "登基", "丞相", "郡主", "王爷"],
}


def _infer_genre(world: dict) -> str:
    """根据 world.json 内容推断题材类型，返回中文题材描述。"""
    text = world.get("world_description", "") + " " + world.get("title", "") + " " + str(world.get("power_system", {}))
    scores = {}
    for genre, keywords in _GENRE_KEYWORDS.items():
        score = sum(text.count(kw) for kw in keywords)
        scores[genre] = score
    if scores:
        best = max(scores, key=scores.get)
        if scores[best] > 0:
            return best
    return "网络小说"


def _character_brief(item: dict) -> str:
    name = str(item.get("name", "")).strip()
    if not name:
        return ""
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
    ):
        value = item.get(key)
        if isinstance(value, (list, tuple)):
            value = "、".join(str(v) for v in value[:3])
        elif isinstance(value, dict):
            value = "；".join(f"{k}:{v}" for k, v in list(value.items())[:3])
        text = str(value or "").strip()
        if text:
            fields.append(text[:140] if key == "life_profile" else text[:80])
    return f"{name}：{'；'.join(fields)[:360]}" if fields else name


def _collect_character_briefs(value, *, limit: int = 10) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    def walk(item) -> None:
        if len(result) >= limit:
            return
        if isinstance(item, dict):
            brief = _character_brief(item)
            name = str(item.get("name", "")).strip()
            if brief and name and name not in seen:
                seen.add(name)
                result.append(brief)
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return result


def normalize_outline_text(value: Any) -> Any:
    if isinstance(value, str):
        return _repair_latin1_gbk_mojibake(value)
    if isinstance(value, list):
        return [normalize_outline_text(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_outline_text(item) for key, item in value.items()}
    return value


def _writer_quality_contract() -> str:
    quality = CONFIG.get("quality", {})
    min_words = int(quality.get("min_chapter_words", 5000))
    max_words = int(quality.get("max_chapter_words", 12000))
    return f"""## 写作约束
- 保持已确立的事实、人物动机、时间、位置、伤势、资源和信息来源一致。
- 叙述贴合当前视角；角色只能依据其已知信息行动，不能将世界真相直接当作角色认知。
- 完成本章既定核心事件和结尾状态，具体表达、段落节奏与细节由场景决定。
- 按本章功能安排张弛。日常、过渡、消化后果、安静结束都可以成立；不要求每章升级赌注、反转或强钩子。
- 人物可以直接说话，也可以回避、误解或沉默；不要求每段对白有潜台词、每个角色有反常反应。
- 只在场景需要时写生活细节、心理、物件和感官；不按词表或数量补齐，不反复用同一种动作代替情绪。
- 给读者推断的空间，避免重复解释已经可见的信息。允许适量概述、自然长短句和不同结束方式。
- 总字数范围为 {min_words}-{max_words}。不得用重复描写、抽象感悟或套话凑字数。
- 尊重 origin 中的事实与风格约束；参考素材不必逐条在本章复述。"""


def _load_9star_examples(chapter_number: int, min_score: float = 9.0, max_examples: int = 2) -> str:
    """加载此前评分 >= min_score 的章节作为9分范本参考。"""
    if not CONFIG.get("writer", {}).get("use_scored_examples", False):
        return ""
    rd = review_dir(NOVELS_DIR)
    if not rd.exists():
        return ""
    examples = []
    for f in sorted(rd.glob("chapter_*_review.json")):
        try:
            data = json.loads(f.read_text("utf-8"))
            score = float(data.get("overall_score", 0))
        except Exception as exc:
            continue
        if score < min_score:
            continue
        m = re.search(r"chapter_(\d+)_review", f.name)
        if not m:
            continue
        ex_num = int(m.group(1))
        if ex_num >= chapter_number:
            continue
        final_file = NOVELS_DIR / "chapters" / "final" / f"chapter_{ex_num:04d}.txt"
        draft_file = NOVELS_DIR / "chapters" / "draft" / f"chapter_{ex_num:04d}.txt"
        src = final_file if final_file.exists() else draft_file
        if not src.exists():
            continue
        text = src.read_text("utf-8")
        head = text[:1200]
        tail = text[-600:] if len(text) > 1800 else ""
        snippet = head + ("\n...\n" if tail else "") + tail
        examples.append((ex_num, score, snippet))
        if len(examples) >= max_examples:
            break
    if not examples:
        return ""
    parts = ["## 9分神作范本参考（仅学习其节奏、悬念与情感写法，不得抄袭剧情）\n"]
    for num, score, snippet in examples:
        parts.append(f"### 第{num}章（评分 {score} 分）节选\n{snippet}\n")
    return "\n".join(parts)


def _load_feedback_as_review(feedback_file: Path, chapter_number: int) -> dict:
    try:
        payload = load_json(feedback_file)
    except Exception as exc:
        return {}
    item = payload.get(str(chapter_number), payload) if isinstance(payload, dict) else {}
    if not isinstance(item, dict):
        return {}
    reviews = item.get("reviews", [])
    analysis = item.get("failure_analysis", {})
    max_repair_tasks = review_quality_settings(CONFIG)["max_repair_tasks"]
    selected: dict = {}
    candidates: list[tuple[int, float, dict]] = []
    if isinstance(reviews, list):
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
            candidates.append((index, score, review))
    if candidates:
        # 反馈必须以最近一次有效审稿为准，不能被早期偶然高分掩盖。
        selected = candidates[-1][2]
    elif isinstance(reviews, list):
        selected = next((review for review in reversed(reviews) if isinstance(review, dict)), {})
    if not isinstance(analysis, dict):
        analysis = {}
    def unique_items(*values) -> list:
        items: list = []
        for value in values:
            if not isinstance(value, list):
                continue
            for item_value in value:
                text = str(item_value or "").strip()
                if text and text not in items:
                    items.append(text)
        return items[:max_repair_tasks]

    repair_tasks = unique_items(
        analysis.get("targeted_repairs", []),
        selected.get("repair_tasks", []),
        analysis.get("adjustments", []),
        selected.get("suggestions", []),
        selected.get("weaknesses", []),
    )

    return {
        "status": "completed",
        "verdict": "需重写",
        "overall_score": analysis.get("best_score", selected.get("overall_score", 0)),
        "strengths": unique_items(selected.get("strengths", [])),
        "weaknesses": unique_items(analysis.get("likely_reasons", []), selected.get("weaknesses", [])),
        "suggestions": repair_tasks,
        "continuity_issues": unique_items(selected.get("continuity_issues", [])),
        "repair_tasks": repair_tasks,
        "summary": selected.get("summary", ""),
        "edits": selected.get("edits", [])[:max_repair_tasks] if isinstance(selected.get("edits"), list) else [],
        "local_analysis": selected.get("local_analysis", {}),
        "raw_response": selected.get("raw_response", ""),
        "candidate_file": selected.get("candidate_file", ""),
    }


def _feedback_base_text_path(review_data: dict, chapter_number: int) -> Path:
    official = CHAPTERS_DIR / f"chapter_{chapter_number:04d}.txt"
    candidate_value = str(review_data.get("candidate_file", "") or "").strip()
    if not candidate_value:
        return official
    try:
        candidate = Path(candidate_value).resolve()
        project = NOVELS_DIR.resolve()
        if candidate.is_file() and candidate.is_relative_to(project):
            return candidate
    except (OSError, RuntimeError, ValueError):
        pass
    return official


def _auto_compress(content: str, chapter_number: int, max_words: int, target_min: int, chapter_outline: dict) -> str:
    """如果章节超过 max_words，自动调用压缩 agent 精简内容。"""
    word_count = len(content)
    if word_count <= max_words:
        return content

    key_events = chapter_outline.get("key_events", [])
    if isinstance(key_events, str):
        key_events = [key_events]
    key_events_text = "\n".join(f"{i+1}. {str(ev)}" for i, ev in enumerate(key_events) if str(ev).strip())
    chapter_hook = str(chapter_outline.get("chapter_hook", "")).strip()

    compress_system = """你是一位资深小说编辑，专门负责将超过字数限制的章节压缩到合格范围。
你的任务是删减冗余，保留精华。禁止改变核心剧情、禁止删除关键事件、禁止削弱章末钩子。
你尤其擅长：删除重复描写、压缩过长的测试/列举/解释段落、把学术论文式对话改写成紧张的对峙。"""

    target = min(max(target_min + 1000, 7000), max_words - 500)
    for attempt in range(3):
        current_count = len(content)
        if current_count <= max_words:
            break

        compress_prompt = f"""以下第{chapter_number}章字数过多（{current_count}字），需要压缩到 {target} 字左右（绝对不要超过 {max_words} 字）。

## 本章必须保留的关键事件
{key_events_text}

## 本章章末钩子（最后200字必须保留）
{chapter_hook}

## 压缩原则
1. 保留所有关键事件和情绪转折点，不能省略大纲规定的任何事件。
2. 删除重复的心理描写、重复的环境渲染、重复的身体感受。
3. 对于AI测试/挑战/列举类场景，最多展示3-4个具体例子，其余用"后面还有数十道类似的题目"等方式概括。
4. 删除大段技术参数、算法解释、设定说明，只保留对情节至关重要的信息。
5. 保留所有对话中的潜台词和情感张力，但删除解释性插话。
6. 章末钩子最后200字必须完整保留，不能削弱。
7. 压缩后仍然是流畅的小说正文，不是大纲或摘要。

请直接输出压缩后的正文，不要输出任何解释、分析或元信息：

{content}"""

        log(f"[Writer] 第{chapter_number}章字数过多（{current_count}字），启动第{attempt+1}次压缩...")
        compressed = call_llm(compress_system, compress_prompt, max_tokens=8192, temperature=0.3)
        if compressed:
            compressed = compressed.strip()
            if compressed.startswith("```"):
                lines = compressed.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                compressed = "\n".join(lines).strip()
            if len(compressed) >= target_min and len(compressed) <= max_words:
                log(f"[Writer] 第{chapter_number}章压缩成功（{len(compressed)}字）")
                return compressed
            if len(compressed) < len(content):
                content = compressed
                log(f"[Writer] 第{chapter_number}章压缩后仍为{len(content)}字，继续压缩...")
            else:
                log(f"[Writer] 第{chapter_number}章压缩未减少字数，停止压缩")
                break
        else:
            log(f"[Writer] 第{chapter_number}章压缩调用失败")
            break

    return content


def _apply_incremental_edits(
    chapter_number: int,
    old_text: str,
    review_data: dict,
    min_words: int,
    max_words: int,
) -> str | None:
    """Only apply bounded patches inside reviewed regions; never regenerate on failure."""
    system, prompt = build_edit_prompt(old_text, review_data, chapter_number, min_words, max_words)
    try:
        raw = call_llm(system, prompt, max_tokens=8192, temperature=0.3)
        edits = parse_edit_ops(raw)
        return apply_reviewed_edits(
            old_text, edits, review_data.get("edits", []),
            max_changed_ratio=float(CONFIG.get("revision", {}).get("max_changed_ratio", 0.15)),
            min_words=min_words, max_words=max_words,
        )
    except (EditApplyError, ValueError, TypeError) as exc:
        log(f"[Writer] 第{chapter_number}章补丁未通过校验，保留原稿: {exc}")
        return None


def generate_chapter(
    chapter_number: int,
    retry: int = 0,
    output_file: str | Path | None = None,
    review_feedback: str | Path | None = None,
) -> str:
    chapter_file = Path(output_file) if output_file else CHAPTERS_DIR / f"chapter_{chapter_number:04d}.txt"

    review_file = Path(review_feedback) if review_feedback else review_dir(NOVELS_DIR) / f"chapter_{chapter_number:04d}_review.json"
    review_data = None
    is_rewrite = False
    if review_file.exists():
        try:
            if review_feedback:
                review_data = _load_feedback_as_review(review_file, chapter_number)
            else:
                review_data = load_json(review_file)
            status = review_data.get("status", "")
            verdict = review_data.get("verdict", "")
            score = review_data.get("overall_score", 10)
            try:
                score = float(score)
            except (TypeError, ValueError):
                score = 0.0
            min_review_score = review_quality_settings(CONFIG)["review_min_score"]
            quality_gate = review_data.get("quality_gate")
            quality_gate_passed = isinstance(quality_gate, dict) and quality_gate.get("passed") is True
            if (
                status != "completed"
                or verdict in {"需重写", "需修改"}
                or score < min_review_score
                or not quality_gate_passed
            ):
                is_rewrite = True
                log(
                    f"[Writer] 第{chapter_number}章检测到未通过审查报告"
                    f"（status:{status}，评分{score}，verdict:{verdict}，quality_gate:{quality_gate_passed}），"
                    "将基于建议重写"
                )
        except Exception as exc:
            pass

    exists, existing_words, existing_ok = read_text_length(chapter_file)
    if exists and existing_ok and not is_rewrite:
        log(f"[Writer] 第{chapter_number}章已存在且字数合格（{existing_words}字），跳过")
        return "exists"
    if exists and is_rewrite:
        log(f"[Writer] 第{chapter_number}章已有初稿但审查未通过，将基于审查意见修订原稿")
    elif exists:
        log(f"[Writer] 第{chapter_number}章已存在但字数不合格（{existing_words}字），重新生成")

    if is_rewrite and not chapter_file.exists():
        log(f"[Writer] 第{chapter_number}章没有原稿，按新章生成")
        is_rewrite = False

    world = load_json(WORLD_FILE)
    characters = load_json(CHARACTERS_FILE)

    quality = CONFIG.get("quality", {})
    min_words = int(quality.get("min_chapter_words", 5000))
    max_words = int(quality.get("max_chapter_words", 12000))

    chapter_outline = normalize_outline_text(load_outline_chapter(NOVELS_DIR, chapter_number))

    if not chapter_outline:
        log(f"[Writer] 第{chapter_number}章大纲不存在")
        return "no_outline"

    prev_summary = normalize_outline_text(load_outline_chapter(NOVELS_DIR, chapter_number - 1)).get("summary", "")
    next_summary = normalize_outline_text(load_outline_chapter(NOVELS_DIR, chapter_number + 1)).get("summary", "")

    prev_ending = ""
    if chapter_number > 1:
        prev_file = CHAPTERS_DIR / f"chapter_{chapter_number-1:04d}.txt"
        if prev_file.exists():
            with open(prev_file, "r", encoding="utf-8") as f:
                content = f.read()
            prev_ending = content[-500:] if len(content) > 500 else content

    # 人物状态追踪：注入上一章结束时的角色状态快照，消除跨章矛盾
    character_state_directive = ""
    try:
        from core.character_state import format_state_for_prompt, latest_state_before
        prev_states = latest_state_before(NOVELS_DIR, chapter_number)
        character_state_directive = format_state_for_prompt(prev_states)
    except Exception as _cse:
        log(f"[Writer] 角色状态加载异常（忽略）: {_cse}")

    # 人物成长弧线追踪：注入主角当前弧线阶段，确保成长不倒退
    arc_state_directive = ""
    try:
        from core.arc_state import format_arc_for_prompt, latest_arc_before
        prev_arc = latest_arc_before(NOVELS_DIR, chapter_number)
        arc_state_directive = format_arc_for_prompt(prev_arc)
    except Exception as _ase:
        log(f"[Writer] 弧线状态加载异常（忽略）: {_ase}")

    # 关系欠账追踪：注入上一章结束时仍在发酵的人情债、误会、承诺和未说出口的话
    relationship_state_directive = ""
    relationship_obligation_directive = ""
    try:
        from core.relationship_state import (
            format_relationships_for_prompt,
            latest_relationships_before,
            relationship_obligation_for_prompt,
        )
        prev_relationships = latest_relationships_before(NOVELS_DIR, chapter_number)
        relationship_state_directive = format_relationships_for_prompt(prev_relationships)
        relationship_obligation_directive = relationship_obligation_for_prompt(prev_relationships)
    except Exception as _rse:
        log(f"[Writer] 关系欠账加载异常（忽略）: {_rse}")

    # 伏笔闭环：注入"本章应回收"的前期伏笔，强制在正文兑现 payoff（治伏笔悬空根因）
    foreshadowing_directive = ""
    try:
        from core.foreshadowing_ledger import rebuild_from_outlines, dangling_threads
        _ledger = rebuild_from_outlines(NOVELS_DIR)
        _due = [
            t for t in dangling_threads(_ledger, as_of_chapter=chapter_number)
            if int(t.get("must_resolve_by", 0) or 0) <= chapter_number
            and int(t.get("planted_at", 0) or 0) < chapter_number
        ]
        if _due:
            _lines = ["## 【本章必须回收的伏笔】（前期埋下、已到回收点，必须在正文兑现 payoff，不得继续悬空）"]
            for t in _due[:6]:
                _setup = str(t.get("setup", "")).strip().replace("\n", " ")[:120]
                _lines.append(f"- 第{t.get('planted_at')}章埋设：{_setup}")
            _lines.append(
                "请在正文自然兑现这些伏笔的 payoff（揭示/呼应/反转），让读者感到「原来如此」且符合前期铺垫；"
                "若无法在本章全部回收，至少兑现最关键的1-2条，其余明确为后续回收，绝不能无声丢弃。"
            )
            foreshadowing_directive = "\n".join(_lines) + "\n"
    except Exception as _fse:
        log(f"[Writer] 伏笔台账加载异常（忽略）: {_fse}")

    if is_rewrite:
        old_file = _feedback_base_text_path(review_data, chapter_number)
        if not old_file.exists() or not review_data.get("edits"):
            log(f"[Writer] 第{chapter_number}章缺少可定位审查意见，保留原稿等待重新审查")
            return "failed"
        with old_file.open("r", encoding="utf-8", newline="") as stream:
            old_content = stream.read()
        revised = _apply_incremental_edits(
            chapter_number, old_content, review_data, min_words, max_words
        )
        if revised is None:
            return "failed"
        # No cleanup, truncation, score-based rollback or extra polishing outside the patch.
        with chapter_file.open("w", encoding="utf-8", newline="") as stream:
            stream.write(revised)
        log(f"[Writer] 第{chapter_number}章局部补丁已保存（{len(revised)}字）")
        return "success"

    # 提取主角名和故事设定
    # 优先从 characters.json 的 protagonist.name 读取，其次从 premise.txt 提取
    protagonist_name = "主角"
    protagonist_data = characters.get("protagonist", {})
    if isinstance(protagonist_data, dict) and protagonist_data.get("name"):
        protagonist_name = protagonist_data["name"]
    else:
        premise_file = NOVELS_DIR / "premise.txt"
        if premise_file.exists():
            premise_text = premise_file.read_text(encoding="utf-8")[:2000]
            import re
            m = re.search(r"主角(\S+?)(?:本|是|穿越|重生|携带|得到|拥有|来到|乃|为)", premise_text)
            if m:
                protagonist_name = m.group(1)

    world_desc = world.get("world_description", "")[:300]
    power_system = world.get("power_system", {})
    power_system_desc = power_system.get("description", "")[:200]

    key_characters = _collect_character_briefs(characters, limit=10)
    if not key_characters:
        for f in world.get("factions", [])[:3]:
            fname = f.get("name", "")
            fdesc = f.get("description", "")
            if fname and fdesc:
                key_characters.append(f"{fname}：{fdesc[:80]}")

    key_chars_text = "\n".join(f"- {kc}" for kc in key_characters) if key_characters else "（暂无详细角色设定）"

    # G8: 注入语言指纹（文风锚定，防AI塑料感和风格漂移）
    lang_fp = characters.get("language_fingerprint", {})
    lang_fp_text = ""
    if lang_fp:
        parts = []
        if lang_fp.get("prose_style"):
            parts.append(f"文风基调：{lang_fp['prose_style']}")
        if lang_fp.get("signature_metaphors"):
            parts.append(f"标志性意象：{'、'.join(lang_fp['signature_metaphors'])}")
        if lang_fp.get("forbidden_expressions"):
            parts.append(f"避免表达：{'、'.join(lang_fp['forbidden_expressions'])}")
        if parts:
            lang_fp_text = "## 【语言指纹】（本章必须严格遵循以下文风，不得漂移）\n" + "\n".join(f"- {p}" for p in parts)

    system_label = "核心规则/能力体系" if power_system_desc else "核心规则/现实约束"
    story_context = f"""主角：{protagonist_name}
世界观：{world_desc[:200]}
{system_label}：{power_system_desc[:150] or "按世界观、题材和人物关系推进，不强加修炼或升级体系。"}
关键势力/角色：
{key_chars_text}"""

    # 9分范本参考：注入此前高分章节作为风格锚定（限制1篇避免 prompt 膨胀）
    examples_section = _load_9star_examples(chapter_number, max_examples=1)

    genre = _infer_genre(world)
    power_system = world.get("power_system", {})
    power_name = power_system.get("name", "")
    target_min = max(min_words + 500, 5500)

    # Keep scene facts separate from evaluative beat/imagery checklists.
    writer_outline = {
        key: chapter_outline.get(key)
        for key in ("title", "summary", "characters_involved", "location", "key_events",
                    "chapter_goal", "chapter_hook", "story_beat")
        if key in chapter_outline
    }
    writer_outline["scenes"] = [
        {key: scene.get(key, "") for key in ("position", "objective", "conflict")}
        for scene in chapter_outline.get("scenes", []) if isinstance(scene, dict)
    ]
    system = f"""你是一位中文小说作者，正在写{genre}小说。
以人物的具体处境、欲望和行动推进故事，保持本书的语言风格。
允许快慢、轻重和留白随场景变化，不把每一章都写成高潮。
直接输出完整正文，不输出分析、自检或评分。"""
    prompt = f"""请写第{chapter_number}章《{chapter_outline.get('title', '未命名')}》。

{_writer_quality_contract()}

## 本章事实与计划
{json.dumps(writer_outline, ensure_ascii=False, indent=2)}
chapter_hook 表示预定结束状态；保留其中的事实，表达可以自然收束，不需额外制造危机。
summary 是策划信息，不要将其中的心理结论和未来真相直接讲给读者。

## 故事背景（角色未知的内容不得泄露到其视角）
{story_context}

## origin 原始参考素材
{ORIGIN_MATERIALS or '（无）'}

## 前一章摘要与结尾
{prev_summary}
{prev_ending}
{character_state_directive}
{arc_state_directive}
{relationship_state_directive}
{relationship_obligation_directive}
{foreshadowing_directive}

## 后续接口（用于承接，不提前揭露）
{next_summary}

{lang_fp_text}
{examples_section}

场景细节从当前环境和人物行为生长出来；不需要复述关系台账或命中任何检测关键词。
请输出 {min_words}-{max_words} 字正文。"""

    log(f"[Writer] 正在生成第{chapter_number}章...")
    start_time = time.time()
    content = call_llm(system, prompt, max_tokens=8192, temperature=0.7)
    elapsed = time.time() - start_time
    log(f"[Writer] 第{chapter_number}章生成 API 调用耗时 {elapsed:.1f}s")

    if not content:
        log(f"[Writer] 第{chapter_number}章收到空响应")
        max_retry = CONFIG["writer"].get("max_retries", 5)
        if retry < max_retry:
            log(f"[Writer] 第{chapter_number}章生成失败，重试({retry+1}/{max_retry})...")
            time.sleep(CONFIG["writer"]["retry_delay"])
            return generate_chapter(chapter_number, retry + 1, output_file, review_feedback)
        log(f"[Writer] 第{chapter_number}章生成失败，已达最大重试次数")
        return "failed"

    content = content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        content = "\n".join(lines).strip()

    # 清理禁用短语
    for phrase in FORBIDDEN_PHRASES:
        content = content.replace(phrase, "")

    # 处理截断：如果文本没有以有效标点结尾，移除最后一个不完整的段落/句子
    if content and not content.endswith(VALID_ENDINGS):
        # 尝试找到最后一个完整段落结尾
        paragraphs = content.split("\n\n")
        if len(paragraphs) > 1 and not paragraphs[-1].strip().endswith(VALID_ENDINGS):
            content = "\n\n".join(paragraphs[:-1]).strip()
        # 如果还是不完整，找到最后一个有效标点位置
        if content and not content.endswith(VALID_ENDINGS):
            last_valid = max(
                (content.rfind(end) for end in VALID_ENDINGS if end in content),
                default=-1,
            )
            if last_valid > len(content) * 0.9:  # 只截掉最后不到10%的不完整内容
                content = content[: last_valid + 1].strip()

    # 自动压缩：如果超过 max_words，调用压缩 agent
    content = _auto_compress(content, chapter_number, max_words, target_min, chapter_outline)

    word_count = len(content)
    if not is_valid_chapter_text(content):
        log(f"[Writer] 第{chapter_number}章字数异常（{word_count}字），但仍保存")
    else:
        log(f"[Writer] 第{chapter_number}章字数合格（{word_count}字）")

    CHAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    with open(chapter_file, "w", encoding="utf-8") as f:
        f.write(content)

    log(f"[Writer] 第{chapter_number}章已保存 -> {chapter_file}")

    # 伏笔回收回验：本章大纲声称回收(claimed)的伏笔，正文是否真的兑现。
    # 通过 bigram 重叠粗判，通过则台账升 resolved，否则降回 planted 并打 unresolved_in_text。
    # 这是治"大纲写了[收]但正文没写"假回收的关键，纯本地计算不耗 LLM。
    try:
        from core.foreshadowing_ledger import load_ledger, save_ledger, verify_resolution_in_text
        _ledger = load_ledger(NOVELS_DIR)
        result = verify_resolution_in_text(_ledger, chapter_number, content)
        if result["changed"]:
            save_ledger(NOVELS_DIR, _ledger)
            if result["verified"]:
                log(f"[Writer] 第{chapter_number}章伏笔回验通过：{result['verified']}")
            if result["failed"]:
                log(
                    f"[Writer] ⚠️ 第{chapter_number}章伏笔回验失败（正文未兑现）：{result['failed']}，"
                    "台账已降回 planted 并标记 unresolved_in_text"
                )
    except Exception as _fre:
        log(f"[Writer] 伏笔回收回验异常（忽略）: {_fre}")

    return "success"


def main():
    global CANDIDATE_MODE
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-p", type=str,
                        default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录（默认从环境变量 NOVEL_PROJECT_DIR 读取）")
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=10, help="结束章节")
    parser.add_argument("--chapter", type=int, default=0, help="只生成某一章")
    parser.add_argument("--output-file", type=str, default="", help="候选模式：写入指定初稿文件，而非正式 draft 目录")
    parser.add_argument("--review-feedback", type=str, default="", help="重写时读取汇总审查反馈")
    args = parser.parse_args()
    CANDIDATE_MODE = bool(args.output_file)

    try:
        project = resolve_project_dir(args.project)
    except ValueError as exc:
        print(f"错误: {exc}")
        sys.exit(1)

    init_project(project)

    print("=" * 60)
    print("Writer Agent 启动")
    print(f"项目: {NOVELS_DIR}")
    print("=" * 60)

    CHAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    failed_chapters = []
    if args.chapter > 0:
        result = generate_chapter(args.chapter, output_file=args.output_file or None, review_feedback=args.review_feedback or None)
        if result == "failed":
            failed_chapters.append(args.chapter)
    else:
        for ch in range(args.start, args.end + 1):
            result = generate_chapter(ch)
            if result == "failed":
                log(f"[Writer] 第{ch}章生成失败，记录并继续")
                failed_chapters.append(ch)
            time.sleep(2)

    if failed_chapters:
        log(f"[Writer] 以下章节生成失败: {failed_chapters}")
        sys.exit(1)
    else:
        log("[Writer] 全部完成")


if __name__ == "__main__":
    main()
