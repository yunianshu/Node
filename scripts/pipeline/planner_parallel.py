#!/usr/bin/env python3
"""
Planner Parallel - 批量Outliner并发生成大纲
先串行生成世界观和角色，然后将大纲分成多段并行生成
每段写入独立文件，并由 Outliner 拆分为单章大纲文件
"""

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))


import argparse
import concurrent.futures
import os
import subprocess
import sys
import time
from pathlib import Path

from core.novel_config import load_config
from core.workflow_state import outline_dir
from tool_paths import script_path

NOVELS_DIR = None
SCRIPTS_DIR = None
CONFIG = None


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, SCRIPTS_DIR, CONFIG
    NOVELS_DIR = Path(project_dir).resolve()
    SCRIPTS_DIR = TOOLS_ROOT
    CONFIG = load_config(NOVELS_DIR)


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)


def run_outliner(start: int, end: int, outline_file: Path) -> int:
    cmd = [
        sys.executable, str(script_path("outliner.py")),
        "--project", str(NOVELS_DIR),
        "--start", str(start), "--end", str(end),
        "--outline-file", str(outline_file)
    ]
    log(f"[Parallel] 启动 Outliner: 第{start}-{end}章 -> {outline_file.name}")
    result = subprocess.run(cmd, capture_output=False, text=True, encoding="utf-8")
    return result.returncode


def count_outline_chapters() -> int:
    """统计已生成的单章大纲文件数量。"""
    return len(list(outline_dir(NOVELS_DIR).glob("chapter_*.json")))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-p", type=str,
                        default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录")
    parser.add_argument("--workers", type=int, default=20, help="并行Planner进程数")
    args = parser.parse_args()

    if not args.project:
        print("错误: 必须指定 --project 或设置 NOVEL_PROJECT_DIR 环境变量")
        sys.exit(1)

    init_project(args.project)

    total_chapters = CONFIG["total_chapters"]
    num_workers = args.workers
    segment_size = total_chapters // num_workers

    print("=" * 70)
    print(f"Planner Parallel - {num_workers} 个Outliner并行大纲生成")
    print(f"项目: {NOVELS_DIR}")
    print("=" * 70)

    log("[Parallel] 步骤1: 生成世界观和角色...")
    rc = subprocess.run(
        [sys.executable, str(script_path("planner.py")), "--project", str(NOVELS_DIR), "--world-only"],
        capture_output=False, text=True, encoding="utf-8"
    )
    if rc.returncode != 0:
        log("[ERROR] 世界观/角色生成失败")
        return
    log("[Parallel] 世界观和角色生成完成")

    segments = []
    for i in range(num_workers):
        start = i * segment_size + 1
        if i == num_workers - 1:
            end = total_chapters
        else:
            end = (i + 1) * segment_size
        outline_file = NOVELS_DIR / f"outline_part_{start:04d}_{end:04d}.json"
        segments.append((start, end, outline_file))

    log(f"[Parallel] 步骤2: 将{total_chapters}章分成{len(segments)}段并行生成")
    for i, (s, e, f) in enumerate(segments):
        log(f"  段{i+1}: 第{s}-{e}章 -> {f.name}")

    log(f"[Parallel] 启动 {len(segments)} 个并行Outliner进程...")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {
            executor.submit(run_outliner, s, e, f): (s, e, f)
            for s, e, f in segments
        }
        for future in concurrent.futures.as_completed(futures):
            s, e, f = futures[future]
            try:
                rc = future.result()
                results.append((s, e, rc))
                if rc == 0:
                    log(f"[Parallel] Outliner {s}-{e} 完成")
                else:
                    log(f"[Parallel] Outliner {s}-{e} 失败 (rc={rc})")
            except Exception as exc:
                log(f"[Parallel] Outliner {s}-{e} 异常: {exc}")
                results.append((s, e, -1))

    success = sum(1 for _, _, rc in results if rc == 0)
    log(f"[Parallel] 全部并行任务完成: {success}/{len(segments)} 段成功")

    total = count_outline_chapters()

    if total >= total_chapters:
        log("[Parallel] 大纲生成完成!")
    else:
        log(f"[Parallel] 警告: 大纲不完整，缺失 {total_chapters - total} 章")


if __name__ == "__main__":
    main()
