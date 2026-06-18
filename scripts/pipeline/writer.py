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

from core.mmx_client import MmxError, call_mmx as call_mmx_client
from core.novel_config import configure_stdio, load_config, load_origin_materials, resolve_project_dir
from core.workflow_state import load_outline_chapter, review_dir
from core.workflow_state import FORBIDDEN_PHRASES, VALID_ENDINGS, is_valid_chapter_text, read_text_length
from core.edit_diff import (
    EditApplyError,
    apply_text_edits,
    build_edit_prompt,
    check_preserved_ratio,
    parse_edit_ops,
    similarity,
)
from core.ai_flavor_detector import detect_ai_flavor

configure_stdio()

NOVELS_DIR = None
CHAPTERS_DIR = None
WORLD_FILE = None
CHARACTERS_FILE = None
LOG_FILE = None
CONFIG = None
ORIGIN_MATERIALS = ""
CANDIDATE_MODE = False


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, CHAPTERS_DIR, WORLD_FILE, CHARACTERS_FILE, LOG_FILE, CONFIG, ORIGIN_MATERIALS
    NOVELS_DIR = Path(project_dir).resolve()
    CHAPTERS_DIR = NOVELS_DIR / "chapters" / "draft"
    WORLD_FILE = NOVELS_DIR / "world.json"
    CHARACTERS_FILE = NOVELS_DIR / "characters.json"
    LOG_FILE = NOVELS_DIR / "logs" / "writer.log"
    CONFIG = load_config(NOVELS_DIR)
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


def call_mmx(system_prompt: str, user_prompt: str, max_tokens: int = 8192, temperature: float = 0.7) -> str:
    try:
        writer_cfg = CONFIG.get("writer", {})
        return call_mmx_client(
            system_prompt,
            user_prompt,
            model=CONFIG["model"],
            mmx_path=CONFIG["mmx_path"],
            max_tokens=max_tokens,
            temperature=temperature,
            retries=int(writer_cfg.get("candidate_retries", 0) if CANDIDATE_MODE else writer_cfg.get("max_retries", 3)),
            retry_delay=float(writer_cfg.get("retry_delay", 5.0)),
            log_dir=NOVELS_DIR / "logs" / "raw_responses",
            raw_name="writer",
            timeout=int(writer_cfg.get("candidate_timeout_seconds", 240) if CANDIDATE_MODE else writer_cfg.get("timeout_seconds", 300)),
            qps=CONFIG["api_qps"],
            rate_state_dir=NOVELS_DIR / "logs" / "rate_limit",
        )
    except MmxError as e:
        log(f"[ERROR] mmx调用失败: {e}")
        return ""


def load_json(filepath: Path) -> dict:
    if not filepath.exists():
        return {}
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


