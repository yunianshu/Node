#!/usr/bin/env python3
"""
Writer Agent - 内容生成Agent
负责根据大纲生成具体章节内容，保存为txt文件
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

NOVELS_DIR = Path("D:/AiProject/Node/projects/novels6")
CHAPTERS_DIR = NOVELS_DIR / "chapters" / "draft"
WORLD_FILE = NOVELS_DIR / "world.json"
OUTLINE_FILE = NOVELS_DIR / "outline.json"
CHARACTERS_FILE = NOVELS_DIR / "characters.json"
LOG_FILE = NOVELS_DIR / "logs" / "writer.log"

# mmx CLI 路径（Windows 需通过 node 直接运行）
MMX_CLI_PATH = "C:/Users/Administrator/AppData/Roaming/npm/node_modules/mmx-cli/dist/mmx.mjs"


def log(msg: str):
    """记录日志"""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def call_mmx(system_prompt: str, user_prompt: str, max_tokens: int = 8192, temperature: float = 0.7) -> str:
    """调用 mmx text chat 生成内容（通过 node 直接运行 mmx-cli）"""
    cmd = [
        "node", MMX_CLI_PATH, "text", "chat",
        "--model", "MiniMax-M2.7-highspeed",
        "--system", system_prompt,
        "--message", user_prompt,
        "--max-tokens", str(max_tokens),
        "--temperature", str(temperature),
        "--stream=false",
        "--quiet"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        if result.returncode != 0:
            err = result.stderr.strip() if result.stderr else "unknown error"
            log(f"[ERROR] mmx call failed (rc={result.returncode}): {err}")
            return ""
        raw = result.stdout.strip()
        try:
            data = json.loads(raw)
            return data.get("content", raw)
        except json.JSONDecodeError:
            pass
        if "Response:" in raw:
            json_part = raw.split("Response:")[-1].strip()
            try:
                data = json.loads(json_part)
                return data.get("content", raw)
            except json.JSONDecodeError:
                return json_part
        return raw
    except Exception as e:
        log(f"[ERROR] mmx subprocess exception: {e}")
        return ""


def load_json(filepath: Path) -> dict:
    """加载JSON文件"""
    if not filepath.exists():
        return {}
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def generate_chapter(chapter_number: int, retry: int = 0) -> str:
    """生成单个章节"""
    chapter_file = CHAPTERS_DIR / f"chapter_{chapter_number:04d}.txt"

    if chapter_file.exists() and chapter_file.stat().st_size > 1000:
        log(f"[Writer] 第{chapter_number}章已存在，跳过")
        return "exists"

    # 加载数据
    world = load_json(WORLD_FILE)
    outline = load_json(OUTLINE_FILE)
    characters = load_json(CHARACTERS_FILE)

    # 找到当前章节大纲
    chapter_outline = None
    for ch in outline.get("chapters", []):
        if ch.get("chapter_number") == chapter_number:
            chapter_outline = ch
            break

    if not chapter_outline:
        log(f"[Writer] 第{chapter_number}章大纲不存在")
        return "no_outline"

    # 找到前一章和后一章的摘要（用于衔接）
    prev_summary = ""
    next_summary = ""
    for ch in outline.get("chapters", []):
        if ch.get("chapter_number") == chapter_number - 1:
            prev_summary = ch.get("summary", "")
        if ch.get("chapter_number") == chapter_number + 1:
            next_summary = ch.get("summary", "")

    # 读取上一章末尾（用于衔接）
    prev_ending = ""
    if chapter_number > 1:
        prev_file = CHAPTERS_DIR / f"chapter_{chapter_number-1:04d}.txt"
        if prev_file.exists():
            with open(prev_file, "r", encoding="utf-8") as f:
                content = f.read()
            prev_ending = content[-500:] if len(content) > 500 else content

    # 检查是否存在审查报告（用于重写时参考）
    review_file = NOVELS_DIR / "reviews" / f"chapter_{chapter_number:04d}_review.json"
    review_data = None
    is_rewrite = False
    if review_file.exists():
        try:
            review_data = load_json(review_file)
            verdict = review_data.get("verdict", "")
            score = review_data.get("overall_score", 10)
            if verdict == "需重写" or score < 7:
                is_rewrite = True
                log(f"[Writer] 第{chapter_number}章检测到低分审查报告（评分{score}，verdict:{verdict}），将基于建议重写")
        except Exception:
            pass

    # 构建审查建议部分（如果存在）
    review_section = ""
    if is_rewrite and review_data:
        suggestions = review_data.get("suggestions", [])
        continuity_issues = review_data.get("continuity_issues", [])
        strengths = review_data.get("strengths", [])
        weaknesses = review_data.get("weaknesses", [])

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

        old_file = CHAPTERS_DIR / f"chapter_{chapter_number:04d}.txt"
        if old_file.exists():
            with open(old_file, "r", encoding="utf-8") as f:
                old_content = f.read()
            review_section += f"\n### 原文参考（前800字）\n{old_content[:800]}\n...\n"

    # 系统提示词
    if is_rewrite:
        system = """你是一位顶尖的中文网络小说作家，同时也是一位资深编辑。
