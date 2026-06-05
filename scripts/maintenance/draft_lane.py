#!/usr/bin/env python3
"""Background draft lane that waits for approved outlines before writing."""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from core.novel_config import load_config, resolve_project_dir
from core.workflow_state import (
    load_outline_review_status,
    load_review_status,
    scan_one_chapter,
    outline_review_path,
    review_dir,
)
from pipeline import coordinator


_LOG_LOCK = threading.Lock()
_REVIEW_LOCK = threading.Lock()


def log(project: Path, message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [DraftLane] {message}"
    with _LOG_LOCK:
        print(line, flush=True)
        log_file = project / "logs" / "draft_lane.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def run_script(project: Path, script_name: str, chapter: int) -> int:
    script = TOOLS_ROOT / "pipeline" / script_name
    cmd = [sys.executable, str(script), "--project", str(project), "--chapter", str(chapter)]
    return subprocess.run(cmd, check=False).returncode


def promote_to_final(project: Path, chapter: int) -> None:
    source = project / "chapters" / "draft" / f"chapter_{chapter:04d}.txt"
    target = project / "chapters" / "final" / f"chapter_{chapter:04d}.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")


def outline_approved(project: Path, chapter: int, min_score: float) -> tuple[bool, float | None]:
    _, _, score, ok = load_outline_review_status(outline_review_path(project, chapter), min_score)
    return ok, score


def outline_review_detail(project: Path, chapter: int, min_score: float) -> str:
    path = outline_review_path(project, chapter)
    if not path.exists():
        return f"review=missing min_score={min_score:g}"
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception as exc:
        return f"review=invalid_json error={exc} min_score={min_score:g}"

    status = data.get("status", "unknown")
    score = data.get("overall_score")
    verdict = data.get("verdict", "unknown")
    return f"status={status} score={score} verdict={verdict} min_score={min_score:g}"


def draft_review_passed(project: Path, chapter: int, min_score: float) -> tuple[bool, float | None]:
    review_file = review_dir(project) / f"chapter_{chapter:04d}_review.json"
    _, _, score, ok = load_review_status(review_file, min_score)
    return ok, score


def draft_review_detail(project: Path, chapter: int, min_score: float) -> str:
    path = review_dir(project) / f"chapter_{chapter:04d}_review.json"
    if not path.exists():
        return f"review=missing min_score={min_score:g}"
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception as exc:
        return f"review=invalid_json error={exc} min_score={min_score:g}"

    status = data.get("status", "unknown")
    score = data.get("overall_score")
    verdict = data.get("verdict", "unknown")
    return f"status={status} score={score} verdict={verdict} min_score={min_score:g}"


def final_exists(project: Path, chapter: int) -> bool:
    return (project / "chapters" / "final" / f"chapter_{chapter:04d}.txt").exists()


def final_ready(project: Path, chapter: int) -> bool:
    return scan_one_chapter(project, chapter).final_ok


def highest_contiguous_final(project: Path, start: int, end: int) -> int:
    last = start - 1
    for chapter in range(start, end + 1):
        if not final_ready(project, chapter):
            break
        last = chapter
    return last


def process_chapter(
    project: Path,
    chapter: int,
    draft_min_score: float,
    max_rounds: int,
    attempts_per_round: int,
) -> tuple[bool, str]:
    if final_ready(project, chapter):
        return True, "final_ok"

    total_attempts = max(1, max_rounds) * max(1, attempts_per_round)
    last_detail = ""
    for attempt in range(1, total_attempts + 1):
        log(project, f"第{chapter}章初稿生成/审查 attempt={attempt}/{total_attempts}")
        writer_rc = run_script(project, "writer.py", chapter)
        if writer_rc != 0:
            last_detail = f"writer_rc={writer_rc}"
            log(project, f"第{chapter}章 writer 失败({last_detail})，继续重试")
            continue

        review_file = review_dir(project) / f"chapter_{chapter:04d}_review.json"
        review_file.unlink(missing_ok=True)

        # Reviewer updates shared progress files; keep that part serialized to avoid progress tmp-file races.
        with _REVIEW_LOCK:
            reviewer_rc = run_script(project, "reviewer.py", chapter)
        if reviewer_rc != 0:
            last_detail = f"reviewer_rc={reviewer_rc}"
            log(project, f"第{chapter}章 reviewer 失败({last_detail})，继续重试")
            continue

        draft_ok, review_score = draft_review_passed(project, chapter, draft_min_score)
        if not draft_ok:
            last_detail = draft_review_detail(project, chapter, draft_min_score)
            log(project, f"第{chapter}章初稿未达标({last_detail})，下一次将带审查意见重写")
            continue

        promote_to_final(project, chapter)
        if not final_ready(project, chapter):
            last_detail = "final_quality_failed"
            log(project, f"第{chapter}章终稿本地质量检查失败，继续重写")
            continue
        return True, f"review_score={review_score} attempts={attempt}"

    return False, last_detail or "draft_attempts_exhausted"


