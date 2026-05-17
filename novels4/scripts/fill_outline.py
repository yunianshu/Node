#!/usr/bin/env python3
"""补全缺失的大纲章节"""
import json
import subprocess
import sys
import time
from pathlib import Path

NOVELS_DIR = Path("D:/AiProject/Node/novels4")
OUTLINE_FILE = NOVELS_DIR / "outline.json"
SCRIPTS_DIR = Path(__file__).parent

# 缺失段列表
MISSING_RANGES = [
    (1, 25),
    (34, 58),
    (100, 124),
    (323, 330),
    (719, 726),
    (1189, 1221),
    (1255, 1279),
    (1618, 1642),
    (1750, 1774),
    (1816, 1840),
    (1973, 1997),
]


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")


def run_planner(start: int, end: int) -> int:
    """运行planner生成缺失段"""
    outfile = NOVELS_DIR / f"outline_part_{start:04d}_{end:04d}.json"
    cmd = [
        sys.executable, str(SCRIPTS_DIR / "planner.py"),
        "--start", str(start), "--end", str(end),
        "--outline-file", str(outfile)
    ]
    log(f"生成第{start}-{end}章 -> {outfile.name}")
    result = subprocess.run(cmd, capture_output=False, text=True, encoding="utf-8")
    return result.returncode


def merge_outlines():
    """合并所有段文件到 outline.json"""
    log("合并所有大纲段...")
    all_chapters = []

    # 读取现有outline
    if OUTLINE_FILE.exists():
        with open(OUTLINE_FILE, "r", encoding="utf-8") as f:
            outline = json.load(f)
        all_chapters.extend(outline.get("chapters", []))

    # 读取所有part文件
    for part_file in sorted(NOVELS_DIR.glob("outline_part_*.json")):
        try:
            with open(part_file, "r", encoding="utf-8") as f:
                part = json.load(f)
            chapters = part.get("chapters", [])
            all_chapters.extend(chapters)
        except Exception as e:
            log(f"读取 {part_file.name} 失败: {e}")

    # 去重排序
    seen = set()
    unique = []
    for ch in all_chapters:
        num = ch.get("chapter_number", 0)
        if num and num not in seen:
            seen.add(num)
            unique.append(ch)
    unique.sort(key=lambda ch: ch.get("chapter_number", 0))

    with open(OUTLINE_FILE, "w", encoding="utf-8") as f:
        json.dump({"chapters": unique}, f, ensure_ascii=False, indent=2)

    log(f"合并完成: {len(unique)}/2000 章")
    return len(unique)


def main():
    print("=" * 60)
    print("补全缺失大纲")
    print("=" * 60)

    for start, end in MISSING_RANGES:
        rc = run_planner(start, end)
        if rc != 0:
            log(f"第{start}-{end}章生成失败 (rc={rc})")
        time.sleep(2)

    # 合并
    total = merge_outlines()
    log(f"大纲总计: {total}/2000 章")

    if total >= 2000:
        log("大纲补全完成!")
    else:
        log(f"仍有缺失: {2000 - total} 章")


if __name__ == "__main__":
    main()
