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
import os

from core.mmx_client import MmxError, call_mmx as call_mmx_client
from core.novel_config import load_config, load_origin_materials, resolve_project_dir
from core.outline_constraints import format_outline_constraints
from core.outline_memory import build_outline_memory, format_outline_memory
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
        return call_mmx_client(
            system_prompt,
            user_prompt,
            model=CONFIG["model"],
            mmx_path=CONFIG["mmx_path"],
            max_tokens=max_tokens,
            temperature=temperature,
            retries=CONFIG["writer"]["max_retries"],
            retry_delay=CONFIG["writer"]["retry_delay"],
            log_dir=NOVELS_DIR / "logs" / "raw_responses",
            raw_name="outliner",
            qps=CONFIG["api_qps"],
            rate_state_dir=NOVELS_DIR / "logs" / "rate_limit",
        )
    except MmxError as e:
        print(f"[ERROR] mmx调用失败: {e}", file=sys.stderr)
        return ""


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


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


def _safe_parse_outline(text: str) -> dict | None:
    """尝试多种方式解析大纲 JSON。"""
    # 1. 直接解析
    try:
        return json.loads(text)
    except Exception:
        pass
    # 2. 修复内部引号后再解析
    try:
        return json.loads(_fix_inner_quotes(text))
    except Exception:
        pass
    # 3. 截断到最后一个完整的 }
    for end_marker in ('"\n    }\n  ]\n}', '"\n    }\n  ]', '"\n    }', '"\n}'):
        idx = text.rfind(end_marker)
        if idx != -1:
            # 找到包裹的右大括号
            end = text.find("}", idx) + 1
            candidate = text[:end]
            try:
                return json.loads(candidate)
            except Exception:
                pass
            try:
                return json.loads(_fix_inner_quotes(candidate))
            except Exception:
                pass
    return None


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
    # 推荐维度（对抗/时间/主线）
    "main_antagonist",
    "time_progression",
    "main_arc_link",
)

# Save the Cat 15 拍 + 网文扩展节拍。story_beat 必须取以下值之一。
# 依据 https://reedsy.com/blog/guide/story-structure/save-the-cat-beat-sheet/
VALID_STORY_BEATS = {
    "opening_image", "theme_stated", "setup", "catalyst", "debate",
    "break_into_two", "b_story", "fun_and_games", "midpoint",
    "bad_guys_close_in", "all_is_lost", "dark_night", "break_into_three",
    "finale", "final_image", "rising_action", "transition",
}

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
    "主要对抗",
    "时间推进",
    "主线关联",
    "故事节拍",
}


