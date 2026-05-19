"""支持 `python novel-tools ...` 调用统一工作流。"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path


TOOLS_ROOT = Path(__file__).resolve().parent


def main() -> None:
    sys.path.insert(0, str(TOOLS_ROOT))
    runpy.run_path(str(TOOLS_ROOT / "cli" / "novel_workflow.py"), run_name="__main__")


if __name__ == "__main__":
    main()

