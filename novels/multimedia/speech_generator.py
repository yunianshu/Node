#!/usr/bin/env python3
"""
语音生成器 - 将章节文本转为语音朗读
使用 mmx speech synthesize 命令 (speech-2.8-hd)
"""
import subprocess
from pathlib import Path

from novels.core.config import NovelConfig
from novels.multimedia.base import BaseMultimediaGenerator


class SpeechGenerator(BaseMultimediaGenerator):
    """语音生成器（TTS）"""

    def __init__(self, config: NovelConfig):
        super().__init__(config, "speech")
        self.audio_dir = config.audio_dir
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = config.multimedia.audio

    def output_path(self, chapter_number: int) -> Path:
        return self.audio_dir / f"chapter_{chapter_number:04d}.mp3"

    def generate(self, chapter_number: int, content: str = "", outline_info: dict = None) -> bool:
        """生成章节语音朗读"""
        if not self.cfg.generate_chapter_audio:
            return False
        if self.exists(chapter_number):
            self.logger.debug(f"第{chapter_number}章语音已存在，跳过")
            return True

        # 读取章节内容（如果未提供）
        text = content or self.read_chapter_content(chapter_number)
        if not text or len(text) < 100:
            self.logger.warning(f"第{chapter_number}章内容过短，跳过语音生成")
            return False

        # speech API 限制约 10k 字符，超长需要截断
        if len(text) > 9000:
            text = text[:9000]
            self.logger.warning(f"第{chapter_number}章内容截断至9000字符")

        out_path = self.output_path(chapter_number)
        return self._call_speech_synthesize(text, out_path)

    def _call_speech_synthesize(self, text: str, out_path: Path) -> bool:
        """调用 mmx speech synthesize"""
        cmd = [
            "node", self.config.mmx_path, "speech", "synthesize",
            "--model", self.cfg.model,
            "--voice", self.cfg.voice,
            "--speed", str(self.cfg.speed),
            "--text", text,
            "--out", str(out_path),
            "--quiet",
        ]
        try:
            self.logger.info(f"生成语音: {out_path.name}")
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=120)
            if result.returncode != 0:
                err = result.stderr[:200] if result.stderr else "unknown"
                self.logger.error(f"语音生成失败: {err}")
                return False
            self.logger.info(f"语音生成成功: {out_path}")
            return True
        except subprocess.TimeoutExpired:
            self.logger.error("语音生成超时")
            return False
        except Exception as e:
            self.logger.error(f"语音生成异常: {e}")
            return False
