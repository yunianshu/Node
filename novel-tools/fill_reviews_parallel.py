#!/usr/bin/env python3
"""并行补全缺失的审查报告"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from novel_config import configure_stdio, get_webhook_url, load_config
from workflow_state import load_review_status, scan_chapter_status, write_status_file

configure_stdio()

NOVELS_DIR = None
REVIEWS_DIR = None
LOG_FILE = None
SCRIPTS_DIR = None
CONFIG = None


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, REVIEWS_DIR, LOG_FILE, SCRIPTS_DIR, CONFIG
    NOVELS_DIR = Path(project_dir).resolve()
    REVIEWS_DIR = NOVELS_DIR / "reviews"
    LOG_FILE = NOVELS_DIR / "logs" / "fill_reviews_parallel.log"
    SCRIPTS_DIR = Path(__file__).parent
    CONFIG = load_config(NOVELS_DIR)


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()


def push_wechat(msg: str):
    url = get_webhook_url(CONFIG)
    if not url:
        return
    import urllib.request
    data = json.dumps({"msgtype": "text", "text": {"content": msg}}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"[WeChat] 推送失败: {e}")


def get_missing_reviews():
    missing = []
    for ch in range(1, CONFIG["total_chapters"] + 1):
        review_file = REVIEWS_DIR / f"chapter_{ch:04d}_review.json"
        _, _, _, ok = load_review_status(review_file)
        if not ok:
            missing.append(ch)
    return missing


def run_reviewer(start: int, end: int) -> tuple:
    cmd = [
        sys.executable, str(SCRIPTS_DIR / "reviewer.py"),
        "--project", str(NOVELS_DIR),
        "--start", str(start), "--end", str(end)
    ]
    result = subprocess.run(cmd, capture_output=False, text=True, encoding="utf-8")
    newly_completed = 0
    for ch in range(start, end + 1):
        f = REVIEWS_DIR / f"chapter_{ch:04d}_review.json"
        if f.exists():
            try:
                data = json.load(open(f, encoding="utf-8"))
                if data.get("status") == "completed":
                    newly_completed += 1
            except Exception:
                pass
    return (start, end, result.returncode, newly_completed)


def chunk_ranges(missing: list, chunk_size: int = 30) -> list:
    if not missing:
        return []
    ranges = []
    start = missing[0]
    prev = missing[0]
    for ch in missing[1:]:
        if ch == prev + 1:
            prev = ch
        else:
            ranges.append((start, prev))
            start = ch
            prev = ch
    ranges.append((start, prev))
    final_ranges = []
    for s, e in ranges:
        while s <= e:
            seg_end = min(s + chunk_size - 1, e)
            final_ranges.append((s, seg_end))
            s = seg_end + 1
    return final_ranges


def pusher_thread():
    while True:
        time.sleep(120)
        missing = get_missing_reviews()
        completed = CONFIG["total_chapters"] - len(missing)
        msg = f"📊 小说生成进度\n审查: {completed}/{CONFIG['total_chapters']} 章\n缺失: {len(missing)} 章"
        push_wechat(msg)
        log(f"[WeChat] 进度已推送: 审查{completed}/{CONFIG['total_chapters']}")


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
    print("Fill Reviews Parallel Agent 启动")
    print(f"项目: {NOVELS_DIR}")
    print("=" * 60)

    missing = get_missing_reviews()
    log(f"需要审查的章节: {len(missing)} 章")
    if not missing:
        log("所有审查报告已完成！")
        return

    ranges = chunk_ranges(missing, chunk_size=30)
    log(f"拆分为 {len(ranges)} 个任务范围")

    pusher = threading.Thread(target=pusher_thread, daemon=True)
    pusher.start()

    max_workers = min(CONFIG["coordinator"]["review_workers"], len(ranges))
    completed_total = CONFIG["total_chapters"] - len(missing)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(run_reviewer, s, e): (s, e) for s, e in ranges}
        for future in as_completed(futures):
            s, e = futures[future]
            try:
                start_r, end_r, rc, newly = future.result()
                completed_total += newly
                log(f"[Worker] 第{s}-{e}章审查完成，本范围完成 {newly} 章，总完成 {completed_total}/{CONFIG['total_chapters']}")
            except Exception as exc:
                log(f"[Worker] 第{s}-{e}章审查异常: {exc}")

    missing = get_missing_reviews()
    statuses = scan_chapter_status(NOVELS_DIR, 1, CONFIG["total_chapters"])
    write_status_file(NOVELS_DIR, statuses.values())
    log(f"最终审查: {CONFIG['total_chapters'] - len(missing)}/{CONFIG['total_chapters']} 章")
    if missing:
        log(f"仍有缺失: {len(missing)} 章: {missing[:20]}...")
    else:
        log("全部审查完成！")
        push_wechat(f"🎉 小说审查全部完成！{CONFIG['total_chapters']}/{CONFIG['total_chapters']} 章")


if __name__ == "__main__":
    main()
