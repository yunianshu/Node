#!/usr/bin/env python3
"""
Polisher Agent - 精修Agent
基于 reviewer 反馈对 draft 进行定向局部修改，目标是让章节从 8 分提升到 9 分。
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

from core.llm_client import LLMError, call_llm as _call_section_llm
from core.novel_config import configure_stdio, load_config, load_origin_materials, resolve_project_dir
from core.workflow_state import (
    FORBIDDEN_PHRASES, VALID_ENDINGS, load_outline_chapter,
    review_dir, report_path,
)

configure_stdio()

NOVELS_DIR = None
CHAPTERS_DIR = None
CHARACTERS_FILE = None
WORLD_FILE = None
REVIEWS_DIR = None
LOG_FILE = None
CONFIG = None
ORIGIN_MATERIALS = ""


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, CHAPTERS_DIR, CHARACTERS_FILE, WORLD_FILE, REVIEWS_DIR, LOG_FILE, CONFIG, ORIGIN_MATERIALS
    NOVELS_DIR = Path(project_dir).resolve()
    CHAPTERS_DIR = NOVELS_DIR / "chapters" / "draft"
    CHARACTERS_FILE = NOVELS_DIR / "characters.json"
    WORLD_FILE = NOVELS_DIR / "world.json"
    REVIEWS_DIR = review_dir(NOVELS_DIR)
    LOG_FILE = NOVELS_DIR / "logs" / "polisher.log"
    CONFIG = load_config(NOVELS_DIR)
    review_cfg = CONFIG.get("reviewer", {})
    ORIGIN_MATERIALS = load_origin_materials(
        NOVELS_DIR,
        max_chars=int(review_cfg.get("origin_max_chars", 4000) or 4000),
    )


def log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [Polisher] {message}"
    print(line)
    if LOG_FILE:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def call_llm(system: str, prompt: str, max_tokens: int = 8192, temperature: float = 0.4) -> str:
    try:
        return _call_section_llm(
            CONFIG,
            NOVELS_DIR if NOVELS_DIR else Path.cwd(),
            "polisher",
            system,
            prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            raw_name="polisher",
        )
    except LLMError as e:
        log(f"LLM 调用失败: {e}")
        return ""


def _draft_file(chapter: int, candidate_id: int = 0) -> Path:
    if candidate_id > 0:
        return CHAPTERS_DIR / f"chapter_{chapter:04d}_polish_{candidate_id}.txt"
    return CHAPTERS_DIR / f"chapter_{chapter:04d}.txt"


def _review_file(chapter: int) -> Path:
    return REVIEWS_DIR / f"chapter_{chapter:04d}_review.json"


def _load_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"读取 JSON 失败 {path}: {e}")
        return {}


def _clean_content(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        content = "\n".join(lines).strip()
    for phrase in FORBIDDEN_PHRASES:
        content = content.replace(phrase, "")
    if content and not content.endswith(VALID_ENDINGS):
        paragraphs = content.split("\n\n")
        if len(paragraphs) > 1 and not paragraphs[-1].strip().endswith(VALID_ENDINGS):
            content = "\n\n".join(paragraphs[:-1]).strip()
        if content and not content.endswith(VALID_ENDINGS):
            last_valid = max(
                (content.rfind(end) for end in VALID_ENDINGS if end in content),
                default=-1,
            )
            if last_valid > len(content) * 0.9:
                content = content[: last_valid + 1].strip()
    return content


def polish_chapter(chapter_number: int, retry: int = 0, candidate_id: int = 0, temperature: float | None = None) -> str:
    source_draft_file = _draft_file(chapter_number)
    draft_file = _draft_file(chapter_number, candidate_id)
    if not source_draft_file.exists():
        log(f"第{chapter_number}章初稿不存在，无法精修")
        return "failed"

    review_file = _review_file(chapter_number)
    if not review_file.exists():
        log(f"第{chapter_number}章审查报告不存在，无法精修")
        return "failed"

    draft_content = source_draft_file.read_text(encoding="utf-8")
    review_data = _load_json_file(review_file)

    score = review_data.get("overall_score", 0)
    verdict = review_data.get("verdict", "")
    weaknesses = review_data.get("weaknesses", [])
    suggestions = review_data.get("suggestions", [])
    continuity_issues = review_data.get("continuity_issues", [])
    summary = review_data.get("summary", "")

    candidate_label = f"（候选{candidate_id}）" if candidate_id > 0 else ""
    log(f"第{chapter_number}章{candidate_label}当前评分: {score}，开始精修...")

    chapter_outline = load_outline_chapter(NOVELS_DIR, chapter_number) or {}
    world = _load_json_file(WORLD_FILE)
    characters = _load_json_file(CHARACTERS_FILE)

    key_events = chapter_outline.get("key_events", [])
    if isinstance(key_events, str):
        key_events = [key_events]
    key_events_text = "\n".join(f"{i+1}. {str(ev)}" for i, ev in enumerate(key_events) if str(ev).strip())
    chapter_hook = str(chapter_outline.get("chapter_hook", "")).strip()

    quality = CONFIG.get("quality", {})
    min_words = int(quality.get("min_chapter_words", 5000))
    max_words = int(quality.get("max_chapter_words", 12000))
    target_min = max(min_words + 500, 5500)

    system = f"""你是一位保守的 9 分神作精修编辑。你的座右铭是：**改得越少，风险越小**。
