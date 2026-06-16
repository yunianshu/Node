#!/usr/bin/env python3
"""Background outline-only lane for an active novel project."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from pipeline import coordinator
from pipeline import outline_gate
from core.novel_config import resolve_project_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="只推进大纲生成与大纲审查，不生成正文")
    parser.add_argument(
        "--project",
        "-p",
        type=str,
        default=os.getenv("NOVEL_PROJECT_DIR", ""),
        help="小说项目目录",
    )
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=0, help="结束章节")
    parser.add_argument("--passes", type=int, default=1, help="失败后因后章重叠触发的额外重试轮数")
    args = parser.parse_args()

    try:
        project = resolve_project_dir(args.project)
    except ValueError as exc:
        print(f"错误: {exc}")
        return 1

    coordinator.init_project(project)
    runtime = coordinator.runtime_context()
    end = args.end or int(coordinator.CONFIG["total_chapters"])
    start = max(1, args.start)
    push_interval = int(coordinator.CONFIG.get("coordinator", {}).get("push_interval_seconds", 120))
    coordinator.ensure_wechat_pusher_process(push_interval)
    coordinator.ensure_gate_watchdog_process("outline")

    coordinator.log(f"[OutlineLane] 启动: 第{start}-{end}章，只处理大纲")
    for chapter in range(start, end + 1):
        ok = outline_gate.process_outline_gate(runtime, chapter, push_on_failure=False)
        if ok:
            continue

        report = outline_gate.load_outline_failure_report(runtime, chapter)
        repaired = False
        for _ in range(max(0, args.passes)):
            overlap_chapter = outline_gate.repair_later_overlap(runtime, chapter, report)
            if overlap_chapter is None:
                break
            if outline_gate.process_outline_gate(runtime, chapter, push_on_failure=False):
                repaired = True
                break
            report = outline_gate.load_outline_failure_report(runtime, chapter)

        if not repaired:
            coordinator.log(f"[OutlineLane] 第{chapter}章大纲未通过，停止 outline lane")
            return 1

    coordinator.log(f"[OutlineLane] 完成: 第{start}-{end}章")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
