#!/usr/bin/env python3
"""并行生成大纲 - 使用subprocess.Popen启动后台进程"""

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
from tool_paths import script_path

NOVELS_DIR = None
SCRIPTS_DIR = None
LOGS_DIR = None
CONFIG = None


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, SCRIPTS_DIR, LOGS_DIR, CONFIG
    NOVELS_DIR = Path(project_dir).resolve()
    SCRIPTS_DIR = Path(__file__).parent
    LOGS_DIR = NOVELS_DIR / "logs"
    CONFIG = load_config(NOVELS_DIR)


def log(msg):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line, flush=True)


def run_outliner(start, end):
    outline_file = NOVELS_DIR / f"outline_part_{start:04d}_{end:04d}.json"
    if outline_file.exists():
        try:
            with open(outline_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            count = len(data.get("chapters", []))
            if count >= (end - start + 1) * 0.8:
                log(f"跳过 {start}-{end}（已存在 {count} 章）")
                return None
        except Exception:
            pass

    log_file = LOGS_DIR / f"outliner_{start}_{end}.log"
    cmd = [
        sys.executable, str(script_path("outliner.py")),
        "--project", str(NOVELS_DIR),
        "--start", str(start), "--end", str(end),
        "--outline-file", str(outline_file)
    ]
    log(f"启动 {start}-{end} -> {outline_file.name}")

    with open(log_file, "w", encoding="utf-8") as lf:
        proc = subprocess.Popen(
            cmd,
            stdout=lf,
            stderr=subprocess.STDOUT,
            encoding="utf-8"
        )
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
    log("并行大纲生成 - 5并发")
    log(f"项目: {NOVELS_DIR}")
    log("=" * 60)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    total = CONFIG["total_chapters"]
    batches = [(i, min(i + 99, total)) for i in range(101, total + 1, 100)]
    total_batches = len(batches)
    log(f"总批次: {total_batches}")

    completed = 1

    max_concurrent = 5
    idx = 0
    active_procs = []

    while idx < len(batches) or active_procs:
        while len(active_procs) < max_concurrent and idx < len(batches):
            start, end = batches[idx]
            proc = run_outliner(start, end)
            if proc:
                active_procs.append((start, end, proc))
            else:
                completed += 1
            idx += 1
            time.sleep(2)

        new_active = []
        for start, end, proc in active_procs:
            ret = proc.poll()
            if ret is not None:
                if ret == 0:
                    log(f"完成 {start}-{end}")
                    completed += 1
                else:
                    log(f"失败 {start}-{end} (rc={ret})")
            else:
                new_active.append((start, end, proc))
        active_procs = new_active

        log(f"进度: {completed}/{total_batches + 1} 批完成, {len(active_procs)} 个运行中")
        time.sleep(30)

    log("全部完成!")


if __name__ == "__main__":
    main()