你相信 8 分稿距离 9 分只差几处精准修改，而不是全局重写。
你的精修原则：
- **最小修改**：只改 reviewer 明确指出的段落，其他段落尽量原样保留
- **保留优点**：原文中高张力的对话、成功悬念、有效刺点必须保留，不能因为"创新"而破坏
- **精准打击**：每个修改只针对一个具体问题，不要连锁改动
- **风格一致**：新增或修改的段落必须与原文语气、节奏、人物口吻一致
- **不解释**：输出完整正文，不输出修改说明

你痛恨：为了修改而修改、全局重写、破坏原有节奏、新增与原文风格不符的内容。"""

    prompt = f"""请对以下第{chapter_number}章初稿进行最小化精修，目标是从 {score} 分提升到 9.0 分以上。

## 精修铁律（违反任何一条都视为失败）
1. **最小修改原则**：只修改 reviewer 明确指出的具体段落。 reviewer 没有批评的段落必须原样保留，禁止因为"优化"而改动。
2. **禁止全文重写**：严禁推倒重来。如果 reviewer 没有批评某个场景，这个场景必须一字不动地保留在输出中。
3. **禁止破坏优点**：原文中被 reviewer 列入 strengths 的优点（高张力对话、有效悬念、成功刺点）必须完整保留。
4. **逐条精准修改**：对 reviewer 的每一条 weakness 和 suggestion，只修改它指向的具体部分，不要连带修改无关内容。
5. **风格一致**：修改后的新增内容必须与原文的语气、节奏、人物口吻完全一致，不能出现风格突变。
6. **字数保护**：当前初稿 {len(draft_content)} 字，精修后字数必须在 {min_words}-{max_words} 字之间，建议 {target_min}-10000 字。输出少于 {min_words} 字或多于 {max_words} 字都视为失败。

## 本章大纲
{json.dumps(chapter_outline, ensure_ascii=False, indent=2)}

## 本章关键事件（不能遗漏）
{key_events_text}

## 本章章末钩子（必须保留并强化）
{chapter_hook}

## origin/ 原始参考素材
{ORIGIN_MATERIALS or "（无）"}

## Reviewer 反馈（必须逐条解决）
**总体评分**: {score}  
**verdict**: {verdict}  
**总体评价**: {summary}

### 主要问题
{chr(10).join(f"{i+1}. {w}" for i, w in enumerate(weaknesses))}

### 修改建议
{chr(10).join(f"{i+1}. {s}" for i, s in enumerate(suggestions))}

### 连续性问题
{chr(10).join(f"{i+1}. {c}" for i, c in enumerate(continuity_issues)) if continuity_issues else "（无）"}

## 当前初稿
{draft_content}

