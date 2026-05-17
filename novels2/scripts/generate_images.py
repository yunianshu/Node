#!/usr/bin/env python3
"""
章节图片生成器 - 为每章生成封面图
利用文本配额等待期间，调用 image-01 生成图片
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

NOVELS_DIR = Path("D:/AiProject/Node/novels2")
CHAPTERS_DIR = NOVELS_DIR / "chapters" / "draft"
IMAGES_DIR = NOVELS_DIR / "images"
OUTLINE_FILE = NOVELS_DIR / "outline.json"
LOG_FILE = NOVELS_DIR / "logs" / "image_generator.log"

MMX_CLI_PATH = "C:/Users/Administrator/AppData/Roaming/npm/node_modules/mmx-cli/dist/mmx.mjs"

def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_chapters_without_images():
    """获取已生成初稿但还没有图片的章节列表"""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    # 获取已有图片的章节
    existing_images = set()
    for img in IMAGES_DIR.glob("chapter_*.jpg"):
        try:
            num = int(img.stem.split("_")[1])
            existing_images.add(num)
        except (ValueError, IndexError):
            pass

    # 获取已有初稿的章节
    draft_chapters = []
    if CHAPTERS_DIR.exists():
        for f in CHAPTERS_DIR.glob("chapter_*.txt"):
            try:
                num = int(f.stem.split("_")[1])
                if f.stat().st_size > 1000 and num not in existing_images:
                    draft_chapters.append(num)
            except (ValueError, IndexError):
                pass

    return sorted(draft_chapters)


def load_outline():
    """加载大纲，获取每章的标题和摘要"""
    if not OUTLINE_FILE.exists():
        return {}
    try:
        with open(OUTLINE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        outline_map = {}
        for ch in data.get("chapters", []):
            num = ch.get("chapter_number", 0)
            if num:
                outline_map[num] = ch
        return outline_map
    except Exception:
        return {}


def generate_image_prompt(chapter_number: int, outline_info: dict, content_sample: str) -> str:
    """根据章节内容生成图片提示词"""
    title = outline_info.get("title", "")
    summary = outline_info.get("summary", "")

    # 构建恐怖灵异风格的提示词
    base_prompt = f"""恐怖灵异小说封面插画，黑暗压抑的氛围，{title}。
场景描述：{summary[:200] if summary else content_sample[:200]}。
风格要求：中式恐怖，阴森诡异，暗色调， cinematic lighting，highly detailed，dark atmosphere，horror art style"""

    return base_prompt


def generate_image(chapter_number: int, prompt: str) -> bool:
    """调用 mmx 生成单张图片"""
    output_path = IMAGES_DIR / f"chapter_{chapter_number:04d}.jpg"

    cmd = [
        "node", MMX_CLI_PATH, "image", "generate",
        "--prompt", prompt,
        "--aspect-ratio", "16:9",
        "--out", str(output_path),
        "--quiet"
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60)
        if result.returncode == 0:
            if output_path.exists() and output_path.stat().st_size > 1000:
                log(f"[Image] 第{chapter_number}章图片生成成功 -> {output_path}")
                return True
            else:
                log(f"[Image] 第{chapter_number}章图片文件异常")
                return False
        else:
            err = result.stderr.strip() if result.stderr else "unknown error"
            log(f"[Image] 第{chapter_number}章生成失败: {err[:200]}")
            return False
    except subprocess.TimeoutExpired:
        log(f"[Image] 第{chapter_number}章生成超时")
        return False
    except Exception as e:
        log(f"[Image] 第{chapter_number}章异常: {e}")
        return False


def check_image_quota() -> int:
    """检查 image-01 配额剩余量"""
    try:
        result = subprocess.run(
            ["node", MMX_CLI_PATH, "quota", "show", "--quiet", "--output", "json"],
            capture_output=True, text=True, encoding="utf-8", timeout=10
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            for item in data.get("model_remains", []):
                if item.get("model_name") == "image-01":
                    remaining = item.get("current_interval_total_count", 0) - item.get("current_interval_usage_count", 0)
                    return remaining
    except Exception:
        pass
    return 0


def main():
    log("=" * 60)
    log("[ImageGenerator] 章节图片生成器启动")
    log("=" * 60)

    # 检查配额
    quota = check_image_quota()
    log(f"[ImageGenerator] image-01 配额剩余: {quota}")
    if quota <= 0:
        log("[ImageGenerator] 图片配额不足，退出")
        return

    # 获取待生成列表
    pending = get_chapters_without_images()
    log(f"[ImageGenerator] 待生成图片: {len(pending)} 章")

    if not pending:
        log("[ImageGenerator] 所有章节已有图片，退出")
        return

    # 加载大纲
    outline = load_outline()

    # 限制本次生成数量（不超过配额）
    max_generate = min(len(pending), quota, 50)  # 最多50张/次，避免阻塞太久
    to_generate = pending[:max_generate]

    success_count = 0
    fail_count = 0

    for ch_num in to_generate:
        # 读取章节内容样本
        chapter_file = CHAPTERS_DIR / f"chapter_{ch_num:04d}.txt"
        content_sample = ""
        if chapter_file.exists():
            try:
                content_sample = chapter_file.read_text(encoding="utf-8")[:500]
            except Exception:
                pass

        # 获取大纲信息
        outline_info = outline.get(ch_num, {})

        # 生成提示词
        prompt = generate_image_prompt(ch_num, outline_info, content_sample)

        # 生成图片
        if generate_image(ch_num, prompt):
            success_count += 1
        else:
            fail_count += 1

        # 每5张暂停1秒，避免限流
        if (success_count + fail_count) % 5 == 0:
            time.sleep(1)

    log(f"[ImageGenerator] 完成: 成功 {success_count}, 失败 {fail_count}")
    log("=" * 60)


if __name__ == "__main__":
    main()
