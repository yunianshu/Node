#!/usr/bin/env python3
"""MiniMax 平台媒体 CLI 路径解析（仅供封面/视频/音乐生成使用）。

文本 LLM 链路已迁移至 core.llm_client；本模块只服务 mmx 媒体子命令。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def _npm_global_candidates() -> list[Path]:
    candidates: list[Path] = []
    npm = shutil.which("npm")
    if npm:
        try:
            result = subprocess.run(
                [npm, "root", "-g"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
            root = result.stdout.strip()
            if result.returncode == 0 and root:
                candidates.append(Path(root) / "mmx-cli" / "dist" / "mmx.mjs")
        except Exception:
            pass
    for env_name in ("APPDATA", "LOCALAPPDATA"):
        base = os.getenv(env_name)
        if base:
            candidates.append(Path(base) / "npm" / "node_modules" / "mmx-cli" / "dist" / "mmx.mjs")
    return candidates


def resolve_media_mmx_path(config: dict) -> str:
    """优先级: 环境变量 > config.media.mmx_path > npm 全局安装。"""
    explicit = os.getenv("NOVEL_MMX_PATH", "").strip()
    if not explicit:
        media_cfg = config.get("media", {}) if isinstance(config.get("media"), dict) else {}
        explicit = str(media_cfg.get("mmx_path", "") or "").strip()
    if explicit:
        expanded = os.path.expandvars(os.path.expanduser(explicit))
        if Path(expanded).exists() or shutil.which(expanded):
            return expanded
        return expanded
    for candidate in _npm_global_candidates():
        if candidate.exists():
            return str(candidate.resolve())
    found = shutil.which("mmx")
    return found or "mmx"


def mmx_base_cmd(mmx_path: str) -> list[str]:
    text = str(mmx_path or "").strip()
    suffix = Path(text).suffix.lower()
    if suffix in {".mjs", ".js", ".cjs"} or Path(text).exists():
        return ["node", text]
    found = shutil.which(text)
    if found:
        return [found]
    return [text]