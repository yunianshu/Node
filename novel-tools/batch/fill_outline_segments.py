#!/usr/bin/env python3
"""补全缺失的大纲段，使用低并发避免API限流"""

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
from core.workflow_state import outline_index_path, write_outline_chapters
from tool_paths import script_path

NOVELS_DIR = None
SCRIPTS_DIR = None
OUTLINE_FILE = None
CONFIG = None
ALL_SEGMENTS = []


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, SCRIPTS_DIR, OUTLINE_FILE, CONFIG, ALL_SEGMENTS
    NOVELS_DIR = Path(project_dir).resolve()
    SCRIPTS_DIR = Path(__file__).parent
    OUTLINE_FILE = outline_index_path(NOVELS_DIR)
    CONFIG = load_config(NOVELS_DIR)

    total_chapters = CONFIG["total_chapters"]
    num_workers = 60
    segment_size = total_chapters // num_workers

    ALL_SEGMENTS = []
    for i in range(num_workers):
        start = i * segment_size + 1
        end = total_chapters if i == num_workers - 1 else (i + 1) * segment_size
        outfile = NOVELS_DIR / f"outline_part_{start:04d}_{end:04d}.json"
        ALL_SEGMENTS.append((start, end, outfile))


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")


def check_segment(start: int, end: int, outfile: Path) -> bool:
    """检查段是否完整生成"""

    if not outfile.exists():
        return False
    try:
        with open(outfile, "r", encoding="utf-8") as f:
            d = json.load(f)
        chapters = d.get("chapters", [])
        expected = end - start + 1
        if len(chapters) >= expected * 0.7:  # 至少70%完成算有效
            return True
    except:
        pass
    return False


def run_planner(start: int, end: int, outfile: Path) -> int:
    """运行planner生成缺失段"""
    cmd = [
        sys.executable, str(script_path("planner.py")),
        "--project", str(NOVELS_DIR),
        "--start", str(start), "--end", str(end),
        "--outline-file", str(outfile)
    ]
    log(f"生成第{start}-{end}章")
    result = subprocess.run(cmd, capture_output=False, text=True, encoding="utf-8")
    return result.returncode


def merge_outlines():
    """合并所有段文件到 outline.json"""
    log("合并所有大纲段...")
    all_chapters = []

    for start, end, outfile in ALL_SEGMENTS:
        if not outfile.exists():
            continue
        try:
            with open(outfile, "r", encoding="utf-8") as f:
                part = json.load(f)
            chapters = part.get("chapters", [])
            all_chapters.extend(chapters)
        except Exception as e:
            log(f"读取 {outfile.name} 失败: {e}")

    # 去重排序
    seen = set()
    unique = []
    for ch in all_chapters:
        num = ch.get("chapter_number", 0)
        if num and num not in seen:
            seen.add(num)
            unique.append(ch)
    unique.sort(key=lambda ch: ch.get("chapter_number", 0))

    outline = {"chapters": unique}
    with open(OUTLINE_FILE, "w", encoding="utf-8") as f:
        json.dump(outline, f, ensure_ascii=False, indent=2)
    write_outline_chapters(NOVELS_DIR, outline)

    total = CONFIG["total_chapters"]
    log(f"合并完成: {len(unique)}/{total} 章")
    return len(unique)


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

    print("=" * 60)
    print("补全缺失大纲段")
    print(f"项目: {NOVELS_DIR}")
    print("=" * 60)

    # 检查哪些段缺失
    missing_segments = []
    for start, end, outfile in ALL_SEGMENTS:
        if not check_segment(start, end, outfile):
            missing_segments.append((start, end, outfile))

    log(f"总段数: {len(ALL_SEGMENTS)}")
    log(f"已完整: {len(ALL_SEGMENTS) - len(missing_segments)}")
    log(f"需补全: {len(missing_segments)}")

    if not missing_segments:
        log("所有段已完整，直接合并")
        merge_outlines()
        return

    # 显示缺失段
    for start, end, _ in missing_segments:
        log(f"  缺失: 第{start}-{end}章")

    # 分批生成缺失段，每批5个并发（避免API限流）
    batch_size = 5
    total_missing = len(missing_segments)

    for i in range(0, total_missing, batch_size):
        batch = missing_segments[i:i+batch_size]
        log(f"=== 批次 {i//batch_size + 1}/{(total_missing + batch_size - 1)//batch_size}: 第{[f'{s}-{e}' for s,e,_ in batch]}章 ===")

        import concurrent.futures
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(batch)) as executor:
            futures = {
                executor.submit(run_planner, s, e, f): (s, e)
                for s, e, f in batch
            }
            for future in concurrent.futures.as_completed(futures):
                s, e = futures[future]
                try:
                    rc = future.result()
                    results.append((s, e, rc))
                    if rc == 0:
                        log(f"  {s}-{e} 完成")
                    else:
                        log(f"  {s}-{e} 失败 (rc={rc})")
                except Exception as exc:
                    log(f"  {s}-{e} 异常: {exc}")
                    results.append((s, e, -1))

        # 批次间等待，避免API限流
        if i + batch_size < total_missing:
            log("等待10秒...")
            time.sleep(10)

    # 合并
    total = merge_outlines()
    total_chapters = CONFIG["total_chapters"]

    if total >= total_chapters:
        log("大纲补全完成!")
    else:
        log(f"仍有缺失: {total_chapters - total} 章")


if __name__ == "__main__":
    main()
