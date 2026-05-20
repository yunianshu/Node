#!/usr/bin/env python3
"""并行补全缺失的初稿章节 - 高效版本"""

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from core.novel_config import configure_stdio, load_config
from core.push_notifier import push_progress as _push_progress, push_stage_complete as _push_stage_complete
from tool_paths import script_path
from core.workflow_state import scan_chapter_status

configure_stdio()

NOVELS_DIR = None
CHAPTERS_DIR = None
SCRIPTS_DIR = None
LOG_FILE = None
CONFIG = None

_progress_lock = threading.Lock()
_last_push_time = 0
_completed_count = 0
_total_missing = 0
_pusher_started = False


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, CHAPTERS_DIR, SCRIPTS_DIR, LOG_FILE, CONFIG
    NOVELS_DIR = Path(project_dir).resolve()
    CHAPTERS_DIR = NOVELS_DIR / "chapters" / "draft"
    SCRIPTS_DIR = Path(__file__).parent
    LOG_FILE = NOVELS_DIR / "logs" / "fill_draft_parallel.log"
    CONFIG = load_config(NOVELS_DIR)


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _get_book_title():
    """获取书名，从 world.json 读取"""
    world_file = NOVELS_DIR / "world.json"
    if world_file.exists():
        try:
            with open(world_file, "r", encoding="utf-8") as f:
                world = json.load(f)
            return world.get("title", NOVELS_DIR.name)
        except Exception:
            pass
    return NOVELS_DIR.name


def get_progress_summary():
    statuses = scan_chapter_status(NOVELS_DIR, 1, CONFIG["total_chapters"])
    completed = sum(1 for s in statuses.values() if s.draft_ok)
    total_words = 0
    if CHAPTERS_DIR.exists():
        for f in CHAPTERS_DIR.glob("chapter_*.txt"):
            if f.stat().st_size > 1000:
                completed += 1
                total_words += len(f.read_text(encoding="utf-8"))
    return {"draft": completed, "total_words": total_words}


def progress_pusher_thread(interval_seconds: int = 120):
    global _last_push_time
    while True:
        time.sleep(interval_seconds)
        with _progress_lock:
            now = time.time()
            if now - _last_push_time < interval_seconds:
                continue
            _last_push_time = now

        p = get_progress_summary()
        title = _get_book_title()
        _push_progress(
            config=CONFIG,
            title=title,
            outline=0,
            draft=p["draft"],
            reviewed=0,
            final=0,
            total_words=p["total_words"],
            total_chapters=CONFIG["total_chapters"],
            active_writers=0,
        )
        log(f"[WeChat] 进度已推送: 初稿{p['draft']}/{CONFIG['total_chapters']}")


def get_missing_ranges():
    statuses = scan_chapter_status(NOVELS_DIR, 1, CONFIG["total_chapters"])
    missing = [i for i in range(1, CONFIG["total_chapters"] + 1) if not statuses[i].draft_ok]
    if not missing:
        return []

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


def run_writer(start: int, end: int) -> tuple:
    global _completed_count
    log(f"[Worker] 开始生成第{start}-{end}章")
    cmd = [
        sys.executable, str(script_path("writer.py")),
        "--project", str(NOVELS_DIR),
        "--start", str(start), "--end", str(end)
    ]
    result = subprocess.run(cmd, capture_output=False, text=True, encoding="utf-8")

    newly_completed = 0
    for ch in range(start, end + 1):
        f = CHAPTERS_DIR / f"chapter_{ch:04d}.txt"
        if f.exists() and f.stat().st_size > 1000:
            newly_completed += 1

    with _progress_lock:
        _completed_count += newly_completed

    if result.returncode == 0:
        log(f"[Worker] 第{start}-{end}章完成，本范围生成 {newly_completed} 章")
    else:
        log(f"[Worker] 第{start}-{end}章返回非零码 (rc={result.returncode})")

    return (start, end, result.returncode, newly_completed)


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
    print("并行补全缺失初稿 - 高效版本")
    print(f"项目: {NOVELS_DIR}")
    print("=" * 60)

    global _pusher_started
    if not _pusher_started:
        _pusher_started = True
        pusher = threading.Thread(target=progress_pusher_thread, args=(120,), daemon=True)
        pusher.start()
        log("[Main] 企业微信进度推送已启动（每2分钟）")

    missing_ranges = get_missing_ranges()
    total_missing = sum(e - s + 1 for s, e in missing_ranges)
    log(f"缺失: {total_missing} 章, 共 {len(missing_ranges)} 段")

    for s, e in missing_ranges:
        log(f"  {s}-{e} ({e-s+1}章)")

    if not missing_ranges:
        log("初稿已完整!")
        return

    global _total_missing
    _total_missing = total_missing

    max_workers = min(CONFIG["coordinator"]["num_workers"], len(missing_ranges))
    log(f"[Main] 启动 {max_workers} 个并发 Worker 生成 {len(missing_ranges)} 段缺失章节")

    completed = 0
    failed = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(run_writer, s, e): (s, e)
            for s, e in missing_ranges
        }
        for future in concurrent.futures.as_completed(futures):
            s, e = futures[future]
            try:
                start, end, rc, count = future.result()
                if rc == 0:
                    completed += 1
                else:
                    failed += 1
            except Exception as exc:
                log(f"[Main] 第{s}-{e}章异常: {exc}")
                failed += 1

    p = get_progress_summary()
    title = _get_book_title()
    log(f"[Main] 补全完成: 初稿 {p['draft']}/{CONFIG['total_chapters']} 章, 总字数 {p['total_words']:,}")

    if missing_ranges:
        first_start = missing_ranges[0][0]
        last_end = missing_ranges[-1][1]
    else:
        first_start = last_end = 0

    _push_stage_complete(
        config=CONFIG,
        title=title,
        stage="补全",
        start_chapter=first_start,
        end_chapter=last_end,
        processed=p["draft"],
        failed=failed,
    )


if __name__ == "__main__":
    main()

