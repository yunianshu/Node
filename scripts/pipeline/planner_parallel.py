#!/usr/bin/env python3
"""
Planner Parallel - 60个Agent并发生成大纲
先串行生成世界观和角色，然后将大纲分成多段并行生成
每段写入独立文件，最后合并到 outline.json
"""

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))


import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from core.novel_config import load_config
from core.workflow_state import outline_index_path, write_outline_chapters
from tool_paths import script_path

NOVELS_DIR = None
SCRIPTS_DIR = None
OUTLINE_FILE = None
CONFIG = None


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, SCRIPTS_DIR, OUTLINE_FILE, CONFIG
    NOVELS_DIR = Path(project_dir).resolve()
    SCRIPTS_DIR = TOOLS_ROOT
    OUTLINE_FILE = outline_index_path(NOVELS_DIR)
    CONFIG = load_config(NOVELS_DIR)


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)


def run_planner(start: int, end: int, outline_file: Path) -> int:
    cmd = [
        sys.executable, str(script_path("planner.py")),
        "--project", str(NOVELS_DIR),
        "--start", str(start), "--end", str(end),
        "--outline-file", str(outline_file)
    ]
    log(f"[Parallel] 启动 Planner: 第{start}-{end}章 -> {outline_file.name}")
    result = subprocess.run(cmd, capture_output=False, text=True, encoding="utf-8")
    return result.returncode


def merge_outlines(segments: list):
    log("[Parallel] 合并所有大纲段到 outline.json...")
    all_chapters = []

    for start, end, outline_file in segments:
        if not outline_file.exists():
            log(f"[Parallel] 警告: {outline_file.name} 不存在，跳过")
            continue
        try:
            with open(outline_file, "r", encoding="utf-8") as f:
                part = json.load(f)
            chapters = part.get("chapters", [])
            all_chapters.extend(chapters)
            log(f"[Parallel] 合并 {outline_file.name}: {len(chapters)} 章")
        except Exception as e:
            log(f"[Parallel] 读取 {outline_file.name} 失败: {e}")

    all_chapters.sort(key=lambda ch: ch.get("chapter_number", 0))

    seen = set()
    unique_chapters = []
    for ch in all_chapters:
        num = ch.get("chapter_number", 0)
        if num not in seen:
            seen.add(num)
            unique_chapters.append(ch)

    outline = {"chapters": unique_chapters}
    with open(OUTLINE_FILE, "w", encoding="utf-8") as f:
        json.dump(outline, f, ensure_ascii=False, indent=2)
    write_outline_chapters(NOVELS_DIR, outline)

    log(f"[Parallel] 合并完成: {len(unique_chapters)}/{CONFIG['total_chapters']} 章")
    return len(unique_chapters)


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
    print(f"Planner Parallel - {num_workers} Agent 并行大纲生成")
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

    log(f"[Parallel] 启动 {len(segments)} 个并行Planner进程...")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {
            executor.submit(run_planner, s, e, f): (s, e, f)
            for s, e, f in segments
        }
        for future in concurrent.futures.as_completed(futures):
            s, e, f = futures[future]
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

    success = sum(1 for _, _, rc in results if rc == 0)
    log(f"[Parallel] 全部并行任务完成: {success}/{len(segments)} 段成功")

    total = merge_outlines(segments)

    if total >= total_chapters:
        log("[Parallel] 大纲生成完成!")
    else:
        log(f"[Parallel] 警告: 大纲不完整，缺失 {total_chapters - total} 章")


if __name__ == "__main__":
    main()
