#!/usr/bin/env python3
"""兼容入口：自动传入 novels6 项目目录，并暴露真实模块函数。"""
import importlib.util
import subprocess
import sys
from pathlib import Path

GLOBAL_TOOLS = Path(__file__).resolve().parents[3] / "novel-tools"
PROJECT = Path(__file__).resolve().parent.parent


def _load_tool_module():
    sys.path.insert(0, str(GLOBAL_TOOLS))
    spec = importlib.util.spec_from_file_location("_novel_tools_repair_quality", GLOBAL_TOOLS / "repair_quality.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_tool = _load_tool_module()
init_project = _tool.init_project
classify_failure = _tool.classify_failure
record_repair_result = _tool.record_repair_result
should_skip_by_state = _tool.should_skip_by_state
main = _tool.main


if __name__ == "__main__":
    cmd = [sys.executable, str(GLOBAL_TOOLS / "repair_quality.py"), "--project", str(PROJECT), *sys.argv[1:]]
    raise SystemExit(subprocess.run(cmd, check=False).returncode)
