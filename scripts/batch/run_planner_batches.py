#!/usr/bin/env python3
"""分批串行生成大纲 - 每批100章"""

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
from core.workflow_state import outline_completed_count
from tool_paths import script_path

NOVELS_DIR = None
SCRIPTS_DIR = None
CONFIG = None


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, SCRIPTS_DIR, CONFIG
    NOVELS_DIR = Path(project_dir).resolve()
    SCRIPTS_DIR = Path(__file__).parent
    CONFIG = load_config(NOVELS_DIR)


def log(msg):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)


def run_batch(start, end):
    outline_file = NOVELS_DIR / f"outline_part_{start:04d}_{end:04d}.json"
    if outline_file.exists():
        try:
            with open(outline_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            count = len(data.get("chapters", []))
            if count >= (end - start + 1):
                log(f"跳过 {start}-{end}（已存在 {count} 章）")
                return 0
        except Exception:
            pass

    log(f"开始生成 {start}-{end} 章...")
    cmd = [
        sys.executable, str(script_path("outliner.py")),
        "--project", str(NOVELS_DIR),
        "--start", str(start), "--end", str(end),
        "--outline-file", str(outline_file)
    ]
    result = subprocess.run(cmd, capture_output=False, text=True, encoding="utf-8")
    return result.returncode


def merge_outlines():
    log("合并所有大纲...")
    all_chapters = []
    for f in sorted(NOVELS_DIR.glob("outline_part_*.json")):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            chapters = data.get("chapters", [])
            all_chapters.extend(chapters)
            log(f"  {f.name}: {len(chapters)} 章")
        except Exception as e:
            log(f"  {f.name}: 读取失败 {e}")

    all_chapters.sort(key=lambda ch: ch.get("chapter_number", 0))
    seen = set()
    unique = []
    for ch in all_chapters:
        num = ch.get("chapter_number", 0)
        if num not in seen:
            seen.add(num)
            unique.append(ch)

    log(f"合并完成: {len(unique)}/{CONFIG['total_chapters']} 章")


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

    log("=" * 60)
    log("Outliner Batches - 分批生成大纲")
    log(f"项目: {NOVELS_DIR}")
    log("=" * 60)

    batch_size = 100
    total = CONFIG["total_chapters"]
    success = 0
    fail = 0

    for start in range(1, total + 1, batch_size):
        end = min(start + batch_size - 1, total)
        rc = run_batch(start, end)
        if rc == 0:
            success += 1
        else:
            fail += 1
            log(f"批次 {start}-{end} 失败")

    log(f"批次完成: {success} 成功, {fail} 失败")
    total = outline_completed_count(NOVELS_DIR, 1, CONFIG["total_chapters"])
    log(f"单章大纲文件: {total}/{CONFIG['total_chapters']} 章")
    log("全部完成")


if __name__ == "__main__":
    main()