_GENRE_KEYWORDS = {
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


def _cjk_count(text: str) -> int:
    return sum(1 for char in text if "\u4e00" <= char <= "\u9fff")


def _repair_latin1_gbk_mojibake(text: str) -> str:
    if not text or _cjk_count(text) > 0:
        return text
    try:
        repaired = text.encode("latin1").decode("gbk")
    except UnicodeError:
        return text
    if _cjk_count(repaired) > _cjk_count(text):
        return repaired
    return text


def normalize_outline_text(value: Any) -> Any:
    if isinstance(value, str):
        return _repair_latin1_gbk_mojibake(value)
    if isinstance(value, list):
        return [normalize_outline_text(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_outline_text(item) for key, item in value.items()}
    return value


def _writer_quality_contract() -> str:
    min_score = float(CONFIG.get("reviewer", {}).get("min_score", 8.5))
    quality = CONFIG.get("quality", {})
    min_words = int(quality.get("min_chapter_words", 5000))
    max_words = int(quality.get("max_chapter_words", 12000))
    return f"""## 【9分神作契约】正文质量红线（必须满足，否则视为不合格）
- 审查目标分必须达到 {min_score:g} 分及以上；低于该分数视为不合格，必须重写。
- 字数必须达到配置要求，建议不少于 {min_words} 字，避免超过 {max_words} 字。
- 必须严格执行本章大纲的核心事件、人物、地点、危机和章末钩子，不得擅自改主线。
- 开头必须自然承接上一章结尾的人物状态、地点、时间和未解决危机，禁止生硬跳转。

### 【悬念密度·强制要求】
- 每章必须包含至少 3 个"让人无法停止阅读"的关键时刻（tension points）。
- 每 800-1200 字必须有一次有效推进：新信息曝光、冲突升级、意外转折、人物关系质变至少一项。
- 禁止连续超过 1500 字没有任何情绪转折或悬念推进的"平铺直叙"。

### 【章末钩子·强制要求】
- 每章结尾必须是以下三种强力钩子之一，且必须在最后 200 字内落地：
  1. 危机升级钩子：主角或核心人物突然陷入更大危险，生存/目标受到直接威胁。
  2. 信息反转钩子：抛出颠覆前文认知的关键信息，让读者产生"原来如此"或"竟然是这样"的震惊。
  3. 情感爆点钩子：人物关系发生剧烈撕裂或质变，让读者对角色命运产生强烈牵挂。
- 禁止以"平静收尾、总结现状、铺垫过渡"作为章末结尾。每一章结束都必须让读者产生"我必须立刻读下一章"的焦虑感。

### 【反套路·强制要求】
- 禁止套路化战斗描写：禁止"主角遇敌→分析弱点→能力爆发→战胜敌人"的标准升级流模板。
- 禁止套路化解谜描写：禁止"发现问题→查阅资料→恍然大悟→轻松解决"的流水账推演。
- 禁止用技术细节、设定解释、数据参数替代人物情感和剧情张力。技术元素必须服务于"人"的困境，不能成为叙事主体。
- 禁止配角沦为解说员或工具人。每个有台词的配角必须有自己的欲望、恐惧和秘密。

### 【情感冲击·强制要求】
- 心理描写必须展现"真实的恐惧、孤独、愤怒或渴望"，禁止空泛的IDE式颅内注释或代码化思维流水账。
- 每章至少有一个"刺点"——一个让读者心头一紧的细节：一个反常的动作、一句没说出口的话、一个突然沉默的瞬间。
- 冲突必须触及角色的核心恐惧或核心欲望，不能停留在表层利害计算。

### 【信息新鲜度·强制要求】
- 每章必须给读者带来至少一个"此前从未出现过的新元素"：新人物、新地点、新规则、新真相、新威胁、新情感关系。
- 禁止整章都在重复已知信息或进行无新意的铺垫。

### 【基础禁令】
- 不得用设定解释替代剧情现场；世界观信息必须通过行动、对话、发现或冲突呈现。
- 人物动机和说话方式必须符合既有设定，不能OOC。
- 爽点必须来自主角判断、能力、资源或协作的实际发挥，不能靠巧合硬赢。
- 不得水文、重复段落、空泛心理独白、元叙述或输出“本章完”等非正文信息。"""


def _deai_rewrite_directives(detection: dict) -> str:
    """根据 C1 检测结果生成针对性去AI味指令。无问题返回空串。"""
    issues = detection.get("issues", []) if isinstance(detection, dict) else []
    if not issues:
        return ""
    parts = ["## 【去AI味专项重写】（本次重写的首要目标，优先于其他修改）"]
    by_type = {}
    for it in issues:
        by_type.setdefault(it.get("type"), it)
    for t, it in by_type.items():
        loc = it.get("paragraph", "")
        loc_str = f"（定位：{loc}）" if loc and loc != "未定位" else ""
        if t == "parallel_sentiment":
            parts.append(f"- 打破排比工整{loc_str}：把'不仅…而且…更…'改为长短句交替，"
                         "用一个具体动作或物象替代抒情（'他把杯子转了三圈' > '他心中涌起波澜'）")
        elif t == "summary_ending":
            parts.append("- 章末禁止总结性收尾：最后一句必须是未解决的问题、突然的威胁或未说出口的话，"
                         "禁止'故事才刚刚开始'式元叙述")
        elif t == "adjective_pileup":
            parts.append("- 删除'璀璨/磅礴/恐怖'等空泛形容词，每个形容词替换为具体感官细节"
                         "（'空气冷得像含着铁片' > '空气无比冰冷'）")
        elif t == "low_variance":
            parts.append("- 段落长度必须有变化：穿插1-2句短段落制造停顿，再用长段落铺陈")
        elif t == "telling_not_showing":
            parts.append("- 用动作和物象替代'他感到/他意识到'式直接告知情绪")
        elif t == "meta_narration":
            parts.append("- 删除一切生成痕迹/元叙述词")
    return "\n".join(parts)


def _load_9star_examples(chapter_number: int, min_score: float = 9.0, max_examples: int = 2) -> str:
    """加载此前评分 >= min_score 的章节作为9分范本参考。"""
    rd = review_dir(NOVELS_DIR)
    if not rd.exists():
        return ""
    examples = []
    for f in sorted(rd.glob("chapter_*_review.json")):
        try:
            data = json.loads(f.read_text("utf-8"))
            score = float(data.get("overall_score", 0))
        except Exception:
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
    except Exception:
        return {}
    item = payload.get(str(chapter_number), payload) if isinstance(payload, dict) else {}
    if not isinstance(item, dict):
        return {}
    reviews = item.get("reviews", [])
    analysis = item.get("failure_analysis", {})
    latest = reviews[-1] if isinstance(reviews, list) and reviews else {}
    if not isinstance(latest, dict):
        latest = {}
    if not isinstance(analysis, dict):
        analysis = {}
    return {
        "status": "completed",
        "verdict": "需重写",
        "overall_score": analysis.get("best_score", latest.get("overall_score", 0)),
        "weaknesses": analysis.get("likely_reasons", latest.get("weaknesses", [])),
        "suggestions": analysis.get("adjustments", latest.get("suggestions", [])),
        "continuity_issues": latest.get("continuity_issues", []),
        "summary": latest.get("summary", ""),
    }


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
        compressed = call_mmx(compress_system, compress_prompt, max_tokens=8192, temperature=0.3)
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
    """尝试用模型输出 diff ops 并 apply 到旧文本。失败返回 None，由调用方回退全文重写。"""
    system, prompt = build_edit_prompt(old_text, review_data, chapter_number, min_words, max_words)
    log(f"[Writer] 第{chapter_number}章尝试增量编辑...")
    start_time = time.time()
    try:
        raw = call_mmx(system, prompt, max_tokens=8192, temperature=0.3)
    except Exception as e:
        log(f"[Writer] 增量编辑调用失败: {e}")
        return None
    elapsed = time.time() - start_time
    log(f"[Writer] 增量编辑 API 调用耗时 {elapsed:.1f}s")
    if not raw:
        log("[Writer] 增量编辑返回空")
        return None

    try:
        edits = parse_edit_ops(raw)
    except EditApplyError as e:
        log(f"[Writer] 增量编辑解析失败: {e}")
        return None

    if not edits:
        log("[Writer] 模型未输出有效 edits，回退到全文重写")
        return None

    log(f"[Writer] 获得 {len(edits)} 条 edit ops: {[e.get('type') for e in edits]}")
    try:
        new_text, applied_log = apply_text_edits(old_text, edits)
    except EditApplyError as e:
        log(f"[Writer] 增量编辑 apply 失败: {e}")
        return None

    preserved_ratio = similarity(old_text, new_text)
    log(f"[Writer] 增量编辑后文本相似度: {preserved_ratio:.2%}")

    # 如果 edits 很多且文本变化过大，说明模型没有遵守局部修改，回退
    if preserved_ratio < 0.45 and len(edits) <= 2:
        log("[Writer] 增量编辑后文本变化过大，疑似全文重写，回退")
        return None

    # 字数校验
    word_count = len(new_text)
    if word_count < min_words:
        log(f"[Writer] 增量编辑后字数不足 ({word_count} < {min_words})，回退")
        return None
    if word_count > max_words:
        log(f"[Writer] 增量编辑后字数超出 ({word_count} > {max_words})，尝试压缩")
        # 简单截断到最大字数附近（这里只做一个安全网，压缩逻辑后续可接入 _auto_compress）
        new_text = new_text[:max_words]
        # 找到最后一个完整句子
        for end in (".", "!", "?", "。", "！", "？", "；"):
            idx = new_text.rfind(end)
            if idx > max_words * 0.85:
                new_text = new_text[: idx + 1]
                break

    log(f"[Writer] 第{chapter_number}章增量编辑成功，{len(old_text)} -> {len(new_text)} 字")
    return new_text


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
            min_review_score = float(CONFIG.get("reviewer", {}).get("min_score", 8.5))
            if status != "completed" or verdict in {"需重写", "需修改"} or score < min_review_score:
                is_rewrite = True
                log(f"[Writer] 第{chapter_number}章检测到未通过审查报告（status:{status}，评分{score}，verdict:{verdict}），将基于建议重写")
        except Exception:
            pass

    exists, existing_words, existing_ok = read_text_length(chapter_file)
    if exists and existing_ok and not is_rewrite:
        log(f"[Writer] 第{chapter_number}章已存在且字数合格（{existing_words}字），跳过")
        return "exists"
    if exists and is_rewrite:
        log(f"[Writer] 第{chapter_number}章已有初稿但审查未通过，基于审查意见重新生成")
    elif exists:
        log(f"[Writer] 第{chapter_number}章已存在但字数不合格（{existing_words}字），重新生成")

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

    review_section = ""
    if is_rewrite and review_data:
        suggestions = review_data.get("suggestions", [])
        continuity_issues = review_data.get("continuity_issues", [])
        strengths = review_data.get("strengths", [])
        weaknesses = review_data.get("weaknesses", [])
        raw_response = str(review_data.get("raw_response", "") or "").strip()

        review_section = "\n\n## 编辑审查反馈（请严格参考以下建议重写）\n"

        if strengths:
            review_section += "\n### 原文优点（请保留）\n"
            for s in strengths:
                review_section += f"- {s}\n"

        if weaknesses:
            review_section += "\n### 原文不足（请改进）\n"
            for w in weaknesses:
                review_section += f"- {w}\n"

        if suggestions:
            review_section += "\n### 具体修改建议（必须落实）\n"
            for s in suggestions:
                review_section += f"- {s}\n"

        if continuity_issues and continuity_issues[0] != "与前文不一致之处（如有）":
            review_section += "\n### 连续性问题（必须修正）\n"
            for c in continuity_issues:
                if c and c != "与前文不一致之处（如有）":
                    review_section += f"- {c}\n"

        if raw_response and not (suggestions or continuity_issues or weaknesses):
            review_section += "\n### 原始审查反馈（解析失败时也必须参考）\n"
            review_section += raw_response[:3000] + "\n"

        old_file = CHAPTERS_DIR / f"chapter_{chapter_number:04d}.txt"
        old_content = ""
        if old_file.exists():
            with open(old_file, "r", encoding="utf-8") as f:
                old_content = f.read()
            review_section += f"\n### 原文参考（前800字）\n{old_content[:800]}\n...\n"

        # === 方案B：增量编辑优先 ===
        # 如果 reviewer 给出了 edits，先尝试定点修改；失败再回退全文重写
        edits = review_data.get("edits") if isinstance(review_data, dict) else None
        if old_content and edits:
            incremental_text = _apply_incremental_edits(
                chapter_number, old_content, review_data, min_words, max_words
            )
            if incremental_text is not None:
                # 清理禁用短语并保存
                for phrase in FORBIDDEN_PHRASES:
                    incremental_text = incremental_text.replace(phrase, "")
                # 处理截断
                if incremental_text and not incremental_text.endswith(VALID_ENDINGS):
                    paragraphs = incremental_text.split("\n\n")
                    if len(paragraphs) > 1 and not paragraphs[-1].strip().endswith(VALID_ENDINGS):
                        incremental_text = "\n\n".join(paragraphs[:-1]).strip()
                    if incremental_text and not incremental_text.endswith(VALID_ENDINGS):
                        last_valid = max(
                            (incremental_text.rfind(end) for end in VALID_ENDINGS if end in incremental_text),
                            default=-1,
                        )
                        if last_valid > len(incremental_text) * 0.9:
                            incremental_text = incremental_text[: last_valid + 1].strip()

                # 去AI味复检：若重写后 ai_flavor 反而下降且原文不算太差，回退保留原文
                if old_content:
                    try:
                        old_detect = detect_ai_flavor(old_content, project=NOVELS_DIR)
                        new_detect = detect_ai_flavor(incremental_text, project=NOVELS_DIR)
                        if (new_detect["ai_flavor_score"] < old_detect["ai_flavor_score"]
                                and old_detect["ai_flavor_score"] >= 6.0):
                            log(f"[Writer] 第{chapter_number}章去AI味复检：重写后 "
                                f"{new_detect['ai_flavor_score']} < 原文 {old_detect['ai_flavor_score']}，回退保留原文")
                            incremental_text = old_content
                    except Exception as _de:
                        log(f"[Writer] 去AI味复检异常（忽略）: {_de}")

                word_count = len(incremental_text)
                CHAPTERS_DIR.mkdir(parents=True, exist_ok=True)
                with open(chapter_file, "w", encoding="utf-8") as f:
                    f.write(incremental_text)
                log(f"[Writer] 第{chapter_number}章通过增量编辑保存（{word_count}字）")
                return "success"
            log(f"[Writer] 第{chapter_number}章增量编辑失败，回退到全文重写")

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

    key_characters = []
    for c in characters.get("characters", [])[:3]:
        name = c.get("name", "")
        desc = c.get("description", "")
        if name and desc:
            key_characters.append(f"{name}：{desc[:80]}")
    if not key_characters:
        for f in world.get("factions", [])[:3]:
            fname = f.get("name", "")
            fdesc = f.get("description", "")
            if fname and fdesc:
                key_characters.append(f"{fname}：{fdesc[:80]}")

    key_chars_text = "\n".join(f"- {kc}" for kc in key_characters) if key_characters else "（暂无详细角色设定）"

    story_context = f"""主角：{protagonist_name}
世界观：{world_desc[:200]}
修炼体系：{power_system_desc[:150]}
关键势力/角色：
{key_chars_text}"""

    # 9分范本参考：精简 prompt 时跳过（原 _load_9star_examples 会加~1800字，导致 API 空响应）
    examples_section = ""

    genre = _infer_genre(world)
    power_system = world.get("power_system", {})
    power_name = power_system.get("name", "")
    target_min = max(min_words + 500, 5500)

    # 从大纲提取关键事件和章末钩子，强制 writer 按节点执行
    key_events = chapter_outline.get("key_events", [])
    if isinstance(key_events, str):
        key_events = [key_events]
    key_events_text = "\n".join(f"{i+1}. {str(ev)}" for i, ev in enumerate(key_events) if str(ev).strip())
    chapter_hook = str(chapter_outline.get("chapter_hook", "")).strip()
    emotional_arc = str(chapter_outline.get("emotional_arc", "")).strip()
    tension_points = chapter_outline.get("tension_points", [])
    if isinstance(tension_points, str):
        tension_points = [tension_points]
    tension_text = "\n".join(f"- {str(tp)}" for tp in tension_points if str(tp).strip())

    # 救猫咪结构 + 网文增强维度：目标赌注/爽点链/结构功能/对抗力量。
    # 旧大纲可能缺这些字段，用空字符串兜底，正文 prompt 中对空值做条件渲染。
    chapter_goal = str(chapter_outline.get("chapter_goal", "")).strip()
    payoff_design = str(chapter_outline.get("payoff_design", "")).strip()
    story_beat = str(chapter_outline.get("story_beat", "")).strip()
    main_antagonist = str(chapter_outline.get("main_antagonist", "")).strip()

    # 结构功能执行指令：把大纲的结构维度翻译成 Writer 必须落实的写作要求。
    # 旧大纲缺这些字段时整段省略，不影响正文生成（向后兼容）。
    beat_guidance = {
        "catalyst": "本章是催化事件：必须打破主角的日常，制造一个不可忽视的起点危机。",
        "midpoint": "本章是中点：必须让赌注升级，主角从被动应对转为主动出击，常伴随重大信息揭示。",
        "all_is_lost": "本章是谷底：主角必须跌入最低点，关键损失/背叛/失败发生，制造全章压抑。",
        "finale": "本章是高潮：主角用此前积累的认知与实力兑现主线承诺，解决核心冲突。",
        "dark_night": "本章是灵魂暗夜：主角在谷底反思，必须展现真实内心挣扎，为顿悟铺垫。",
    }
    beat_hint = beat_guidance.get(story_beat, "")
    structure_parts = []
    if beat_hint:
        structure_parts.append(f"- 【结构定位·{story_beat}】{beat_hint}")
    if chapter_goal:
        structure_parts.append(f"- 【章节目标与赌注】{chapter_goal}（必须在正文中落实目标的推进或受挫，让读者感受到赌注的分量）")
    if payoff_design:
        structure_parts.append(f"- 【爽点链】{payoff_design}（必须按「期待→压制→反转→碾压」的节奏落地，爽点来自主角实力真实发挥）")
    if main_antagonist:
        structure_parts.append(f"- 【主要对抗】{main_antagonist}（必须塑造对抗力量的具体威胁，让读者感受到压力，而非抽象的「敌人」）")
    structure_directive = "\n".join(structure_parts) if structure_parts else "（本章大纲未提供结构功能/目标赌注/爽点链字段，按既有大纲执行即可）"

    if is_rewrite:
        system = f"""你是一位追求9分神作的顶尖中文网络小说作家，同时也是一位冷酷的资深编辑。
你现在需要对一篇接近9分但未达标的章节进行**局部精修**，而不是推倒重来。
你擅长创作{genre}，但你的标准不是"合格"，而是"惊艳"。

## 精修原则（必须遵守）
1. **保留优点，只改问题**：原文中 reviewer 没有批评的部分（特别是高张力的对话、成功的悬念场景、有效的刺点）必须保留，不得因为重写而丢失。
2. **逐条落实修改建议**：你必须针对 reviewer 的每一条 weakness 和 suggestion 进行具体修改，修改后要在心中自检"这条建议是否已被解决"。
3. **禁止为改而改**：不要改变原文中本已有效的部分，不要为了"创新"而破坏原有节奏。
4. **局部手术，整体保留**：大多数问题只需要修改几个段落、几句对话或一个场景结尾，不需要全文重写。
5. **修正连续性问题**
6. 正文字数必须不少于{min_words}字，建议写到{target_min}-8000字，严格不得超过{max_words}字
7. 保持角色性格一致性
8. 主角是{protagonist_name}，请参考故事设定保持角色一致性
9. 核心目标：精修后的章节必须让第一次读的人产生"我必须立刻知道下一章发生了什么"的冲动

## 精修操作指南
- 如果 reviewer 说"某段落太长"，直接删减该段落至合适长度，不要重写其他部分。
- 如果 reviewer 说"某角色符号化"，给该角色增加一个动作、一句潜台词或一个反常反应，不要重写整个场景。
- 如果 reviewer 说"章末钩子弱"，只修改最后200字，保留前文。
- 如果 reviewer 说"技术细节过多"，删除技术解释，替换为人物反应。
- 如果 reviewer 说"中段缺乏小高潮"，在中段插入一个短小的冲突或转折场景，不要打乱整体结构。"""
        + "\n\n"
        + _deai_rewrite_directives(
            (review_data.get("local_analysis", {}) or {}).get("ai_flavor_detection", {})
            if isinstance(review_data, dict) else {}
        )
    else:
        system = f"""你是一位追求9分神作的顶尖中文网络小说作家，擅长创作{genre}。
你的标准不是"写出一章合格的内容"，而是"写出一章让人欲罢不能的神作"。
你的文笔锋利如刀，对话充满潜台词，场景描写让人身临其境，节奏像过山车一样让人喘不过气。
你痛恨套路，痛恨水文，痛恨用技术细节或设定解释来填充篇幅。
你相信真正的好小说每一章都必须回答一个问题："读者为什么必须继续读下去？"
正文字数必须不少于{min_words}字，建议写到{target_min}-8000字；低于{min_words}字会被系统拒绝。
注意保持角色性格一致性，前后情节衔接自然。
但"衔接自然"不等于"平淡过渡"——衔接处也要有张力、有悬念、有未知。
主角是{protagonist_name}，请参考故事设定保持角色一致性。"""

    prompt = f"""请根据以下信息，写出第{chapter_number}章《{chapter_outline.get('title', '未命名')}》的完整内容。

## 世界观背景
{json.dumps(world, ensure_ascii=False, indent=2)[:1500]}

## 角色信息
{json.dumps(characters, ensure_ascii=False, indent=2)[:1500]}

## origin/ 原始参考素材
{ORIGIN_MATERIALS or "（无）"}

## 本章大纲
{json.dumps(chapter_outline, ensure_ascii=False, indent=2)}

## 本章【必须严格执行的关键事件清单】
以下是大纲中规定的关键事件，你必须按顺序、按时间点全部写入正文，不能遗漏、不能跳过、不能过度发挥成无关内容：
{key_events_text}

## 本章【情绪曲线】
{emotional_arc}

## 本章【张力节点】
{tension_text}

## 本章【章末钩子——最后200字必须落在这里】
{chapter_hook}

## 本章【结构功能 / 目标赌注 / 爽点链 / 对抗力量】
{structure_directive}
## 前一章摘要（用于衔接）
{prev_summary}

## 前一章结尾（用于衔接）
{prev_ending[:300]}
{character_state_directive}
## 后一章摘要（为后续铺垫）
{next_summary}{review_section}

## 故事设定
{story_context}

{_writer_quality_contract()}

{examples_section}

## 写作要求（9分神作执行清单）
1. 【字数红线】本章必须不少于{min_words}字，**严格不得超过{max_words}字**。建议写到{target_min}-8000字。超过{max_words}字视为不合格，必须删减。低于{min_words}字不得结束，必须继续补充。
2. 【关键事件红线】必须严格按照上方【必须严格执行的关键事件清单】逐条写入正文，不能遗漏、不能跳过、不能替换时间点。每个关键事件都必须有主角的现场参与和情感反应。
3. 【技术细节禁令】禁止大段参数、数值、算法、代码、专业术语的解释。Loss值可以出现，但只能用一句话带过；禁止连续超过200字解释技术概念。读者要的是"沈越害怕了"，不是"Loss值的小数点后第八位代表什么"。
4. 【开头】必须自然衔接前一章，但衔接的第一句话就要有张力——禁止以"过了几天""沈越醒来"等平淡方式开头。理想开头：直接切入一个正在进行的动作、一个突然发生的事件、或一个让人不安的细节
5. 【对话】要符合角色性格，推动情节发展，且每段重要对话必须包含至少一层潜台词（言外之意）。禁止长篇解释性对话，禁止配角当解说员
6. 【场景】描写要生动，但重点不是"画面感"，而是"氛围感"——让读者感到压抑、紧迫、诡异或震撼，而不是"看清了这个地方长什么样"
7. 【冲突】必须触及角色核心恐惧或核心欲望，不能停留在表层利害计算。冲突的输赢不重要，重要的是冲突过程中暴露了什么秘密、改变了什么关系
8. 【心理】描写要展现真实的情感波动（恐惧、愤怒、孤独、渴望、自我怀疑），严禁代码化/分析化的"IDE式颅内注释"。主角是程序员，但他首先是个人——他会害怕、会冲动、会后悔
9. 【感官冲击】必须有至少一个"主角本人直面威胁"的近距离刺点，不能所有危险都发生在监控屏幕或远处。
10. 【爽点】必须来自主角在极端压力下的判断、抉择或牺牲，不能靠巧合、升级或突然觉醒硬赢。最顶级的爽点是"主角明知道会输，还是做了最正确的选择"
11. 【配角】要有各自的欲望、恐惧和秘密，不是纯背景板。即使是只出现一次的龙套，也要让读者感觉到"这个人有自己的故事"
12. 如果 origin/ 中存在素材，必须参考其中的原始设定、人物关系、历史事件、语气风格和限制，不能与其冲突
13. 【节奏】禁止流水账。每800-1200字必须有一次有效推进。章节中段必须有一个"小高潮"或"小反转"，不能把所有爆点都堆在结尾
14. 【信息】每章必须给读者带来至少一个"此前从未出现过的新元素"，禁止整章重复已知信息
15. 不要输出章节标题，直接从正文开始
16. 不要输出任何元信息（如"字数：""本章完"等），只输出正文

## 最终硬性要求
输出正文必须不少于{min_words}字，严格不得超过{max_words}字。生成结束前请自行检查篇幅：
- 如果低于{min_words}字，必须继续补充符合大纲的行动、冲突、对话和场景细节。
- 如果超过{max_words}字，必须删减冗余描写、重复叙述和技术解释，保留核心事件和情感张力。
生成结束前，请额外自检以下5个问题并确保答案为"是"：
- 本章最后200字是否精准落在上方【章末钩子】上，且让人心跳加速？
- 本章是否包含上方【关键事件清单】中的每一个事件？
- 本章是否至少有一个"主角本人直面威胁"的近距离刺点？
- 本章是否至少有三个有效的情绪转折（参考上方【情绪曲线】）？
- 如果我是第一次读这本书的读者，读完这章后会不会立刻想打开下一章？

请开始写作："""

    log(f"[Writer] 正在生成第{chapter_number}章...")
    start_time = time.time()
    content = call_mmx(system, prompt, max_tokens=8192, temperature=0.7)
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
