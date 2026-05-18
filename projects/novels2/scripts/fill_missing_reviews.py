#!/usr/bin/env python3
"""补充缺失的审查报告"""
import json
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import subprocess

NOVELS_DIR = Path("D:/AiProject/Node/novels2")
REVIEWS_DIR = NOVELS_DIR / "reviews"
LOG_FILE = NOVELS_DIR / "logs" / "fill_reviews.log"

SCRIPT = Path("D:/AiProject/Node/novels2/scripts/reviewer.py")


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_missing_reviews():
    review_nums = set()
    if REVIEWS_DIR.exists():
        for c in REVIEWS_DIR.glob("chapter_*_review.json"):
            try:
                review_nums.add(int(c.stem.split("_")[1]))
            except:
                pass
    return [i for i in range(1, 2001) if i not in review_nums]


def review_one(chapter: int) -> dict:
    try:
        result = subprocess.run(
            ["python", str(SCRIPT), "--chapter", str(chapter)],
            capture_output=True, text=True, encoding="utf-8", timeout=120
        )
        if result.returncode == 0:
            return {"chapter": chapter, "status": "success"}
        else:
            return {"chapter": chapter, "status": "failed", "err": result.stderr[:200]}
    except Exception as e:
        return {"chapter": chapter, "status": "error", "err": str(e)}


def main():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    missing = get_missing_reviews()
    log(f"[FillReviews] 缺失审查报告: {len(missing)} 章")
    log(f"[FillReviews] 缺失列表: {missing}")

    if not missing:
        log("[FillReviews] 无缺失，退出")
        return

    success = 0
    failed = 0
    workers = min(5, len(missing))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(review_one, ch): ch for ch in missing}
        for future in as_completed(future_map):
            ch = future_map[future]
            try:
                result = future.result()
                if result["status"] == "success":
                    success += 1
                    log(f"[FillReviews] 第{ch}章审查完成 ({success}/{len(missing)})")
                else:
                    failed += 1
                    log(f"[FillReviews] 第{ch}章审查失败: {result.get('err', '')}")
            except Exception as e:
                failed += 1
                log(f"[FillReviews] 第{ch}章异常: {e}")

    log(f"[FillReviews] 完成: 成功 {success}, 失败 {failed}")


if __name__ == "__main__":
    main()
