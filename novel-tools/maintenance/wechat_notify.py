#!/usr/bin/env python3
"""企业微信机器人进度推送"""

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

import argparse
import json
import os
import urllib.request
import time
from pathlib import Path

from core.novel_config import get_webhook_url, load_config
from core.workflow_state import outline_index_path, scan_chapter_status

NOVELS_DIR = None
CONFIG = None
WEBHOOK_URL = ""


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, CONFIG, WEBHOOK_URL
    NOVELS_DIR = Path(project_dir).resolve()
    CONFIG = load_config(NOVELS_DIR)
    WEBHOOK_URL = get_webhook_url(CONFIG)


def get_progress():
    outline_file = outline_index_path(NOVELS_DIR)
    outline_count = 0
    if outline_file.exists():
        try:
            with open(outline_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            chapters = data.get("chapters", [])
            outline_count = len(chapters)
        except Exception:
            pass

    statuses = scan_chapter_status(NOVELS_DIR, 1, CONFIG["total_chapters"])
    draft_count = sum(1 for s in statuses.values() if s.draft_exists)
    review_count = sum(1 for s in statuses.values() if s.review_ok)
    final_count = sum(1 for s in statuses.values() if s.final_ok)
    total_words = sum(s.draft_words for s in statuses.values() if s.draft_exists)
    scores = [s.review_score for s in statuses.values() if s.review_score is not None]
    avg_score = sum(scores) / len(scores) if scores else 0

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    msg = f"""生成任务完成! ({now})

━━━━━━━━━━━━━━━━━━━━
大纲: {outline_count}/{CONFIG['total_chapters']} 章
初稿: {draft_count}/{CONFIG['total_chapters']} 章
字数: {total_words:,}
审查: {review_count}/{CONFIG['total_chapters']} 章
终稿: {final_count}/{CONFIG['total_chapters']} 章
平均评分: {avg_score:.2f}
━━━━━━━━━━━━━━━━━━━━"""

    return msg


def send_wechat(msg: str):
    if not WEBHOOK_URL:
        print("警告: 未配置 webhook_url，跳过推送")
        return ""
    data = json.dumps({"msgtype": "text", "text": {"content": msg}}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(WEBHOOK_URL, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        return str(e)


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
    msg = get_progress()
    result = send_wechat(msg)
    print(result)


if __name__ == "__main__":
    main()
