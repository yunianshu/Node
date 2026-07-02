#!/usr/bin/env python3
"""小说项目配置读取。"""
from __future__ import annotations

import copy
import json
import os
import re
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
        "max_tokens": 6144,
        "temperature": 0.1,
        "min_score": 8.5,
        "origin_max_chars": 4000,
        "semantic_retries": 1,
    },
    "outline_reviewer": {
        "max_tokens": 6144,
        "temperature": 0.1,
        "min_score": 8.5,
        "origin_max_chars": 4000,
    },
    "outline_memory": {
        "milestone_size": 20,
        "recent_chapters": 12,
        "max_prompt_chars": 7000,
    },
    "outline_book_reviewer": {
        "enabled": False,
        "required_before_draft": False,
        "min_score": 9.0,
        "segment_size": 25,
        "volume_size": 100,
        "workers": 3,
        "max_tokens": 6144,
        "temperature": 0.1,
        "retries": 2,
        "retry_delay": 5.0,
        "timeout_seconds": 300,
        "semantic_retries": 1,
        "review_rounds": 1,
        "required_votes": 1,
        "max_repair_rounds": 2,
        "max_repair_chapters": 80,
    },
    "planner": {
        "max_tokens": 8192,
        "temperature": 0.3,
        "parallel_agents": 2,
        "semantic_retries": 1,
    },
    "coordinator": {
        "draft_workers": 1,
        "draft_continuity_window": 1,
        "push_interval_seconds": 120,
        "outline_lookahead_chapters": 10,
        "outline_overlap_repair_passes": 1,
        "outline_attempts_per_round": 1,
        "outline_analysis_rounds": 2,
        "draft_attempts_per_round": 1,
        "draft_analysis_rounds": 2,
    },
    "outline_race": {
        "enabled": False,
        "candidates": 3,
        "max_workers": 3,
        "stop_on_first_pass": True,
        "early_stop_score": 8.7,
    },
    "draft_race": {
        "enabled": True,
        "candidates": 3,
        "max_workers": 3,
        "stop_on_first_pass": True,
        "early_stop_score": 8.6,
    },
    "repair": {
        "max_consecutive_failures": 5,
    },
    "book_reviewer": {
        "enabled": True,
        "required_on_finish": True,
        "min_score": 8.5,
        "workers": 5,
        "max_full_chapters": 16,
        "repair_relationship_targets_on_failure": True,
        "max_relationship_repair_chapters": 10,
        "repair_human_warmth_streaks_on_failure": True,
        "max_human_warmth_repair_chapters": 10,
        "auto_resume_after_repair": False,
        "max_auto_resume_cycles": 1,
        "auto_retry_verification_feedback": False,
        "max_verification_retry_cycles": 1,
        "segment_size": 10,
        "volume_size": 50,
        "max_tokens": 6144,
        "temperature": 0.1,
        "semantic_retries": 1,
    },
    "relationship_state": {
        "max_tokens": 2048,
        "temperature": 0.1,
        "retries": 2,
        "retry_delay": 5.0,
        "timeout_seconds": 120,
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
    "chapters/outline_review",
    "chapters/draft",
    "chapters/review",
    "chapters/final",
    "chapters/character_states",
    "chapters/arc_states",
    "chapters/relationship_states",
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

ORIGIN_FACT_DIR_NAMES = {
    "fact",
    "facts",
    "setting",
    "settings",
    "world",
    "worldbuilding",
    "character",
    "characters",
    "timeline",
    "canon",
}

ORIGIN_STYLE_DIR_NAMES = {
    "style",
    "styles",
    "voice",
    "tone",
    "sample",
    "samples",
    "example",
    "examples",
    "prose",
}

REPO_ROOT = Path(__file__).resolve().parents[2]


def ensure_project_structure(project_dir: Path) -> None:
    """确保小说项目目录符合统一结构。"""
    project_dir.mkdir(parents=True, exist_ok=True)
    for rel in STANDARD_PROJECT_DIRS:
        (project_dir / rel).mkdir(parents=True, exist_ok=True)


def _origin_material_kind(path: Path, origin_dir: Path) -> str:
    try:
        rel_parts = path.relative_to(origin_dir).parts
    except ValueError:
        rel_parts = path.parts
    normalized = {part.lower().replace("-", "_") for part in rel_parts}
    if normalized & ORIGIN_FACT_DIR_NAMES:
        return "fact"
    if normalized & ORIGIN_STYLE_DIR_NAMES:
        return "style"
    name = path.stem.lower().replace("-", "_")
    if any(token in name for token in ("fact", "setting", "world", "character", "timeline", "canon")):
        return "fact"
    if any(token in name for token in ("style", "voice", "tone", "sample", "example", "prose")):
        return "style"
    return "general"


def load_origin_materials(project_dir: Path, *, max_files: int = 20, max_chars: int = 12000) -> str:
    """读取 origin/ 中的文本素材，作为生成参考资料。"""
    origin_dir = project_dir / "origin"
    if not origin_dir.exists():
        return ""

    files = [
        path
        for path in sorted(origin_dir.rglob("*"))
        if path.is_file() and path.suffix.lower() in ORIGIN_TEXT_EXTENSIONS
    ]
    order = {"fact": 0, "general": 1, "style": 2}
    files = sorted(files, key=lambda path: (order.get(_origin_material_kind(path, origin_dir), 9), path.as_posix()))[:max_files]
    if not files:
        return ""

    remaining = max_chars
    sections_by_kind = {"fact": [], "general": [], "style": []}
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
        kind = _origin_material_kind(path, origin_dir)
        sections_by_kind.setdefault(kind, []).append(f"### origin/{rel}\n{excerpt}")
        remaining -= len(excerpt) + len(rel) + 32

    grouped_sections = []
    if sections_by_kind.get("fact"):
        grouped_sections.append(
            "## origin/facts 事实素材（最高优先级：设定、人物关系、历史事件、时间线不得违背）\n"
            + "\n\n".join(sections_by_kind["fact"])
        )
    if sections_by_kind.get("general"):
        grouped_sections.append(
            "## origin/general 综合参考素材（需吸收具体人物、地点、事件和限制）\n"
            + "\n\n".join(sections_by_kind["general"])
        )
    if sections_by_kind.get("style"):
        grouped_sections.append(
            "## origin/style 风格素材（只约束语气、叙述节奏、描写质感；不得覆盖事实素材）\n"
            + "\n\n".join(sections_by_kind["style"])
        )

    if not grouped_sections:
        return ""
    return "\n\n".join(grouped_sections)


def origin_fact_block(origin_materials: str) -> str:
    """Return only the fact-priority part of load_origin_materials() output."""
    text = str(origin_materials or "")
    if "## origin/facts" not in text:
        return ""
    start = text.find("## origin/facts")
    rest = text[start:]
    next_heading = re.search(r"\n## origin/(?:general|style)\b", rest)
    if next_heading:
        return rest[: next_heading.start()]
    return rest


def extract_origin_fact_terms(origin_materials: str, *, limit: int = 40) -> list[str]:
    """Extract compact concrete terms from origin/facts for prompts and review evidence."""
    block = origin_fact_block(origin_materials)
    if not block:
        return []
    stopwords = {
        "origin", "facts", "事实素材", "最高优先级", "设定", "人物关系", "历史事件", "时间线",
        "不得违背", "chapter", "json", "txt", "markdown", "yaml", "csv",
        "fact", "setting", "settings", "worldbuilding", "character", "characters", "timeline", "canon",
        "一个", "一种", "这个", "那个", "本章", "主角", "角色", "故事", "世界", "需要", "必须",
    }
    stop_phrases = ("最高优先级", "事实素材", "不得违背", "时间线", "人物关系", "历史事件")
    candidates: list[str] = []
    for token in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_]{2,}", block):
        token = token.strip()
        if (
            not token
            or token in stopwords
            or token.lower() in stopwords
            or any(phrase in token for phrase in stop_phrases)
        ):
            continue
        candidates.append(token)
        if re.fullmatch(r"[\u4e00-\u9fff]{7,}", token):
            candidates.append(token[:2])
            for size in (3, 4, 2):
                for index in range(0, len(token) - size + 1):
                    piece = token[index:index + size]
                    if piece not in stopwords and not any(phrase in piece for phrase in stop_phrases):
                        candidates.append(piece)
    seen: list[str] = []
    for token in candidates:
        if token not in seen:
            seen.append(token)
        if len(seen) >= limit:
            break
    return seen


