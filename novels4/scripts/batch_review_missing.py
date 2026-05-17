#!/usr/bin/env python3
"""批量审查缺失的章节报告"""
import concurrent.futures
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from reviewer import review_chapter, log

NOVELS_DIR = Path("D:/AiProject/Node/novels4")
REVIEWS_DIR = NOVELS_DIR / "reviews"

def get_missing_reviews():
    existing = set()
    for f in REVIEWS_DIR.glob("chapter_*_review.json"):
        try:
            num = int(f.stem.split("_")[1])
            existing.add(num)
        except:
            pass
    return [i for i in range(1, 2001) if i not in existing]

def review_single(ch):
    try:
        result = review_chapter(ch)
        return ch, result.get("status", "unknown")
    except Exception as e:
        log(f"[BatchReview] 第{ch}章异常: {e}")
        return ch, "error"

def main():
    missing = get_missing_reviews()
    log(f"[BatchReview] 共 {len(missing)} 章需要审查")
    if not missing:
        return

    completed = 0
    failed = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(review_single, ch): ch for ch in missing}
        for future in concurrent.futures.as_completed(futures):
            ch, status = future.result()
            if status in ("completed", "exists"):
                completed += 1
            else:
                failed.append(ch)
            if (completed + len(failed)) % 10 == 0:
                log(f"[BatchReview] 进度: {completed}/{len(missing)} 完成, 失败: {len(failed)}")

    log(f"[BatchReview] 完成: {completed} 成功, {len(failed)} 失败")
    if failed:
        log(f"[BatchReview] 失败章节: {failed}")

if __name__ == "__main__":
    main()
