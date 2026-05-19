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
import sys
import time
from pathlib import Path

from core.mmx_client import MmxError, call_mmx as call_mmx_client
from core.novel_config import configure_stdio, load_config
from core.workflow_state import load_review_status

configure_stdio()

NOVELS_DIR = None
CHAPTERS_DIR = None
OUTLINE_FILE = None
CHARACTERS_FILE = None
REVIEWS_DIR = None
LOG_FILE = None
CONFIG = None


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, CHAPTERS_DIR, OUTLINE_FILE, CHARACTERS_FILE, REVIEWS_DIR, LOG_FILE, CONFIG
    NOVELS_DIR = Path(project_dir).resolve()
    CHAPTERS_DIR = NOVELS_DIR / "chapters" / "draft"
    OUTLINE_FILE = NOVELS_DIR / "outline.json"
    CHARACTERS_FILE = NOVELS_DIR / "characters.json"
    REVIEWS_DIR = NOVELS_DIR / "reviews"
    LOG_FILE = NOVELS_DIR / "logs" / "reviewer.log"
    CONFIG = load_config(NOVELS_DIR)


def analyze_chapter_text(chapter_content: str) -> dict:
    text = chapter_content.strip()
    length = len(text)
    paragraph_count = len([p for p in text.splitlines() if p.strip()])
    dialogue_count = text.count("\u201c") + text.count('"')
    issues = []
    if length < 4500:
        issues.append(f"字数低于4500字，当前{length}字")
    if length > 5500:
        issues.append(f"字数超过5500字，当前{length}字")
    if paragraph_count < 20:
        issues.append(f"段落数量偏少，当前{paragraph_count}段")
    if dialogue_count < 4:
        issues.append("对话标记偏少，可能缺少角色互动")
    return {
        "word_count": length,
        "word_count_ok": 4500 <= length <= 5500,
        "paragraph_count": paragraph_count,
        "dialogue_marker_count": dialogue_count,
        "issues": issues,
        "local_ok": not issues,
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
            raw_name="reviewer",
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


def review_chapter(chapter_number: int) -> dict:
    chapter_file = CHAPTERS_DIR / f"chapter_{chapter_number:04d}.txt"
    review_file = REVIEWS_DIR / f"chapter_{chapter_number:04d}_review.json"

    if not chapter_file.exists():
        log(f"[Reviewer] 第{chapter_number}章文件不存在")
        return {"status": "no_file"}

    if review_file.exists():
        _, status, _, ok = load_review_status(review_file)
        if ok:
            log(f"[Reviewer] 第{chapter_number}章已有有效审查报告，跳过")
            with open(review_file, "r", encoding="utf-8") as f:
                return json.load(f)
        log(f"[Reviewer] 第{chapter_number}章审查报告无效（{status}），重新审查")

    with open(chapter_file, "r", encoding="utf-8") as f:
        chapter_content = f.read()
    local_analysis = analyze_chapter_text(chapter_content)

    outline = load_json(OUTLINE_FILE)
    chapter_outline = None
    for ch in outline.get("chapters", []):
        if ch.get("chapter_number") == chapter_number:
            chapter_outline = ch
            break

    characters = load_json(CHARACTERS_FILE)

    content_sample = chapter_content[:2000]
    mid_start = max(0, len(chapter_content) // 2 - 500)
    content_sample += "\n\n[中间部分...]\n\n" + chapter_content[mid_start:mid_start + 1000]
    content_sample += "\n\n[结尾部分...]\n\n" + chapter_content[-1000:]

    system = """你是一位资深网络小说编辑，拥有20年审稿经验。
你需要从多个维度审查章节质量，并给出具体的修改建议。
本书是"书生武道通神"题材，主角外表文弱但实力深不可测。
评分标准严格：9-10分优秀，7-8分良好，5-6分及格但需修改，低于5分需重写。
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

## 本地全文检查
{json.dumps(local_analysis, ensure_ascii=False, indent=2)}

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
  "strengths": ["优点1", "优点2"],
  "weaknesses": ["不足1", "不足2"],
  "suggestions": ["具体修改建议1", "具体修改建议2"],
  "continuity_issues": ["与前文不一致之处（如有）"],
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
        review_data = {
            "chapter_number": chapter_number,
            "status": "failed",
            "local_analysis": local_analysis,
        }
        REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
        with open(review_file, "w", encoding="utf-8") as f:
            json.dump(review_data, f, ensure_ascii=False, indent=2)
        return review_data

    try:
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        review_data = json.loads(content)
        review_data["status"] = "completed"
        review_data["local_analysis"] = local_analysis
    except Exception as e:
        log(f"[Reviewer] JSON解析失败: {e}")
        review_data = {
            "chapter_number": chapter_number,
            "status": "parse_error",
            "raw_response": content,
            "local_analysis": local_analysis,
        }

    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    with open(review_file, "w", encoding="utf-8") as f:
        json.dump(review_data, f, ensure_ascii=False, indent=2)

    overall = review_data.get("overall_score", "N/A")
    verdict = review_data.get("verdict", "N/A")
    log(f"[Reviewer] 第{chapter_number}章审查完成，评分: {overall}， verdict: {verdict}")
    return review_data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-p", type=str,
                        default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录（默认从环境变量 NOVEL_PROJECT_DIR 读取）")
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=10, help="结束章节")
    parser.add_argument("--chapter", type=int, default=0, help="只审查某一章")
    args = parser.parse_args()

    if not args.project:
        print("错误: 必须指定 --project 或设置 NOVEL_PROJECT_DIR 环境变量")
        sys.exit(1)

    init_project(args.project)

    print("=" * 60)
    print("Reviewer Agent 启动")
    print(f"项目: {NOVELS_DIR}")
    print("=" * 60)

    CHAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    if args.chapter > 0:
        review_chapter(args.chapter)
    else:
        for ch in range(args.start, args.end + 1):
            review_chapter(ch)
            time.sleep(1)

    log("[Reviewer] 全部完成")


if __name__ == "__main__":
    main()
