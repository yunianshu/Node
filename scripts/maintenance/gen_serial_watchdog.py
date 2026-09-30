#!/usr/bin/env python3
"""监督旧入口兼容层；每次启动和重启均委托 Coordinator，保留失败退出码。"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

TOOLS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_ROOT))
from core.novel_config import configure_stdio, resolve_project_dir, load_config
from core.workflow_state import atomic_write_json, scan_chapter_status
configure_stdio()


def start_gen_serial(project: Path, extra_args: list[str]) -> subprocess.Popen:
    cmd = [sys.executable, str(TOOLS_ROOT / 'maintenance/gen_serial.py'), '--project', str(project), *extra_args]
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    return subprocess.Popen(cmd, creationflags=flags)


def stop_owned_process(proc: subprocess.Popen) -> None:
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True, check=False)
    else:
        proc.terminate()
    proc.wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='监督旧入口兼容层，重启也走 Coordinator 正式质量门')
    parser.add_argument('--project', '-p', default=os.getenv('NOVEL_PROJECT_DIR', ''))
    parser.add_argument('--interval', type=int, default=180)
    parser.add_argument('--max-stale-checks', type=int, default=3)
    parser.add_argument('--max-restart-count', type=int, default=5)
    parser.add_argument('--no-restart-stalled', action='store_true')
    parser.add_argument('--once', action='store_true', help='监督一次完整子进程执行并返回其退出码')
    parser.add_argument('passthrough', nargs=argparse.REMAINDER, help='-- 后透传旧入口参数')
    args = parser.parse_args(argv)
    if args.interval < 1 or args.max_stale_checks < 1 or args.max_restart_count < 0:
        parser.error('检查间隔和卡住次数必须为正，重启次数不能为负')
    try:
        project = resolve_project_dir(args.project)
        config = load_config(project)
    except ValueError as exc:
        print(f'错误：{exc}', file=sys.stderr)
        return 1
    extra = args.passthrough
    if extra[:1] == ['--']:
        extra = extra[1:]
    last_code = 1
    for restart in range(args.max_restart_count + 1):
        proc = start_gen_serial(project, extra)
        stale, previous = 0, None
        try:
            while True:
                try:
                    last_code = proc.wait(timeout=args.interval)
                    break
                except subprocess.TimeoutExpired:
                    statuses = scan_chapter_status(project, 1, config['total_chapters'])
                    count = sum(s.final_ok for s in statuses.values())
                    stale = stale + 1 if count == previous else 0
                    previous = count
                    if stale >= args.max_stale_checks and not args.no_restart_stalled:
                        print('监控：终稿长时间无进展，停止本次受监督的进程树后重启')
                        stop_owned_process(proc)
                        last_code = 1
                        break
        except KeyboardInterrupt:
            stop_owned_process(proc)
            return 130
        atomic_write_json(project / 'reports/gen_serial_watchdog_state.json',
                          {'restart_count': restart, 'last_exit_code': last_code})
        if args.once or last_code == 0 or last_code == 2:
            return last_code
        if restart < args.max_restart_count:
            print(f'监控：子进程退出码 {last_code}，准备第{restart + 1}次重启')
            time.sleep(min(5, args.interval))
    return last_code


if __name__ == '__main__':
    raise SystemExit(main())
