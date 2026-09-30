#!/usr/bin/env python3
"""兼容转发；工作流实现位于 scripts/maintenance/gen_serial.py。"""
import runpy
from pathlib import Path

if __name__ == '__main__':
    runpy.run_path(str(Path(__file__).resolve().parent / 'scripts/maintenance/gen_serial.py'), run_name='__main__')
