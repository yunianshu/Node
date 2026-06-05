#!/usr/bin/env python3
"""Read-only watchdog for outline/draft gate stalls."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from core.novel_config import configure_stdio, load_config, resolve_project_dir
from core.push_notifier import push_stage_event
from core.workflow_state import atomic_write_json, report_path, scan_chapter_status

configure_stdio()


MODE_MARKERS = {
    "outline": ("outline_lane.py", "outliner.py", "outline_reviewer.py"),
    "draft": ("draft_lane.py", "writer.py", "reviewer.py"),
}


@dataclass
class GateWatchdogState:
    signature: str = ""
    stale_checks: int = 0
    last_notify_signature: str = ""
    last_notify_at: str = ""


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(project: Path, mode: str, message: str) -> None:
    line = f"[{now_text()}] [GateWatchdog:{mode}] {message}"
    print(line, flush=True)
    log_file = project / "logs" / f"{mode}_gate_watchdog.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def process_rows() -> list[dict]:
    ps = r"""
$rows = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Select-Object ProcessId, CreationDate, CommandLine
if ($rows) { $rows | ConvertTo-Json -Compress -Depth 3 }
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except Exception:
        return []
    output = result.stdout.strip()
    if not output:
        return []
    try:
        data = json.loads(output)
    except Exception:
        return []
    if isinstance(data, dict):
        return [data]
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def matching_processes(project: Path, markers: tuple[str, ...]) -> list[dict]:
    project_key = str(project).replace("\\", "/").lower()
    project_name = project.name.lower()
    rows = []
    for row in process_rows():
        cmd = str(row.get("CommandLine", "") or "").replace("\\", "/").lower()
        if not cmd:
            continue
        if project_key not in cmd and project_name not in cmd:
            continue
        if any(marker.lower() in cmd for marker in markers):
            rows.append(row)
    return rows


def state_path(project: Path, mode: str) -> Path:
    return report_path(project, f"{mode}_gate_watchdog_state.json")


def load_state(project: Path, mode: str) -> GateWatchdogState:
    path = state_path(project, mode)
    if not path.exists():
        return GateWatchdogState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return GateWatchdogState()
    return GateWatchdogState(
        signature=str(data.get("signature", "")),
        stale_checks=int(data.get("stale_checks", 0) or 0),
        last_notify_signature=str(data.get("last_notify_signature", "")),
        last_notify_at=str(data.get("last_notify_at", "")),
    )


def save_state(project: Path, mode: str, state: GateWatchdogState) -> None:
    atomic_write_json(state_path(project, mode), asdict(state))


def book_title(project: Path) -> str:
    world_file = project / "world.json"
    if world_file.exists():
        try:
            return json.loads(world_file.read_text(encoding="utf-8")).get("title", project.name)
        except Exception:
            pass
    return project.name


def outline_blocker(statuses: dict) -> tuple[int | None, str]:
    for chapter, status in statuses.items():
        if not status.outline_review_ok:
            if status.outline_review_exists:
                return chapter, (
                    f"第{chapter}章大纲未过审 "
                    f"status={status.outline_review_status} score={status.outline_review_score}"
                )
            return chapter, f"第{chapter}章大纲审缺失"
    return None, "全部大纲已过审"


def draft_blocker(statuses: dict) -> tuple[int | None, str]:
    for chapter, status in statuses.items():
        if not status.final_ok:
            if not status.outline_review_ok:
                return chapter, (
                    f"第{chapter}章等待大纲过审 "
                    f"status={status.outline_review_status} score={status.outline_review_score}"
                )
            if status.review_exists:
                return chapter, (
                    f"第{chapter}章初稿未过审 "
                    f"status={status.review_status} score={status.review_score}"
                )
            if status.draft_exists:
                return chapter, f"第{chapter}章等待正文审查"
            return chapter, f"第{chapter}章等待初稿生成"
    return None, "全部终稿已通过"


def recent_log_mtime(project: Path, mode: str) -> int:
    names = ["coordinator.log"]
    if mode == "draft":
        names.append("draft_lane.log")
    if mode == "outline":
        names.append("outline_reviewer.log")
    mtimes = []
    for name in names:
        path = project / "logs" / name
        if path.exists():
            mtimes.append(int(path.stat().st_mtime))
    return max(mtimes) if mtimes else 0


