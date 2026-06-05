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
import sys
import time
from pathlib import Path
from typing import Any

from core.mmx_client import MmxError, call_mmx as call_mmx_client
from core.novel_config import configure_stdio, load_config, load_origin_materials, resolve_project_dir
from core.workflow_state import load_outline_chapter, outline_index_path, review_dir
from core.workflow_state import is_valid_chapter_text, read_text_length

configure_stdio()

NOVELS_DIR = None
CHAPTERS_DIR = None
WORLD_FILE = None
OUTLINE_FILE = None
CHARACTERS_FILE = None
LOG_FILE = None
CONFIG = None
ORIGIN_MATERIALS = ""


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, CHAPTERS_DIR, WORLD_FILE, OUTLINE_FILE, CHARACTERS_FILE, LOG_FILE, CONFIG, ORIGIN_MATERIALS
    NOVELS_DIR = Path(project_dir).resolve()
    CHAPTERS_DIR = NOVELS_DIR / "chapters" / "draft"
    WORLD_FILE = NOVELS_DIR / "world.json"
    OUTLINE_FILE = outline_index_path(NOVELS_DIR)
    CHARACTERS_FILE = NOVELS_DIR / "characters.json"
    LOG_FILE = NOVELS_DIR / "logs" / "writer.log"
    CONFIG = load_config(NOVELS_DIR)
    ORIGIN_MATERIALS = load_origin_materials(NOVELS_DIR)


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def call_mmx(system_prompt: str, user_prompt: str, max_tokens: int = 8192, temperature: float = 0.7) -> str:
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
            raw_name="writer",
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
    min_score = float(CONFIG.get("reviewer", {}).get("min_score", 7.0))
    quality = CONFIG.get("quality", {})
    min_words = int(quality.get("min_chapter_words", 5000))
    max_words = int(quality.get("max_chapter_words", 12000))
    return f"""## 正文质量契约（必须满足）
- 正文审查目标分必须达到 {min_score:g} 分及以上；低于该分数视为不合格，需要重写。
- 字数必须达到配置要求，建议不少于 {min_words} 字，避免超过 {max_words} 字。
- 必须严格执行本章大纲的核心事件、人物、地点、危机和章末钩子，不得擅自改主线。
- 开头必须自然承接上一章结尾的人物状态、地点、时间和未解决危机。
- 结尾必须为下一章留下行动目标、危机升级、信息反转或悬念钩子。
- 每 800-1200 字必须有有效推进：新信息、冲突升级、行动结果、人物关系变化至少一项。
- 不得用设定解释替代剧情现场；世界观信息必须通过行动、对话、发现或冲突呈现。
- 人物动机和说话方式必须符合既有设定，不能OOC，配角不能只当背景板。
- 爽点必须来自主角判断、能力、资源或协作的实际发挥，不能靠巧合硬赢。
- 不得水文、重复段落、空泛心理独白、元叙述或输出“本章完”等非正文信息。"""


def generate_chapter(chapter_number: int, retry: int = 0) -> str:
    chapter_file = CHAPTERS_DIR / f"chapter_{chapter_number:04d}.txt"

    review_file = review_dir(NOVELS_DIR) / f"chapter_{chapter_number:04d}_review.json"
    review_data = None
    is_rewrite = False
    if review_file.exists():
        try:
            review_data = load_json(review_file)
            status = review_data.get("status", "")
            verdict = review_data.get("verdict", "")
            score = review_data.get("overall_score", 10)
            try:
                score = float(score)
            except (TypeError, ValueError):
                score = 0.0
            min_review_score = float(CONFIG.get("reviewer", {}).get("min_score", 7.0))
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
        if old_file.exists():
            with open(old_file, "r", encoding="utf-8") as f:
                old_content = f.read()
            review_section += f"\n### 原文参考（前800字）\n{old_content[:800]}\n...\n"

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

    genre = _infer_genre(world)
    power_system = world.get("power_system", {})
    power_name = power_system.get("name", "")

    if is_rewrite:
        system = f"""你是一位顶尖的中文网络小说作家，同时也是一位资深编辑。
你现在需要对一篇已完成的章节进行**重写**，而不是从零创作。
你擅长创作{genre}，文笔流畅、对话生动、场景描写细腻、节奏紧凑。
重写原则：
1. 保留原文的优点和核心情节框架
2. 严格落实验编给出的具体修改建议
3. 修正连续性问题
4. 弥补原文的不足之处
5. 每章约5000字，尽量控制在4500-8000字之间
6. 保持角色性格一致性，前后情节衔接自然
7. 文笔要比原文更加流畅、细腻、有张力
8. 主角是{protagonist_name}，请参考故事设定保持角色一致性"""
    else:
        system = f"""你是一位顶尖的中文网络小说作家，擅长创作{genre}。
你的文笔流畅、对话生动、场景描写细腻、节奏紧凑。
你尤其擅长描写人物成长、社会百态、人际冲突和心理活动。
每章约5000字，尽量控制在4500-8000字之间。
注意保持角色性格一致性，前后情节衔接自然。
要写出主角在故事中逐步成长的独特风格。
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

## 前一章摘要（用于衔接）
{prev_summary}

## 前一章结尾（用于衔接）
{prev_ending[:300]}

## 后一章摘要（为后续铺垫）
{next_summary}{review_section}

## 故事设定
{story_context}

{_writer_quality_contract()}

## 写作要求
1. 本章约5000字，严格按照大纲核心事件展开
2. 开头要自然衔接前一章，结尾要留悬念或引出下一章
3. 对话要符合角色性格，推动情节发展
4. 场景描写要生动，让读者有画面感
5. 冲突/对抗场面要紧张刺激，有层次感
6. 心理描写要细腻，展现主角内心变化和成长
7. 要体现主角逐步成长、逆风翻盘的核心爽点
8. 主角言行要符合其身份性格和故事背景，不能OOC
9. 配角要有各自的剧情线和存在感，不是纯背景板
10. 探索不同场景时要展现环境差异，增加趣味性
11. 如果 origin/ 中存在素材，必须参考其中的原始设定、人物关系、历史事件、语气风格和限制，不能与其冲突
12. 不要流水账，要有起伏和转折
13. 不要输出章节标题，直接从正文开始
14. 不要输出任何元信息（如"字数：""本章完"等），只输出正文

请开始写作："""

    log(f"[Writer] 正在生成第{chapter_number}章...")
    content = call_mmx(system, prompt, max_tokens=8192, temperature=0.7)

    if not content:
        log(f"[Writer] 第{chapter_number}章收到空响应")
        max_retry = CONFIG["writer"].get("max_retries", 5)
        if retry < max_retry:
            log(f"[Writer] 第{chapter_number}章生成失败，重试({retry+1}/{max_retry})...")
            time.sleep(CONFIG["writer"]["retry_delay"])
            return generate_chapter(chapter_number, retry + 1)
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-p", type=str,
                        default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录（默认从环境变量 NOVEL_PROJECT_DIR 读取）")
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=10, help="结束章节")
    parser.add_argument("--chapter", type=int, default=0, help="只生成某一章")
    args = parser.parse_args()

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
        result = generate_chapter(args.chapter)
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
