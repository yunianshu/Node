#!/usr/bin/env python3
"""
Agent 基类 - 所有 Agent 的公共抽象
定义统一接口：run()、load_prompt()、save_output()、report()
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from novels.core.config import NovelConfig
from novels.core.llm_client import LLMClient
from novels.core.logger import get_logger


@dataclass
class AgentResult:
    success: bool
    chapter_number: int
    output_path: Path | None = None
    error: str | None = None
    metadata: dict | None = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class BaseAgent(ABC):
    """Agent 抽象基类"""

    def __init__(self, config: NovelConfig):
        self.config = config
        self.llm = LLMClient(
            mmx_path=config.mmx_path,
            model=config.model,
            qps=config.api_qps,
            log_dir=str(config.logs_dir),
        )
        self.logger = get_logger(self.__class__.__name__, config.logs_dir)

    @abstractmethod
    def run(self, chapter_number: int, **kwargs: Any) -> AgentResult:
        """执行 Agent 的核心任务"""
        pass

    def load_prompt(self, prompt_name: str) -> str:
        """
        加载 prompt 模板。
        优先从项目 prompts/ 目录加载，回退到框架默认 prompts。
        """
        # 1. 项目级
        proj_path = self.config.prompts_dir / f"{prompt_name}.txt"
        if proj_path.exists():
            return proj_path.read_text(encoding="utf-8")

        # 2. 框架默认
        fallback = Path(__file__).parent.parent / "default_prompts" / f"{prompt_name}.txt"
        if fallback.exists():
            return fallback.read_text(encoding="utf-8")

        self.logger.warning(f"Prompt 模板不存在: {prompt_name}")
        return ""

    def load_json(self, filepath: Path) -> dict:
        """安全加载 JSON"""
        if not filepath.exists():
            return {}
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            self.logger.error(f"加载 JSON 失败 {filepath}: {e}")
            return {}

    def save_json(self, filepath: Path, data: dict):
        """安全保存 JSON"""
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def chapter_draft_path(self, chapter_number: int) -> Path:
        return self.config.draft_dir / f"chapter_{chapter_number:04d}.txt"

    def chapter_final_path(self, chapter_number: int) -> Path:
        return self.config.final_dir / f"chapter_{chapter_number:04d}.txt"

    def review_path(self, chapter_number: int) -> Path:
        return self.config.reviews_dir / f"chapter_{chapter_number:04d}_review.json"

    def chapter_exists(self, chapter_number: int, stage: str = "draft") -> bool:
        """检查章节是否已存在且有效（>1000字节）"""
        path = self.chapter_draft_path(chapter_number) if stage == "draft" else self.chapter_final_path(chapter_number)
        return path.exists() and path.stat().st_size > 1000

    def review_exists(self, chapter_number: int) -> bool:
        path = self.review_path(chapter_number)
        return path.exists()


import json
