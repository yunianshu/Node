#!/usr/bin/env python3
"""兼容入口：自动传入 novels6 项目目录。"""
import subprocess
import sys
from pathlib import Path

GLOBAL_TOOLS = Path("D:/AiProject/Node/novel-tools")
PROJECT = Path(__file__).parent.parent
cmd = [sys.executable, str(GLOBAL_TOOLS / "fill_draft.py"), "--project", str(PROJECT), *sys.argv[1:]]
subprocess.run(cmd, check=False)
