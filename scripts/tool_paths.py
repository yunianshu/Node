"""小说工具脚本路径索引。"""
from __future__ import annotations

from pathlib import Path


TOOLS_ROOT = Path(__file__).resolve().parent

SCRIPT_PATHS = {
    "novel_workflow.py": TOOLS_ROOT / "cli" / "novel_workflow.py",
    "preflight_check.py": TOOLS_ROOT / "cli" / "preflight_check.py",
    "coordinator.py": TOOLS_ROOT / "pipeline" / "coordinator.py",
    "planner.py": TOOLS_ROOT / "pipeline" / "planner.py",
    "outliner.py": TOOLS_ROOT / "pipeline" / "outliner.py",
    "planner_parallel.py": TOOLS_ROOT / "pipeline" / "planner_parallel.py",
    "writer.py": TOOLS_ROOT / "pipeline" / "writer.py",
    "reviewer.py": TOOLS_ROOT / "pipeline" / "reviewer.py",
    "rewrite_agent.py": TOOLS_ROOT / "pipeline" / "rewrite_agent.py",
    "fill_draft.py": TOOLS_ROOT / "batch" / "fill_draft.py",
    "fill_draft_parallel.py": TOOLS_ROOT / "batch" / "fill_draft_parallel.py",
    "fill_missing.py": TOOLS_ROOT / "batch" / "fill_missing.py",
    "fill_missing_outline.py": TOOLS_ROOT / "batch" / "fill_missing_outline.py",
    "fill_outline.py": TOOLS_ROOT / "batch" / "fill_outline.py",
    "fill_outline_segments.py": TOOLS_ROOT / "batch" / "fill_outline_segments.py",
    "fill_reviews_parallel.py": TOOLS_ROOT / "batch" / "fill_reviews_parallel.py",
    "run_outlines_parallel.py": TOOLS_ROOT / "batch" / "run_outlines_parallel.py",
    "run_planner_batches.py": TOOLS_ROOT / "batch" / "run_planner_batches.py",
    "repair_quality.py": TOOLS_ROOT / "maintenance" / "repair_quality.py",
    "rewrite_short.py": TOOLS_ROOT / "maintenance" / "rewrite_short.py",
    "wechat_notify.py": TOOLS_ROOT / "maintenance" / "wechat_notify.py",
    "reader_server.py": TOOLS_ROOT / "maintenance" / "reader_server.py",
    "generate_cover_and_trailer.py": TOOLS_ROOT / "media" / "generate_cover_and_trailer.py",
}


def script_path(name: str) -> Path:
    return SCRIPT_PATHS[name]

