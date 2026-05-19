#!/usr/bin/env python3
"""兼容入口：自动传入 novels6 父目录作为书库扫描路径。"""
import subprocess
import sys
from pathlib import Path

GLOBAL_TOOLS = Path("D:/AiProject/Node/novel-tools")
PROJECT = Path(__file__).parent.parent
PARENT = PROJECT.parent
cmd = [sys.executable, str(GLOBAL_TOOLS / "reader_server.py"), "--novels-dir", str(PARENT), *sys.argv[1:]]
subprocess.run(cmd, check=False)
