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

from core.novel_config import load_config
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
    draft_count = sum(1 for s in statuses.values() if s.draft_exists)
    review_count = sum(1 for s in statuses.values() if s.review_ok)
    final_count = sum(1 for s in statuses.values() if s.final_ok)
    total_words = sum(s.draft_words for s in statuses.values() if s.draft_exists)
    scores = [s.review_score for s in statuses.values() if s.review_score is not None]
    avg_score = sum(scores) / len(scores) if scores else None

    return {
        "outline": outline_count,
        "draft": draft_count,
        "reviewed": review_count,
        "final": final_count,
        "total_words": total_words,
        "avg_score": avg_score,
    }


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

    if not args.project:
        print("错误: 必须指定 --project 或设置 NOVEL_PROJECT_DIR 环境变量")
        sys.exit(1)

    init_project(args.project)

    p = get_progress_data()
    title = _get_book_title()

    ok = push_progress(
        config=CONFIG,
        title=title,
        outline=p["outline"],
        draft=p["draft"],
        reviewed=p["reviewed"],
        final=p["final"],
        total_words=p["total_words"],
        total_chapters=CONFIG["total_chapters"],
        active_writers=0,
        avg_score=p["avg_score"],
    )
    print("推送成功" if ok else "推送失败")


if __name__ == "__main__":
    main()
