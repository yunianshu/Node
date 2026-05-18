#!/usr/bin/env python3
"""
Reviewer Agent - 配置驱动版
对章节进行评分审查，输出 JSON 格式评审报告
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from novels.core.config import NovelConfig
from novels.core.llm_client import LLMClient
from novels.core.logger import get_logger


def load_prompt(config: NovelConfig, name: str) -> str:
    project_prompt = config.prompts_dir / f"{name}.txt"
    if project_prompt.exists():
        with open(project_prompt, "r", encoding="utf-8") as f:
            return f.read()
    default_prompt = config.path.parent.parent / "novels" / "default_prompts" / f"{name}.txt"
    if default_prompt.exists():
        with open(default_prompt, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def load_json(filepath: Path) -> dict:
    if not filepath.exists():
        return {}
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_json(text: str) -> dict:
    text = text.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.index("{")
        end = text.rindex("}") + 1
        return json.loads(text[start:end])


def review_chapter(chapter_number: int, client: LLMClient, config: NovelConfig,
                   world_data: dict, outline: dict, characters: dict,
                   reviewer_prompt: str, logger) -> str:
    chapter_file = config.draft_dir / f"chapter_{chapter_number:04d}.txt"
    review_file = config.reviews_dir / f"chapter_{chapter_number:04d}_review.json"

    if not chapter_file.exists():
        logger.error(f"第{chapter_number}章草稿不存在")
        return "no_draft"

    if review_file.exists():
        logger.info(f"第{chapter_number}章审查已存在，跳过")
        return "exists"

    with open(chapter_file, "r", encoding="utf-8") as f:
        content = f.read()

    # 找到当前章节大纲
    chapter_outline = None
    for ch in outline.get("chapters", []):
        if ch.get("chapter_number") == chapter_number:
            chapter_outline = ch
            break

    # 前一章摘要
    prev_summary = ""
    for ch in outline.get("chapters", []):
        if ch.get("chapter_number") == chapter_number - 1:
            prev_summary = ch.get("summary", "")
            break

    # 读取前一章末尾
    prev_ending = ""
    if chapter_number > 1:
        prev_file = config.draft_dir / f"chapter_{chapter_number-1:04d}.txt"
        if prev_file.exists():
            with open(prev_file, "r", encoding="utf-8") as f:
                prev_content = f.read()
            prev_ending = prev_content[-300:] if len(prev_content) > 300 else prev_content

    outline_json = json.dumps(chapter_outline, ensure_ascii=False, indent=2) if chapter_outline else ""
    world_summary = json.dumps(world_data, ensure_ascii=False, indent=2)[:1000]
    chars_summary = json.dumps(characters, ensure_ascii=False, indent=2)[:1000]

    system = reviewer_prompt
    user = f"""请对以下章节进行专业评审。

## 世界观背景
{world_summary}

## 角色信息
{chars_summary}

## 本章大纲
{outline_json}

## 前一章摘要
{prev_summary}

## 前一章结尾
{prev_ending[:200]}

## 待评审章节正文（前4000字）
{content[:4000]}

{'...（正文截断，共' + str(len(content)) + '字）' if len(content) > 4000 else ''}

请严格按照系统提示中的JSON格式输出评审结果。"""

    logger.info(f"正在审查第{chapter_number}章...")
    result_text = client.call(system_prompt=system, user_prompt=user,
                              max_tokens=config.reviewer.max_tokens,
                              temperature=config.reviewer.temperature)

    if not result_text:
        logger.error(f"第{chapter_number}章审查失败")
        return "failed"

    try:
        review_data = extract_json(result_text)
        config.reviews_dir.mkdir(parents=True, exist_ok=True)
        with open(review_file, "w", encoding="utf-8") as f:
            json.dump(review_data, f, ensure_ascii=False, indent=2)
        score = review_data.get("overall_score", 0)
        verdict = review_data.get("verdict", "未知")
        logger.info(f"第{chapter_number}章审查完成：评分 {score}， verdict: {verdict}")
        return "success"
    except Exception as e:
        logger.error(f"第{chapter_number}章评审解析失败: {e}")
        raw_file = config.reviews_dir / f"chapter_{chapter_number:04d}_review.raw"
        config.reviews_dir.mkdir(parents=True, exist_ok=True)
        with open(raw_file, "w", encoding="utf-8") as f:
            f.write(result_text)
        return "parse_failed"


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--chapter", type=int, required=True, help="章节号")
    args = parser.parse_args()

    config_path = Path(__file__).parent.parent / "config.json"
    config = NovelConfig.load(config_path)
    logger = get_logger("Reviewer", config.logs_dir)

    logger.info("=" * 60)
    logger.info("Reviewer Agent 启动")
    logger.info("=" * 60)

    client = LLMClient(
        mmx_path=config.mmx_path,
        model=config.model,
        qps=config.api_qps,
        log_dir=str(config.logs_dir)
    )
    reviewer_prompt = load_prompt(config, "reviewer")

    world_data = load_json(config.world_file)
    outline = load_json(config.outline_file)
    characters = load_json(config.characters_file)

    result = review_chapter(args.chapter, client, config, world_data, outline,
                            characters, reviewer_prompt, logger)
    logger.info(f"结果: {result}")
    return 0 if result in ("success", "exists") else 1


if __name__ == "__main__":
    sys.exit(main())