def main() -> int:
    parser = argparse.ArgumentParser(description="等待大纲过审后生成初稿")
    parser.add_argument("--project", "-p", type=str, default=os.getenv("NOVEL_PROJECT_DIR", ""))
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=0)
    parser.add_argument("--wait-seconds", type=float, default=5.0)
    parser.add_argument("--workers", type=int, default=0, help="并发初稿 worker 数，默认读取 coordinator.draft_workers 或 2")
    parser.add_argument(
        "--continuity-window",
        type=int,
        default=0,
        help="最多领先已完成终稿多少章，默认等于 workers；设为1则严格顺序",
    )
    args = parser.parse_args()

    try:
        project = resolve_project_dir(args.project)
    except ValueError as exc:
        print(f"错误: {exc}")
        return 1

    config = load_config(project)
    end = args.end or int(config["total_chapters"])
    outline_min_score = float(config.get("outline_reviewer", {}).get("min_score", 8.5))
    draft_min_score = float(config.get("reviewer", {}).get("min_score", 7.0))
    max_rounds = int(config.get("coordinator", {}).get("draft_analysis_rounds", 3) or 3)
    attempts_per_round = int(config.get("coordinator", {}).get("draft_attempts_per_round", 3) or 3)
    workers = args.workers or int(config.get("coordinator", {}).get("draft_workers", 2) or 2)
    workers = max(1, workers)
    continuity_window = args.continuity_window or workers
    continuity_window = max(1, continuity_window)

    coordinator.init_project(project)
    push_interval = int(config.get("coordinator", {}).get("push_interval_seconds", 120))
    coordinator.ensure_wechat_pusher_process(push_interval)
    coordinator.ensure_gate_watchdog_process("draft")

    log(
        project,
        f"启动: 第{args.start}-{end}章，workers={workers}，continuity_window={continuity_window}，"
        f"正文门槛={draft_min_score:g}，每章最多{max_rounds * attempts_per_round}次重写，"
        f"未过审大纲每{args.wait_seconds:g}秒检查一次",
    )
    next_chapter = max(1, args.start)
    in_flight = {}
    wait_seconds = max(1.0, args.wait_seconds)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        while next_chapter <= end or in_flight:
            scheduled = False
            while next_chapter <= end and len(in_flight) < workers:
                final_frontier = highest_contiguous_final(project, args.start, end)
                if next_chapter > final_frontier + continuity_window:
                    break

                outline_ok, outline_score = outline_approved(project, next_chapter, outline_min_score)
                if not outline_ok:
                    detail = outline_review_detail(project, next_chapter, outline_min_score)
                    log(project, f"第{next_chapter}章大纲未过审({detail})，等待{wait_seconds:g}秒")
                    break

                log(project, f"第{next_chapter}章大纲已过审(score={outline_score})，提交初稿 worker")
                future = executor.submit(
                    process_chapter,
                    project,
                    next_chapter,
                    draft_min_score,
                    max_rounds,
                    attempts_per_round,
                )
                in_flight[future] = next_chapter
                next_chapter += 1
                scheduled = True

            if not in_flight:
                time.sleep(wait_seconds)
                continue

            timeout = 0 if scheduled else wait_seconds
            done, _ = wait(in_flight, timeout=timeout, return_when=FIRST_COMPLETED)
            for future in done:
                chapter = in_flight.pop(future)
                try:
                    ok, detail = future.result()
                except Exception as exc:
                    log(project, f"第{chapter}章初稿 worker 异常: {exc}")
                    return 1
                if not ok:
                    log(project, f"第{chapter}章初稿处理失败({detail})，停止 draft lane")
                    return 1
                log(project, f"第{chapter}章初稿审查通过，已写入 final ({detail})")

    log(project, "全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
