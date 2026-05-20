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
from core.workflow_state import outline_exists
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
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run_outliner(start, end):
    outline_file = NOVELS_DIR / f"outline_fill_{start:04d}_{end:04d}.json"
    log_file = NOVELS_DIR / f"logs/outliner_fill_{start}_{end}.log"
    cmd = [
        sys.executable, str(script_path("outliner.py")),
        "--project", str(NOVELS_DIR),
        "--start", str(start), "--end", str(end),
        "--outline-file", str(outline_file)
    ]
    log(f"启动 {start}-{end}")
    with open(log_file, "w", encoding="utf-8") as lf:
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, encoding="utf-8")
    return proc


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
    log("补全缺失大纲")
    log(f"项目: {NOVELS_DIR}")
    log("=" * 60)

    missing = [i for i in range(1, CONFIG["total_chapters"] + 1) if not outline_exists(NOVELS_DIR, i)]
    log(f"缺失: {len(missing)} 章")

    batches = []
    if missing:
        start = missing[0]
        prev = missing[0]
        for num in missing[1:]:
            if num == prev + 1:
                prev = num
            else:
                batches.append((start, prev))
                start = num
                prev = num
        batches.append((start, prev))

    log(f"批次: {len(batches)}")

    max_concurrent = 5
    idx = 0
    active = []

    while idx < len(batches) or active:
        while len(active) < max_concurrent and idx < len(batches):
            s, e = batches[idx]
            proc = run_outliner(s, e)
            active.append((s, e, proc))
            idx += 1
            time.sleep(3)

        new_active = []
        for s, e, proc in active:
            ret = proc.poll()
            if ret is not None:
                if ret == 0:
                    log(f"完成 {s}-{e}")
                else:
                    log(f"失败 {s}-{e} (rc={ret})")
            else:
                new_active.append((s, e, proc))
        active = new_active

        if active:
            log(f"运行中: {len(active)} 个批次")
            time.sleep(30)

    log("补全完成!")


if __name__ == "__main__":
    main()

