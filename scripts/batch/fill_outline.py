#!/usr/bin/env python3
"""补全缺失的大纲章节"""

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from core.novel_config import load_config
from core.workflow_state import list_outline_chapters, outline_completed_count
from tool_paths import script_path

NOVELS_DIR = None
SCRIPTS_DIR = None
CONFIG = None

MISSING_RANGES = []


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, SCRIPTS_DIR, CONFIG
    NOVELS_DIR = Path(project_dir).resolve()
    SCRIPTS_DIR = Path(__file__).parent
    CONFIG = load_config(NOVELS_DIR)


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")


def run_outliner(start: int, end: int) -> int:
    outfile = NOVELS_DIR / f"outline_part_{start:04d}_{end:04d}.json"
    cmd = [
        sys.executable, str(script_path("outliner.py")),
        "--project", str(NOVELS_DIR),
        "--start", str(start), "--end", str(end),
        "--outline-file", str(outfile)
    ]
    log(f"生成第{start}-{end}章 -> {outfile.name}")
    result = subprocess.run(cmd, capture_output=False, text=True, encoding="utf-8")
    return result.returncode


def merge_outlines():
    log("合并所有大纲段...")
    all_chapters = []

    all_chapters.extend(list_outline_chapters(NOVELS_DIR))

    for part_file in sorted(NOVELS_DIR.glob("outline_part_*.json")):
        try:
            with open(part_file, "r", encoding="utf-8") as f:
                part = json.load(f)
            chapters = part.get("chapters", [])
            all_chapters.extend(chapters)
        except Exception as e:
            log(f"读取 {part_file.name} 失败: {e}")

    seen = set()
    unique = []
    for ch in all_chapters:
        num = ch.get("chapter_number", 0)
        if num and num not in seen:
            seen.add(num)
            unique.append(ch)
    unique.sort(key=lambda ch: ch.get("chapter_number", 0))

    log(f"合并完成: {len(unique)}/{CONFIG['total_chapters']} 章")
    return len(unique)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-p", type=str,
                        default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录")
    args = parser.parse_args()

    if not args.project:
        print("错误: 必须指定 --project 或设置 NOVEL_PROJECT_DIR 环境变量")
        sys.exit(1)

    init_project(args.project)

    print("=" * 60)
    print("补全缺失大纲")
    print(f"项目: {NOVELS_DIR}")
    print("=" * 60)

    for start, end in MISSING_RANGES:
        rc = run_outliner(start, end)
        if rc != 0:
            log(f"第{start}-{end}章生成失败 (rc={rc})")
        time.sleep(2)

    total = merge_outlines()
    log(f"大纲总计: {total}/{CONFIG['total_chapters']} 章")

    if total >= CONFIG["total_chapters"]:
        log("大纲补全完成!")
    else:
        log(f"仍有缺失: {CONFIG['total_chapters'] - total} 章")


if __name__ == "__main__":
    main()

