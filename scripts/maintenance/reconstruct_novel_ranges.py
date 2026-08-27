#!/usr/bin/env python3
"""Back up and reconstruct selected novel ranges through existing quality lanes."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

TOOLS_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = TOOLS_ROOT.parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from core.novel_config import load_config, resolve_project_dir
from core.workflow_state import scan_chapter_status


RANGES = [(91, 150), (461, 500)]
ARTIFACTS = {
    "outline": ("chapters/outline", "chapter_{chapter:04d}.json"),
    "outline_review": ("chapters/outline_review", "chapter_{chapter:04d}_review.json"),
    "draft": ("chapters/draft", "chapter_{chapter:04d}.txt"),
    "review": ("chapters/review", "chapter_{chapter:04d}_review.json"),
    "final": ("chapters/final", "chapter_{chapter:04d}.txt"),
}


def log(project: Path, message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [Reconstruct] {message}"
    print(line, flush=True)
    path = project / "logs" / "reconstruct_ranges.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def iter_chapters() -> list[int]:
    return [chapter for start, end in RANGES for chapter in range(start, end + 1)]


def parse_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            left, right = item.split("-", 1)
            start, end = int(left), int(right)
        else:
            start = end = int(item)
        if start <= 0 or end < start:
            raise ValueError(f"非法章节范围: {item}")
        ranges.append((start, end))
    if not ranges:
        raise ValueError("章节范围为空")
    return ranges


def artifact_path(project: Path, kind: str, chapter: int) -> Path:
    folder, pattern = ARTIFACTS[kind]
    return project / folder / pattern.format(chapter=chapter)


def prepare_backup(project: Path, backup: Path) -> dict:
    manifest = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ranges": RANGES,
        "files": {},
    }
    for kind in ARTIFACTS:
        copied = 0
        for chapter in iter_chapters():
            source = artifact_path(project, kind, chapter)
            if not source.exists():
                raise RuntimeError(f"备份前缺少产物: {source}")
            target = backup / source.relative_to(project)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(source, target)
            copied += 1
        manifest["files"][kind] = copied
    atomic_json(backup / "manifest.json", manifest)
    return manifest


def verify_backup(backup: Path) -> None:
    for kind, (folder, pattern) in ARTIFACTS.items():
        for chapter in iter_chapters():
            path = backup / folder / pattern.format(chapter=chapter)
            if not path.exists() or path.stat().st_size == 0:
                raise RuntimeError(f"备份校验失败: {kind} 第{chapter}章")


def clear_targets(project: Path) -> None:
    for kind in ARTIFACTS:
        for chapter in iter_chapters():
            path = artifact_path(project, kind, chapter)
            if path.exists():
                path.unlink()


def run_command(project: Path, name: str, args: list[str]) -> None:
    log(project, f"启动{name}: {' '.join(args)}")
    completed = subprocess.run(
        [sys.executable, *args],
        cwd=REPO_ROOT,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{name}失败，returncode={completed.returncode}")
    log(project, f"{name}完成")


def range_outlines_ok(project: Path, start: int, end: int, min_score: float) -> bool:
    for chapter in range(start, end + 1):
        outline = artifact_path(project, "outline", chapter)
        review = artifact_path(project, "outline_review", chapter)
        if not outline.exists() or not review.exists():
            return False
        try:
            data = json.loads(review.read_text(encoding="utf-8"))
            score = float(data.get("overall_score", 0))
        except Exception as exc:
            return False
        if data.get("status") != "completed" or data.get("verdict") in {"需修改", "需重写"} or score < min_score:
            return False
    return True


def range_finals_ok(project: Path, start: int, end: int) -> bool:
    statuses = scan_chapter_status(project, start, end, use_cache=False)
    return len(statuses) == end - start + 1 and all(status.final_ok for status in statuses.values())


def stop_helpers(project: Path) -> None:
    script = """
