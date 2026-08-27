#!/usr/bin/env python3
"""_gen_serial.py 守护监控。

监控目标：_gen_serial.py（用户实际的串行生成入口，绕开 coordinator）。
现有 coordinator_watchdog.py 只认 coordinator.py，无法监控 _gen_serial.py，本脚本补这个缺口。

行为：
- 定时检查 _gen_serial.py 进程是否存活（按命令行匹配项目目录）
- 进程消失但项目未完成（final 章数 < total）→ 整个脚本重启（依赖其断点续传）
- 进程存活但长时间无 final 进展（stale）→ 视为卡死，杀掉后重启
- 达到最大重启次数 → 停止自动重启，仅观测
- 配额/错误信号 → 降并发后重启

用法: python scripts/maintenance/gen_serial_watchdog.py --project projects/novels14
"""
from __future__ import annotations

from pathlib import Path
import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from core.novel_config import configure_stdio, load_config, resolve_project_dir
from core.workflow_state import atomic_write_json, report_path, scan_chapter_status

configure_stdio()

ROOT = TOOLS_ROOT.parent  # 仓库根
GEN_SERIAL_MARKER = "_gen_serial.py"
ERROR_MARKERS = (
    "配额不足", "失败率", "返回非零退出码", "异常",
    "parse_error", "hard_fail", "timeout", "Timeout", "TIMEOUT_KILLED",
)


@dataclass
class WatchdogState:
    last_final_count: int = -1
    stale_checks: int = 0
    restart_count: int = 0
    last_action: str = ""
    last_action_at: str = ""
    last_error_signals: list[str] | None = None


def normalize_cmdline(text: str) -> str:
    return str(text or "").replace("\\", "/").lower()


def current_time_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def powershell_json(script: str) -> list[dict]:
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
    except Exception as exc:
        return []
    output = result.stdout.strip()
    if not output:
        return []
    try:
        data = json.loads(output)
    except Exception as exc:
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


def find_gen_serial_processes(project: Path) -> list[dict]:
    """匹配 _gen_serial.py 且命令行含项目目录/名的 python 进程。"""
    project_key = normalize_cmdline(str(project))
    project_name = project.name.lower()
    matches = []
    for proc in list_python_processes():
        cmd = normalize_cmdline(proc.get("CommandLine", ""))
        if GEN_SERIAL_MARKER in cmd and (project_key in cmd or project_name in cmd):
            matches.append(proc)
    return matches


def count_final(project: Path, total: int) -> int:
    """统计已生成的 final 章数（_gen_serial 的完成标志）。"""
    final_dir = project / "chapters" / "final"
    if not final_dir.exists():
        return 0
    return len(list(final_dir.glob("chapter_*.txt")))


def project_complete(project: Path, total: int) -> bool:
    return count_final(project, total) >= total


def read_tail(path: Path, limit: int = 40) -> list[str]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
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
    return report_path(project, "gen_serial_watchdog_state.json")


def load_state(project: Path) -> WatchdogState:
    path = state_file(project)
    if not path.exists():
        return WatchdogState(last_error_signals=[])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return WatchdogState(last_error_signals=[])
    return WatchdogState(
        last_final_count=int(data.get("last_final_count", -1)),
        stale_checks=int(data.get("stale_checks", 0) or 0),
        restart_count=int(data.get("restart_count", 0) or 0),
        last_action=str(data.get("last_action", "")),
        last_action_at=str(data.get("last_action_at", "")),
        last_error_signals=list(data.get("last_error_signals", []) or []),
    )


def save_state(project: Path, state: WatchdogState) -> None:
    atomic_write_json(state_file(project), asdict(state))


