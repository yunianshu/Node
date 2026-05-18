#!/usr/bin/env python3
"""补全缺失的大纲章节"""
import json
import subprocess
import sys
import time
from pathlib import Path

NOVELS_DIR = Path("D:/AiProject/Node/novels5")
SCRIPTS_DIR = Path(__file__).parent

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def run_planner(start, end):
    outline_file = NOVELS_DIR / f"outline_fill_{start:04d}_{end:04d}.json"
    log_file = NOVELS_DIR / f"logs/planner_fill_{start}_{end}.log"
    cmd = [
        sys.executable, str(SCRIPTS_DIR / "planner.py"),
        "--start", str(start), "--end", str(end),
        "--outline-file", str(outline_file)
    ]
    log(f"启动 {start}-{end}")
    with open(log_file, "w", encoding="utf-8") as lf:
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, encoding="utf-8")
    return proc

def main():
    log("=" * 60)
    log("补全缺失大纲")
    log("=" * 60)

    with open(NOVELS_DIR / "outline.json", "r", encoding="utf-8") as f:
        data = json.load(f)

    seen = set(ch.get("chapter_number", 0) for ch in data.get("chapters", []))
    missing = [i for i in range(1, 2001) if i not in seen]
    log(f"缺失: {len(missing)} 章")

    # 分组
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
            proc = run_planner(s, e)
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