$matches = Get-CimInstance Win32_Process | Where-Object {
  $_.Name -in @('python.exe','node.exe') -and
  ($_.CommandLine -match 'wechat_pusher_lane.py|gate_watchdog.py') -and
  $_.CommandLine -match 'novels12'
}
foreach ($item in $matches) { Stop-Process -Id $item.ProcessId -Force -ErrorAction SilentlyContinue }
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", script], cwd=REPO_ROOT)
    for name in ("wechat_pusher.lock", "outline_gate_watchdog.lock", "draft_gate_watchdog.lock"):
        path = project / "logs" / name
        if path.exists():
            path.unlink()


def main() -> int:
    global RANGES
    parser = argparse.ArgumentParser(description="连续重构指定小说章节范围")
    parser.add_argument("--project", "-p", default=os.getenv("NOVEL_PROJECT_DIR", ""))
    parser.add_argument("--ranges", default="91-150,461-500", help="逗号分隔章节范围，如 15-40,56-80")
    parser.add_argument("--run-name", default="continuous_reconstruction", help="状态文件名与备份名后缀")
    parser.add_argument("--resume", action="store_true", help="从现有重构产物继续")
    args = parser.parse_args()

    project = resolve_project_dir(args.project)
    RANGES = parse_ranges(args.ranges)
    config = load_config(project)
    outline_min = float(config.get("outline_reviewer", {}).get("min_score", 8.5))
    safe_run_name = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in args.run_name)
    backup = project / "backups" / f"pre_{safe_run_name}_20260608"
    state_path = project / "reports" / "reconstruction" / f"{safe_run_name}_state.json"
    state = {
        "status": "running",
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ranges": RANGES,
        "phase": "prepare",
    }
    if state_path.exists() and args.resume:
        try:
            state.update(json.loads(state_path.read_text(encoding="utf-8")))
            state["status"] = "running"
        except Exception as exc:
            pass
    atomic_json(state_path, state)

    try:
        canon = project / "origin" / "00_reconstruction_canon.md"
        if not canon.exists():
            raise RuntimeError(f"缺少重构基线: {canon}")

        if not args.resume:
            log(project, "备份100章的大纲、审查、初稿、正文审查和终稿")
            prepare_backup(project, backup)
            verify_backup(backup)
            clear_targets(project)
            log(project, "备份校验完成，目标正式产物已清理")
        else:
            verify_backup(backup)
            log(project, "断点续跑，备份校验通过")

        state["phase"] = "outline"
        atomic_json(state_path, state)
        for start, end in RANGES:
            if range_outlines_ok(project, start, end, outline_min):
                log(project, f"第{start}-{end}章大纲已全部过审，跳过")
                continue
            run_command(
                project,
                f"第{start}-{end}章大纲重构",
                [
                    str(TOOLS_ROOT / "maintenance" / "outline_lane.py"),
                    "--project", str(project),
                    "--start", str(start),
                    "--end", str(end),
                    "--passes", "2",
                ],
            )
            if not range_outlines_ok(project, start, end, outline_min):
                raise RuntimeError(f"第{start}-{end}章大纲未全部达到{outline_min:g}分")

        state["phase"] = "draft"
        atomic_json(state_path, state)
        for start, end in RANGES:
            if range_finals_ok(project, start, end):
                log(project, f"第{start}-{end}章正文已全部过审，跳过")
                continue
            run_command(
                project,
                f"第{start}-{end}章正文重构",
                [
                    str(TOOLS_ROOT / "maintenance" / "draft_lane.py"),
                    "--project", str(project),
                    "--start", str(start),
                    "--end", str(end),
                    "--workers", "1",
                    "--continuity-window", "1",
                    "--wait-seconds", "5",
                ],
            )
            if not range_finals_ok(project, start, end):
                raise RuntimeError(f"第{start}-{end}章正文未全部通过质量门")

        state["status"] = "generation_completed"
        state["phase"] = "review_pending"
        state["generation_completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        atomic_json(state_path, state)
        log(project, "两个范围连续重构完成，等待整本复审")
        return 0
    except Exception as exc:
        state["status"] = "failed"
        state["error"] = str(exc)
        state["failed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        atomic_json(state_path, state)
        log(project, f"失败: {exc}")
        return 1
    finally:
        stop_helpers(project)


if __name__ == "__main__":
    raise SystemExit(main())
