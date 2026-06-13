#!/usr/bin/env python3
"""企业微信机器人进度推送 - 统一格式版本"""

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

import argparse
import json
import os
import time
from pathlib import Path

from core.novel_config import load_config, resolve_project_dir
from core.push_notifier import push_progress
from core.workflow_state import outline_completed_count, scan_chapter_status

NOVELS_DIR = None
CONFIG = None


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, CONFIG
    NOVELS_DIR = Path(project_dir).resolve()
    CONFIG = load_config(NOVELS_DIR)


def get_progress_data():
    """获取进度数据"""
    outline_count = outline_completed_count(NOVELS_DIR, 1, CONFIG["total_chapters"])

    statuses = scan_chapter_status(NOVELS_DIR, 1, CONFIG["total_chapters"])
    outline_reviewed_count = sum(1 for s in statuses.values() if s.outline_review_status == "completed")
    outline_approved_count = sum(1 for s in statuses.values() if s.outline_review_ok)
    draft_count = sum(1 for s in statuses.values() if s.draft_exists)
    review_count = sum(1 for s in statuses.values() if s.review_ok)
    final_count = sum(1 for s in statuses.values() if s.final_ok)
    total_words = sum(s.draft_words for s in statuses.values() if s.draft_exists)
    scores = [s.review_score for s in statuses.values() if s.final_ok and s.review_score is not None]
    avg_score = sum(scores) / len(scores) if scores else None
    status_note = current_status_note(statuses)
    eta_text = estimate_remaining_time(NOVELS_DIR, CONFIG["total_chapters"], final_count)

    return {
        "outline": outline_count,
        "outline_reviewed": outline_reviewed_count,
        "outline_approved": outline_approved_count,
        "draft": draft_count,
        "reviewed": review_count,
        "final": final_count,
        "total_words": total_words,
        "avg_score": avg_score,
        "status_note": status_note,
        "eta_text": eta_text,
    }


def current_status_note(statuses: dict) -> str:
    for chapter, status in statuses.items():
        if not status.outline_review_ok:
            if status.outline_review_exists:
                return f"第{chapter}章大纲未过审 status={status.outline_review_status} score={status.outline_review_score}"
            return f"第{chapter}章大纲审缺失，等待大纲生成/审查"
    for chapter, status in statuses.items():
        if not status.final_ok:
            if not status.review_ok and status.review_exists:
                return f"第{chapter}章初稿未过审 status={status.review_status} score={status.review_score}"
            if status.draft_exists:
                return f"第{chapter}章初稿已生成，等待审查/终稿质量门"
            return f"第{chapter}章等待初稿生成"
    return "全部章节已通过当前质量门"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}天{hours}小时"
    if hours > 0:
        return f"{hours}小时{minutes}分钟"
    return f"{max(1, minutes)}分钟"


def estimate_remaining_time(project: Path, total: int, final_count: int) -> str:
    remaining = max(0, total - final_count)
    if remaining == 0:
        return "已完成"

    final_dir = project / "chapters" / "final"
    if not final_dir.exists():
        return "样本不足"

    now = time.time()
    windows = (6 * 3600, 24 * 3600, 72 * 3600)
    files = [
        path for path in final_dir.glob("chapter_*.txt")
        if path.is_file() and path.stat().st_size > 0
    ]
    for window in windows:
        recent = [path.stat().st_mtime for path in files if now - path.stat().st_mtime <= window]
        if len(recent) >= 2:
            elapsed = max(1.0, max(recent) - min(recent))
            rate = (len(recent) - 1) / elapsed
            if rate > 0:
                return format_duration(remaining / rate)
    return "样本不足"


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-p", type=str,
                        default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录")
    args = parser.parse_args()

    try:
        project = resolve_project_dir(args.project)
    except ValueError as exc:
        print(f"错误: {exc}")
        sys.exit(1)

    init_project(project)

    p = get_progress_data()
    title = _get_book_title()

    ok = push_progress(
        config=CONFIG,
        title=title,
        outline=p["outline"],
        outline_reviewed=p["outline_reviewed"],
        outline_approved=p["outline_approved"],
        draft=p["draft"],
        reviewed=p["reviewed"],
        final=p["final"],
        total_words=p["total_words"],
        total_chapters=CONFIG["total_chapters"],
        active_writers=0,
        avg_score=p["avg_score"],
        status_note=p["status_note"],
        eta_text=p["eta_text"],
    )
    print("推送成功" if ok else "推送失败")


if __name__ == "__main__":
    main()
