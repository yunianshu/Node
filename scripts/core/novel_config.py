#!/usr/bin/env python3
"""小说项目配置读取。"""
from __future__ import annotations

import copy
import json
import os
import re

import sys
from pathlib import Path


DEFAULT_CONFIG = {
    "total_chapters": 2000,
    "webhook_url": "",
    "api_qps": 5.0,
    # 共享 LLM 缺省配置：Agent 节未指定 provider/model 时回退到这里。
    "llm": {
        "provider": "deepseek",
        "model": "",
        "base_url": "",
        "api_key_env": "",
        "api_key": "",
        "api_qps": None,
        "timeout_seconds": None,
        "extra_body": {},
        "headers": {},
    },
    # 共享审查 LLM 配置（兼容旧字段名）：优先级低于各审查 Agent 自己的节。
    "review_ai": {
        "provider": "glm",
        "model": "",
        "base_url": "",
        "api_key_env": "",
        "api_key": "",
        "api_qps": None,
        "timeout_seconds": None,
        "extra_body": {},
        "headers": {},
    },
    "writer": {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "max_tokens": 8192,
        "temperature": 0.7,
        "max_retries": 3,
        "retry_delay": 5.0,
    },
    "reviewer": {
        "provider": "glm",
        "model": "glm-4.6",
        "max_tokens": 6144,
        "temperature": 0.1,
        "min_score": 8.5,
        "origin_max_chars": 4000,
        "semantic_retries": 1,
    },
    "outline_reviewer": {
        "provider": "glm",
        "model": "glm-4.6",
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

    "planner": {
        "provider": "deepseek",
        "model": "deepseek-chat",
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
        "provider": "deepseek",
        "model": "deepseek-chat",
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
    "outliner": {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "max_tokens": 8192,
        "temperature": 0.5,
    },
    "polisher": {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "max_tokens": 8192,
        "temperature": 0.4,
    },
    "character_state": {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "max_tokens": 2048,
        "temperature": 0.1,
        "retries": 2,
        "retry_delay": 5.0,
        "timeout_seconds": 120,
    },
    "arc_state": {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "max_tokens": 2048,
        "temperature": 0.1,
        "retries": 2,
        "retry_delay": 5.0,
        "timeout_seconds": 120,
    },
}

# 生成端 Agent 与审查端 Agent 分组；两端的 (provider, model) 必须不同。
GENERATOR_LLM_SECTIONS = ("planner", "outliner", "writer", "polisher")
REVIEWER_LLM_SECTIONS = ("reviewer", "outline_reviewer")

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
        except Exception as exc:
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



def _env_section_name(section: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(section or "").upper()).strip("_")


def _first_env_value(names: list[str] | tuple[str, ...]) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _resolve_tool_path(value: str, project_dir: Path) -> str:
    text = _expand_path_text(value)
    if not text:
        return ""
    return text


def resolve_agent_model(
    config: dict,
    section: str,
    *,
    shared_section: str | None = "llm",
    env_aliases: tuple[str, ...] = (),
) -> str:
    """Resolve a stage-specific model with shared llm fallback."""
    env_names = [f"NOVEL_{_env_section_name(section)}_MODEL", *env_aliases]
    if shared_section:
        env_names.append(f"NOVEL_{_env_section_name(shared_section)}_MODEL")
    env_value = _first_env_value(env_names)
    if env_value:
        return env_value

    section_cfg = config.get(section, {})
    if isinstance(section_cfg, dict) and str(section_cfg.get("model", "") or "").strip():
        return str(section_cfg["model"]).strip()

    shared_cfg = config.get(shared_section, {}) if shared_section else {}
    if isinstance(shared_cfg, dict) and str(shared_cfg.get("model", "") or "").strip():
        return str(shared_cfg["model"]).strip()

    return ""


def resolve_agent_qps(
    config: dict,
    section: str,
    *,
    shared_section: str | None = "llm",
) -> float:
    for cfg_name in (section, shared_section):
        cfg = config.get(cfg_name, {}) if cfg_name else {}
        if isinstance(cfg, dict) and cfg.get("api_qps") not in (None, ""):
            try:
                return float(cfg["api_qps"])
            except (TypeError, ValueError):
                pass
    try:
        return float(config.get("api_qps", DEFAULT_CONFIG["api_qps"]))
    except (TypeError, ValueError):
        return float(DEFAULT_CONFIG["api_qps"])


def validate_model_separation(config: dict) -> None:
    """校验审查端与生成端的 LLM (provider, model) 不得相同。

    违规时抛 ValueError，阻断流程——质量检查必须用与生成不同的模型，
    避免同模型自审带来的系统性偏差。
    """
    from core.llm_client import resolve_provider, resolve_model

    def identity(section: str) -> tuple[str, str]:
        provider = str(resolve_provider(config, section)).lower()
        try:
            model = str(resolve_model(config, section)).lower()
        except Exception as exc:
            model = ""
        return provider, model

    generator_ids = {section: identity(section) for section in GENERATOR_LLM_SECTIONS}
    reviewer_ids = {section: identity(section) for section in REVIEWER_LLM_SECTIONS}

    for review_section, review_identity in reviewer_ids.items():
        for gen_section, gen_identity in generator_ids.items():
            if not review_identity[1] or not gen_identity[1]:
                continue
            if review_identity == gen_identity:
                raise ValueError(
                    f"模型分离校验失败：{review_section} 与生成端 {gen_section} 使用了相同模型 "
                    f"{review_identity[0]}/{review_identity[1]}。"
                    "质量检查必须与大纲/草稿生成使用不同模型，请修改 config.json 中对应节的 "
                    "provider/model 配置。"
                )


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
    else:
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
        except Exception as exc:
            config = copy.deepcopy(DEFAULT_CONFIG)
        else:
            config = deep_merge(DEFAULT_CONFIG, data)
    validate_model_separation(config)
    return config


def get_book_title(project_dir: Path, *, default: str = "未命名小说") -> str:
    """读取 world.json 中的书名，失败时返回 default。"""
    world_file = Path(project_dir) / "world.json"
    if not world_file.exists():
        return default
    try:
        world = json.loads(world_file.read_text(encoding="utf-8"))
        return str(world.get("title") or default)
    except Exception as exc:
        return default
