#!/usr/bin/env python3
"""
为 novels5 生成封面和预告片视频
直接调用 mmx CLI，不依赖框架路径
"""
import json
import subprocess
from pathlib import Path

PROJECT_DIR = Path("D:/AiProject/Node/novels5")
WORLD_FILE = PROJECT_DIR / "world.json"
IMAGES_DIR = PROJECT_DIR / "images"
VIDEOS_DIR = PROJECT_DIR / "videos"
MMX_PATH = "C:/Users/Administrator/AppData/Roaming/npm/node_modules/mmx-cli/dist/mmx.mjs"

IMAGES_DIR.mkdir(parents=True, exist_ok=True)
VIDEOS_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str):
    print(f"[Media] {msg}")


def load_world() -> dict:
    with open(WORLD_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def generate_cover(world: dict) -> bool:
    """生成小说封面"""
    cover_path = IMAGES_DIR / "cover.jpg"
    if cover_path.exists():
        log(f"封面已存在: {cover_path}")
        return True

    title = world.get("title", "长生武道")
    desc = world.get("world_description", "")[:300]
    style = "史诗玄幻，东方仙侠"

    prompt = (
        f"小说封面插画，{style}风格，书名《{title}》。"
        f"场景：{desc}。"
        f"宏大世界观，星空虚空，武道强者，"
        f"cinematic lighting, highly detailed, epic composition, "
        f"专业书籍封面设计，无文字，画面震撼，4K画质"
    )

    cmd = [
        "node", MMX_PATH, "image", "generate",
        "--model", "image-01",
        "--prompt", prompt,
        "--aspect-ratio", "3:4",
        "--out", str(cover_path),
        "--quiet",
    ]

    log(f"生成封面: {title}")
    log(f"Prompt: {prompt[:100]}...")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=120)
        if result.returncode != 0:
            err = result.stderr[:300] if result.stderr else "unknown"
            log(f"封面生成失败: {err}")
            return False
        log(f"封面生成成功: {cover_path}")
        return True
    except subprocess.TimeoutExpired:
        log("封面生成超时")
        return False
    except Exception as e:
        log(f"封面生成异常: {e}")
        return False


def generate_trailer(world: dict) -> bool:
    """生成小说预告片"""
    trailer_path = VIDEOS_DIR / "trailer.mp4"
    if trailer_path.exists():
        log(f"预告片已存在: {trailer_path}")
        return True

    title = world.get("title", "长生武道")
    desc = world.get("world_description", "")[:200]

    prompt = (
        f"史诗级玄幻小说《{title}》预告片，"
        f"虚空万界，武道长生，"
        f"主角在无尽虚空中征战四方，"
        f"宏大战斗场面，星空破碎，法则碰撞，"
        f" cinematic, highly detailed, dramatic lighting, epic battle"
    )

    cmd = [
        "node", MMX_PATH, "video", "generate",
        "--model", "MiniMax-Hailuo-2.3",
        "--prompt", prompt,
        "--duration", "6s",
        "--resolution", "768p",
        "--out", str(trailer_path),
        "--quiet",
    ]

    log(f"生成预告片: {title}")
    log(f"Prompt: {prompt[:100]}...")
    log("注意: 视频生成需要较长时间，请耐心等待...")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=300)
        if result.returncode != 0:
            err = result.stderr[:300] if result.stderr else "unknown"
            log(f"预告片生成失败: {err}")
            return False
        log(f"预告片生成成功: {trailer_path}")
        return True
    except subprocess.TimeoutExpired:
        log("预告片生成超时")
        return False
    except Exception as e:
        log(f"预告片生成异常: {e}")
        return False


def main():
    print("=" * 60)
    print("  novels5 多媒体生成 - 封面 + 预告片")
    print("=" * 60)

    if not WORLD_FILE.exists():
        log(f"世界观文件不存在: {WORLD_FILE}")
        return

    world = load_world()
    title = world.get("title", "未知")
    log(f"书名: {title}")

    # 生成封面
    log("-" * 40)
    cover_ok = generate_cover(world)

    # 生成预告片
    log("-" * 40)
    trailer_ok = generate_trailer(world)

    log("-" * 40)
    log("任务完成:")
    log(f"  封面: {'成功' if cover_ok else '失败'} -> {IMAGES_DIR / 'cover.jpg'}")
    log(f"  预告片: {'成功' if trailer_ok else '失败'} -> {VIDEOS_DIR / 'trailer.mp4'}")


if __name__ == "__main__":
    main()
