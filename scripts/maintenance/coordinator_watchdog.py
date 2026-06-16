#!/usr/bin/env python3
"""协调器守护监控子进程。

每隔固定时间检查小说项目的进度、协调器进程和日志活跃度：
- 协调器正常运行时，只做观测，并在发现错误信号时保守降并发；
- 协调器退出但项目未完成时，先跑一次轻量修复，再拉起协调器；
- 协调器卡死且长时间无日志更新时，重启协调器。
"""

from __future__ import annotations

from pathlib import Path
import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, asdict

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from core.novel_config import configure_stdio, load_config, resolve_project_dir
from core.workflow_state import atomic_write_json, outline_completed_count, report_path, scan_chapter_status

configure_stdio()

COORDINATOR_MARKER = "scripts/pipeline/coordinator.py"
AGENT_MARKERS = (
    "scripts/pipeline/planner.py",
    "scripts/pipeline/media_generator.py",
    "scripts/pipeline/outliner.py",
    "scripts/pipeline/outline_reviewer.py",
    "scripts/pipeline/writer.py",
    "scripts/pipeline/reviewer.py",
)
ERROR_MARKERS = (
    "配额不足",
    "失败率",
    "返回非零退出码",
    "异常",
    "parse_error",
    "hard_fail",
    "timeout",
    "Timeout",
)


@dataclass
class WatchdogState:
    last_signature: str = ""
    stale_checks: int = 0
    restart_count: int = 0
    last_action: str = ""
    last_action_at: str = ""
    last_adjust_at: str = ""
    last_error_signals: list[str] | None = None


def normalize_cmdline(text: str) -> str:
    return str(text or "").replace("\\", "/").lower()


def powershell_json(script: str) -> list[dict]:
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
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
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def list_python_processes() -> list[dict]:
    ps = r"""
$rows = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Select-Object ProcessId, ParentProcessId, CommandLine, CreationDate
if ($rows) { $rows | ConvertTo-Json -Compress -Depth 3 }
"""
    return powershell_json(ps)


def find_coordinator_processes(project: Path) -> list[dict]:
    project_key = normalize_cmdline(str(project))
    project_name = project.name.lower()
    matches = []
    for proc in list_python_processes():
        cmd = normalize_cmdline(proc.get("CommandLine", ""))
        if COORDINATOR_MARKER in cmd and (project_key in cmd or project_name in cmd):
            matches.append(proc)
    return matches


def find_child_processes(parent_pid: int) -> list[dict]:
    matches = []
    for proc in list_python_processes():
        try:
            ppid = int(proc.get("ParentProcessId", 0) or 0)
        except (TypeError, ValueError):
            continue
        if ppid != parent_pid:
            continue
        cmd = normalize_cmdline(proc.get("CommandLine", ""))
        if any(marker in cmd for marker in AGENT_MARKERS):
            matches.append(proc)
    return matches


def read_tail(path: Path, limit: int = 80) -> list[str]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    return lines[-limit:]


def detect_error_signals(lines: list[str]) -> list[str]:
    hits = []
    for line in lines:
        for marker in ERROR_MARKERS:
            if marker in line and marker not in hits:
                hits.append(marker)
    return hits


def state_file(project: Path) -> Path:
    return report_path(project, "watchdog_state.json")


def load_state(project: Path) -> WatchdogState:
    path = state_file(project)
    if not path.exists():
        return WatchdogState(last_error_signals=[])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return WatchdogState(last_error_signals=[])
    return WatchdogState(
        last_signature=str(data.get("last_signature", "")),
        stale_checks=int(data.get("stale_checks", 0) or 0),
        restart_count=int(data.get("restart_count", 0) or 0),
        last_action=str(data.get("last_action", "")),
        last_action_at=str(data.get("last_action_at", "")),
        last_adjust_at=str(data.get("last_adjust_at", "")),
        last_error_signals=list(data.get("last_error_signals", []) or []),
    )


def save_state(project: Path, state: WatchdogState) -> None:
    atomic_write_json(state_file(project), asdict(state))


