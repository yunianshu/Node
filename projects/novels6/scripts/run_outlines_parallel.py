#!/usr/bin/env python3
"""
并行生成大纲 - 使用subprocess.Popen启动后台进程
Windows兼容版本
"""
import json
import subprocess
import sys
import time
from pathlib import Path

NOVELS_DIR = Path("D:/AiProject/Node/projects/novels6")
SCRIPTS_DIR = Path(__file__).parent
LOGS_DIR = NOVELS_DIR / "logs"

def log(msg):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line, flush=True)

def run_planner(start, end):
    outline_file = NOVELS_DIR / f"outline_part_{start:04d}_{end:04d}.json"
    if outline_file.exists():
        try:
            with open(outline_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            count = len(data.get("chapters", []))
            if count >= (end - start + 1) * 0.8:
                log(f"跳过 {start}-{end}（已存在 {count} 章）")
                return None
        except:
            pass

    log_file = LOGS_DIR / f"planner_{start}_{end}.log"
    cmd = [
        sys.executable, str(SCRIPTS_DIR / "planner.py"),
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
    log("=" * 60)
    log("并行大纲生成 - 5并发")
    log("=" * 60)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # 所有批次
    batches = [(i, min(i + 99, 2000)) for i in range(101, 2001, 100)]
    total_batches = len(batches)
    log(f"总批次: {total_batches}")

    # 第一批1-100已完成
    completed = 1

    max_concurrent = 5
    idx = 0
    active_procs = []

    while idx < len(batches) or active_procs:
        # 启动新的进程直到达到并发上限
        while len(active_procs) < max_concurrent and idx < len(batches):
            start, end = batches[idx]
            proc = run_planner(start, end)
            if proc:
                active_procs.append((start, end, proc))
            else:
                completed += 1
            idx += 1
            time.sleep(2)  # 间隔启动，避免瞬间爆发

        # 检查已有进程
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
