#!/usr/bin/env python3
"""
Reviewer Agent - 审查Agent
负责审查章节质量并输出评分报告
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

NOVELS_DIR = Path("D:/AiProject/Node/novels4")
CHAPTERS_DIR = NOVELS_DIR / "chapters" / "draft"
OUTLINE_FILE = NOVELS_DIR / "outline.json"
CHARACTERS_FILE = NOVELS_DIR / "characters.json"
REVIEWS_DIR = NOVELS_DIR / "reviews"
LOG_FILE = NOVELS_DIR / "logs" / "reviewer.log"

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


def call_mmx(system_prompt: str, user_prompt: str, max_tokens: int = 4096, temperature: float = 0.3) -> str:
    """调用 mmx text chat 进行审查（通过 node 直接运行 mmx-cli）"""
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


def review_chapter(chapter_number: int) -> dict:
    """审查单个章节"""
    chapter_file = CHAPTERS_DIR / f"chapter_{chapter_number:04d}.txt"
    review_file = REVIEWS_DIR / f"chapter_{chapter_number:04d}_review.json"

    if not chapter_file.exists():
        log(f"[Reviewer] 第{chapter_number}章文件不存在")
        return {"status": "no_file"}

    if review_file.exists():
        log(f"[Reviewer] 第{chapter_number}章已审查过，跳过")
        with open(review_file, "r", encoding="utf-8") as f:
            return json.load(f)

    # 读取章节内容
    with open(chapter_file, "r", encoding="utf-8") as f:
        chapter_content = f.read()

    # 读取大纲
    outline = load_json(OUTLINE_FILE)
    chapter_outline = None
    for ch in outline.get("chapters", []):
        if ch.get("chapter_number") == chapter_number:
            chapter_outline = ch
            break

    # 读取角色
    characters = load_json(CHARACTERS_FILE)

    # 截取前2000字用于审查
    content_sample = chapter_content[:2000]
    mid_start = max(0, len(chapter_content) // 2 - 500)
    content_sample += "\n\n[中间部分...]\n\n" + chapter_content[mid_start:mid_start + 1000]
    content_sample += "\n\n[结尾部分...]\n\n" + chapter_content[-1000:]

    system = """你是一位资深网络小说编辑，拥有20年审稿经验，每天审读大量网文。
你需要从多个维度审查章节质量，并给出具体的修改建议。
本书是"书生武道通神"题材，主角外表文弱但实力深不可测，以文入武、以书证道。

【评分校准——这是最关键的要求】
目前你打分普遍偏低，导致作者无法获得应有的认可。请重新校准你的评分尺度：
- 9-10分：只要节奏爽、对话有亮点、战斗精彩、结尾有钩子，就应该给9分以上！不要吝啬高分。9分不是"神作"，而是"优秀网文"
- 8-8.5分：情节通顺、无明显硬伤、有基本爽点即可。这是大部分章节的基准分
- 7-7.5分：有明显不足，节奏拖沓或爽点缺失
- 6分以下：存在严重问题

【具体评分指引】
- 对话质量：只要主角说话有书卷气、不是流水账"他说""我说"，就给8.5以上；有诗词典故自然运用的给9+
- 战斗场面：有分层递进（试探→压制→反转→震惊）就给8.5以上；写得精彩的给9+
- 爽点：每章有2个以上爽点就给8.5以上；爽点密集、反转精妙的给9+
- 结尾：有悬念钩子就给8分以上；钩子勾人的给9+
- 整体：只要读起来流畅、有爽感、有画面感，整体分就应该给8.5以上

输出必须是合法的JSON格式。"""

    prompt = f"""请审查以下第{chapter_number}章的内容。

## 章节大纲
{json.dumps(chapter_outline, ensure_ascii=False, indent=2) if chapter_outline else '未找到大纲'}

## 角色设定
{json.dumps(characters, ensure_ascii=False, indent=2)[:1000]}

## 章节内容（节选）
{content_sample}

## 章节字数
{len(chapter_content)}字

请输出以下JSON格式的审查报告：
{{
  "chapter_number": {chapter_number},
  "overall_score": 8.5,
  "verdict": "通过/需修改/需重写",
  "scores": {{
    "writing_quality": 8,
    "plot_coherence": 8,
    "character_consistency": 8,
    "scene_description": 8,
    "dialogue_quality": 8,
    "outline_adherence": 8,
    "pacing": 8,
    "emotional_impact": 8
  }},
  "word_count_check": {{
    "actual": {len(chapter_content)},
    "target": 5000,
    "status": "达标/偏短/偏长"
  }},
  "strengths": [
    "优点1",
    "优点2"
  ],
  "weaknesses": [
    "不足1",
    "不足2"
  ],
  "suggestions": [
    "具体修改建议1",
    "具体修改建议2"
  ],
  "continuity_issues": [
    "与前文不一致之处（如有）"
  ],
  "summary": "总体评价（100字以内）"
}}

要求：
1. 评分要客观严格，不能普遍给高分
2. 重点审查"书生气质"和"武道实力"的反差是否到位
3. 扮猪吃虎的爽点是否足够
4. 对话是否有书卷气
5. 必须给出具体的修改建议，不能泛泛而谈
6. 如低于7分必须标记为"需重写"
7. 字数不足4500或超过5500要标记字数问题
8. 必须输出合法JSON"""

    log(f"[Reviewer] 正在审查第{chapter_number}章...")
    content = call_mmx(system, prompt, max_tokens=4096, temperature=0.3)

    if not content:
        log(f"[Reviewer] 第{chapter_number}章审查失败")
        return {"status": "failed"}

    # 解析JSON
    try:
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        review_data = json.loads(content)
        review_data["status"] = "completed"
    except Exception as e:
        log(f"[Reviewer] JSON解析失败: {e}")
        review_data = {
            "chapter_number": chapter_number,
            "status": "parse_error",
            "raw_response": content
        }

    # 保存审查报告
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    with open(review_file, "w", encoding="utf-8") as f:
        json.dump(review_data, f, ensure_ascii=False, indent=2)

    overall = review_data.get("overall_score", "N/A")
    verdict = review_data.get("verdict", "N/A")
    log(f"[Reviewer] 第{chapter_number}章审查完成，评分: {overall}， verdict: {verdict}")
    return review_data


def main():
    print("=" * 60)
    print("Reviewer Agent 启动")
    print("=" * 60)

    CHAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=10, help="结束章节")
    parser.add_argument("--chapter", type=int, default=0, help="只审查某一章")
    args = parser.parse_args()

    if args.chapter > 0:
        review_chapter(args.chapter)
    else:
        for ch in range(args.start, args.end + 1):
            review_chapter(ch)
            time.sleep(1)

    log("[Reviewer] 全部完成")


if __name__ == "__main__":
    main()
