#!/usr/bin/env python3
"""小说项目配置读取。"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


DEFAULT_CONFIG = {
    "total_chapters": 2000,
    "model": "MiniMax-M3",
    "mmx_path": "C:/Users/59290/AppData/Roaming/npm/node_modules/mmx-cli/dist/mmx.mjs",
    "webhook_url": "",
    "api_qps": 5.0,
    "writer": {
        "max_tokens": 8192,
        "temperature": 0.7,
        "max_retries": 3,
        "retry_delay": 5.0,
    },
    "reviewer": {
        "max_tokens": 4096,
        "temperature": 0.3,
        "min_score": 7.0,
        "origin_max_chars": 4000,
    },
    "outline_reviewer": {
        "max_tokens": 4096,
        "temperature": 0.3,
        "min_score": 8.5,
        "origin_max_chars": 4000,
    },
    "planner": {
        "max_tokens": 8192,
        "temperature": 0.5,
        "parallel_agents": 2,
    },
    "coordinator": {
        "batch_size": 40,
        "num_workers": 2,
        "review_workers": 2,
        "draft_workers": 2,
        "pause_between_batches": 3.0,
        "push_interval_seconds": 120,
        "outline_lookahead_chapters": 10,
    },
    "repair": {
        "max_consecutive_failures": 5,
    },
    "media": {
        "enabled": True,
        "generate_after_planner": True,
        "cover_aspect_ratio": "3:4",
        "video_model": "MiniMax-Hailuo-2.3",
        "song_format": "mp3",
    },
    "quality": {
        "min_chapter_words": 5000,
        "max_chapter_words": 12000,
        "warn_min_chapter_words": 4800,
        "warn_max_chapter_words": 15000,
        "hard_fail_min_chapter_words": 3000,
        "min_paragraphs": 20,
        "max_duplicate_paragraph_ratio": 0.25,
        "max_similar_paragraph_ratio": 0.20,
        "similar_paragraph_threshold": 0.88,
        "title_required": False,
        "title_prefixes": ["第"],
        "title_keywords": ["章", "节", "回"],
    },
}

STANDARD_PROJECT_DIRS = (
    "chapters/outline",
    "chapters/draft",
    "chapters/review",
    "chapters/final",
    "logs",
    "media/audio",
    "media/images",
    "media/music",
    "media/videos",
    "origin",
    "reports",
)

ORIGIN_TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".json",
    ".jsonl",
    ".csv",
    ".tsv",
    ".yaml",
    ".yml",
}

REPO_ROOT = Path(__file__).resolve().parents[2]


def ensure_project_structure(project_dir: Path) -> None:
    """确保小说项目目录符合统一结构。"""
    project_dir.mkdir(parents=True, exist_ok=True)
    for rel in STANDARD_PROJECT_DIRS:
        (project_dir / rel).mkdir(parents=True, exist_ok=True)


def load_origin_materials(project_dir: Path, *, max_files: int = 20, max_chars: int = 12000) -> str:
    """读取 origin/ 中的文本素材，作为生成参考资料。"""
    origin_dir = project_dir / "origin"
    if not origin_dir.exists():
        return ""

    files = [
        path
        for path in sorted(origin_dir.rglob("*"))
        if path.is_file() and path.suffix.lower() in ORIGIN_TEXT_EXTENSIONS
    ][:max_files]
    if not files:
        return ""

    remaining = max_chars
    sections = []
    for path in files:
        if remaining <= 0:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:
            continue
        if not text:
            continue
        rel = path.relative_to(origin_dir).as_posix()
        budget = max(0, remaining - len(rel) - 32)
        if budget <= 0:
            break
        excerpt = text[:budget]
        sections.append(f"### origin/{rel}\n{excerpt}")
        remaining -= len(excerpt) + len(rel) + 32

    if not sections:
        return ""
    return "\n\n".join(sections)


def get_webhook_url(config: dict) -> str:
    env_url = os.getenv("NOVEL_WEBHOOK_URL", "").strip()
    if env_url:
        return env_url
    url = str(config.get("webhook_url", "") or "").strip()
    if url:
        return url
    coordinator = config.get("coordinator", {})
    if isinstance(coordinator, dict):
        return str(coordinator.get("wechat_webhook", "") or "").strip()
    return ""


def configure_stdio() -> None:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _expand_path_text(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(str(value or "").strip()))


def _path_candidates(value: str, project_dir: Path) -> list[Path]:
    text = _expand_path_text(value)
    if not text:
        return []
    path = Path(text)
    if path.is_absolute():
        return [path]
    return [
        (project_dir / path).resolve(),
        (REPO_ROOT / path).resolve(),
        path.resolve(),
    ]


def _npm_global_mmx_candidates() -> list[Path]:
    candidates: list[Path] = []
    npm = shutil.which("npm")
    if npm:
        try:
            result = subprocess.run(
                [npm, "root", "-g"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
            root = result.stdout.strip()
            if result.returncode == 0 and root:
                candidates.append(Path(root) / "mmx-cli" / "dist" / "mmx.mjs")
        except Exception:
            pass
    for env_name in ("APPDATA", "LOCALAPPDATA"):
        base = os.getenv(env_name)
        if base:
            candidates.append(Path(base) / "npm" / "node_modules" / "mmx-cli" / "dist" / "mmx.mjs")
            candidates.append(Path(base) / "Roaming" / "npm" / "node_modules" / "mmx-cli" / "dist" / "mmx.mjs")
    return candidates


def resolve_mmx_path(config: dict, project_dir: Path) -> str:
    """Resolve MiniMax CLI location across machines.

    Priority: env override, project config, npm global install, command on PATH.
    The returned value may be a .mjs/.js path or an executable command.
    """
    explicit = os.getenv("NOVEL_MMX_PATH") or os.getenv("MMX_PATH") or str(config.get("mmx_path", "") or "")
    for candidate in _path_candidates(explicit, project_dir):
        if candidate.exists():
            return str(candidate)

    for candidate in _npm_global_mmx_candidates():
        if candidate.exists():
            return str(candidate.resolve())

    for command in ("mmx", "mmx-cli"):
        found = shutil.which(command)
        if found:
            return found

    expanded = _expand_path_text(explicit)
    return expanded or str(DEFAULT_CONFIG["mmx_path"])


def resolve_project_dir(value: str | os.PathLike | None = None) -> Path:
    """Resolve a project directory without hardcoding the repository path."""
    raw = str(value or "").strip() or os.getenv("NOVEL_PROJECT_DIR", "").strip()
    if raw:
        return Path(_expand_path_text(raw)).resolve()

    cwd = Path.cwd().resolve()
    if (cwd / "config.json").exists() and ((cwd / "chapters").exists() or (cwd / "premise.txt").exists()):
        return cwd

    projects_dir = cwd / "projects"
    if projects_dir.exists():
        project_candidates = [
            item for item in projects_dir.iterdir()
            if item.is_dir() and (item / "config.json").exists()
        ]
        if len(project_candidates) == 1:
            return project_candidates[0].resolve()

    repo_projects = REPO_ROOT / "projects"
    if repo_projects.exists():
        project_candidates = [
            item for item in repo_projects.iterdir()
            if item.is_dir() and (item / "config.json").exists()
        ]
        if len(project_candidates) == 1:
            return project_candidates[0].resolve()

    raise ValueError("必须指定 --project、设置 NOVEL_PROJECT_DIR，或在项目目录/单项目仓库根目录运行")


def deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(project_dir: Path) -> dict:
    project_dir = Path(project_dir).resolve()
    ensure_project_structure(project_dir)
    config_file = project_dir / "config.json"
    if not config_file.exists():
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["mmx_path"] = resolve_mmx_path(config, project_dir)
        return config
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except Exception:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["mmx_path"] = resolve_mmx_path(config, project_dir)
        return config
    config = deep_merge(DEFAULT_CONFIG, data)
    config["mmx_path"] = resolve_mmx_path(config, project_dir)
    return config
