#!/usr/bin/env python3
"""
音乐生成器 - 为章节生成背景音乐/主题曲
使用 mmx music generate 命令 (music-2.6)
流程：先生成歌词(text chat) → 再生成音乐(music generate)
"""
import subprocess
from pathlib import Path

from novels.core.config import NovelConfig
from novels.multimedia.base import BaseMultimediaGenerator


class MusicGenerator(BaseMultimediaGenerator):
    """音乐生成器"""

    def __init__(self, config: NovelConfig):
        super().__init__(config, "music")
        self.music_dir = config.music_dir
        self.music_dir.mkdir(parents=True, exist_ok=True)
        self.lyrics_dir = config.path / "lyrics"
        self.lyrics_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = config.multimedia.music

    def output_path(self, chapter_number: int) -> Path:
        return self.music_dir / f"chapter_{chapter_number:04d}.mp3"

    def theme_path(self) -> Path:
        return self.music_dir / "theme.mp3"

    def generate_theme(self, world_info: dict = None) -> bool:
        """生成小说主题曲"""
        if not self.cfg.generate_theme_music:
            return False
        if self.theme_path().exists():
            self.logger.info("主题曲已存在，跳过")
            return True

        title = world_info.get("title", self.config.title) if world_info else self.config.title
        prompt = f"小说《{title}》主题曲，{self.cfg.genre}风格，{self.cfg.mood}"
        lyrics = f"[{title}]\n\n穿越浩瀚的星空\n踏上未知的旅程\n命运的齿轮转动\n英雄的故事开启"

        return self._call_music_generate(prompt, lyrics, self.theme_path())

    def generate(self, chapter_number: int, content: str = "", outline_info: dict = None) -> bool:
        """生成章节背景音乐"""
        if not self.cfg.generate_chapter_music:
            return False
        if self.exists(chapter_number):
            self.logger.debug(f"第{chapter_number}章音乐已存在，跳过")
            return True

        outline = outline_info or self.read_outline_info(chapter_number)
        title = outline.get("title", "")
        summary = outline.get("summary", "")

        # 1. 生成歌词
        lyrics = self._generate_lyrics(chapter_number, title, summary)
        if not lyrics:
            self.logger.warning(f"第{chapter_number}章歌词生成失败")
            return False

        # 2. 生成音乐提示词
        prompt = (
            f"小说第{chapter_number}章《{title}》背景音乐，"
            f"{self.cfg.genre}风格，{self.cfg.mood}，"
            f"场景：{summary[:100] if summary else ''}"
        )

        out_path = self.output_path(chapter_number)
        return self._call_music_generate(prompt, lyrics, out_path)

    def _generate_lyrics(self, chapter_number: int, title: str, summary: str) -> str:
        """使用 text chat 生成歌词"""
        lyrics_file = self.lyrics_dir / f"chapter_{chapter_number:04d}.txt"
        if lyrics_file.exists():
            return lyrics_file.read_text(encoding="utf-8")

        system = "你是一位专业的歌词创作人，擅长根据小说情节创作古风歌词。"
        user = (
            f"请为小说第{chapter_number}章《{title}》创作一段歌词（4-8句）。\n"
            f"情节概要：{summary[:200] if summary else '精彩的玄幻故事'}\n"
            f"要求：古风意境，押韵，有画面感，直接输出歌词内容，不要解释。"
        )

        text = self.llm.call(system, user, max_tokens=1024, temperature=0.7)
        if not text:
            return ""

        # 保存歌词
        lyrics_file.write_text(text, encoding="utf-8")
        return text

    def _call_music_generate(self, prompt: str, lyrics: str, out_path: Path) -> bool:
        """调用 mmx music generate"""
        cmd = [
            "node", self.config.mmx_path, "music", "generate",
            "--model", self.cfg.model,
            "--prompt", prompt,
            "--lyrics", lyrics,
            "--vocals", self.cfg.vocals,
            "--genre", self.cfg.genre,
            "--mood", self.cfg.mood,
            "--instruments", self.cfg.instruments,
            "--tempo", self.cfg.tempo,
            "--out", str(out_path),
            "--quiet",
        ]
        try:
            self.logger.info(f"生成音乐: {out_path.name}")
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=180)
            if result.returncode != 0:
                err = result.stderr[:200] if result.stderr else "unknown"
                self.logger.error(f"音乐生成失败: {err}")
                return False
            self.logger.info(f"音乐生成成功: {out_path}")
            return True
        except subprocess.TimeoutExpired:
            self.logger.error("音乐生成超时")
            return False
        except Exception as e:
            self.logger.error(f"音乐生成异常: {e}")
            return False
