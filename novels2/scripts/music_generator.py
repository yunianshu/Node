#!/usr/bin/env python3
"""
Music Generator Agent - 为每章生成歌词和音乐
流程: 读取大纲 → 生成歌词(text chat) → 生成音乐(music-2.6)
并行: 5个worker
"""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

NOVELS_DIR = Path("D:/AiProject/Node/novels2")
OUTLINE_FILE = NOVELS_DIR / "outline.json"
LYRICS_DIR = NOVELS_DIR / "lyrics"
MUSIC_DIR = NOVELS_DIR / "music"
LOG_FILE = NOVELS_DIR / "logs" / "music_generator.log"
PROGRESS_FILE = NOVELS_DIR / "logs" / "music_progress.json"

MMX_CLI_PATH = "C:/Users/Administrator/AppData/Roaming/npm/node_modules/mmx-cli/dist/mmx.mjs"


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except:
            pass
    return {"completed": [], "failed": [], "last_chapter": 0}


def save_progress(progress: dict):
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def call_mmx_text(system_prompt: str, user_prompt: str, max_tokens: int = 2048) -> str:
    """调用 mmx text chat 生成歌词"""
    cmd = [
        "node", MMX_CLI_PATH, "text", "chat",
        "--model", "MiniMax-M2.7-highspeed",
        "--system", system_prompt,
        "--message", user_prompt,
        "--max-tokens", str(max_tokens),
        "--temperature", "0.7",
        "--stream=false",
        "--quiet"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60)
        if result.returncode != 0:
            log(f"[Music] text chat failed: {result.stderr[:200]}")
            return ""
        raw = result.stdout.strip()
        try:
            data = json.loads(raw)
            return data.get("content", raw)
        except json.JSONDecodeError:
            if "Response:" in raw:
                json_part = raw.split("Response:")[-1].strip()
                try:
                    data = json.loads(json_part)
                    return data.get("content", raw)
                except json.JSONDecodeError:
                    return json_part
            return raw
    except Exception as e:
        log(f"[Music] text chat exception: {e}")
        return ""