def _has_placeholder(value) -> bool:
    if isinstance(value, str):
        text = value.strip()
        return not text or text in PLACEHOLDER_TEXTS or "示例" in text or "占位" in text
    if isinstance(value, list):
        return any(_has_placeholder(item) for item in value)
    return False


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
    if not isinstance(key_events, list) or len([item for item in key_events if str(item).strip()]) < 3:
        issues.append("key_events必须至少包含3个具体事件")
    elif _has_placeholder(key_events):
        issues.append("key_events包含占位文本")

    for field in ("title", "location", "mood", "foreshadowing", "power_progression", "chapter_hook", "emotional_arc"):
        value = str(chapter.get(field, "")).strip()
        if len(value) < 2:
            issues.append(f"{field}不能为空")
        if _has_placeholder(value):
            issues.append(f"{field}仍是占位文本")

    # story_beat 枚举硬校验（防模型乱填结构功能标签）
    story_beat = str(chapter.get("story_beat", "")).strip()
    if story_beat not in VALID_STORY_BEATS:
        issues.append(f"story_beat 必须是有效节拍值之一（如 catalyst/midpoint/all_is_lost/finale 等），当前为：{story_beat}")

    # 核心增强字段：目标赌注 + 爽点链，要求≥15字（与 summary 校验风格一致）
    for field in ("chapter_goal", "payoff_design"):
        value = str(chapter.get(field, "")).strip()
        if len(value) < 15:
            issues.append(f"{field}不少于15字（chapter_goal 写清主角想要什么+失败后果；payoff_design 写清期待→压制→反转→碾压式爽点链）")
        if _has_placeholder(value):
            issues.append(f"{field}仍是占位文本")

    # 推荐字段：仅校验非空（对抗/时间/主线）
    for field in ("main_antagonist", "time_progression", "main_arc_link"):
        value = str(chapter.get(field, "")).strip()
        if len(value) < 2:
            issues.append(f"{field}不能为空")
        if _has_placeholder(value):
            issues.append(f"{field}仍是占位文本")

    tension_points = chapter.get("tension_points")
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
    return f"""## 【高质量单章大纲契约】（必须满足，否则视为不合格）
- 大纲审查目标分必须达到 {min_score:g} 分及以上；低于该分数视为不合格，需要重写。
- 必须顺接前章结尾的人物状态、地点、时间和危机，不能跳场景、跳时间、跳动机。
- 输出前必须逐条自检：钩子是否强力？情绪是否起伏？张力节点是否≥3个？是否反套路？人物动机是否合理？

### 【章末钩子·强制要求】
- chapter_hook 字段必须明确写出本章最后200字要落地的强力钩子，且必须是以下三种之一：
  1. 危机升级钩子：主角或核心人物突然陷入更大危险
  2. 信息反转钩子：抛出颠覆前文认知的关键信息
  3. 情感爆点钩子：人物关系发生剧烈撕裂或质变
- 禁止以"平静收尾、总结现状、铺垫过渡"作为章末钩子。

### 【情绪曲线·强制要求】
- emotional_arc 字段必须描述本章的情绪变化轨迹，例如："压抑→紧张→短暂希望→绝望反转"。
- 禁止一整章都是同一种情绪（如全程紧张或全程平淡）。

### 【张力节点·强制要求】
- tension_points 字段必须列出至少3个"让人无法停止阅读"的关键时刻，标注它们在章节中的大致位置（前/中/后）。
- 每个张力节点必须是：新信息曝光、冲突升级、意外转折、或人物关系质变。

### 【反套路·强制要求】
- 不得与前后章节核心事件重复；若发现重复，必须重新设计本章独有冲突和爽点。
- 禁止套路化设计：禁止"遇敌→分析→升级→打赢"的标准战斗流程，禁止"发现问题→查资料→解决"的标准解谜流程。
- 每章必须产生有效的新进展：新信息、关系变化、风险升级、目标推进或旧伏笔回收至少一项；不强制新增人物或地点。

### 【基础要求】
- summary 必须写具体剧情链路：起因、冲突、转折、结果、章末钩子，不得写模板话。
- key_events 至少 5 条，按发生顺序列出，每条必须包含行动、阻碍和结果。
- foreshadowing 必须包含本章埋下或回收的具体伏笔，不能只写抽象评价。
- power_progression 必须说明主角能力、资源、关系、情报或目标的具体变化。
- 人物动机必须可执行、可理解，不能为了剧情强行行动。
- 每章必须有冲突升级和阅读回报，回报来自主角判断、能力、资源、关系或协作的实际发挥。
- 回报应触及角色核心欲望、恐惧或当前阶段目标，不能只有表层事件堆叠。
- 四项硬门槛必须在章级尺度成立：明确欲望、产生真实代价的承诺或选择、中段改变行动方案、章末正在发生的强钩子。

### 【结构功能·强制要求】（story_beat）
- story_beat 必须从节拍枚举中选取，且必须呼应本章在全卷/全书中的结构位置。
- 关键节拍判据：catalyst（催化事件）打破日常、midpoint（中点）让赌注升级且主角从被动转主动、all_is_lost（谷底）制造最低点、finale（高潮）兑现主线承诺。
- 禁止给中段连续多章都标 transition/rising_action 而无任何 catalyst/midpoint 式转折——那是"注水腰"的信号。

### 【章节目标与赌注·强制要求】（chapter_goal）
- chapter_goal 必须写清：①主角本章具体想要什么（可执行的目标）；②失败的后果是什么（赌注）。
- 目标必须是"本章可推进、可部分达成或可受挫"的，不能是全书级宏大目标（如"成为最强"）。
- 赌注必须触及角色核心利益（生存/关系/目标/秘密），不能只是无关痛痒的得失。

### 【爽点链·强制要求】（payoff_design）
- payoff_design 必须描述完整的爽点链条：期待（铺垫读者预期）→压制（主角受挫或被低估）→反转（局势逆转）→碾压（主角用积累的实力兑现）。
- 爽点必须来自主角的判断、能力、资源或协作的真实发挥，禁止靠巧合或天降外挂硬赢。
- 爽点应触及角色核心欲望或恐惧，而非只有表层战力数值变化。"""


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
    constraints: list[str] = []
    for key, value in review_feedback_data.items():
        if not isinstance(value, dict):
            continue
        chapter_no = value.get("chapter")
        if not chapter_no or not (batch_start <= int(chapter_no) <= batch_end):
            continue
        reviews = value.get("reviews") if isinstance(value.get("reviews"), list) else []
        # Recent concrete continuity and repair instructions must outrank stale
        # aggregate reasons accumulated across many failed attempts.
        for review in reversed(reviews[-3:]):
            if not isinstance(review, dict):
                continue
            for field in ("continuity_issues", "suggestions", "weaknesses"):
                values = review.get(field, [])
                if isinstance(values, list):
                    constraints.extend(str(item).strip() for item in values if str(item).strip())
        analysis = value.get("failure_analysis") if isinstance(value.get("failure_analysis"), dict) else {}
        for field in ("adjustments", "likely_reasons"):
            values = analysis.get(field, [])
            if isinstance(values, list):
                constraints.extend(str(item).strip() for item in values if str(item).strip())
    return list(dict.fromkeys(constraints))[:12]