def extract_origin_fact_clauses(origin_materials: str, *, limit: int = 12) -> list[str]:
    """Extract compact fact clauses from origin/facts, excluding headings and labels."""
    block = origin_fact_block(origin_materials)
    if not block:
        return []
    clauses: list[str] = []
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^[-*]\s*", "", line).strip()
        line = re.sub(r"^[\"'“”]+|[\"'“”]+$", "", line).strip()
        if not line or line.lower().startswith("origin/"):
            continue
        for piece in re.split(r"[。！？!?；;\n]+", line):
            text = re.sub(r"\s+", "", piece).strip("，,：:、 ")
            if len(text) < 8:
                continue
            if any(phrase in text for phrase in ("事实素材", "最高优先级", "不得违背")):
                continue
            clauses.append(text[:120])
            if len(clauses) >= limit:
                return clauses
    return clauses


def build_origin_fact_directive(origin_materials: str, *, limit: int = 8) -> str:
    """Build a short generation directive that makes fact usage explicit."""
    terms = extract_origin_fact_terms(origin_materials, limit=limit)
    if not terms:
        return ""
    clauses = extract_origin_fact_clauses(origin_materials, limit=3)
    clause_line = f"- 必须兑现的事实短句：{'；'.join(clauses)}\n" if clauses else ""
    return (
        "## origin/facts 本章事实落点\n"
        f"- 可选事实线索：{'、'.join(terms)}\n"
        f"{clause_line}"
        "- 本章必须主动选择并落地 1-2 条事实线索，让人物、地点、旧事、物件或关系进入剧情现场；"
        "不得只模仿 origin/style 的语气而忽略事实素材。\n"
    )


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
