#!/usr/bin/env python3
"""
为小说项目生成封面和预告片视频
直接调用 mmx CLI，不依赖框架路径
"""

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))


import argparse
import json
import os
import subprocess
from pathlib import Path

PROJECT_DIR = None
WORLD_FILE = None
IMAGES_DIR = None
VIDEOS_DIR = None
MMX_PATH = "C:/Users/Administrator/AppData/Roaming/npm/node_modules/mmx-cli/dist/mmx.mjs"


def init_project(project_dir: str | Path) -> None:
    global PROJECT_DIR, WORLD_FILE, IMAGES_DIR, VIDEOS_DIR
    PROJECT_DIR = Path(project_dir).resolve()
    WORLD_FILE = PROJECT_DIR / "world.json"
    IMAGES_DIR = PROJECT_DIR / "images"
    VIDEOS_DIR = PROJECT_DIR / "videos"
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str):
    print(f"[Media] {msg}")


def load_world() -> dict:
    with open(WORLD_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def generate_cover(world: dict) -> bool:
    title = world.get("title", "长生武道")
    prompt = f"""东方玄幻仙侠小说封面，书名《{title}》，
画面中央是一位白衣长发的武道强者，背后有虚空万界的景象，
星空、裂缝、不同世界的碎片漂浮在虚空中，
整体色调以深蓝和金色为主，大气磅礴，史诗感"""

    output = IMAGES_DIR / "cover.jpg"
    cmd = [
        "node", MMX_PATH, "image", "generate",
        "--prompt", prompt,
        "--output", str(output),
        "--quiet"
    ]
    log(f"生成封面: {output}")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        log(f"封面生成失败: {result.stderr}")
        return False
    log("封面生成完成")
    return True


def generate_trailer(world: dict) -> bool:
    title = world.get("title", "长生武道")
    prompt = f"""制作一段东方玄幻小说的预告片视频。
画面展现主角苏长空从平凡少年成长为虚空万界最强者的历程，
包含修炼、战斗、探索异世界、与同伴并肩作战等场景，
节奏紧凑，充满史诗感和热血感，时长约30秒"""

    output = VIDEOS_DIR / "trailer.mp4"
    cmd = [
        "node", MMX_PATH, "video", "generate",
        "--prompt", prompt,
        "--output", str(output),
        "--quiet"
    ]
    log(f"生成预告片: {output}")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        log(f"预告片生成失败: {result.stderr}")
        return False
    log("预告片生成完成")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-p", type=str,
                        default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录")
    parser.add_argument("--cover-only", action="store_true", help="只生成封面")
    parser.add_argument("--trailer-only", action="store_true", help="只生成预告片")
    args = parser.parse_args()

    if not args.project:
        print("错误: 必须指定 --project 或设置 NOVEL_PROJECT_DIR 环境变量")
        sys.exit(1)

    init_project(args.project)

    print("=" * 60)
    print("生成封面和预告片")
    print(f"项目: {PROJECT_DIR}")
    print("=" * 60)

    if not WORLD_FILE.exists():
        log(f"错误: {WORLD_FILE} 不存在，请先运行 planner")
        return

    world = load_world()

    if not args.trailer_only:
        generate_cover(world)

    if not args.cover_only:
        generate_trailer(world)

    log("全部完成")


if __name__ == "__main__":
    main()
