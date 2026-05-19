#!/usr/bin/env python3
"""补全缺失或质量门不通过的初稿章节。"""

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

import argparse
import os
import subprocess
import sys
from pathlib import Path

from core.novel_config import load_config
from tool_paths import script_path
from core.workflow_state import scan_chapter_status, write_status_file

NOVELS_DIR = None
CONFIG = None


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, CONFIG
    NOVELS_DIR = Path(project_dir).resolve()
    CONFIG = load_config(NOVELS_DIR)


def chunk_ranges(chapters: list[int], chunk_size: int) -> list[tuple[int, int]]:
    if not chapters:
        return []
    ranges = []
    start = prev = chapters[0]
    for chapter in chapters[1:]:
        if chapter == prev + 1 and chapter - start < chunk_size:
            prev = chapter
        else:
            ranges.append((start, prev))
            start = prev = chapter
    ranges.append((start, prev))
    return ranges


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

    total = CONFIG["total_chapters"]
    statuses = scan_chapter_status(NOVELS_DIR, 1, total)
    missing = [chapter for chapter, status in statuses.items() if not status.draft_ok]
    print(f"需要补全/重生成初稿: {len(missing)} 章")
    if not missing:
        write_status_file(NOVELS_DIR, statuses.values())
        return

    scripts_dir = Path(__file__).parent
    ranges = chunk_ranges(missing, CONFIG["coordinator"]["batch_size"])
    for start, end in ranges:
        print(f"生成第{start}-{end}章")
        cmd = [
            sys.executable, str(script_path("writer.py")),
            "--project", str(NOVELS_DIR),
            "--start", str(start), "--end", str(end)
        ]
        subprocess.run(cmd, check=False, text=True, encoding="utf-8")

    statuses = scan_chapter_status(NOVELS_DIR, 1, total)
    write_status_file(NOVELS_DIR, statuses.values())
    print(f"初稿质量门通过: {sum(1 for s in statuses.values() if s.draft_ok)}/{total}")


if __name__ == "__main__":
    main()