def _compact_review_feedback(review_feedback_data: dict, batch_start: int, batch_end: int) -> dict:
    compact: dict[str, dict] = {}
    for key, value in review_feedback_data.items():
        if not isinstance(value, dict):
            continue
        chapter_no = value.get("chapter")
        if not chapter_no or not (batch_start <= int(chapter_no) <= batch_end):
            continue

        analysis = value.get("failure_analysis") if isinstance(value.get("failure_analysis"), dict) else {}
        latest_reviews = value.get("reviews") if isinstance(value.get("reviews"), list) else []
        latest = latest_reviews[-1] if latest_reviews and isinstance(latest_reviews[-1], dict) else {}
        compact[key] = {
            "chapter": chapter_no,
            "best_score": analysis.get("best_score"),
            "must_fix": list(analysis.get("likely_reasons", []))[:8],
            "required_adjustments": list(analysis.get("adjustments", []))[:8],
            "latest_verdict": latest.get("verdict"),
            "latest_score": latest.get("overall_score"),
            "latest_weaknesses": list(latest.get("weaknesses", []))[:3],
            "latest_suggestions": list(latest.get("suggestions", []))[:3],
            "latest_continuity_issues": list(latest.get("continuity_issues", []))[:3],
        }
    return compact


def _get_direct_outline_edits(review_feedback_data: dict, chapter_no: int) -> list[dict]:
    """从 review_feedback 中提取指定章节的字段级 edits。"""
    for key, value in review_feedback_data.items():
        if not isinstance(value, dict):
            continue
        if int(value.get("chapter", 0)) != chapter_no:
            continue
        latest_reviews = value.get("reviews") if isinstance(value.get("reviews"), list) else []
        if not latest_reviews:
            continue
        latest = latest_reviews[-1]
        if not isinstance(latest, dict):
            continue
        edits = latest.get("edits")
        if isinstance(edits, list) and edits:
            return edits
    return []


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
    limit = 2 if rescue else 3
    items = before[-limit:] + after[:limit]
    if not items:
        return ""

    title = "前后章摘要（用于衔接，不得照抄或冲突）" if rescue else "前一批最后几章摘要（用于衔接）"
    lines = [f"\n{title}："]
    summary_limit = 240 if rescue else 100
    for ch in items:
        lines.append(f"第{ch.get('chapter_number')}章《{ch.get('title')}》：{str(ch.get('summary', ''))[:summary_limit]}...")
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
    return f"""你正在修复第{batch_start}章到第{batch_end}章的大纲卡点。目标不是扩写，而是输出短小、闭合、可审查通过的JSON。

世界观摘要：
{world_json}

角色摘要：
{chars_json}

故事前提摘要：
{premise}

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
- 字段必须完整：chapter_number/title/summary/characters_involved/location/mood/key_events/foreshadowing/power_progression/word_count_target/chapter_hook/emotional_arc/tension_points/story_beat/chapter_goal/payoff_design/main_antagonist/time_progression/main_arc_link。
- story_beat 必须取枚举值：opening_image/theme_stated/setup/catalyst/debate/break_into_two/b_story/fun_and_games/midpoint/bad_guys_close_in/all_is_lost/dark_night/break_into_three/finale/final_image/rising_action/transition。
- summary 控制在150-220字，key_events 只写5-6条，每条不超过70字。
- chapter_goal 写清主角想要什么+失败后果；payoff_design 写清期待→压制→反转→碾压式爽点链。
- 不允许尾随逗号，不允许注释，不允许省略号，不允许占位文本。

JSON结构：
{{
  "chapters": [
    {{
      "chapter_number": {batch_start},
      "title": "具体章节名",
      "summary": "150-220字具体剧情摘要，必须顺接前章并给下一章留下接口",
      "characters_involved": ["沈越"],
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
      "payoff_design": "期待→压制→反转→碾压式爽点链，15字以上",
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
    batch_size = 15
    if outline_file is not None:
        print("[Outliner] --outline-file 已废弃并被忽略；大纲统一写入 chapters/outline/chapter_XXXX.json")
    failures = []

    world = _load_json(WORLD_FILE)
    characters = _load_json(CHARACTERS_FILE)
    
    # 精简世界观设定，避免请求过大导致 API 超时
    world_summary = {
        "title": world.get("title", ""),
        "world_name": world.get("world_name", ""),
        "world_description": world.get("world_description", "")[:800],
        "power_system": {
            "name": world.get("power_system", {}).get("name", ""),
            "description": world.get("power_system", {}).get("description", "")[:500],
        },
    }
    world_json = json.dumps(world_summary, ensure_ascii=False, indent=2)
    
    # 精简角色设定，只保留主角和关键角色
    chars_summary = {"_meta": characters.get("_meta", {})}
    protagonist = characters.get("protagonist", {})
    chars_summary["protagonist"] = {
        "name": protagonist.get("name", ""),
        "identity": protagonist.get("identity", ""),
        "growth_path": protagonist.get("growth_path", []),
        "signature_ability": protagonist.get("signature_ability", ""),
    }
    chars_summary["companions"] = [
        {"name": c.get("name"), "identity": c.get("identity"), "role": c.get("role")}
        for c in characters.get("companions", [])[:3]
    ]
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
                existing_file = outline_dir(NOVELS_DIR) / f"chapter_{chapter:04d}.json"
                if existing_file.exists():
                    try:
                        chapter_data = _load_json(existing_file)
                        for edit in direct_edits:
                            apply_json_field_edit(chapter_data, edit)
                        issues = _validate_chapter_outline(chapter_data, expected_number=chapter)
                        if not issues:
                            write_outline_chapters(NOVELS_DIR, {"chapters": [chapter_data]}, skip_existing=False)
                            print(f"[Outliner] 第{chapter}章通过字段级 edits 直接修复")
                            continue
                        else:
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
            and _feedback_attempt_count(review_feedback_data, batch_start) >= 8
        )

        prev_context = _context_lines(outline, batch_start, batch_end, rescue=rescue_mode)

        review_section = ""
        if review_feedback_data:
            batch_feedback = _compact_review_feedback(review_feedback_data, batch_start, batch_end)
            if batch_feedback:
                review_section = f"""\n上一轮大纲审查反馈（请特别注意并改进以下问题）：\n{json.dumps(batch_feedback, ensure_ascii=False, indent=2)}\n"""

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
            prompt = f"""你正在为一部追求9分神作的中文网文设计单章大纲。下面是本次要生成的章节范围、世界观、角色设定和参考素材。

