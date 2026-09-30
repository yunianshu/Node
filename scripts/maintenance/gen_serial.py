#!/usr/bin/env python3
"""旧串行入口兼容层：唯一生成流程为 Coordinator 的正式质量门。"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))
from core.novel_config import configure_stdio
configure_stdio()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="旧串行入口，统一转发 Coordinator")
    parser.add_argument('--project', '-p', default='', help='项目目录，默认使用 NOVEL_PROJECT_DIR 或自动识别')
    parser.add_argument('--start', type=int, default=0, help='0=Coordinator 从第1章检查并续跑')
    parser.add_argument('--end', type=int, default=0, help='0=使用配置总章数')
    parser.add_argument('--skip-planner', action='store_true')
    parser.add_argument('--outline-lookahead', type=int, default=0)
    parser.add_argument('--candidates', type=int, default=None, help='仅兼容1；已移除候选赛马')
    parser.add_argument('--workers', type=int, default=None, help='仅兼容1；统一流程逐章串行')
    parser.add_argument('--max-rounds', type=int, default=None, help='映射 coordinator 的正文分析轮数，每轮尝试数仍读配置')
    parser.add_argument('--timeout', type=int, default=None, help='不支持；各Agent调用超时请使用 timeout_seconds 配置')
    parser.add_argument('--time-limit', type=int, default=0, help='仅兼容0；不支持自动截断质量门')
    args = parser.parse_args(argv)
    for name in ('candidates', 'workers'):
        value = getattr(args, name)
        if value is not None and value != 1:
            parser.error(f'--{name}={value} 无法映射；旧赛马已移除，请省略或设为1')
        if value is not None:
            print(f'兼容提示：--{name}=1 使用 Coordinator 单章串行质量门', file=sys.stderr)
    if args.timeout is not None:
        parser.error('--timeout 无等价映射，请在各 Agent 配置 timeout_seconds')
    if args.time_limit != 0:
        parser.error('--time-limit 非0无法映射；正式质量门不按墙钟预算截断')
    if args.max_rounds is not None and args.max_rounds < 1:
        parser.error('--max-rounds 必须大于0')
    if args.start < 0 or args.end < 0 or args.outline_lookahead < 0:
        parser.error('章节与提前窗口不能为负数')
    cmd = [sys.executable, str(Path(__file__).resolve().parents[1] / 'pipeline/coordinator.py')]
    if args.project:
        cmd += ['--project', args.project]
    for name in ('start', 'end', 'outline_lookahead'):
        if value := getattr(args, name):
            cmd += ['--' + name.replace('_', '-'), str(value)]
    if args.max_rounds is not None:
        print('兼容提示：--max-rounds 映射正文分析轮数；不改变每轮尝试数', file=sys.stderr)
        cmd += ['--draft-rounds', str(args.max_rounds)]
    if args.skip_planner:
        cmd.append('--skip-planner')
    return subprocess.run(cmd, check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