def notify(config: dict, project: Path, mode: str, detail: str) -> None:
    stage = "大纲审查卡章监控" if mode == "outline" else "初稿审查卡章监控"
    push_stage_event(
        config=config,
        title=book_title(project),
        stage=stage,
        status="异常",
        error=detail[:300],
    )


def inspect_once(project: Path, mode: str, stale_threshold: int, notify_cooldown: int) -> None:
    config = load_config(project)
    total = int(config["total_chapters"])
    statuses = scan_chapter_status(project, 1, total, use_cache=False)
    procs = matching_processes(project, MODE_MARKERS[mode])
    lane_name = "outline_lane.py" if mode == "outline" else "draft_lane.py"
    lane_running = any(lane_name.lower() in str(p.get("CommandLine", "")).lower() for p in procs)
    child_count = max(0, len(procs) - (1 if lane_running else 0))
    blocker_chapter, blocker_detail = outline_blocker(statuses) if mode == "outline" else draft_blocker(statuses)
    mtime = recent_log_mtime(project, mode)

    signature = json.dumps(
        {
            "blocker": blocker_chapter,
            "detail": blocker_detail,
            "lane_running": lane_running,
            "child_count": child_count,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    state = load_state(project, mode)
    if signature == state.signature:
        state.stale_checks += 1
    else:
        state.stale_checks = 0
    state.signature = signature

    log(
        project,
        mode,
        f"lane={'running' if lane_running else 'stopped'} child={child_count} "
        f"stale={state.stale_checks}/{stale_threshold} blocker={blocker_detail}",
    )

    should_notify = False
    reason = ""
    if blocker_chapter is not None and not lane_running:
        should_notify = True
        reason = f"{mode} lane 已停止；{blocker_detail}"
    elif blocker_chapter is not None and state.stale_checks >= stale_threshold:
        should_notify = True
        reason = f"{mode} lane 可能卡章；连续{state.stale_checks}次检查无推进；{blocker_detail}"

    last_notify_ts = 0.0
    if state.last_notify_at:
        try:
            last_notify_ts = time.mktime(time.strptime(state.last_notify_at, "%Y-%m-%d %H:%M:%S"))
        except Exception:
            last_notify_ts = 0.0
    if should_notify and (
        reason != state.last_notify_signature or time.time() - last_notify_ts >= notify_cooldown
    ):
        notify(config, project, mode, reason)
        state.last_notify_signature = reason
        state.last_notify_at = now_text()
        log(project, mode, f"已推送告警: {reason}")

    save_state(project, mode, state)


def acquire_lock(project: Path, mode: str) -> Path | None:
    lock = project / "logs" / f"{mode}_gate_watchdog.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        pid = lock.read_text(encoding="utf-8", errors="ignore").strip()
        if pid:
            rows = process_rows()
            if any(str(row.get("ProcessId")) == pid for row in rows):
                return None
        lock.unlink(missing_ok=True)
    lock.write_text(str(os.getpid()), encoding="utf-8")
    return lock


def main() -> int:
    parser = argparse.ArgumentParser(description="每隔固定时间检查大纲/初稿质量门卡章")
    parser.add_argument("--project", "-p", default=os.getenv("NOVEL_PROJECT_DIR", ""))
    parser.add_argument("--mode", choices=("outline", "draft"), required=True)
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--stale-threshold", type=int, default=2)
    parser.add_argument("--notify-cooldown", type=int, default=900)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    try:
        project = resolve_project_dir(args.project)
    except ValueError as exc:
        print(f"错误: {exc}")
        return 1
    lock = acquire_lock(project, args.mode)
    if lock is None:
        print(f"{args.mode} gate watchdog 已存在，本进程退出")
        return 0
    try:
        log(project, args.mode, f"启动: interval={args.interval}s stale_threshold={args.stale_threshold}")
        while True:
            inspect_once(project, args.mode, args.stale_threshold, args.notify_cooldown)
            if args.once:
                break
            time.sleep(max(30, args.interval))
    finally:
        try:
            if lock.read_text(encoding="utf-8").strip() == str(os.getpid()):
                lock.unlink(missing_ok=True)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
