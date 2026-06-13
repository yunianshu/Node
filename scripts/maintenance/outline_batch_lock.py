from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from core.novel_config import resolve_project_dir
from core.outline_batch_lock import load_locks, unlock_chapters


def main() -> None:
    parser = argparse.ArgumentParser(description="查看或解锁25章大纲批次")
    parser.add_argument("--project", "-p", default="")
    parser.add_argument("--unlock-start", type=int, default=0)
    parser.add_argument("--unlock-end", type=int, default=0)
    args = parser.parse_args()

    project = resolve_project_dir(args.project)
    if args.unlock_start > 0:
        end = args.unlock_end or args.unlock_start
        unlocked = unlock_chapters(project, list(range(args.unlock_start, end + 1)))
        print(json.dumps({"unlocked": unlocked}, ensure_ascii=False, indent=2))
        return
    print(json.dumps(load_locks(project), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
