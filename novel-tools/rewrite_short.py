#!/usr/bin/env python3
"""兼容旧入口：修复字数不合格的初稿。"""
import argparse
import os
import subprocess
import sys
from pathlib import Path


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

    scripts_dir = Path(__file__).parent
    cmd = [
        sys.executable, str(scripts_dir / "repair_quality.py"),
        "--project", project, "--mode", "draft"
    ]
    raise SystemExit(subprocess.run(cmd, check=False, text=True, encoding="utf-8").returncode)


if __name__ == "__main__":
    main()
