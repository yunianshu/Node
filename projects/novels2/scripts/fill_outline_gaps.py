#!/usr/bin/env python3
"""
补充大纲缺失章节 - 并行补充缺失范围
"""
import concurrent.futures
import json
import subprocess
import sys
from pathlib import Path

NOVELS_DIR = Path("D:/AiProject/Node/novels2")
SCRIPTS_DIR = Path(__file__).parent
OUTLINE_FILE = NOVELS_DIR / "outline.json"

def find_missing_ranges():
    """找出缺失的章节范围"""
    outline = json.load(open(OUTLINE_FILE, encoding='utf-8'))
    chapters = outline.get('chapters', [])
    seen = set(ch.get('chapter_number', 0) for ch in chapters)

    missing = [i for i in range(1, 2001) if i not in seen]
    if not missing:
        return []

    # 将缺失章节分组为连续范围
    ranges = []
    start = missing[0]
    prev = missing[0]
    for m in missing[1:]:
        if m == prev + 1:
            prev = m
        else:
            ranges.append((start, prev))
            start = m
            prev = m
    ranges.append((start, prev))
    return ranges


def fill_range(start: int, end: int) -> int:
    """为指定范围生成大纲"""
    cmd = [sys.executable, str(SCRIPTS_DIR / "planner.py"), "--start", str(start), "--end", str(end), "--outline-only"]
    print(f"[Fill] 启动 {start}-{end}")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    return result.returncode


def merge_parts():
    """合并所有part到outline.json"""
    outline = json.load(open(OUTLINE_FILE, encoding='utf-8'))
    seen = set(ch.get('chapter_number', 0) for ch in outline.get('chapters', []))
    all_chapters = list(outline.get('chapters', []))

    for p in sorted(NOVELS_DIR.glob("outline_part_*.json")):
        try:
            d = json.load(open(p, encoding='utf-8'))
            for ch in d.get('chapters', []):
                num = ch.get('chapter_number', 0)
                if num and num not in seen:
                    seen.add(num)
                    all_chapters.append(ch)
        except:
            pass

    all_chapters.sort(key=lambda x: x.get('chapter_number', 0))
    outline = {'chapters': all_chapters}
    with open(OUTLINE_FILE, 'w', encoding='utf-8') as f:
        json.dump(outline, f, ensure_ascii=False, indent=2)

    return len(all_chapters)


def main():
    ranges = find_missing_ranges()
    if not ranges:
        print("[Fill] 大纲已完整！")
        return

    print(f"[Fill] 发现 {len(ranges)} 个缺失范围，共 {sum(e-s+1 for s,e in ranges)} 章")
    print(f"[Fill] 范围: {ranges}")

    # 限制并发数为5
    max_workers = 5

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fill_range, s, e): (s, e)
            for s, e in ranges
        }
        for future in concurrent.futures.as_completed(futures):
            s, e = futures[future]
            try:
                rc = future.result()
                print(f"[Fill] {s}-{e} 完成 (rc={rc})")
            except Exception as exc:
                print(f"[Fill] {s}-{e} 异常: {exc}")

    # 合并
    total = merge_parts()
    print(f"[Fill] 合并完成，共 {total}/2000 章")

    # 再次检查
    ranges = find_missing_ranges()
    if ranges:
        print(f"[Fill] 仍有缺失: {ranges}")
    else:
        print("[Fill] 大纲已完整！")


if __name__ == "__main__":
    main()
