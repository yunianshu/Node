#!/usr/bin/env python3
"""
视频生成器 - 生成小说预告片和章节短视频
使用 mmx video generate 命令 (Hailuo-2.3)
"""
import subprocess
from pathlib import Path

from novels.core.config import NovelConfig
from novels.multimedia.base import BaseMultimediaGenerator


class VideoGenerator(BaseMultimediaGenerator):
    """视频生成器"""

    def __init__(self, config: NovelConfig):
        super().__init__(config, "video")
        self.videos_dir = config.videos_dir
        self.videos_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = config.multimedia.videos

    def output_path(self, chapter_number: int) -> Path:
        return self.videos_dir / f"chapter_{chapter_number:04d}.mp4"

    def trailer_path(self) -> Path:
        return self.videos_dir / "trailer.mp4"

    def generate_trailer(self, world_info: dict = None) -> bool:
        """生成小说预告片"""
        if not self.cfg.generate_trailer:
            return False
        if self.trailer_path().exists():
            self.logger.info("预告片已存在，跳过")
            return True

        title = world_info.get("title", self.config.title) if world_info else self.config.title
        prompt = (
            f"小说《{title}》预告片，史诗级玄幻场景，"
            f"宏大世界观，主角出场，战斗场面，震撼特效，"
            f" cinematic, highly detailed, dramatic lighting"
        )

        return self._call_video_generate(prompt, self.trailer_path())

    def generate(self, chapter_number: int, content: str = "", outline_info: dict = None) -> bool:
        """生成章节短视频"""
        if not self.cfg.generate_chapter_videos:
            return False
        if self.exists(chapter_number):
            self.logger.debug(f"第{chapter_number}章视频已存在，跳过")
            return True

        # 视频配额极低（每天3个），只在关键章节生成
        if chapter_number not in self.cfg.trailer_chapters:
            return False

        outline = outline_info or self.read_outline_info(chapter_number)
        title = outline.get("title", "")
        summary = outline.get("summary", "")
        content_sample = content[:200] if content else ""

        prompt = (
            f"小说第{chapter_number}章《{title}》场景，"
            f"{summary or content_sample}，"
            f" cinematic, highly detailed, dramatic"
        )
        out_path = self.output_path(chapter_number)

        return self._call_video_generate(prompt, out_path)

    def _call_video_generate(self, prompt: str, out_path: Path) -> bool:
        """调用 mmx video generate"""
        cmd = [
            "node", self.config.mmx_path, "video", "generate",
            "--model", self.cfg.model,
            "--prompt", prompt,
            "--duration", self.cfg.duration,
            "--resolution", self.cfg.resolution,
            "--out", str(out_path),
            "--quiet",
        ]
        try:
            self.logger.info(f"生成视频: {out_path.name}")
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=300)
            if result.returncode != 0:
                err = result.stderr[:200] if result.stderr else "unknown"
                self.logger.error(f"视频生成失败: {err}")
                return False
            self.logger.info(f"视频生成成功: {out_path}")
            return True
        except subprocess.TimeoutExpired:
            self.logger.error("视频生成超时")
            return False
        except Exception as e:
            self.logger.error(f"视频生成异常: {e}")
            return False
