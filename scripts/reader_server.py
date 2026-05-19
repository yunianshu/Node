#!/usr/bin/env python3
"""兼容入口：实际阅读器实现位于 scripts/maintenance/reader_server.py。"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "scripts"
READER_SERVER = TOOLS_ROOT / "maintenance" / "reader_server.py"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

runpy.run_path(str(READER_SERVER), run_name="__main__")
