#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniMax CLI 语音合成批量生成器
用于将小说章节文本转换为有声读物 MP3

功能：
- 读取章节 txt 文件
- 调用 mmx-cli 进行语音合成
- 支持断点续传
- 配额监控和等待逻辑
- 适配 Windows 环境

作者: AI Assistant
日期: 2026-05-15
"""

import os
import sys
import json
import time
import subprocess
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any

# ==================== 配置区域 ====================

# mmx-cli 路径 (Windows)
MMX_CLI_PATH = "C:/Users/Administrator/AppData/Roaming/npm/node_modules/mmx-cli/dist/mmx.mjs"

# 默认语音 (推荐中文语音)
DEFAULT_VOICE = "Chinese (Mandarin)_News_Anchor"

# 音频输出格式
DEFAULT_FORMAT = "mp3"

# 默认语速 (1.0 = 正常, 1.2 = 稍快)
DEFAULT_SPEED = 1.0

# 每章最大字符数 (mmx 支持最多 10k，留一些余量)
MAX_CHARS_PER_CALL = 9500

# 配额检查间隔 (秒)
QUOTA_CHECK_INTERVAL = 300

# 合成失败重试次数
MAX_RETRIES = 3

# 重试等待时间 (秒)
RETRY_DELAY = 10

# 进度保存文件
PROGRESS_FILE = "speech_progress.json"

# 日志文件
LOG_FILE = "speech_generator.log"

# =================================================


class SpeechGenerator:
    """MiniMax 语音合成批量生成器"""

    # 推荐中文语音列表
    CHINESE_VOICES = {
        "news_anchor": {
            "id": "Chinese (Mandarin)_News_Anchor",
            "desc": "新闻主播 - 专业、沉稳、适合 narration",
            "gender": "男",
            "style": "专业新闻播报",
        },
        "reliable_exec": {
            "id": "Chinese (Mandarin)_Reliable_Executive",
            "desc": "可靠高管 - 成熟稳重",
            "gender": "男",
            "style": "商务/正式",
        },
        "mature_woman": {
            "id": "Chinese (Mandarin)_Mature_Woman",
            "desc": "成熟女性 - 温柔知性",
            "gender": "女",
            "style": "温柔知性",
        },
        "sweet_lady": {
            "id": "Chinese (Mandarin)_Sweet_Lady",
            "desc": "甜美女士 - 甜美亲切",
            "gender": "女",
            "style": "甜美亲切",
        },
        "gentleman": {
            "id": "Chinese (Mandarin)_Gentleman",
            "desc": "绅士 - 优雅温和",
            "gender": "男",
            "style": "优雅温和",
        },
        "warm_girl": {
            "id": "Chinese (Mandarin)_Warm_Girl",
            "desc": "温暖女孩 - 活泼温暖",
            "gender": "女",
            "style": "活泼温暖",
        },
        "lyrical": {
            "id": "Chinese (Mandarin)_Lyrical_Voice",
            "desc": "抒情声音 - 富有感情",
            "gender": "男",
            "style": "抒情/富有感情",
        },
        "radio_host": {
            "id": "Chinese (Mandarin)_Radio_Host",
            "desc": "电台主持 - 亲和力强",
            "gender": "男",
            "style": "电台主持",
        },
        "unrestrained_youth": {
            "id": "Chinese (Mandarin)_Unrestrained_Young_Man",
            "desc": "不羁少年 - 年轻有活力",
            "gender": "男",
            "style": "年轻活力",
        },
        "gentle_youth": {
            "id": "Chinese (Mandarin)_Gentle_Youth",
            "desc": "温柔青年 - 温和细腻",
            "gender": "男",
            "style": "温柔细腻",
        },
        "yujie": {
            "id": "female-yujie",
            "desc": "御姐 - 成熟魅力",
            "gender": "女",
            "style": "成熟魅力",
        },
        "shaonv": {
            "id": "female-shaonv",
            "desc": "少女 - 清纯可爱",
            "gender": "女",
            "style": "清纯可爱",
        },
        "chengshu": {
            "id": "female-chengshu",
            "desc": "成熟 - 稳重成熟",
            "gender": "女",
            "style": "稳重成熟",
        },
        "tianmei": {
            "id": "female-tianmei",
            "desc": "甜美 - 甜美动人",
            "gender": "女",
            "style": "甜美动人",
        },
        "qingse": {
            "id": "male-qn-qingse",
            "desc": "青涩 - 青涩少年",
            "gender": "男",
            "style": "青涩少年",
        },
        "jingying": {
            "id": "male-qn-jingying",
            "desc": "精英 - 职场精英",
            "gender": "男",
            "style": "职场精英",
        },
        "badao": {
            "id": "male-qn-badao",
            "desc": "霸道 - 霸道总裁",
            "gender": "男",
            "style": "霸道总裁",
        },
        "daxuesheng": {
            "id": "male-qn-daxuesheng",
            "desc": "大学生 - 阳光学生",
            "gender": "男",
            "style": "阳光学生",
        },
    }

    def __init__(
        self,
        novel_dir: str,
        output_dir: str,
        voice: str = DEFAULT_VOICE,
        audio_format: str = DEFAULT_FORMAT,
        speed: float = DEFAULT_SPEED,
        resume: bool = True,
    ):
        self.novel_dir = Path(novel_dir)
        self.output_dir = Path(output_dir)
        self.voice = voice
        self.audio_format = audio_format
        self.speed = speed
        self.resume = resume

        # 确保输出目录存在
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 加载进度
        self.progress_file = self.output_dir / PROGRESS_FILE
        self.progress = self._load_progress()

        # 统计
        self.total_chars = 0
        self.total_duration_ms = 0
        self.total_api_calls = 0

    def _load_progress(self) -> Dict[str, Any]:
        """加载进度文件"""
        if self.progress_file.exists():
            try:
                with open(self.progress_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"completed": [], "failed": [], "stats": {}}

    def _save_progress(self):
        """保存进度文件"""
        with open(self.progress_file, "w", encoding="utf-8") as f:
            json.dump(self.progress, f, ensure_ascii=False, indent=2)

    def _log(self, message: str):
        """记录日志"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] {message}"
        print(log_line)
        with open(self.output_dir / LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_line + "\n")

    def _run_mmx_command(self, args: List[str]) -> tuple[bool, dict]:
        """
        运行 mmx-cli 命令
        返回: (success, result_dict)
        """
        cmd = ["node", MMX_CLI_PATH] + args
        self._log(f"执行命令: {' '.join(cmd)}")

        for attempt in range(MAX_RETRIES):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=300,
                )

                if result.returncode == 0:
                    # 解析输出
                    output = result.stdout.strip()
                    # 尝试从输出中提取 JSON
                    try:
                        # 找到 JSON 部分
                        json_start = output.find("{")
                        json_end = output.rfind("}") + 1
                        if json_start >= 0 and json_end > json_start:
                            json_str = output[json_start:json_end]
                            data = json.loads(json_str)
                            return True, data
                        return True, {"raw_output": output}
                    except json.JSONDecodeError:
                        return True, {"raw_output": output}
                else:
                    error_msg = result.stderr.strip() or result.stdout.strip()
                    self._log(f"命令失败 (尝试 {attempt + 1}/{MAX_RETRIES}): {error_msg}")
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(RETRY_DELAY)

            except subprocess.TimeoutExpired:
                self._log(f"命令超时 (尝试 {attempt + 1}/{MAX_RETRIES})")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)
            except Exception as e:
                self._log(f"命令异常 (尝试 {attempt + 1}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)

        return False, {"error": "Max retries exceeded"}

    def check_quota(self) -> Dict[str, Any]:
        """检查 speech-hd 配额"""
        success, data = self._run_mmx_command(["quota", "show", "--output", "json"])
        if not success:
            self._log("无法获取配额信息")
            return {}

        try:
            model_remains = data.get("model_remains", [])
            for item in model_remains:
                if item.get("model_name") == "speech-hd":
                    return {
                        "interval_total": item.get("current_interval_total_count", 0),
                        "interval_used": item.get("current_interval_usage_count", 0),
                        "interval_remains": item.get("current_interval_total_count", 0)
                        - item.get("current_interval_usage_count", 0),
                        "weekly_total": item.get("current_weekly_total_count", 0),
                        "weekly_used": item.get("current_weekly_usage_count", 0),
                        "weekly_remains": item.get("current_weekly_total_count", 0)
                        - item.get("current_weekly_usage_count", 0),
                        "remains_time_ms": item.get("remains_time", 0),
                    }
        except Exception as e:
            self._log(f"解析配额信息失败: {e}")

        return {}

    def wait_for_quota(self):
        """等待配额重置"""
        while True:
            quota = self.check_quota()
            if not quota:
                self._log("无法检查配额，等待 60 秒后重试...")
                time.sleep(60)
                continue

            interval_remains = quota.get("interval_remains", 0)
            weekly_remains = quota.get("weekly_remains", 0)

            self._log(
                f"配额状态 - 周期剩余: {interval_remains}, 周剩余: {weekly_remains}"
            )

            if interval_remains > 0 and weekly_remains > 0:
                return quota

            # 计算等待时间
            remains_time_ms = quota.get("remains_time_ms", 0)
            wait_seconds = max(remains_time_ms // 1000, 60)
            wait_minutes = wait_seconds // 60

            self._log(f"配额不足，等待 {wait_minutes} 分钟后重试...")
            time.sleep(min(wait_seconds, 300))  # 最多等5分钟再检查

    def synthesize_text(
        self, text: str, output_path: str, subtitles: bool = False
    ) -> tuple[bool, dict]:
        """
        合成单段文本为音频
        返回: (success, result_info)
        """
        # 构建命令
        args = [
            "speech",
            "synthesize",
            "--text",
            text,
            "--voice",
            self.voice,
            "--format",
            self.audio_format,
            "--speed",
            str(self.speed),
            "--out",
            output_path,
        ]

        if subtitles:
            args.append("--subtitles")

        success, data = self._run_mmx_command(args)

        if success and "saved" in data:
            return True, {
                "saved": data.get("saved"),
                "duration_ms": data.get("duration_ms", 0),
                "size_bytes": data.get("size_bytes", 0),
                "sample_rate": data.get("sample_rate", 32000),
            }

        return False, data

    def split_text(self, text: str, max_chars: int = MAX_CHARS_PER_CALL) -> List[str]:
        """
        将长文本按句子分割成多个片段
        确保每个片段不超过 max_chars
        """
        # 按句号、问号、感叹号分割
        import re

        sentences = re.split(r"([。！？\.\!\?])", text)
        # 重新组合句子和标点
        chunks = []
        current = ""

        i = 0
        while i < len(sentences):
            sentence = sentences[i]
            if i + 1 < len(sentences) and sentences[i + 1] in "。！？.!?":
                sentence += sentences[i + 1]
                i += 2
            else:
                i += 1

            if len(current) + len(sentence) <= max_chars:
                current += sentence
            else:
                if current:
                    chunks.append(current)
                current = sentence

        if current:
            chunks.append(current)

        return chunks

    def process_chapter(self, chapter_file: Path) -> bool:
        """
        处理单个章节文件
        返回: 是否成功
        """
        chapter_name = chapter_file.stem
        output_file = self.output_dir / f"{chapter_name}.{self.audio_format}"

        # 检查是否已完成
        if self.resume and chapter_name in self.progress.get("completed", []):
            self._log(f"跳过已完成: {chapter_name}")
            return True

        # 读取章节内容
        try:
            with open(chapter_file, "r", encoding="utf-8") as f:
                text = f.read().strip()
        except Exception as e:
            self._log(f"读取文件失败 {chapter_file}: {e}")
            self.progress.setdefault("failed", []).append(
                {"chapter": chapter_name, "error": f"读取失败: {e}"}
            )
            self._save_progress()
            return False

        if not text:
            self._log(f"空文件，跳过: {chapter_name}")
            return True

        self._log(f"处理章节: {chapter_name} ({len(text)} 字符)")

        # 检查配额
        quota = self.wait_for_quota()
        interval_remains = quota.get("interval_remains", 0)

        # 分割文本
        chunks = self.split_text(text)
        self._log(f"分割为 {len(chunks)} 个片段")

        if len(chunks) > interval_remains:
            self._log(
                f"警告: 需要 {len(chunks)} 次调用，但周期配额只剩 {interval_remains}"
            )

        # 合成每个片段
        chunk_files = []
        total_duration = 0

        for i, chunk in enumerate(chunks):
            self._log(f"  合成片段 {i + 1}/{len(chunks)} ({len(chunk)} 字符)")

            # 检查配额
            if i > 0 and i % 5 == 0:
                quota = self.check_quota()
                if quota.get("interval_remains", 0) < 2:
                    self._log("配额即将耗尽，等待重置...")
                    self.wait_for_quota()

            chunk_output = str(self.output_dir / f"{chapter_name}_chunk_{i:03d}.mp3")
            success, info = self.synthesize_text(chunk, chunk_output)

            if success:
                chunk_files.append(chunk_output)
                total_duration += info.get("duration_ms", 0)
                self.total_api_calls += 1
                self._log(
                    f"    成功: {info.get('duration_ms', 0)}ms, {info.get('size_bytes', 0)} bytes"
                )
            else:
                self._log(f"    失败: {info}")
                # 清理已生成的片段
                for cf in chunk_files:
                    try:
                        os.remove(cf)
                    except Exception:
                        pass
                self.progress.setdefault("failed", []).append(
                    {"chapter": chapter_name, "error": f"片段 {i} 合成失败"}
                )
                self._save_progress()
                return False

            # 避免请求过快
            time.sleep(1)

        # 合并片段 (如果需要)
        if len(chunk_files) == 1:
            # 只有一个片段，直接重命名
            os.rename(chunk_files[0], str(output_file))
        elif len(chunk_files) > 1:
            # 合并多个片段
            self._merge_audio_files(chunk_files, str(output_file))
            # 删除临时片段
            for cf in chunk_files:
                try:
                    os.remove(cf)
                except Exception:
                    pass

        self._log(
            f"章节完成: {chapter_name} -> {output_file} ({total_duration / 1000:.1f} 秒)"
        )

        # 更新进度
        self.progress.setdefault("completed", []).append(chapter_name)
        self.progress["stats"][chapter_name] = {
            "chars": len(text),
            "chunks": len(chunks),
            "duration_ms": total_duration,
            "output": str(output_file),
        }
        self._save_progress()

        self.total_chars += len(text)
        self.total_duration_ms += total_duration

        return True

    def _merge_audio_files(self, input_files: List[str], output_file: str):
        """合并多个 MP3 文件"""
        # 尝试使用 ffmpeg
        try:
            # 创建文件列表
            list_file = self.output_dir / "merge_list.txt"
            with open(list_file, "w", encoding="utf-8") as f:
                for inp in input_files:
                    # Windows 路径需要转义
                    f.write(f"file '{inp.replace('\\', '/')}'\n")

            cmd = [
                "ffmpeg",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-acodec",
                "copy",
                output_file,
                "-y",
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8"
            )

            if result.returncode != 0:
                self._log(f"ffmpeg 合并失败: {result.stderr}")
                # 回退：直接复制第一个文件
                import shutil
                shutil.copy(input_files[0], output_file)

            try:
                os.remove(list_file)
            except Exception:
                pass

        except FileNotFoundError:
            self._log("ffmpeg 未找到，使用第一个片段作为输出")
            import shutil
            shutil.copy(input_files[0], output_file)

    def process_all_chapters(self):
        """处理所有章节"""
        # 查找所有 txt 文件
        chapter_files = sorted(self.novel_dir.glob("*.txt"))

        if not chapter_files:
            self._log(f"在 {self.novel_dir} 中未找到 txt 文件")
            return

        self._log(f"找到 {len(chapter_files)} 个章节文件")
        self._log(f"使用语音: {self.voice}")
        self._log(f"音频格式: {self.audio_format}")
        self._log(f"语速: {self.speed}")

        # 检查初始配额
        quota = self.check_quota()
        if quota:
            self._log(
                f"初始配额 - 周期: {quota.get('interval_remains', 0)}/{quota.get('interval_total', 0)}, "
                f"周: {quota.get('weekly_remains', 0)}/{quota.get('weekly_total', 0)}"
            )

        # 处理每个章节
        success_count = 0
        fail_count = 0

        for i, chapter_file in enumerate(chapter_files):
            self._log(
                f"\n========== 进度: {i + 1}/{len(chapter_files)} =========="
            )
            if self.process_chapter(chapter_file):
                success_count += 1
            else:
                fail_count += 1

        # 最终统计
        self._log("\n========== 生成完成 ==========")
        self._log(f"成功: {success_count} 章")
        self._log(f"失败: {fail_count} 章")
        self._log(f"总字符数: {self.total_chars}")
        self._log(f"总音频时长: {self.total_duration_ms / 1000 / 60:.1f} 分钟")
        self._log(f"总 API 调用: {self.total_api_calls} 次")
        self._log(f"输出目录: {self.output_dir}")

    @classmethod
    def list_voices(cls):
        """列出所有推荐中文语音"""
        print("=" * 60)
        print("MiniMax CLI 推荐中文语音列表")
        print("=" * 60)
        for key, info in cls.CHINESE_VOICES.items():
            print(f"\n  [{key}]")
            print(f"    ID: {info['id']}")
            print(f"    描述: {info['desc']}")
            print(f"    性别: {info['gender']}")
            print(f"    风格: {info['style']}")
        print("\n" + "=" * 60)
        print(f"使用方式: python speech_generator.py --voice <ID>")
        print("=" * 60)

    @classmethod
    def test_voice(cls, voice_id: str, text: Optional[str] = None):
        """测试指定语音"""
        test_text = text or (
            "这是一个语音合成测试。修仙世界，浩瀚无垠。"
            "少年林凡出身贫寒，却意外获得上古传承，从此踏上逆天改命之路。"
            "前方有无数强敌，但他心中只有一个信念：我命由我不由天！"
        )

        output_dir = Path("novels/audio")
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / f"test_{voice_id.replace(' ', '_').replace('(', '').replace(')', '')}.mp3"

        print(f"测试语音: {voice_id}")
        print(f"测试文本: {test_text[:50]}...")
        print(f"输出文件: {output_file}")

        gen = cls("novels", "novels/audio", voice=voice_id)
        success, info = gen.synthesize_text(test_text, str(output_file))

        if success:
            print(f"\n合成成功!")
            print(f"  时长: {info.get('duration_ms', 0)} ms")
            print(f"  大小: {info.get('size_bytes', 0)} bytes")
            print(f"  采样率: {info.get('sample_rate', 0)} Hz")
        else:
            print(f"\n合成失败: {info}")


def main():
    parser = argparse.ArgumentParser(
        description="MiniMax CLI 语音合成批量生成器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 列出所有推荐中文语音
  python speech_generator.py --list-voices

  # 测试特定语音
  python speech_generator.py --test-voice "Chinese (Mandarin)_News_Anchor"

  # 生成所有章节 (默认使用新闻主播语音)
  python speech_generator.py --novel-dir novels/chapters --output-dir novels/audio

  # 使用御姐语音生成
  python speech_generator.py --novel-dir novels/chapters --voice "female-yujie"

  # 使用1.2倍速
  python speech_generator.py --novel-dir novels/chapters --speed 1.2

  # 不启用断点续传 (重新生成所有)
  python speech_generator.py --novel-dir novels/chapters --no-resume
        """,
    )

    parser.add_argument(
        "--novel-dir",
        default="novels/chapters",
        help="小说章节目录 (默认: novels/chapters)",
    )
    parser.add_argument(
        "--output-dir",
        default="novels/audio",
        help="音频输出目录 (默认: novels/audio)",
    )
    parser.add_argument(
        "--voice",
        default=DEFAULT_VOICE,
        help=f"语音ID (默认: {DEFAULT_VOICE})",
    )
    parser.add_argument(
        "--format",
        default=DEFAULT_FORMAT,
        choices=["mp3", "wav", "pcm", "flac", "opus"],
        help=f"音频格式 (默认: {DEFAULT_FORMAT})",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=DEFAULT_SPEED,
        help=f"语速倍数 (默认: {DEFAULT_SPEED})",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="不启用断点续传 (默认启用)",
    )
    parser.add_argument(
        "--list-voices",
        action="store_true",
        help="列出所有推荐中文语音",
    )
    parser.add_argument(
        "--test-voice",
        metavar="VOICE_ID",
        help="测试指定语音",
    )
    parser.add_argument(
        "--test-text",
        help="测试语音时使用的文本",
    )
    parser.add_argument(
        "--quota",
        action="store_true",
        help="仅查看配额信息",
    )

    args = parser.parse_args()

    if args.list_voices:
        SpeechGenerator.list_voices()
        return

    if args.test_voice:
        SpeechGenerator.test_voice(args.test_voice, args.test_text)
        return

    # 创建生成器实例
    generator = SpeechGenerator(
        novel_dir=args.novel_dir,
        output_dir=args.output_dir,
        voice=args.voice,
        audio_format=args.format,
        speed=args.speed,
        resume=not args.no_resume,
    )

    if args.quota:
        quota = generator.check_quota()
        print(json.dumps(quota, ensure_ascii=False, indent=2))
        return

    # 开始处理
    generator.process_all_chapters()


if __name__ == "__main__":
    main()
