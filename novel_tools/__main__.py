"""支持 `python -m novel_tools ...` 调用统一工作流。"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    sys.path.insert(0, str(ROOT / "novel-tools"))
    runpy.run_path(str(ROOT / "novel-tools" / "novel_workflow.py"), run_name="__main__")


if __name__ == "__main__":
    main()