def count_statuses(project: Path, total: int) -> dict:
    statuses = scan_chapter_status(project, 1, total)
    return {
        "outline": outline_completed_count(project, 1, total),
        "draft": sum(1 for s in statuses.values() if s.draft_exists),
        "review": sum(1 for s in statuses.values() if s.review_ok),
        "final": sum(1 for s in statuses.values() if s.final_ok),
        "reviewed": sum(1 for s in statuses.values() if s.review_exists),
    }


def progress_signature(project: Path, config: dict) -> tuple[str, dict]:
    total = int(config["total_chapters"])
    progress_file = report_path(project, "progress.json")
    log_file = project / "logs" / "coordinator.log"
    progress_mtime = int(progress_file.stat().st_mtime) if progress_file.exists() else 0
    log_mtime = int(log_file.stat().st_mtime) if log_file.exists() else 0
    progress = {}
    if progress_file.exists():
        try:
            progress = json.loads(progress_file.read_text(encoding="utf-8"))
        except Exception:
            progress = {}
    counts = count_statuses(project, total)
    payload = {
        "progress_mtime": progress_mtime,
        "log_mtime": log_mtime,
        "outline": counts["outline"],
        "draft": counts["draft"],
        "review": counts["review"],
        "final": counts["final"],
        "last_generated": int(progress.get("last_generated_chapter", 0) or 0),
        "last_reviewed": int(progress.get("last_reviewed_chapter", 0) or 0),
        "last_outline_reviewed": int(progress.get("last_outline_reviewed_chapter", 0) or 0),
        "failed": len(progress.get("failed_chapters", []) or []),
    }
    signature = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return signature, payload


def lower_worker_counts(project: Path, reason: str) -> bool:
    config_path = project / "config.json"
    if not config_path.exists():
        return False
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return False

    changed = False

    coordinator = config.setdefault("coordinator", {})
    for key in ("draft_workers", "num_workers", "review_workers"):
        if key not in coordinator:
            continue
        current = int(coordinator.get(key, 1) or 1)
        new_value = max(1, current - 1)
        if new_value < current:
            coordinator[key] = new_value
            changed = True

    for section_name in ("outline_race", "draft_race"):
        section = config.get(section_name)
        if not isinstance(section, dict):
            continue
        current = int(section.get("max_workers", 1) or 1)
        new_value = max(1, current - 1)
        if new_value < current:
            section["max_workers"] = new_value
            changed = True

    if changed:
        coordinator["last_watchdog_reason"] = reason[:200]
        atomic_write_json(config_path, config)
    return changed


def run_repair_pass(project: Path, limit: int) -> int:
    # The current workflow repairs chapters inside coordinator quality gates.
    return 0


def start_coordinator(project: Path) -> subprocess.Popen | None:
    cmd = [
        sys.executable,
        str(TOOLS_ROOT / "pipeline" / "coordinator.py"),
        "--project",
        str(project),
    ]
    try:
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        print(f"$ {' '.join(cmd)}")
        return subprocess.Popen(cmd, cwd=str(Path.cwd()), creationflags=flags)
    except Exception:
        return None


