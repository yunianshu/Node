#!/usr/bin/env python3
"""Standalone Enterprise WeChat progress pusher for lane-based workflows."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from core.novel_config import load_config, resolve_project_dir
from core.push_notifier import push_progress
from core.workflow_state import outline_completed_count, scan_chapter_status


def process_exists(pid_text: str) -> bool:
    try:
        pid = int(str(pid_text).strip())
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return result.returncode == 0 and str(pid) in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock(project: Path) -> Path | None:
    lock_file = project / "logs" / "wechat_pusher.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    if lock_file.exists():
        pid_text = lock_file.read_text(encoding="utf-8", errors="ignore").strip()
        if process_exists(pid_text):
            return None
        lock_file.unlink(missing_ok=True)
    lock_file.write_text(str(os.getpid()), encoding="utf-8")
    return lock_file


def book_title(project: Path) -> str:
    world_file = project / "world.json"
    if world_file.exists():
        try:
            return json.loads(world_file.read_text(encoding="utf-8")).get("title", project.name)
        except Exception:
            pass
    return project.name


def active_lane_count(project: Path) -> int:
    marker = str(project).replace("\\", "/")
    try:
        result = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "CommandLine"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except Exception:
        return 0
    count = 0
    for line in result.stdout.splitlines():
        normalized = line.replace("\\", "/")
        if marker in normalized and any(name in normalized for name in ("outline_lane.py", "draft_lane.py", "writer.py", "reviewer.py", "outliner.py", "outline_reviewer.py")):
            count += 1
    return count


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


def push_once(project: Path, config: dict) -> bool:
    total = int(config["total_chapters"])
    statuses = scan_chapter_status(project, 1, total, use_cache=False)
    outline = outline_completed_count(project, 1, total)
    outline_reviewed = sum(1 for s in statuses.values() if s.outline_review_status == "completed")
    outline_approved = sum(1 for s in statuses.values() if s.outline_review_ok)
    draft = sum(1 for s in statuses.values() if s.draft_exists)
    reviewed = sum(1 for s in statuses.values() if s.review_ok)
    final = sum(1 for s in statuses.values() if s.final_ok)
    total_words = sum(s.draft_words for s in statuses.values() if s.draft_exists)
    scores = [s.review_score for s in statuses.values() if s.final_ok and s.review_score is not None]
    avg_score = sum(scores) / len(scores) if scores else None
    status_note = current_status_note(statuses)
    return push_progress(
        config=config,
        title=book_title(project),
        outline=outline,
        outline_reviewed=outline_reviewed,
        outline_approved=outline_approved,
        draft=draft,
        reviewed=reviewed,
        final=final,
        total_words=total_words,
        total_chapters=total,
        active_writers=active_lane_count(project),
        avg_score=avg_score,
        status_note=status_note,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="独立企业微信进度推送线程")
    parser.add_argument("--project", "-p", type=str, default=os.getenv("NOVEL_PROJECT_DIR", ""))
    parser.add_argument("--interval", type=int, default=120)
    args = parser.parse_args()

    try:
        project = resolve_project_dir(args.project)
    except ValueError as exc:
        print(f"错误: {exc}")
        return 1

    config = load_config(project)
    lock_file = acquire_lock(project)
    if lock_file is None:
        print("已有企业微信推送进程，本进程退出")
        return 0

    log_file = project / "logs" / "wechat_pusher_lane.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        while True:
            ok = push_once(project, config)
            line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 推送{'成功' if ok else '失败'}"
            print(line, flush=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            time.sleep(max(30, args.interval))
    finally:
        try:
            if lock_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
                lock_file.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
