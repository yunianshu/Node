#!/usr/bin/env python3
"""
图片生成器 - 生成小说封面和章节配图
使用 mmx image generate 命令
"""
import subprocess
from pathlib import Path

from novels.core.config import NovelConfig
from novels.multimedia.base import BaseMultimediaGenerator


class ImageGenerator(BaseMultimediaGenerator):
    """图片生成器"""

    def __init__(self, config: NovelConfig):
        super().__init__(config, "image")
        self.images_dir = config.images_dir
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = config.multimedia.images

    def output_path(self, chapter_number: int) -> Path:
        return self.images_dir / f"chapter_{chapter_number:04d}.jpg"

    def cover_path(self) -> Path:
        return self.images_dir / "cover.jpg"

    def generate_cover(self, world_info: dict = None) -> bool:
        """生成小说封面"""
        if not self.cfg.generate_cover:
            return False
        if self.cover_path().exists():
            self.logger.info("封面已存在，跳过")
            return True

        title = world_info.get("title", self.config.title) if world_info else self.config.title
        prompt = self._build_cover_prompt(title, world_info)

        return self._call_image_generate(prompt, self.cover_path())

    def generate(self, chapter_number: int, content: str = "", outline_info: dict = None) -> bool:
        """生成章节配图"""
        if not self.cfg.generate_chapter_images:
            return False
        if self.exists(chapter_number):
            self.logger.debug(f"第{chapter_number}章配图已存在，跳过")
            return True

        outline = outline_info or self.read_outline_info(chapter_number)
        prompt = self._build_chapter_prompt(chapter_number, outline, content)
        out_path = self.output_path(chapter_number)

        return self._call_image_generate(prompt, out_path)

    def _build_cover_prompt(self, title: str, world_info: dict = None) -> str:
        """构建封面提示词"""
        base = world_info.get("style", "玄幻仙侠") if world_info else "玄幻仙侠"
        desc = world_info.get("summary", "") if world_info else ""
        prompt = (
            f"小说封面插画，{base}风格，书名《{title}》。"
            f"场景：{desc[:200] if desc else '宏大世界观'}。"
            f" cinematic lighting, highly detailed, epic composition, "
            f"专业书籍封面设计，无文字，画面震撼"
        )
        return prompt

    def _build_chapter_prompt(self, chapter_number: int, outline: dict, content: str) -> str:
        """构建章节配图提示词"""
        title = outline.get("title", "")
        summary = outline.get("summary", "")
        scene = outline.get("scene", "")

        # 从内容提取前200字作为场景描述
        content_sample = content[:300] if content else ""

        prompt = (
            f"小说章节插画，{self.config.title}第{chapter_number}章。"
            f"标题：{title}。"
            f"场景：{scene or summary or content_sample[:200]}。"
            f" cinematic lighting, highly detailed, dramatic composition, "
            f"中式风格插画，无文字"
        )
        return prompt

    def _call_image_generate(self, prompt: str, out_path: Path) -> bool:
        """调用 mmx image generate"""
        cmd = [
            "node", self.config.mmx_path, "image", "generate",
            "--model", self.cfg.model,
            "--prompt", prompt,
            "--aspect-ratio", self.cfg.aspect_ratio,
            "--out", str(out_path),
            "--quiet",
        ]
        try:
            self.logger.info(f"生成图片: {out_path.name}")
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=120)
            if result.returncode != 0:
                err = result.stderr[:200] if result.stderr else "unknown"
                self.logger.error(f"图片生成失败: {err}")
                return False
            self.logger.info(f"图片生成成功: {out_path}")
            return True
        except subprocess.TimeoutExpired:
            self.logger.error("图片生成超时")
            return False
        except Exception as e:
            self.logger.error(f"图片生成异常: {e}")
            return False
