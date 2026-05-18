#!/usr/bin/env python3
"""
统一日志 - 所有 Agent 共享的日志实现
支持多 Logger 实例（按模块名区分日志文件）
"""
import sys
import time
from pathlib import Path
from threading import Lock


class NovelLogger:
    """小说生成专用 Logger，每个模块独立日志文件"""

    def __init__(self, name: str, log_dir: Path):
        self.name = name
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / f"{name.lower()}.log"
        self.lock = Lock()

    def log(self, msg: str):
        """记录日志到文件并打印到 stdout"""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] [{self.name}] {msg}"
        print(line)
        with self.lock:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()

    def info(self, msg: str):
        self.log(f"[INFO] {msg}")

    def warning(self, msg: str):
        self.log(f"[WARNING] {msg}")

    def error(self, msg: str):
        self.log(f"[ERROR] {msg}")

    def debug(self, msg: str):
        self.log(f"[DEBUG] {msg}")


# 全局 Logger 缓存（同一名称共享同一实例）
_loggers: dict[str, NovelLogger] = {}
_logger_lock = Lock()


def get_logger(name: str, log_dir: Path) -> NovelLogger:
    """获取（或创建）指定名称的 Logger"""
    key = f"{name}@{log_dir}"
    with _logger_lock:
        if key not in _loggers:
            _loggers[key] = NovelLogger(name, log_dir)
        return _loggers[key]