def stop_process(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def current_time_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def project_complete(project: Path, total: int) -> bool:
    counts = count_statuses(project, total)
    return counts["outline"] >= total and counts["draft"] >= total and counts["review"] >= total and counts["final"] >= total


def inspect_once(project: Path, interval_seconds: int, max_stale_checks: int, repair_limit: int,
                 adjust_cooldown_minutes: int, max_restart_count: int, restart_stalled: bool) -> None:
    config = load_config(project)
    state = load_state(project)
    total = int(config["total_chapters"])
    log_file = project / "logs" / "coordinator.log"

    signature, payload = progress_signature(project, config)
    if signature == state.last_signature:
        state.stale_checks += 1
    else:
        state.stale_checks = 0
    state.last_signature = signature

    coord_procs = find_coordinator_processes(project)
    coord_running = bool(coord_procs)
    coord_pid = int(coord_procs[0].get("ProcessId", 0) or 0) if coord_procs else 0
    child_procs = find_child_processes(coord_pid) if coord_pid else []
    recent_lines = read_tail(log_file, 120)
    error_signals = detect_error_signals(recent_lines)
    state.last_error_signals = error_signals

    print(
        f"[{current_time_text()}] watchdog: running={coord_running}, pid={coord_pid or '-'}, "
        f"stale={state.stale_checks}, children={len(child_procs)}, "
        f"outline={payload['outline']}/{total}, draft={payload['draft']}/{total}, "
        f"review={payload['review']}/{total}, final={payload['final']}/{total}, "
        f"errors={','.join(error_signals) if error_signals else 'none'}"
    )

    if error_signals:
        last_adjust = time.mktime(time.strptime(state.last_adjust_at, "%Y-%m-%d %H:%M:%S")) if state.last_adjust_at else 0
        if time.time() - last_adjust >= adjust_cooldown_minutes * 60:
            if lower_worker_counts(project, f"watchdog detected errors: {', '.join(error_signals)}"):
                state.last_adjust_at = current_time_text()
                state.last_action = "lower_workers"
                print(f"[{current_time_text()}] watchdog: 已降低并发，等待下一轮协调器使用新配置")

    if coord_running and state.stale_checks < max_stale_checks:
        save_state(project, state)
        return

    if coord_running and state.stale_checks >= max_stale_checks and restart_stalled:
        if state.restart_count >= max_restart_count:
            print(f"[{current_time_text()}] watchdog: 已达到最大重启次数 {max_restart_count}，暂停自动重启")
            state.last_action = "restart_limit_reached"
            save_state(project, state)
            return
        if coord_pid and stop_process(coord_pid):
            print(f"[{current_time_text()}] watchdog: 已停止卡住的协调器 PID {coord_pid}")
            state.restart_count += 1
            state.last_action = "restart_stalled"
            state.last_action_at = current_time_text()
            time.sleep(3)
            start_coordinator(project)
            print(f"[{current_time_text()}] watchdog: 已重启协调器")
            save_state(project, state)
            return
        print(f"[{current_time_text()}] watchdog: 尝试重启失败，保持观测")
        save_state(project, state)
        return

    if not coord_running and not project_complete(project, total):
        last_repair = time.mktime(time.strptime(state.last_action_at, "%Y-%m-%d %H:%M:%S")) if state.last_action_at else 0
        if time.time() - last_repair >= max(600, adjust_cooldown_minutes * 60):
            rc = run_repair_pass(project, repair_limit)
            print(f"[{current_time_text()}] watchdog: repair-all rc={rc}")
            state.last_action = "repair_all"
            state.last_action_at = current_time_text()
        start_coordinator(project)
        state.last_action = "start_coordinator"
        state.last_action_at = current_time_text()
        save_state(project, state)
        return

    state.last_action = "observe"
    save_state(project, state)


def main() -> int:
    parser = argparse.ArgumentParser(description="协调器守护监控子进程")
    parser.add_argument(
        "--project",
        "-p",
        type=str,
        default=os.getenv("NOVEL_PROJECT_DIR", ""),
        help="小说项目目录",
    )
    parser.add_argument("--interval", type=int, default=120, help="检查间隔（秒）")
    parser.add_argument("--max-stale-checks", type=int, default=2, help="连续多少次无变化后视为异常")
    parser.add_argument("--repair-limit", type=int, default=8, help="协调器退出后先执行的修复章节上限")
    parser.add_argument("--adjust-cooldown-minutes", type=int, default=30, help="并发降档冷却时间（分钟）")
    parser.add_argument("--max-restart-count", type=int, default=3, help="最大自动重启次数")
    parser.add_argument("--no-restart-stalled", action="store_true", help="发现卡住时不自动重启")
    parser.add_argument("--once", action="store_true", help="只执行一次监控")
    args = parser.parse_args()

    try:
        project = resolve_project_dir(args.project)
    except ValueError as exc:
        print(f"错误: {exc}")
        return 1

    print(f"[{current_time_text()}] watchdog: start project={project}")
    while True:
        inspect_once(
            project,
            args.interval,
            args.max_stale_checks,
            args.repair_limit,
            args.adjust_cooldown_minutes,
            args.max_restart_count,
            restart_stalled=not args.no_restart_stalled,
        )
        if args.once:
            break
        time.sleep(max(10, args.interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
