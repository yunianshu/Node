"""支持 `python novel-tools ...` 调用统一工作流。"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent


def main() -> None:
    sys.path.insert(0, str(SCRIPTS_DIR))
    runpy.run_path(str(SCRIPTS_DIR / "novel_workflow.py"), run_name="__main__")


if __name__ == "__main__":
    main()

