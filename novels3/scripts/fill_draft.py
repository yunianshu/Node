#!/usr/bin/env python3
"""补全缺失的初稿章节"""
import subprocess
import sys
import time
from pathlib import Path

NOVELS_DIR = Path("D:/AiProject/Node/novels3")
CHAPTERS_DIR = NOVELS_DIR / "chapters" / "draft"


def get_missing_ranges():
    """获取缺失的章节范围"""
    existing = set()
    for f in CHAPTERS_DIR.glob("chapter_*.txt"):
        if f.stat().st_size > 1000:
            num = int(f.stem.split("_")[1])
            existing.add(num)

    missing = [i for i in range(1, 2001) if i not in existing]
    if not missing:
        return []

    # 分组
    groups = []
    start = missing[0]
    prev = missing[0]
    for n in missing[1:]:
        if n == prev + 1:
            prev = n
        else:
            groups.append((start, prev))
            start = n
            prev = n
    groups.append((start, prev))
    return groups


def main():
    print("=" * 60)
    print("补全缺失初稿")
    print("=" * 60)

    missing_ranges = get_missing_ranges()
    total_missing = sum(e - s + 1 for s, e in missing_ranges)
    print(f"缺失: {total_missing} 章, 共 {len(missing_ranges)} 段")

    for s, e in missing_ranges:
        print(f"  {s}-{e} ({e-s+1}章)")

    if not missing_ranges:
        print("初稿已完整!")
        return

    # 逐段生成
    for s, e in missing_ranges:
        print(f"\n生成第{s}-{e}章...")
        cmd = [
            sys.executable, str(Path(__file__).parent / "writer.py"),
            "--start", str(s), "--end", str(e)
        ]
        result = subprocess.run(cmd, capture_output=False, text=True, encoding="utf-8")
        if result.returncode != 0:
            print(f"第{s}-{e}章生成失败")
        time.sleep(2)

    print("\n补全完成")


if __name__ == "__main__":
    main()
