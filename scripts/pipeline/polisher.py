#!/usr/bin/env python3
"""
Polisher Agent - 精修Agent
基于 reviewer 定位对 draft 应用有限修改量的精确补丁，保留其余文本。
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
from core.edit_diff import EditApplyError, apply_reviewed_edits, build_edit_prompt, parse_edit_ops
from core.novel_config import configure_stdio, load_config, load_origin_materials, resolve_project_dir
from core.workflow_state import review_dir

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


def polish_chapter(chapter_number: int, retry: int = 0, candidate_id: int = 0, temperature: float | None = None) -> str:
    source = _draft_file(chapter_number)
    target = _draft_file(chapter_number, candidate_id)
    review_file = _review_file(chapter_number)
    if not source.exists() or not review_file.exists():
        log(f"第{chapter_number}章缺少原稿或审查报告")
        return "failed"
    with source.open("r", encoding="utf-8", newline="") as stream:
        original = stream.read()
    review = _load_json_file(review_file)
    if not review.get("edits"):
        log(f"第{chapter_number}章无可定位修改，保留原稿")
        return "failed"
    quality = CONFIG.get("quality", {})
    min_words = int(quality.get("min_chapter_words", 5000))
    max_words = int(quality.get("max_chapter_words", 12000))
    system, prompt = build_edit_prompt(original, review, chapter_number, min_words, max_words)
    temp = temperature if temperature is not None else float(CONFIG.get("polisher", {}).get("temperature", 0.4))
    try:
        raw = call_llm(system, prompt, max_tokens=8192, temperature=temp)
        revised = apply_reviewed_edits(
            original, parse_edit_ops(raw), review["edits"],
            max_changed_ratio=float(CONFIG.get("revision", {}).get("max_changed_ratio", 0.15)),
            min_words=min_words, max_words=max_words,
        )
    except (EditApplyError, ValueError, TypeError) as exc:
        log(f"第{chapter_number}章精修补丁无效，原稿保留: {exc}")
        return "failed"
    with target.open("w", encoding="utf-8", newline="") as stream:
        stream.write(revised)
    log(f"第{chapter_number}章精修补丁已保存（{len(revised)}字）")
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