## ⚠️ 硬门槛（必须优先满足，否则视为不合格）
{_outline_quality_contract()}

## 世界观设定
{world_json}

## 角色设定
{chars_json}

## 故事前提
{prompt_premise}

## origin/ 原始参考素材
{prompt_origin}

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
      "payoff_design": "期待→压制→反转→碾压式爽点链，15字以上",
      "main_antagonist": "本章主要对抗力量（具体人或势力或困境）",
      "time_progression": "本章相对前章的时间推进（如次日清晨/三天后/同一夜）",
      "main_arc_link": "本章如何推进全书主线（如揭示主线新线索/达成阶段目标）"
    }}
  ]
}}

要求：
1. 每章必须有独特的核心事件，不能流水账；key_events 必须5-7条，不能为空，单条不超过90字
2. story_beat 必须与本章实际剧情结构相符，且要考虑全卷/全书节奏曲线——催化事件(catalyst)、中点(midpoint)、谷底(all_is_lost)、高潮(finale)等关键节拍要落在合理位置，禁止中段连续多章 transition 造成注水腰
3. chapter_goal 必须是本章可推进的具体目标，不能是全书级宏大目标；必须写清失败后果（赌注）
4. payoff_design 必须设计完整爽点链：期待→压制→反转→碾压，爽点来自主角实力真实发挥而非巧合
5. 情节要有起伏，有高潮有低谷，有扮猪吃虎的爽点
6. 主角的实力和技能要逐步成长，保持升级爽感
7. 伏笔要前后呼应，与前一批大纲自然衔接
8. 要有强敌轻视主角，结果被主角以积累的实力碾压的爽文桥段（写入 main_antagonist 和 payoff_design）
9. 探索不同场景时要展现环境差异和世界多样性
10. 如果 origin/ 中存在素材，必须参考其中的设定、人物关系、历史事件和风格约束，不能与其冲突
11. summary 必须是具体剧情摘要，不能写“200字详细摘要”等占位内容
12. foreshadowing 和 power_progression 必须有具体内容，不能缺失或留空
13. 必须输出合法JSON，总共{batch_end - batch_start + 1}个章节对象
14. 单章大纲整体保持紧凑，避免长段解释；必须优先保证JSON闭合和所有必填字段完整"""

        import time as _time
        start_time = _time.time()
        content = call_mmx(
            system,
            prompt,
            max_tokens=4096 if rescue_mode else 8192,
            temperature=0.25 if rescue_mode else 0.5,
        )
        elapsed = _time.time() - start_time
        print(f"[Outliner] 第 {batch_start}-{batch_end} 章大纲生成 API 调用耗时 {elapsed:.1f}s")
        if not content:
            print(f"[Outliner] 第 {batch_start}-{batch_end} 章大纲生成失败")
            failures.append((batch_start, batch_end, "empty_response"))
            continue

        try:
            stripped = _strip_json_markdown(content)
            # 先做 mojibake 修复
            try:
                from core.json_repair import repair_latin1_gbk_mojibake
                stripped = repair_latin1_gbk_mojibake(stripped)
            except Exception:
                pass
            # 修复被 max_tokens 截断的 JSON
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
            print(f"[Outliner] 第 {batch_start}-{batch_end} 章大纲已生成（{len(new_chapters)}章）")
            if candidate_file:
                candidate_file.parent.mkdir(parents=True, exist_ok=True)
                candidate_file.write_text(json.dumps(new_chapters[0], ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"[Outliner] 候选大纲已保存 -> {candidate_file}")
                continue
            outline["chapters"].extend(new_chapters)
            skip_existing = fill_gaps and chapter is None
            write_outline_chapters(NOVELS_DIR, {"chapters": new_chapters}, skip_existing=skip_existing)
        except Exception as e:
            print(f"[Outliner] 第 {batch_start}-{batch_end} 章解析失败: {e}")
            failures.append((batch_start, batch_end, str(e)))
            raw_file = NOVELS_DIR / "logs" / f"outline_batch_{batch_start:04d}.raw"
            raw_file.parent.mkdir(parents=True, exist_ok=True)
            raw_file.write_text(content, encoding="utf-8")

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