def call_mmx_music(prompt: str, lyrics: str, out_path: str) -> bool:
    """调用 mmx music generate 生成音乐"""
    cmd = [
        "node", MMX_CLI_PATH, "music", "generate",
        "--model", "music-2.6",
        "--prompt", prompt,
        "--lyrics", lyrics,
        "--vocals", "male, deep, emotional",
        "--genre", "dark folk",
        "--mood", "mysterious, tense",
        "--instruments", "acoustic guitar, cello, piano",
        "--tempo", "slow",
        "--out", out_path,
        "--quiet"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=120)
        if result.returncode != 0:
            err = result.stderr.strip() if result.stderr else ""
            # 配额不足
            if "quota" in err.lower() or "usage limit" in err.lower():
                log(f"[Music] 配额不足: {err[:200]}")
                return False
            log(f"[Music] music generate failed: {err[:200]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        log(f"[Music] music generate timeout")
        return False
    except Exception as e:
        log(f"[Music] music generate exception: {e}")
        return False


def generate_lyrics(chapter_number: int, title: str, summary: str) -> str:
    """为单章生成歌词"""
    lyrics_file = LYRICS_DIR / f"chapter_{chapter_number:04d}.txt"
    if lyrics_file.exists():
        return lyrics_file.read_text(encoding="utf-8").strip()

    system = "你是一位擅长为恐怖灵异小说创作主题曲歌词的词作家。你的歌词要贴合章节内容，营造悬疑、压抑或悲壮的氛围。每首歌包含主歌和副歌，总长度控制在150-250字。"

    prompt = f"""请为小说《复苏之夜》第{chapter_number}章创作主题曲歌词。

章节标题: {title}
章节摘要: {summary}

要求:
1. 歌词贴合章节情节和氛围
2. 包含[主歌]和[副歌]标记
3. 总字数150-250字
4. 风格: 暗黑民谣，悬疑压抑
5. 直接输出歌词文本，不要解释"""

    lyrics = call_mmx_text(system, prompt, max_tokens=2048)
    if not lyrics:
        return ""

    # 清理输出
    lyrics = lyrics.strip()
    if lyrics.startswith("```"):
        lines = lyrics.split("\n")
        if len(lines) > 2:
            lyrics = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

    if lyrics:
        LYRICS_DIR.mkdir(parents=True, exist_ok=True)
        lyrics_file.write_text(lyrics, encoding="utf-8")

    return lyrics


def generate_music(chapter_number: int, title: str, lyrics: str) -> bool:
    """为单章生成音乐"""
    music_file = MUSIC_DIR / f"chapter_{chapter_number:04d}.mp3"
    if music_file.exists() and music_file.stat().st_size > 1000:
        return True

    music_prompt = (
        f"Dark folk theme song for chapter '{title}' of a horror supernatural novel. "
        f"Mysterious, tense atmosphere with emotional undertones. "
        f"Slow tempo, featuring acoustic guitar, cello, and piano. "
        f"Deep male vocals conveying dread and determination."
    )

    return call_mmx_music(music_prompt, lyrics, str(music_file))


def process_one_chapter(chapter_number: int, title: str, summary: str) -> dict:
    """处理单章：生成歌词 + 音乐"""
    log(f"[Music] 开始处理第{chapter_number}章: {title}")

    # 1. 生成歌词
    lyrics = generate_lyrics(chapter_number, title, summary)
    if not lyrics:
        log(f"[Music] 第{chapter_number}章歌词生成失败")
        return {"chapter": chapter_number, "status": "lyrics_failed"}

    # 2. 生成音乐
    success = generate_music(chapter_number, title, lyrics)
    if success:
        log(f"[Music] 第{chapter_number}章音乐生成成功")
        return {"chapter": chapter_number, "status": "success"}
    else:
        log(f"[Music] 第{chapter_number}章音乐生成失败")
        return {"chapter": chapter_number, "status": "music_failed"}


def get_pending_chapters(outline: dict, progress: dict) -> list:
    """获取待处理的章节列表"""
    completed = set(progress.get("completed", []))
    chapters = outline.get("chapters", [])
    pending = []
    for ch in chapters:
        num = ch.get("chapter_number", 0)
        if num > 0 and num not in completed:
            music_file = MUSIC_DIR / f"chapter_{num:04d}.mp3"
            if not music_file.exists() or music_file.stat().st_size < 1000:
                pending.append(ch)
    return pending


def main():
    print("=" * 60)
    print("Music Generator Agent 启动")
    print("=" * 60)

    LYRICS_DIR.mkdir(parents=True, exist_ok=True)
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    # 加载大纲
    if not OUTLINE_FILE.exists():
        log("[Music] outline.json 不存在")
        return

    outline = json.loads(OUTLINE_FILE.read_text(encoding="utf-8"))
    progress = load_progress()

    pending = get_pending_chapters(outline, progress)
    log(f"[Music] 待处理章节: {len(pending)}/2000")

    if not pending:
        log("[Music] 所有章节音乐已生成，退出")
        return

    # 分批处理，每批最多50章（music-2.6 日配额100，5个worker各处理约20章）
    batch_size = 50
    total_success = 0
    total_failed = 0

    for batch_start in range(0, len(pending), batch_size):
        batch = pending[batch_start:batch_start + batch_size]
        log(f"[Music] 处理批次 {batch_start//batch_size + 1}/{(len(pending)+batch_size-1)//batch_size}, 本批 {len(batch)} 章")

        success = 0
        failed = 0

        with ThreadPoolExecutor(max_workers=5) as executor:
            future_map = {
                executor.submit(
                    process_one_chapter,
                    ch.get("chapter_number", 0),
                    ch.get("title", ""),
                    ch.get("summary", "")
                ): ch.get("chapter_number", 0)
                for ch in batch
            }

            for future in as_completed(future_map):
                ch_num = future_map[future]
                try:
                    result = future.result()
                    if result["status"] == "success":
                        success += 1
                        total_success += 1
                        progress["completed"].append(ch_num)
                    else:
                        failed += 1
                        total_failed += 1
                        if ch_num not in progress["failed"]:
                            progress["failed"].append(ch_num)
                except Exception as e:
                    log(f"[Music] 第{ch_num}章异常: {e}")
                    failed += 1
                    total_failed += 1

        save_progress(progress)
        log(f"[Music] 批次完成: 成功 {success}, 失败 {failed}")
        log(f"[Music] 累计: 成功 {total_success}, 失败 {total_failed}")

        # 批次间短暂休息
        if batch_start + batch_size < len(pending):
            log("[Music] 批次间休息30秒...")
            time.sleep(30)

    log(f"[Music] 全部完成: 成功 {total_success}, 失败 {total_failed}")


if __name__ == "__main__":
    main()
