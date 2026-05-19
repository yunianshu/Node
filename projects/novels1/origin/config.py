#!/usr/bin/env python3
"""
配置管理 - 从项目级 config.json 加载配置
所有 Agent 共享同一套配置对象
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_MMX_PATH = "C:/Users/Administrator/AppData/Roaming/npm/node_modules/mmx-cli/dist/mmx.mjs"


@dataclass
class WriterConfig:
    max_tokens: int = 8192
    temperature: float = 0.7
    context_chapters: int = 3
    max_retries: int = 3
    retry_delay: float = 5.0


@dataclass
class ReviewerConfig:
    max_tokens: int = 4096
    temperature: float = 0.3
    min_score: float = 7.0
    rewrite_threshold: float = 6.5
    dimensions: list = field(default_factory=lambda: ["文笔", "剧情", "人设", "连贯性"])
    max_retries: int = 3


@dataclass
class PlannerConfig:
    parallel_agents: int = 60
    segments: int = 60
    max_tokens: int = 8192
    temperature: float = 0.7


@dataclass
class CoordinatorConfig:
    batch_size: int = 20
    num_workers: int = 5
    wechat_webhook: str = ""
    push_interval_seconds: int = 120
    auto_fill_missing: bool = True
    auto_rewrite: bool = True
    pause_between_batches: float = 3.0


@dataclass
class ImageConfig:
    enabled: bool = True
    model: str = "image-01"
    aspect_ratio: str = "16:9"
    generate_cover: bool = True
    generate_chapter_images: bool = True
    max_images_per_chapter: int = 1


@dataclass
class VideoConfig:
    enabled: bool = False
    model: str = "MiniMax-Hailuo-2.3"
    generate_trailer: bool = True
    generate_chapter_videos: bool = False
    trailer_chapters: list = field(default_factory=lambda: [1, 500, 1000, 1500, 2000])
    duration: str = "6s"
    resolution: str = "768p"


@dataclass
class AudioConfig:
    enabled: bool = False
    model: str = "speech-2.8-hd"
    voice: str = "Chinese_Mandarin_Gentle_Youth"
    generate_chapter_audio: bool = True
    speed: float = 1.0


@dataclass
class MusicConfig:
    enabled: bool = False
    model: str = "music-2.6"
    generate_chapter_music: bool = False
    generate_theme_music: bool = True
    genre: str = "dark folk"
    mood: str = "mysterious, tense"
    vocals: str = "male, deep, emotional"
    instruments: str = "acoustic guitar, cello, piano"
    tempo: str = "slow"


@dataclass
class MultimediaConfig:
    enabled: bool = False
    images: ImageConfig = field(default_factory=ImageConfig)
    videos: VideoConfig = field(default_factory=VideoConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    music: MusicConfig = field(default_factory=MusicConfig)


@dataclass
class NovelConfig:
    """项目级配置"""
    title: str = "未命名小说"
    project_dir: str = ""
    total_chapters: int = 2000
    model: str = "MiniMax-M2.7-highspeed"
    mmx_path: str = DEFAULT_MMX_PATH
    api_qps: float = 2.0

    writer: WriterConfig = field(default_factory=WriterConfig)
    reviewer: ReviewerConfig = field(default_factory=ReviewerConfig)
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    coordinator: CoordinatorConfig = field(default_factory=CoordinatorConfig)
    multimedia: MultimediaConfig = field(default_factory=MultimediaConfig)

    @property
    def path(self) -> Path:
        return Path(self.project_dir)

    @property
    def output_dir(self) -> Path:
        return self.path / "output"

    @property
    def chapters_dir(self) -> Path:
        return self.output_dir / "chapters"

    @property
    def draft_dir(self) -> Path:
        return self.chapters_dir / "draft"

    @property
    def final_dir(self) -> Path:
        return self.chapters_dir / "final"

    @property
    def reviews_dir(self) -> Path:
        return self.output_dir / "reviews"

    @property
    def logs_dir(self) -> Path:
        return self.path / "logs"

    @property
    def world_file(self) -> Path:
        return self.output_dir / "world.json"

    @property
    def outline_file(self) -> Path:
        return self.output_dir / "outline.json"

    @property
    def characters_file(self) -> Path:
        return self.output_dir / "characters.json"

    @property
    def progress_file(self) -> Path:
        return self.output_dir / "progress.json"

    @property
    def summary_file(self) -> Path:
        return self.output_dir / "summary_report.json"

    @property
    def prompts_dir(self) -> Path:
        return self.path / "prompts"

    @property
    def images_dir(self) -> Path:
        return self.path / "images"

    @property
    def videos_dir(self) -> Path:
        return self.path / "videos"

    @property
    def audio_dir(self) -> Path:
        return self.path / "audio"

    @property
    def music_dir(self) -> Path:
        return self.path / "music"

    @classmethod
    def load(cls, config_path: str | Path) -> "NovelConfig":
        """从 JSON 文件加载配置"""
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")

        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        # 递归转换 dict -> dataclass（支持多层嵌套）
        NESTED_MAP = {
            "writer": WriterConfig, "reviewer": ReviewerConfig,
            "planner": PlannerConfig, "coordinator": CoordinatorConfig,
            "multimedia": MultimediaConfig,
            "images": ImageConfig, "videos": VideoConfig,
            "audio": AudioConfig, "music": MusicConfig,
        }

        def _pop(cfg_class, data: dict):
            kwargs = {}
            for field_name in cfg_class.__dataclass_fields__:
                if field_name in data:
                    val = data[field_name]
                    # 递归处理嵌套 dataclass
                    if isinstance(val, dict) and field_name in NESTED_MAP:
                        kwargs[field_name] = _pop(NESTED_MAP[field_name], val)
                    else:
                        kwargs[field_name] = val
            return cfg_class(**kwargs)

        return _pop(cls, raw)

    def save(self, config_path: str | Path | None = None):
        """保存配置到 JSON"""
        path = Path(config_path) if config_path else self.path / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)

        def to_dict(obj: Any) -> Any:
            if isinstance(obj, Path):
                return str(obj)
            if hasattr(obj, "__dataclass_fields__"):
                return {k: to_dict(v) for k, v in obj.__dict__.items()}
            return obj

        with open(path, "w", encoding="utf-8") as f:
            json.dump(to_dict(self), f, ensure_ascii=False, indent=2)
