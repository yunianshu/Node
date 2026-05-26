#!/usr/bin/env python3
"""并行补全缺失的审查报告"""

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
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from core.novel_config import configure_stdio, get_webhook_url, load_config
from tool_paths import script_path
from core.workflow_state import load_review_status, review_dir, scan_chapter_status, write_status_file

configure_stdio()

NOVELS_DIR = None
REVIEWS_DIR = None
LOG_FILE = None
SCRIPTS_DIR = None
CONFIG = None


def init_project(project_dir: str | Path, final_mode: bool = False) -> None:
    global NOVELS_DIR, REVIEWS_DIR, LOG_FILE, SCRIPTS_DIR, CONFIG
    NOVELS_DIR = Path(project_dir).resolve()
    if final_mode:
        REVIEWS_DIR = NOVELS_DIR / "chapters" / "review_final"
    else:
        REVIEWS_DIR = review_dir(NOVELS_DIR)
    LOG_FILE = NOVELS_DIR / "logs" / "fill_reviews_parallel.log"
    SCRIPTS_DIR = Path(__file__).parent
    CONFIG = load_config(NOVELS_DIR)


def get_novel_title() -> str:
    world_file = NOVELS_DIR / "world.json"
    if world_file.exists():
        try:
            with open(world_file, "r", encoding="utf-8") as f:
                return json.load(f).get("title", "未知小说")
        except Exception:
            pass
    return "未知小说"


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


def run_reviewer(start: int, end: int, final_mode: bool = False) -> tuple:
    cmd = [
        sys.executable, str(script_path("reviewer.py")),
        "--project", str(NOVELS_DIR),
        "--start", str(start), "--end", str(end)
    ]
    if final_mode:
        cmd.append("--final")
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
    title = get_novel_title()
    while True:
        time.sleep(120)
        missing = get_missing_reviews()
        completed = CONFIG["total_chapters"] - len(missing)
        msg = f"📖 《{title}》\n📊 审查进度: {completed}/{CONFIG['total_chapters']} 章\n⏳ 缺失: {len(missing)} 章"
        push_wechat(msg)
        log(f"[WeChat] 进度已推送: 审查{completed}/{CONFIG['total_chapters']}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-p", type=str,
                        default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录")
    parser.add_argument("--final", action="store_true", help="终稿审查模式（review_final目录）")
    args = parser.parse_args()

    if not args.project:
        print("错误: 必须指定 --project 或设置 NOVEL_PROJECT_DIR 环境变量")
        sys.exit(1)

    init_project(args.project, final_mode=args.final)

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
        futures = {executor.submit(run_reviewer, s, e, args.final): (s, e) for s, e in ranges}
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
        title = get_novel_title()
        push_wechat(f"🎉 《{title}》审查全部完成！\n✅ {CONFIG['total_chapters']}/{CONFIG['total_chapters']} 章")


if __name__ == "__main__":
    main()

