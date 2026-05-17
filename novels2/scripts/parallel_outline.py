#!/usr/bin/env python3
"""
并行大纲生成器 - 使用100个并行Agent生成2000章大纲
"""
import concurrent.futures
import json
import subprocess
import sys
import time
from pathlib import Path

NOVELS_DIR = Path("D:/AiProject/Node/novels2")
SCRIPTS_DIR = Path(__file__).parent
OUTLINE_FILE = NOVELS_DIR / "outline.json"

def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)


def run_planner_range(start: int, end: int) -> int:
    """运行planner处理指定范围"""
    cmd = [sys.executable, str(SCRIPTS_DIR / "planner.py"), "--start", str(start), "--end", str(end), "--outline-only"]
    log(f"[Parallel] 启动 Planner {start}-{end}")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    return result.returncode


def merge_outlines():
    """合并所有outline_part文件到outline.json"""
    log("[Parallel] 开始合并所有大纲部分...")
    all_chapters = []

    # 先读取已有的outline.json（保留之前的进度）
    if OUTLINE_FILE.exists():
        try:
            with open(OUTLINE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            existing = data.get("chapters", [])
            all_chapters.extend(existing)
            log(f"[Parallel] 保留已有大纲: {len(existing)} 章")
        except Exception as e:
            log(f"[Parallel] 读取已有大纲失败: {e}")

    part_files = sorted(NOVELS_DIR.glob("outline_part_*.json"))
    if not part_files:
        log("[Parallel] 未找到任何新的part文件")
        return

    for part_file in part_files:
        try:
            with open(part_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            chapters = data.get("chapters", [])
            all_chapters.extend(chapters)
            log(f"[Parallel] 合并 {part_file.name}: {len(chapters)} 章")
        except Exception as e:
            log(f"[Parallel] 合并失败 {part_file.name}: {e}")

    # 去重并按章节号排序
    seen = set()
    unique_chapters = []
    for ch in sorted(all_chapters, key=lambda x: x.get("chapter_number", 0)):
        num = ch.get("chapter_number", 0)
        if num and num not in seen:
            seen.add(num)
            unique_chapters.append(ch)

    outline = {"chapters": unique_chapters}
    with open(OUTLINE_FILE, "w", encoding="utf-8") as f:
        json.dump(outline, f, ensure_ascii=False, indent=2)

    log(f"[Parallel] 合并完成，共 {len(unique_chapters)} 章 -> {OUTLINE_FILE}")


def main():
    num_workers = 10
    total_chapters = 2000
    chapters_per_worker = total_chapters // num_workers  # 200章/worker

    log("=" * 60)
    log(f"并行大纲生成器启动: {num_workers}个Agent × {chapters_per_worker}章 = {total_chapters}章")
    log("=" * 60)

    # 构建范围列表
    ranges = []
    for i in range(num_workers):
        start = i * chapters_per_worker + 1
        end = min(start + chapters_per_worker - 1, total_chapters)
        if i == num_workers - 1:
            end = total_chapters  # 最后一个worker处理到2000
        ranges.append((start, end))

    log(f"[Parallel] 范围分配: {ranges[:3]} ... {ranges[-3:]}")

    # 并行执行
    success_count = 0
    fail_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {
            executor.submit(run_planner_range, s, e): (s, e)
            for s, e in ranges
        }
        for future in concurrent.futures.as_completed(futures):
            s, e = futures[future]
            try:
                rc = future.result()
                if rc == 0:
                    log(f"[Parallel] Planner {s}-{e} 完成 (rc={rc})")
                    success_count += 1
                else:
                    log(f"[Parallel] Planner {s}-{e} 失败 (rc={rc})")
                    fail_count += 1
            except Exception as exc:
                log(f"[Parallel] Planner {s}-{e} 异常: {exc}")
                fail_count += 1

    log(f"[Parallel] 全部完成: 成功 {success_count}/{num_workers}, 失败 {fail_count}/{num_workers}")

    # 合并所有部分
    merge_outlines()

    log("=" * 60)
    log("并行大纲生成结束")
    log("=" * 60)


if __name__ == "__main__":
    main()
