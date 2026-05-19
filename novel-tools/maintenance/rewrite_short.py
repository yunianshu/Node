#!/usr/bin/env python3
"""兼容旧入口：修复字数不合格的初稿。"""

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

import argparse
import os
import subprocess
import sys
from pathlib import Path

from tool_paths import script_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-p", type=str,
                        default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录")
    args = parser.parse_args()

    project = args.project or os.getenv("NOVEL_PROJECT_DIR", "")
    if not project:
        print("错误: 必须指定 --project 或设置 NOVEL_PROJECT_DIR 环境变量")
        sys.exit(1)

    cmd = [
        sys.executable, str(script_path("repair_quality.py")),
        "--project", project, "--mode", "draft"
    ]
    raise SystemExit(subprocess.run(cmd, check=False, text=True, encoding="utf-8").returncode)


if __name__ == "__main__":
    main()

