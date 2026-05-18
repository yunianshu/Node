#!/usr/bin/env python3
"""
Planner Parallel - 60个Agent并发生成大纲
先串行生成世界观和角色，然后将2000章分成60段并行生成大纲
每段写入独立文件，最后合并到 outline.json
"""
import concurrent.futures
import json
import subprocess
import sys
import time
from pathlib import Path

NOVELS_DIR = Path("D:/AiProject/Node/projects/novels6")
SCRIPTS_DIR = Path(__file__).parent
OUTLINE_FILE = NOVELS_DIR / "outline.json"


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)


def run_planner(start: int, end: int, outline_file: Path) -> int:
    """运行单个planner进程，负责指定范围，写入独立文件"""
    cmd = [
        sys.executable, str(SCRIPTS_DIR / "planner.py"),
        "--start", str(start), "--end", str(end),
        "--outline-file", str(outline_file)
    ]
    log(f"[Parallel] 启动 Planner: 第{start}-{end}章 -> {outline_file.name}")
    result = subprocess.run(cmd, capture_output=False, text=True, encoding="utf-8")
    return result.returncode


def merge_outlines(segments: list):
    """合并所有段的大纲文件到 outline.json"""
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

    # 按章节号排序
    all_chapters.sort(key=lambda ch: ch.get("chapter_number", 0))

    # 去重（以防万一）
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

    log(f"[Parallel] 合并完成: {len(unique_chapters)}/2000 章")
    return len(unique_chapters)


def main():
    print("=" * 70)
    print("Planner Parallel - 20 Agent 并行大纲生成")
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

    # 步骤2: 将2000章分成20段，每段约100章
    total_chapters = 2000
    num_workers = 20
    segment_size = total_chapters // num_workers  # 100

    segments = []
    for i in range(num_workers):
        start = i * segment_size + 1
        if i == num_workers - 1:
            end = total_chapters
        else:
            end = (i + 1) * segment_size
        outline_file = NOVELS_DIR / f"outline_part_{start:04d}_{end:04d}.json"
        segments.append((start, end, outline_file))

    log(f"[Parallel] 步骤2: 将2000章分成{len(segments)}段并行生成")
    for i, (s, e, f) in enumerate(segments):
        log(f"  段{i+1}: 第{s}-{e}章 -> {f.name}")

    # 步骤3: 并行启动60个planner
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

    # 汇总
    success = sum(1 for _, _, rc in results if rc == 0)
    log(f"[Parallel] 全部并行任务完成: {success}/{len(segments)} 段成功")

    # 步骤4: 合并所有段
    total = merge_outlines(segments)

    if total >= 2000:
        log("[Parallel] 大纲生成完成!")
    else:
        log(f"[Parallel] 警告: 大纲不完整，缺失 {2000 - total} 章")


if __name__ == "__main__":
    main()