def stop_process(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        return result.returncode == 0
    except Exception as exc:
        return False


def kill_stray_node() -> None:
    """清理媒体 mmx 调用残留的 node 进程（_gen_serial 卡死时常残留）。"""
    try:
        subprocess.run(["taskkill", "/F", "/IM", "node.exe"],
                       capture_output=True, timeout=15)
    except Exception as exc:
        pass


def start_gen_serial(project: Path, total: int, extra_args: list[str] | None = None) -> subprocess.Popen | None:
    """重启 _gen_serial.py。不指定 --start/--end，依赖其自动从最后 final+1 续传。"""
    cmd = [sys.executable, str(ROOT / "_gen_serial.py"),
           "--project", str(project)]
    if extra_args:
        cmd.extend(extra_args)
    try:
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        print(f"[watchdog] 启动: {' '.join(cmd)}")
        return subprocess.Popen(cmd, cwd=str(ROOT), creationflags=flags)
    except Exception as exc:
        print(f"[watchdog] 启动失败: {exc}")
        return None


def inspect_once(project: Path, config: dict, state: WatchdogState,
                 interval: int, max_stale_checks: int, max_restart_count: int,
                 restart_stalled: bool, extra_args: list[str]) -> bool:
    """单次检查。返回 True 表示应继续循环，False 表示项目完成。"""
    total = int(config.get("total_chapters", 0))
    final_count = count_final(project, total)

    if project_complete(project, total):
        print(f"[{current_time_text()}] watchdog: 项目完成 final={final_count}/{total}，停止监控")
        state.last_action = "complete"
        save_state(project, state)
        return False

    # stale 检测：final 数是否增长
    if final_count == state.last_final_count:
        state.stale_checks += 1
    else:
        state.stale_checks = 0
    state.last_final_count = final_count

    procs = find_gen_serial_processes(project)
    running = bool(procs)
    pid = int(procs[0].get("ProcessId", 0) or 0) if procs else 0

    # 错误信号（从 _gen_serial 自己的 stdout 无法直接读，这里看 writer/reviewer 日志尾部）
    log_dir = project / "logs"
    error_signals: list[str] = []
    for log_name in ("writer.log", "reviewer.log"):
        error_signals.extend(detect_error_signals(read_tail(log_dir / log_name, 40)))
    error_signals = list(dict.fromkeys(error_signals))  # 去重保序
    state.last_error_signals = error_signals

    print(
        f"[{current_time_text()}] watchdog: running={running}, pid={pid or '-'}, "
        f"final={final_count}/{total}, stale={state.stale_checks}, "
        f"restarts={state.restart_count}/{max_restart_count}, "
        f"errors={','.join(error_signals) if error_signals else 'none'}"
    )

    # 情况1：进程存活且未卡死 → 仅观测
    if running and state.stale_checks < max_stale_checks:
        state.last_action = "observe"
        save_state(project, state)
        return True

    # 情况2：进程存活但卡死（连续 max_stale_checks 次 final 无增长）
    if running and state.stale_checks >= max_stale_checks and restart_stalled:
        if state.restart_count >= max_restart_count:
            print(f"[{current_time_text()}] watchdog: 已达最大重启次数 {max_restart_count}，停止自动重启")
            state.last_action = "restart_limit_reached"
            save_state(project, state)
            return True  # 继续观测但不重启
        print(f"[{current_time_text()}] watchdog: 检测到卡死（final 连续 {state.stale_checks} 次无增长），杀掉重启")
        if pid:
            stop_process(pid)
        kill_stray_node()
        time.sleep(3)
        state.restart_count += 1
        state.stale_checks = 0
        state.last_action = "restart_stalled"
        state.last_action_at = current_time_text()
        start_gen_serial(project, total, extra_args)
        save_state(project, state)
        return True

    # 情况3：进程消失且项目未完成 → 重启
    if not running:
        if state.restart_count >= max_restart_count:
            print(f"[{current_time_text()}] watchdog: 进程已退出但已达最大重启次数 {max_restart_count}，仅观测")
            state.last_action = "exited_no_restart"
            save_state(project, state)
            return True
        print(f"[{current_time_text()}] watchdog: _gen_serial 进程消失，重启（断点续传：从 final={final_count}+1 继续）")
        kill_stray_node()
        time.sleep(3)
        state.restart_count += 1
        state.last_action = "restart_exited"
        state.last_action_at = current_time_text()
        start_gen_serial(project, total, extra_args)
        save_state(project, state)
        return True

    state.last_action = "observe"
    save_state(project, state)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="_gen_serial.py 守护监控")
    parser.add_argument("--project", "-p", default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录")
    parser.add_argument("--interval", type=int, default=180, help="检查间隔（秒，默认180）")
    parser.add_argument("--max-stale-checks", type=int, default=3,
                        help="连续多少次 final 无增长后视为卡死（默认3，即约9分钟无进展）")
    parser.add_argument("--max-restart-count", type=int, default=5, help="最大自动重启次数（默认5）")
    parser.add_argument("--no-restart-stalled", action="store_true", help="卡死时不自动重启")
    parser.add_argument("--once", action="store_true", help="只检查一次")
    parser.add_argument("--", dest="passthrough", nargs="*", default=[],
                        help="透传给 _gen_serial.py 的额外参数（如 --workers 6）")
    args = parser.parse_args()

    try:
        project = resolve_project_dir(args.project)
    except ValueError as exc:
        print(f"错误: {exc}")
        return 1

    config = load_config(project)
    extra_args = list(args.passthrough or [])
    state = load_state(project)

    print(f"[{current_time_text()}] gen_serial_watchdog 启动: project={project}")
    print(f"  interval={args.interval}s, max_stale={args.max_stale_checks}, "
          f"max_restarts={args.max_restart_count}, extra_args={extra_args}")

    while True:
        cont = inspect_once(
            project, config, state,
            args.interval, args.max_stale_checks, args.max_restart_count,
            restart_stalled=not args.no_restart_stalled,
            extra_args=extra_args,
        )
        if args.once or not cont:
            break
        time.sleep(max(10, args.interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