你现在需要对一篇已完成的章节进行**重写**，而不是从零创作。
重写原则：
1. 保留原文的优点和核心情节框架
2. 严格落实验编给出的具体修改建议
3. 修正连续性问题
4. 弥补原文的不足之处
5. 每章约5000字，不能少于4500字或超过5500字
6. 保持角色性格一致性，前后情节衔接自然
7. 文笔要比原文更加流畅、细腻、有张力
8. 主角是苏长空，魂界境强者，在无尽虚空中探索万界，要保持低调谦逊但关键时刻碾压一切的爽感"""
    else:
        system = """你是一位顶尖的中文网络小说作家，擅长创作东方玄幻/武侠爽文。
你的文笔流畅、对话生动、场景描写细腻、节奏紧凑。
你尤其擅长描写战斗场面、心理活动和长生武道的世界观展现。
你笔下的主角苏长空是魂界境强者，识海中开辟了一方独立世界，在无尽虚空中探索万界。
每章必须约5000字，不能少于4500字或超过5500字。
注意保持角色性格一致性，前后情节衔接自然。
要写出长生武道追求永恒的气质与武道霸气并存的独特风格。"""

    prompt = f"""请根据以下信息，写出第{chapter_number}章《{chapter_outline.get('title', '未命名')}》的完整内容。

## 世界观背景
{json.dumps(world, ensure_ascii=False, indent=2)[:1500]}

## 角色信息
{json.dumps(characters, ensure_ascii=False, indent=2)[:1500]}

## 本章大纲
{json.dumps(chapter_outline, ensure_ascii=False, indent=2)}

## 前一章摘要（用于衔接）
{prev_summary}

## 前一章结尾（用于衔接）
{prev_ending[:300]}

## 后一章摘要（为后续铺垫）
{next_summary}{review_section}

## 写作要求
1. 本章约5000字，严格按照大纲核心事件展开
2. 开头要自然衔接前一章，结尾要留悬念或引出下一章
3. 对话要符合角色性格，推动情节发展。苏长空沉稳内敛，华善温和慈祥，姬雪潇冷艳高傲
4. 场景描写要生动，让读者有画面感。虚空中的景象要宏大神秘，不同天地的文明差异要鲜明
5. 战斗/冲突场面要紧张刺激，有层次感。苏长空使用魂界之力碾压对手时要展现绝对实力的震撼感
6. 心理描写要细腻，展现主角内心变化。苏长空对武道的执着、对同伴的守护、对新天地的探索欲
7. 要体现"虚空强者轻视苏长空，结果被魂界之力碾压"的反差爽点
8. 苏长空说话要沉稳低调，不张扬，但关键时刻果决狠辣
9. 华善、姬雪潇等同伴要有各自的剧情线和存在感，不是纯背景板
10. 探索不同天地时要展现文明差异（修仙、魔法、科技等），增加趣味性
11. 不要流水账，要有起伏和转折
12. 不要输出章节标题，直接从正文开始
13. 不要输出任何元信息（如"字数：""本章完"等），只输出正文

请开始写作："""

    log(f"[Writer] 正在生成第{chapter_number}章...")
    content = call_mmx(system, prompt, max_tokens=8192, temperature=0.7)

    if not content:
        if retry < 3:
            log(f"[Writer] 第{chapter_number}章生成失败，重试({retry+1}/3)...")
            time.sleep(5)
            return generate_chapter(chapter_number, retry + 1)
        log(f"[Writer] 第{chapter_number}章生成失败，已达最大重试次数")
        return "failed"

    # 清理内容
    content = content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        content = "\n".join(lines).strip()

    # 保存
    CHAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    with open(chapter_file, "w", encoding="utf-8") as f:
        f.write(content)

    word_count = len(content)
    log(f"[Writer] 第{chapter_number}章已保存（{word_count}字） -> {chapter_file}")
    return "success"


def main():
    print("=" * 60)
    print("Writer Agent 启动")
    print("=" * 60)

    CHAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=10, help="结束章节")
    parser.add_argument("--chapter", type=int, default=0, help="只生成某一章")
    args = parser.parse_args()

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
                # 不break，继续尝试后续章节
            time.sleep(2)

    if failed_chapters:
        log(f"[Writer] 以下章节生成失败: {failed_chapters}")
        sys.exit(1)
    else:
        log("[Writer] 全部完成")


if __name__ == "__main__":
    main()
