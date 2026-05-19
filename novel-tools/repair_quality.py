#!/usr/bin/env python3
"""按质量门修复历史产物。"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from novel_config import configure_stdio, load_config
from workflow_state import atomic_write_json, scan_chapter_status, write_status_file

configure_stdio()

NOVELS_DIR = None
SCRIPTS_DIR = None
CONFIG = None
REPAIR_STATE_FILE = None


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, SCRIPTS_DIR, CONFIG, REPAIR_STATE_FILE
    NOVELS_DIR = Path(project_dir).resolve()
    SCRIPTS_DIR = Path(__file__).parent
    CONFIG = load_config(NOVELS_DIR)
    REPAIR_STATE_FILE = NOVELS_DIR / "repair_state.json"


def select_chapters(mode: str) -> list[int]:
    statuses = scan_chapter_status(NOVELS_DIR, 1, CONFIG["total_chapters"])
    if mode == "draft":
        return [ch for ch, status in statuses.items() if not status.draft_ok]
    if mode == "review":
        return [ch for ch, status in statuses.items() if not status.review_ok]
    return [ch for ch, status in statuses.items() if not status.final_ok]


def sort_by_severity(mode: str, chapters: list[int]) -> list[int]:
    statuses = scan_chapter_status(NOVELS_DIR, 1, CONFIG["total_chapters"])
    grade_attr = "draft_grade" if mode == "draft" else "final_grade"
    rank = {"missing": 0, "hard_fail": 1, "warn": 2, "ok": 3}
    return sorted(chapters, key=lambda ch: (rank.get(getattr(statuses[ch], grade_attr, "hard_fail"), 1), ch))


def run_chapter(script: str, chapter: int, dry_run: bool) -> int:
    cmd = [sys.executable, str(SCRIPTS_DIR / script), "--chapter", str(chapter), "--project", str(NOVELS_DIR)]
    print(" ".join(cmd))
    if dry_run:
        return 0
    return subprocess.run(cmd, check=False, text=True, encoding="utf-8").returncode


def classify_failure(mode: str, chapter: int, rc: int, statuses: dict | None = None) -> str:
    if rc != 0:
        return "command_failed"
    statuses = statuses or scan_chapter_status(NOVELS_DIR, 1, CONFIG["total_chapters"])
    status = statuses.get(chapter)
    if not status:
        return "status_missing"
    if mode == "draft":
        if not status.draft_exists:
            return "draft_missing"
        if not status.draft_ok:
            return status.failed_reason or "draft_quality_failed"
    if mode == "review":
        if not status.review_exists:
            return "review_missing"
        if not status.review_ok:
            return status.review_status or "review_quality_failed"
    if mode == "final":
        if not status.final_exists:
            return "final_missing"
        if not status.final_ok:
            return status.failed_reason or "final_quality_failed"
    return ""


def load_repair_state() -> dict:
    if not REPAIR_STATE_FILE.exists():
        return {"chapters": {}}
    try:
        return json.loads(REPAIR_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"chapters": {}}


def save_repair_state(state: dict) -> None:
    atomic_write_json(REPAIR_STATE_FILE, state)


def should_skip_by_state(state: dict, mode: str, chapter: int) -> bool:
    item = state.get("chapters", {}).get(f"{chapter:04d}")
    if not item or item.get("mode") != mode:
        return False
    if item.get("status") == "success":
        return True
    next_retry_at = float(item.get("next_retry_at", 0) or 0)
    return next_retry_at > time.time()


def record_repair_result(state: dict, mode: str, chapter: int, rc: int, failure_reason: str = "") -> None:
    key = f"{chapter:04d}"
    chapters = state.setdefault("chapters", {})
    old = chapters.get(key, {})
    attempts = int(old.get("attempts", 0)) + 1
    now = time.time()
    success = rc == 0 and not failure_reason
    chapters[key] = {
        "mode": mode,
        "status": "success" if success else "failed",
        "failure_reason": failure_reason,
        "attempts": attempts,
        "last_rc": rc,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "next_retry_at": 0 if success else now + min(3600, 60 * attempts),
    }


def main():
    parser = argparse.ArgumentParser(description="按质量门修复小说历史产物")
    parser.add_argument("--project", "-p", type=str,
                        default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录（默认从环境变量 NOVEL_PROJECT_DIR 读取）")
    parser.add_argument("--mode", choices=["draft", "review", "final"], default="final")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少章，0表示全部")
    parser.add_argument("--dry-run", action="store_true", help="只列出命令，不实际调用模型")
    parser.add_argument("--ignore-state", action="store_true", help="忽略断点状态，重新尝试")
    args = parser.parse_args()

    if not args.project:
        print("错误: 必须指定 --project 或设置 NOVEL_PROJECT_DIR 环境变量")
        sys.exit(1)

    init_project(args.project)

    state = load_repair_state()
    chapters = sort_by_severity(args.mode, select_chapters(args.mode))
    if not args.ignore_state:
        chapters = [ch for ch in chapters if not should_skip_by_state(state, args.mode, ch)]
    if args.limit > 0:
        chapters = chapters[:args.limit]
    print(f"待修复 {args.mode}: {len(chapters)} 章")

    script = {
        "draft": "writer.py",
        "review": "reviewer.py",
        "final": "rewrite_agent.py",
    }[args.mode]

    failed = []
    consecutive_failures = 0
    max_consecutive_failures = int(CONFIG.get("repair", {}).get("max_consecutive_failures", 5) or 5)
    for chapter in chapters:
        rc = run_chapter(script, chapter, args.dry_run)
        failure_reason = ""
        if not args.dry_run:
            current_statuses = scan_chapter_status(NOVELS_DIR, 1, CONFIG["total_chapters"])
            failure_reason = classify_failure(args.mode, chapter, rc, current_statuses)
            record_repair_result(state, args.mode, chapter, rc, failure_reason)
            save_repair_state(state)
        if rc != 0 or (not args.dry_run and failure_reason):
            failed.append(chapter)
            consecutive_failures += 1
            if not args.dry_run and consecutive_failures >= max_consecutive_failures:
                print(f"连续失败达到 {max_consecutive_failures} 次，触发熔断")
                break
        else:
            consecutive_failures = 0

    statuses = scan_chapter_status(NOVELS_DIR, 1, CONFIG["total_chapters"])
    write_status_file(NOVELS_DIR, statuses.values())
    print(
        f"质量门: draft={sum(1 for s in statuses.values() if s.draft_ok)}/{CONFIG['total_chapters']}, "
        f"review={sum(1 for s in statuses.values() if s.review_ok)}/{CONFIG['total_chapters']}, "
        f"final={sum(1 for s in statuses.values() if s.final_ok)}/{CONFIG['total_chapters']}"
    )
    if failed:
        print(f"失败章节: {failed}")


if __name__ == "__main__":
    main()
