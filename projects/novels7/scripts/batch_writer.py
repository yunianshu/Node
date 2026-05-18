#!/usr/bin/env python3
"""
Batch Writer - 批量章节生成器
并发生成指定范围内的章节初稿
"""
import argparse
import json
import sys
import concurrent.futures
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


def generate_chapter(chapter_number: int, client: LLMClient, config: NovelConfig,
                     world_data: dict, outline: dict, characters: dict,
                     writer_prompt: str, logger) -> str:
    chapter_file = config.draft_dir / f"chapter_{chapter_number:04d}.txt"

    if chapter_file.exists() and chapter_file.stat().st_size > 1000:
        logger.info(f"第{chapter_number}章已存在，跳过")
        return "exists"

    chapter_outline = None
    for ch in outline.get("chapters", []):
        if ch.get("chapter_number") == chapter_number:
            chapter_outline = ch
            break

    if not chapter_outline:
        logger.error(f"第{chapter_number}章大纲不存在")
        return "no_outline"

    prev_summary = ""
    next_summary = ""
    for ch in outline.get("chapters", []):
        if ch.get("chapter_number") == chapter_number - 1:
            prev_summary = ch.get("summary", "")
        if ch.get("chapter_number") == chapter_number + 1:
            next_summary = ch.get("summary", "")

    prev_ending = ""
    if chapter_number > 1:
        prev_file = config.draft_dir / f"chapter_{chapter_number-1:04d}.txt"
        if prev_file.exists():
            with open(prev_file, "r", encoding="utf-8") as f:
                content = f.read()
            prev_ending = content[-500:] if len(content) > 500 else content

    world_summary = json.dumps(world_data, ensure_ascii=False, indent=2)[:1500]
    chars_summary = json.dumps(characters, ensure_ascii=False, indent=2)[:1500]
    chapter_json = json.dumps(chapter_outline, ensure_ascii=False, indent=2)

    system = writer_prompt
    user = f"""请根据以下信息，写出第{chapter_number}章《{chapter_outline.get('title', '未命名')}》的完整内容。

## 世界观背景
{world_summary}

## 角色信息
{chars_summary}

## 本章大纲
{chapter_json}

## 前一章摘要（用于衔接）
{prev_summary}

## 前一章结尾（用于衔接）
{prev_ending[:300]}

## 后一章摘要（为后续铺垫）
{next_summary}

## 写作要求
1. 本章约5000字，严格按照大纲核心事件展开
2. 开头要自然衔接前一章，结尾要留悬念或引出下一章
3. 对话要符合角色性格，推动情节发展
4. 场景描写要生动，让读者有画面感
5. 战斗/冲突场面要紧张刺激，有层次感
6. 心理描写要细腻，展现主角内心变化
7. 不要流水账，要有起伏和转折
8. 不要输出章节标题，直接从正文开始
9. 不要输出任何元信息（如"字数：""本章完"等），只输出正文

请开始写作："""

    logger.info(f"正在生成第{chapter_number}章...")
    content = client.call(system_prompt=system, user_prompt=user,
                          max_tokens=config.writer.max_tokens,
                          temperature=config.writer.temperature)

    if not content:
        logger.error(f"第{chapter_number}章生成失败")
        return "failed"

    content = content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        content = "\n".join(lines).strip()

    config.draft_dir.mkdir(parents=True, exist_ok=True)
    with open(chapter_file, "w", encoding="utf-8") as f:
        f.write(content)

    word_count = len(content)
    logger.info(f"第{chapter_number}章已保存（{word_count}字）")
    return "success"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, required=True, help="起始章节")
    parser.add_argument("--end", type=int, required=True, help="结束章节")
    parser.add_argument("--workers", type=int, default=5, help="并行worker数（默认5）")
    parser.add_argument("--qps", type=float, default=None, help="覆盖API QPS限制")
    args = parser.parse_args()

    config_path = Path(__file__).parent.parent / "config.json"
    config = NovelConfig.load(config_path)
    logger = get_logger("BatchWriter", config.logs_dir)

    logger.info("=" * 60)
    logger.info(f"Batch Writer 启动 | 范围: {args.start}-{args.end} | Workers: {args.workers}")
    logger.info("=" * 60)

    qps = args.qps if args.qps is not None else config.api_qps
    client = LLMClient(
        mmx_path=config.mmx_path,
        model=config.model,
        qps=qps,
        log_dir=str(config.logs_dir)
    )
    writer_prompt = load_prompt(config, "writer")

    world_data = load_json(config.world_file)
    outline = load_json(config.outline_file)
    characters = load_json(config.characters_file)

    total = args.end - args.start + 1
    success = 0
    failed = []
    exists = 0

    def process_chapter(ch_num):
        result = generate_chapter(ch_num, client, config, world_data, outline,
                                  characters, writer_prompt, logger)
        return ch_num, result

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_chapter, ch): ch
                   for ch in range(args.start, args.end + 1)}
        for future in concurrent.futures.as_completed(futures):
            ch_num, result = future.result()
            if result == "success":
                success += 1
            elif result == "exists":
                exists += 1
            else:
                failed.append(ch_num)

    logger.info("=" * 60)
    logger.info(f"Batch Writer 完成 | 成功: {success} | 已存在: {exists} | 失败: {len(failed)}")
    if failed:
        logger.info(f"失败章节: {failed}")
    logger.info("=" * 60)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
