#!/usr/bin/env python3
"""补全缺失的初稿章节"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from novel_config import load_config
from workflow_state import scan_chapter_status

NOVELS_DIR = None
CONFIG = None


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, CONFIG
    NOVELS_DIR = Path(project_dir).resolve()
    CONFIG = load_config(NOVELS_DIR)


def get_missing_ranges():
    total = CONFIG["total_chapters"]
    statuses = scan_chapter_status(NOVELS_DIR, 1, total)
    missing = [i for i in range(1, total + 1) if not statuses[i].draft_ok]
    if not missing:
        return []

    groups = []
    start = missing[0]
    prev = missing[0]
    for n in missing[1:]:
        if n == prev + 1:
            prev = n
        else:
            groups.append((start, prev))
            start = n
            prev = n
    groups.append((start, prev))
    return groups


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
    print("补全缺失初稿")
    print(f"项目: {NOVELS_DIR}")
    print("=" * 60)

    missing_ranges = get_missing_ranges()
    total_missing = sum(e - s + 1 for s, e in missing_ranges)
    print(f"缺失: {total_missing} 章, 共 {len(missing_ranges)} 段")

    for s, e in missing_ranges:
        print(f"  {s}-{e} ({e-s+1}章)")

    if not missing_ranges:
        print("初稿已完整!")
        return

    scripts_dir = Path(__file__).parent
    for s, e in missing_ranges:
        print(f"\n生成第{s}-{e}章...")
        cmd = [
            sys.executable, str(scripts_dir / "writer.py"),
            "--project", str(NOVELS_DIR),
            "--start", str(s), "--end", str(e)
        ]
        result = subprocess.run(cmd, capture_output=False, text=True, encoding="utf-8")
        if result.returncode != 0:
            print(f"第{s}-{e}章生成失败")
        time.sleep(CONFIG["coordinator"]["pause_between_batches"])

    print("\n补全完成")


if __name__ == "__main__":
    main()
