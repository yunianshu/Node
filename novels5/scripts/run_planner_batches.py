#!/usr/bin/env python3
"""
分批串行生成大纲 - 每批100章，共20批
稳定可靠，适合Windows环境
"""
import json
import subprocess
import sys
import time
from pathlib import Path

NOVELS_DIR = Path("D:/AiProject/Node/novels5")
SCRIPTS_DIR = Path(__file__).parent

def log(msg):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)

def run_batch(start, end):
    outline_file = NOVELS_DIR / f"outline_part_{start:04d}_{end:04d}.json"
    if outline_file.exists():
        try:
            with open(outline_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            count = len(data.get("chapters", []))
            if count >= (end - start + 1):
                log(f"跳过 {start}-{end}（已存在 {count} 章）")
                return 0
        except:
            pass

    log(f"开始生成 {start}-{end} 章...")
    cmd = [
        sys.executable, str(SCRIPTS_DIR / "planner.py"),
        "--start", str(start), "--end", str(end),
        "--outline-file", str(outline_file)
    ]
    result = subprocess.run(cmd, capture_output=False, text=True, encoding="utf-8")
    return result.returncode

def merge_outlines():
    log("合并所有大纲...")
    all_chapters = []
    for f in sorted(NOVELS_DIR.glob("outline_part_*.json")):
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            chapters = data.get("chapters", [])
            all_chapters.extend(chapters)
            log(f"  {f.name}: {len(chapters)} 章")
        except Exception as e:
            log(f"  {f.name}: 读取失败 {e}")

    all_chapters.sort(key=lambda ch: ch.get("chapter_number", 0))
    seen = set()
    unique = []
    for ch in all_chapters:
        num = ch.get("chapter_number", 0)
        if num not in seen:
            seen.add(num)
            unique.append(ch)

    outline = {"chapters": unique}
    with open(NOVELS_DIR / "outline.json", "w", encoding="utf-8") as f:
        json.dump(outline, f, ensure_ascii=False, indent=2)
    log(f"合并完成: {len(unique)}/2000 章")

def main():
    log("=" * 60)
    log("Planner Batches - 分批生成大纲")
    log("=" * 60)

    batch_size = 100
    total = 2000
    success = 0
    fail = 0

    for start in range(1, total + 1, batch_size):
        end = min(start + batch_size - 1, total)
        rc = run_batch(start, end)
        if rc == 0:
            success += 1
        else:
            fail += 1
            log(f"批次 {start}-{end} 失败")

    log(f"批次完成: {success} 成功, {fail} 失败")
    merge_outlines()
    log("全部完成")

if __name__ == "__main__":
    main()
