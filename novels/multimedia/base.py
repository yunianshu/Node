#!/usr/bin/env python3
"""
多媒体生成器基类
所有多媒体生成器（图片、视频、语音、音乐）的公共抽象
"""
from abc import ABC, abstractmethod
from pathlib import Path

from novels.core.config import NovelConfig
from novels.core.llm_client import LLMClient
from novels.core.logger import get_logger


class BaseMultimediaGenerator(ABC):
    """多媒体生成器抽象基类"""

    def __init__(self, config: NovelConfig, media_type: str):
        self.config = config
        self.media_type = media_type
        self.llm = LLMClient(
            mmx_path=config.mmx_path,
            model=config.model,
            qps=config.api_qps,
            log_dir=str(config.logs_dir),
        )
        self.logger = get_logger(f"{media_type.capitalize()}Gen", config.logs_dir)

    @abstractmethod
    def generate(self, chapter_number: int, content: str = "", outline_info: dict = None) -> bool:
        """生成多媒体内容。返回是否成功。"""
        pass

    @abstractmethod
    def output_path(self, chapter_number: int) -> Path:
        """返回该章节的输出文件路径"""
        pass

    def exists(self, chapter_number: int) -> bool:
        """检查该章节的多媒体文件是否已存在且有效"""
        path = self.output_path(chapter_number)
        return path.exists() and path.stat().st_size > 1024

    def read_chapter_content(self, chapter_number: int, stage: str = "draft") -> str:
        """读取章节内容"""
        if stage == "draft":
            path = self.config.draft_dir / f"chapter_{chapter_number:04d}.txt"
        else:
            path = self.config.final_dir / f"chapter_{chapter_number:04d}.txt"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    def read_outline_info(self, chapter_number: int) -> dict:
        """读取章节大纲信息"""
        if not self.config.outline_file.exists():
            return {}
        try:
            import json
            with open(self.config.outline_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for ch in data.get("chapters", []):
                if ch.get("chapter_number") == chapter_number:
                    return ch
        except Exception:
            pass
        return {}
