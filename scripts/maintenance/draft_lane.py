#!/usr/bin/env python3
"""Background draft lane that waits for approved outlines before writing."""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
import os
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
    scan_one_chapter,
    outline_review_path,
)
from pipeline import coordinator
from pipeline import draft_gate


_LOG_LOCK = threading.Lock()


def log(project: Path, message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [DraftLane] {message}"
    with _LOG_LOCK:
        print(line, flush=True)
        log_file = project / "logs" / "draft_lane.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def outline_approved(project: Path, chapter: int, min_score: float) -> tuple[bool, float | None]:
    config = load_config(project)
    gate_cfg = config.get("outline_quality_gate", {})
    require_quality_gate = bool(
        isinstance(gate_cfg, dict) and gate_cfg.get("enabled", False)
    )
    _, _, score, ok = load_outline_review_status(
        outline_review_path(project, chapter),
        min_score,
        require_quality_gate=require_quality_gate,
    )
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


def final_ready(project: Path, chapter: int) -> bool:
    return scan_one_chapter(project, chapter).final_ok


def highest_contiguous_final(project: Path, start: int, end: int) -> int:
    last = start - 1
    for chapter in range(start, end + 1):
        if not final_ready(project, chapter):
            break
        last = chapter
    return last


def process_chapter_with_shared_gate(project: Path, chapter: int) -> tuple[bool, str]:
    if final_ready(project, chapter):
        return True, "final_ok"
    ok = draft_gate.process_draft_gate(coordinator.runtime_context(), chapter)
    if not ok:
        return False, "shared_draft_gate_failed"
    if not final_ready(project, chapter):
        return False, "final_quality_failed"
    return True, "shared_draft_gate"


def main() -> int:
    parser = argparse.ArgumentParser(description="等待大纲过审后生成初稿")
    parser.add_argument("--project", "-p", type=str, default=os.getenv("NOVEL_PROJECT_DIR", ""))
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=0)
    parser.add_argument("--wait-seconds", type=float, default=5.0)
    parser.add_argument("--workers", type=int, default=0, help="兼容参数；正文 lane 始终按章节顺序单 worker 推进，不再有同章候选并发")
    parser.add_argument(
        "--continuity-window",
        type=int,
        default=0,
        help="兼容参数；正文 lane 始终要求前一章 final 通过后才推进下一章",
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
    draft_min_score = float(config.get("reviewer", {}).get("min_score", 8.5))
    max_rounds = int(config.get("coordinator", {}).get("draft_analysis_rounds", 3) or 3)
    attempts_per_round = int(config.get("coordinator", {}).get("draft_attempts_per_round", 3) or 3)
    requested_workers = args.workers or int(config.get("coordinator", {}).get("draft_workers", 1) or 1)
    requested_continuity_window = args.continuity_window or int(config.get("coordinator", {}).get("draft_continuity_window", 1) or 1)
    workers = 1
    continuity_window = 1
    coordinator.init_project(project)
    push_interval = int(config.get("coordinator", {}).get("push_interval_seconds", 120))
    coordinator.ensure_wechat_pusher_process(push_interval)
    coordinator.ensure_gate_watchdog_process("draft")

    if requested_workers != 1 or requested_continuity_window != 1:
        log(
            project,
            "正文 lane 已强制使用 workers=1、continuity_window=1；"
            f"忽略请求值 workers={requested_workers}, continuity_window={requested_continuity_window}",
        )

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
                future = executor.submit(process_chapter_with_shared_gate, project, next_chapter)
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
                    next_chapter = min(next_chapter, chapter)
                    continue
                if not ok:
                    log(project, f"第{chapter}章初稿处理失败({detail})，停止 draft lane")
                    return 1
                log(project, f"第{chapter}章初稿审查通过，已写入 final ({detail})")

    log(project, "全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
