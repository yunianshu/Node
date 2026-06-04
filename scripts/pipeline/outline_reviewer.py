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
import time
from pathlib import Path

from core.mmx_client import MmxError, call_mmx as call_mmx_client
from core.novel_config import configure_stdio, load_config
from core.workflow_state import (
    load_outline_chapter,
    outline_dir,
    outline_review_dir,
)

configure_stdio()

NOVELS_DIR = None
OUTLINE_REVIEW_DIR = None
LOG_FILE = None
CONFIG = None


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, OUTLINE_REVIEW_DIR, LOG_FILE, CONFIG
    NOVELS_DIR = Path(project_dir).resolve()
    OUTLINE_REVIEW_DIR = outline_review_dir(NOVELS_DIR)
    LOG_FILE = NOVELS_DIR / "logs" / "outline_reviewer.log"
    CONFIG = load_config(NOVELS_DIR)


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def call_mmx(system_prompt: str, user_prompt: str, max_tokens: int = 4096, temperature: float = 0.3) -> str:
    try:
        cfg = CONFIG.get("outline_reviewer", {})
        return call_mmx_client(
            system_prompt,
            user_prompt,
            model=CONFIG["model"],
            mmx_path=CONFIG["mmx_path"],
            max_tokens=cfg.get("max_tokens", max_tokens),
            temperature=cfg.get("temperature", temperature),
            retries=CONFIG["writer"]["max_retries"],
            retry_delay=CONFIG["writer"]["retry_delay"],
            log_dir=NOVELS_DIR / "logs" / "raw_responses",
            raw_name="outline_reviewer",
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


def review_outline(chapter_number: int) -> dict:
    outline_file = outline_dir(NOVELS_DIR) / f"chapter_{chapter_number:04d}.json"
    review_file = OUTLINE_REVIEW_DIR / f"chapter_{chapter_number:04d}_review.json"

    if not outline_file.exists():
        log(f"[OutlineReviewer] 第{chapter_number}章大纲文件不存在")
        return {"status": "no_file"}

    if review_file.exists():
        try:
            existing = json.loads(review_file.read_text(encoding="utf-8"))
            status = existing.get("status")
            if status == "completed":
                log(f"[OutlineReviewer] 第{chapter_number}章大纲已有审查报告，跳过")
                return existing
            # 只有 failed/parse_error/no_file 才重新审查
            log(f"[OutlineReviewer] 第{chapter_number}章大纲审查报告状态为{status}，重新审查")
        except Exception:
            pass

    outline = load_json(outline_file)

    # 加载前后章节作为上下文
    prev_outline = load_outline_chapter(NOVELS_DIR, chapter_number - 1)
    next_outline = load_outline_chapter(NOVELS_DIR, chapter_number + 1)

    world = load_json(NOVELS_DIR / "world.json")
    characters = load_json(NOVELS_DIR / "characters.json")

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

    system = f"""你是一位资深网络小说总编，拥有20年大纲评审经验。
你需要从多个维度审查单章大纲的质量，判断该大纲是否足以支撑 Writer 写出高质量正文。
本书是《{book_title}》。
{genre_text}
评分标准：9-10分优秀，达到{min_score}分为良好可写，低于{min_score}分需修改或重生成。
优秀大纲完全可以给出9分以上，请根据实际质量客观评分，不要人为压低分数。
输出必须是合法的JSON格式。"""

    context_parts = []
    if prev_outline:
        context_parts.append(f"""## 前一章大纲（第{chapter_number - 1}章）
标题：{prev_outline.get('title', 'N/A')}
摘要：{prev_outline.get('summary', 'N/A')[:200]}
关键事件：{prev_outline.get('key_events', [])}""")
    if next_outline:
        context_parts.append(f"""## 后一章大纲（第{chapter_number + 1}章）
标题：{next_outline.get('title', 'N/A')}
摘要：{next_outline.get('summary', 'N/A')[:200]}
关键事件：{next_outline.get('key_events', [])}""")
    context_text = "\n\n".join(context_parts)

    prompt = f"""请审查以下第{chapter_number}章的单章大纲。

## 世界观与角色设定
{json.dumps(characters, ensure_ascii=False, indent=2)[:800]}

{context_text}

## 待审查大纲（第{chapter_number}章）
{json.dumps(outline, ensure_ascii=False, indent=2)}

请输出以下JSON格式的审查报告：
{{
  "chapter_number": {chapter_number},
  "overall_score": "请给出0-10的客观评分，质量优秀的大纲可给9分以上",
  "verdict": "通过/需修改/需重写",
  "scores": {{
    "plot_attraction": "剧情吸引力（0-10）",
    "pacing": "节奏把控（0-10）",
    "character_motivation": "人物动机合理性（0-10）",
    "satisfaction_design": "爽点设计（0-10）",
    "foreshadowing": "伏笔与呼应（0-10）",
    "scene_diversity": "场景多样性（0-10）",
    "power_consistency": "力量体系一致性（0-10）",
    "writeability": "整体可写性（0-10）"
  }},
  "strengths": ["优点1", "优点2"],
  "weaknesses": ["不足1", "不足2"],
  "suggestions": ["具体修改建议1", "具体修改建议2"],
  "continuity_issues": ["与前后章衔接问题（如有）"],
  "summary": "总体评价（100字以内）"
}}

要求：
1. 评分要客观公正，质量优秀的大纲完全可以给出9分以上
2. 重点审查：剧情是否有真正的冲突和转折，而非流水账
3. 爽点设计是否到位：是否有期待感、压制、反转、碾压等要素
4. 人物动机是否合理，是否与角色设定一致
5. 与前后章的衔接是否自然，伏笔是否呼应
6. 场景是否多样，避免反复在同一地点做同样的事
7. 力量体系是否自洽，实力成长是否有合理铺垫
8. 信息是否足够详细，Writer 能否据此写出{outline.get('word_count_target', 5000)}字高质量正文
9. 如低于{min_score}分必须标记为\"需重写\"
10. 必须输出合法JSON"""

    log(f"[OutlineReviewer] 正在审查第{chapter_number}章大纲...")
    content = call_mmx(system, prompt, max_tokens=4096, temperature=0.3)

    if not content:
        log(f"[OutlineReviewer] 第{chapter_number}章大纲审查失败")
        review_data = {
            "chapter_number": chapter_number,
            "status": "failed",
        }
        OUTLINE_REVIEW_DIR.mkdir(parents=True, exist_ok=True)
        with open(review_file, "w", encoding="utf-8") as f:
            json.dump(review_data, f, ensure_ascii=False, indent=2)
        return review_data

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

    try:
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        review_data = json.loads(content)
        review_data["status"] = "completed"
        # 将 overall_score 统一转为 float
        review_data["overall_score"] = _parse_score(review_data.get("overall_score"))
        # 将 scores 子项也转为 float
        scores = review_data.get("scores")
        if isinstance(scores, dict):
            for k, v in list(scores.items()):
                scores[k] = _parse_score(v)
    except Exception as e:
        log(f"[OutlineReviewer] JSON解析失败: {e}")
        review_data = {
            "chapter_number": chapter_number,
            "status": "parse_error",
            "raw_response": content,
        }

    OUTLINE_REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    with open(review_file, "w", encoding="utf-8") as f:
        json.dump(review_data, f, ensure_ascii=False, indent=2)

    overall = review_data.get("overall_score", "N/A")
    verdict = review_data.get("verdict", "N/A")
    log(f"[OutlineReviewer] 第{chapter_number}章大纲审查完成，评分: {overall}，verdict: {verdict}")
    return review_data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-p", type=str,
                        default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录")
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=10, help="结束章节")
    parser.add_argument("--chapter", type=int, default=0, help="只审查某一章")
    args = parser.parse_args()

    if not args.project:
        print("错误: 必须指定 --project 或设置 NOVEL_PROJECT_DIR 环境变量")
        sys.exit(1)

    init_project(args.project)

    print("=" * 60)
    print("Outline Reviewer Agent 启动")
    print(f"项目: {NOVELS_DIR}")
    print("=" * 60)

    OUTLINE_REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    total = 1 if args.chapter > 0 else (args.end - args.start + 1)
    failed = 0
    if args.chapter > 0:
        result = review_outline(args.chapter)
        if result.get("status") in ("failed", "no_file", "parse_error"):
            failed += 1
    else:
        for ch in range(args.start, args.end + 1):
            result = review_outline(ch)
            if result.get("status") in ("failed", "no_file", "parse_error"):
                failed += 1
            time.sleep(1)

    log(f"[OutlineReviewer] 完成 {total - failed} 章，失败 {failed} 章")
    log("[OutlineReviewer] 全部完成")


if __name__ == "__main__":
    main()
