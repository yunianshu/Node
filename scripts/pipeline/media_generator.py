#!/usr/bin/env python3
"""根据 world.json 中的媒体提示词生成封面、世界观视频和主题歌。"""
from __future__ import annotations

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

import argparse
import json
import os
import subprocess
import time

from core.mmx_cli import mmx_base_cmd, resolve_media_mmx_path
from core.novel_config import configure_stdio, load_config, resolve_project_dir

configure_stdio()

NOVELS_DIR: Path | None = None
CONFIG: dict | None = None
LOG_FILE: Path | None = None


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, CONFIG, LOG_FILE
    NOVELS_DIR = Path(project_dir).resolve()
    CONFIG = load_config(NOVELS_DIR)
    LOG_FILE = NOVELS_DIR / "logs" / "media_generator.log"


def log(msg: str) -> None:
    assert LOG_FILE is not None
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _load_world() -> dict:
    assert NOVELS_DIR is not None
    world_file = NOVELS_DIR / "world.json"
    if not world_file.exists():
        return {}
    try:
        return json.loads(world_file.read_text(encoding="utf-8"))
    except Exception as exc:
        return {}


def _run_mmx(args: list[str], timeout: int) -> bool:
    assert CONFIG is not None
    cmd = [*mmx_base_cmd(resolve_media_mmx_path(CONFIG)), *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        log(f"[Media] 命令超时: {' '.join(cmd[:4])}... {exc}")
        return False
    except Exception as exc:
        log(f"[Media] 命令异常: {exc}")
        return False

    if result.returncode == 0:
        output = (result.stdout or "").strip()
        if output:
            log(f"[Media] 输出: {output[:500]}")
        return True
    err = (result.stderr or result.stdout or "").strip()
    log(f"[Media] 命令失败 rc={result.returncode}: {err[:1000]}")
    return False


def _has_cover_image() -> bool:
    assert NOVELS_DIR is not None
    image_exts = {".png", ".jpg", ".jpeg", ".webp"}
    return any(
        path.is_file() and path.suffix.lower() in image_exts
        for path in (NOVELS_DIR / "media" / "images").glob("cover*")
    )


def _enrich_media_prompt(prompt: str, asset_type: str) -> str:
    """Append non-generic quality constraints without mutating world.json."""
    prompt = str(prompt or "").strip()
    if not prompt:
        return ""
    common = (
        "质量要求：必须呈现本书专属人物、核心矛盾、具体地点或物件；"
        "避免通用网文模板、纯氛围空镜、抽象能量、廉价广告感、文字水印和无意义装饰。"
    )
    if asset_type == "cover":
        detail = (
            "封面必须有可识别的主视觉焦点和前中后景层次；"
            "背景至少包含一个能暗示主线秘密、势力压迫或人物代价的细节。"
        )
    elif asset_type == "video":
        detail = (
            "视频镜头必须形成小叙事：生活压力细节→冲突升级→主角选择或反转；"
            "不要只生成风景、云海、火焰、城市空镜或角色站立摆拍。"
        )
    else:
        detail = (
            "音乐要有清晰情绪弧线和副歌记忆点，体现困境、牵挂、选择代价与阶段性爆发；"
            "避免通用史诗配乐描述。"
        )
    return f"{prompt}\n\n{common}{detail}"


def generate_cover(media_prompts: dict) -> bool:
    assert NOVELS_DIR is not None and CONFIG is not None
    if _has_cover_image():
        log("[Media] cover 已存在，跳过")
        return True
    prompt = _enrich_media_prompt(media_prompts.get("cover_prompt", ""), "cover")
    if not prompt:
        log("[Media] cover_prompt 为空，跳过")
        return False
    out_dir = NOVELS_DIR / "media" / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    media_cfg = CONFIG.get("media", {})
    args = [
        "image",
        "generate",
        "--prompt",
        prompt,
        "--aspect-ratio",
        str(media_cfg.get("cover_aspect_ratio", "3:4")),
        "--n",
        "1",
        "--out-dir",
        str(out_dir),
        "--out-prefix",
        "cover",
        "--quiet",
    ]
    return _run_mmx(args, timeout=600)


def generate_video(media_prompts: dict) -> bool:
    assert NOVELS_DIR is not None and CONFIG is not None
    target = NOVELS_DIR / "media" / "videos" / "world_video.mp4"
    if target.exists():
        log("[Media] world_video.mp4 已存在，跳过")
        return True
    prompt = _enrich_media_prompt(media_prompts.get("video_prompt", ""), "video")
    if not prompt:
        log("[Media] video_prompt 为空，跳过")
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    media_cfg = CONFIG.get("media", {})
    args = [
        "video",
        "generate",
        "--prompt",
        prompt,
        "--model",
        str(media_cfg.get("video_model", "MiniMax-Hailuo-2.3")),
        "--download",
        str(target),
        "--quiet",
    ]
    return _run_mmx(args, timeout=2400)


def generate_song(media_prompts: dict) -> bool:
    assert NOVELS_DIR is not None and CONFIG is not None
    media_cfg = CONFIG.get("media", {})
    fmt = str(media_cfg.get("song_format", "mp3")).lstrip(".") or "mp3"
    target = NOVELS_DIR / "media" / "music" / f"theme_song.{fmt}"
    if target.exists():
        log(f"[Media] {target.name} 已存在，跳过")
        return True
    prompt = _enrich_media_prompt(media_prompts.get("song_prompt", ""), "song")
    lyrics = str(media_prompts.get("song_lyrics", "")).strip()
    if not prompt:
        log("[Media] song_prompt 为空，跳过")
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "music",
        "generate",
        "--prompt",
        prompt,
        "--out",
        str(target),
        "--format",
        fmt,
        "--quiet",
    ]
    if lyrics:
        args.extend(["--lyrics", lyrics])
    else:
        args.append("--instrumental")
    return _run_mmx(args, timeout=1800)


def generate_media() -> bool:
    assert CONFIG is not None
    if not CONFIG.get("media", {}).get("enabled", True):
        log("[Media] media.enabled=false，跳过媒体生成")
        return True

    world = _load_world()
    media_prompts = world.get("media_prompts", {})
    if not isinstance(media_prompts, dict):
        log("[Media] world.json 缺少 media_prompts，请先运行 planner.py")
        return False

    ok_cover = generate_cover(media_prompts)
    ok_video = generate_video(media_prompts)
    ok_song = generate_song(media_prompts)
    return ok_cover and ok_video and ok_song


def main() -> None:
    parser = argparse.ArgumentParser(description="生成小说媒体资产")
    parser.add_argument(
        "--project",
        "-p",
        type=str,
        default="",
        help="小说项目目录（默认从环境变量 NOVEL_PROJECT_DIR 读取）",
    )
    args = parser.parse_args()
    try:
        project = resolve_project_dir(args.project)
    except ValueError as exc:
        print(f"错误: {exc}")
        sys.exit(1)
    init_project(project)
    ok = generate_media()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
