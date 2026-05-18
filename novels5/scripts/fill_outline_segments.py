#!/usr/bin/env python3
"""补全缺失的大纲段，使用低并发避免API限流"""
import concurrent.futures
import json
import subprocess
import sys
import time
from pathlib import Path

NOVELS_DIR = Path("D:/AiProject/Node/novels5")
SCRIPTS_DIR = Path(__file__).parent
OUTLINE_FILE = NOVELS_DIR / "outline.json"

# 2000章分成60段
total_chapters = 2000
num_workers = 60
segment_size = total_chapters // num_workers

# 构建所有段
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
        sys.executable, str(SCRIPTS_DIR / "planner.py"),
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

    with open(OUTLINE_FILE, "w", encoding="utf-8") as f:
        json.dump({"chapters": unique}, f, ensure_ascii=False, indent=2)

    log(f"合并完成: {len(unique)}/2000 章")
    return len(unique)


def main():
    print("=" * 60)
    print("补全缺失大纲段")
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

    if total >= 2000:
        log("大纲补全完成!")
    else:
        log(f"仍有缺失: {2000 - total} 章")


if __name__ == "__main__":
    main()
