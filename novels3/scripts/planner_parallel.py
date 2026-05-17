#!/usr/bin/env python3
"""
Planner Parallel - 60个Agent并发生成大纲
先串行生成世界观和角色，然后将2000章分成60段并行生成大纲
"""
import concurrent.futures
import json
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent

def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)


def run_planner(start: int, end: int) -> int:
    """运行单个planner进程，负责指定范围"""
    cmd = [sys.executable, str(SCRIPTS_DIR / "planner.py"), "--start", str(start), "--end", str(end)]
    log(f"[Parallel] 启动 Planner: 第{start}-{end}章")
    result = subprocess.run(cmd, capture_output=False, text=True, encoding="utf-8")
    return result.returncode


def main():
    print("=" * 70)
    print("Planner Parallel - 60 Agent 并行大纲生成")
    print("=" * 70)

    # 步骤1: 先生成世界观和角色（串行）
    log("[Parallel] 步骤1: 生成世界观和角色...")
    rc = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "planner.py"), "--world-only"],
        capture_output=False, text=True, encoding="utf-8"
    )
    if rc.returncode != 0:
        log("[ERROR] 世界观/角色生成失败")
        return
    log("[Parallel] 世界观和角色生成完成")

    # 步骤2: 将2000章分成60段，每段约33章
    total_chapters = 2000
    num_workers = 60
    segment_size = total_chapters // num_workers  # 33

    segments = []
    for i in range(num_workers):
        start = i * segment_size + 1
        if i == num_workers - 1:
            end = total_chapters
        else:
            end = (i + 1) * segment_size
        segments.append((start, end))

    log(f"[Parallel] 步骤2: 将2000章分成{len(segments)}段并行生成")
    for i, (s, e) in enumerate(segments):
        log(f"  段{i+1}: 第{s}-{e}章")

    # 步骤3: 并行启动60个planner
    # 注意：由于大纲需要衔接，同一segment内的批次仍串行，但不同segment之间并行
    log(f"[Parallel] 启动 {len(segments)} 个并行Planner进程...")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {
            executor.submit(run_planner, s, e): (s, e)
            for s, e in segments
        }
        for future in concurrent.futures.as_completed(futures):
            s, e = futures[future]
            try:
                rc = future.result()
                results.append((s, e, rc))
                if rc == 0:
                    log(f"[Parallel] Planner {s}-{e} 完成")
                else:
                    log(f"[Parallel] Planner {s}-{e} 失败 (rc={rc})")
            except Exception as exc:
                log(f"[Parallel] Planner {s}-{e} 异常: {exc}")
                results.append((s, e, -1))

    # 汇总
    success = sum(1 for _, _, rc in results if rc == 0)
    log(f"[Parallel] 全部完成: {success}/{len(segments)} 段成功")

    # 验证大纲完整性
    outline_file = Path("D:/AiProject/Node/novels3/outline.json")
    if outline_file.exists():
        with open(outline_file, "r", encoding="utf-8") as f:
            outline = json.load(f)
        count = len(outline.get("chapters", []))
        log(f"[Parallel] 大纲共 {count}/2000 章")
        if count >= 2000:
            log("[Parallel] 大纲生成完成!")
        else:
            log(f"[Parallel] 警告: 大纲不完整，缺失 {2000 - count} 章")
    else:
        log("[ERROR] outline.json 不存在")


if __name__ == "__main__":
    main()