请直接输出精修后的完整正文。字数必须在 {min_words}-{max_words} 之间。不要输出解释、不要输出修改清单、不要输出任何元信息。"""

    temp = temperature if temperature is not None else float(CONFIG.get("polisher", {}).get("temperature", 0.2))
    content = call_llm(system, prompt, max_tokens=8192, temperature=temp)
    if not content:
        log(f"第{chapter_number}章精修收到空响应")
        max_retry = CONFIG.get("polisher", {}).get("max_retries", 3)
        if retry < max_retry:
            log(f"第{chapter_number}章精修失败，重试({retry+1}/{max_retry})...")
            time.sleep(CONFIG.get("polisher", {}).get("retry_delay", 5.0))
            return polish_chapter(chapter_number, retry + 1, candidate_id, temperature)
        log(f"第{chapter_number}章精修失败，已达最大重试次数")
        return "failed"

    content = _clean_content(content)

    word_count = len(content)
    original_count = len(draft_content)
    # 字数变化保护：精修应最小化改动，避免大幅改写破坏优点
    min_ratio = float(CONFIG.get("polisher", {}).get("min_length_ratio", 0.85))
    max_ratio = float(CONFIG.get("polisher", {}).get("max_length_ratio", 1.15))
    if word_count < original_count * min_ratio or word_count > original_count * max_ratio:
        log(f"第{chapter_number}章精修后字数变化过大（原{original_count}字 -> 现{word_count}字），尝试重试...")
        max_retry = CONFIG.get("polisher", {}).get("max_retries", 3)
        if retry < max_retry:
            time.sleep(CONFIG.get("polisher", {}).get("retry_delay", 5.0))
            return polish_chapter(chapter_number, retry + 1, candidate_id, temperature)
        log(f"第{chapter_number}章精修后字数仍变化过大，保留原稿")
        return "failed"
    if word_count < min_words:
        log(f"第{chapter_number}章精修后字数不足（{word_count}字），尝试重试...")
        max_retry = CONFIG.get("polisher", {}).get("max_retries", 3)
        if retry < max_retry:
            time.sleep(CONFIG.get("polisher", {}).get("retry_delay", 5.0))
            return polish_chapter(chapter_number, retry + 1, candidate_id, temperature)
        log(f"第{chapter_number}章精修后字数仍不足（{word_count}字），保留原稿")
        return "failed"
    if word_count > max_words:
        log(f"第{chapter_number}章精修后字数超标（{word_count}字），尝试重试...")
        max_retry = CONFIG.get("polisher", {}).get("max_retries", 3)
        if retry < max_retry:
            time.sleep(CONFIG.get("polisher", {}).get("retry_delay", 5.0))
            return polish_chapter(chapter_number, retry + 1, candidate_id, temperature)
        log(f"第{chapter_number}章精修后字数仍超标（{word_count}字），保留原稿")
        return "failed"

    draft_file.write_text(content, encoding="utf-8")
    log(f"第{chapter_number}章精修完成（{word_count}字），覆盖原初稿")
    return "success"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-p", type=str,
                        default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录")
    parser.add_argument("--chapter", type=int, default=0, help="只精修某一章")
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=10, help="结束章节")
    parser.add_argument("--candidate-id", type=int, default=0, help="候选编号（>0 时写入独立候选文件）")
    parser.add_argument("--temperature", type=float, default=None, help="覆盖默认 temperature")
    args = parser.parse_args()

    try:
        project = resolve_project_dir(args.project)
    except ValueError as exc:
        print(f"错误: {exc}")
        sys.exit(1)

    init_project(project)

    print("=" * 60)
    print("Polisher Agent 启动")
    print(f"项目: {NOVELS_DIR}")
    print("=" * 60)

    failed = []
    if args.chapter > 0:
        chapters = [args.chapter]
    else:
        chapters = list(range(args.start, args.end + 1))

    for ch in chapters:
        result = polish_chapter(ch, candidate_id=args.candidate_id, temperature=args.temperature)
        if result == "failed":
            failed.append(ch)
        time.sleep(1)

    if failed:
        log(f"以下章节精修失败: {failed}")
        sys.exit(1)
    log("全部完成")


if __name__ == "__main__":
    main()
