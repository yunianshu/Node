#!/usr/bin/env python3
"""
批量生成歌词 - 使用 mmx text chat
每种风格20首，分4批，每批5首
"""
import json
import os
import subprocess
import time
from pathlib import Path

BASE_DIR = Path("D:/AiProject/Node/lyrics_collection")

STYLES = {
    "方文山风格": (
        "你是方文山，台湾著名作词人。你的歌词风格：大量运用中国风意象（青花瓷、烟雨、江南、红尘、兰亭等），"
        "文字古典唯美，充满画面感，善用比喻和借景抒情，押韵工整，意境深远。"
        "请生成5首原创歌词，每首都要有歌名，标注[Verse][Chorus][Bridge]等结构，主题各不相同。"
    ),
    "许嵩风格": (
        "你是许嵩，内地创作歌手。你的歌词风格：文艺清新，带有些许颓废和哲思，"
        "善于用现代都市生活场景表达情感，文字简洁但有韵味，偶尔带点小幽默和自嘲，"
        "押韵自然不刻意。请生成5首原创歌词，每首都要有歌名，标注[Verse][Chorus][Bridge]等结构，主题各不相同。"
    ),
    "林俊杰风格": (
        "你是林俊杰，新加坡创作歌手。你的歌词风格：情感浓烈直接，以爱情为主题，"
        "旋律感强，歌词朗朗上口，善于用简单的词汇表达深刻的情感，"
        "充满温暖和治愈感。请生成5首原创歌词，每首都要有歌名，标注[Verse][Chorus][Bridge]等结构，主题各不相同。"
    ),
    "陈奕迅风格": (
        "你是陈奕迅，香港歌手。你的歌词风格：以林夕、黄伟文词作为代表，"
        "情感细腻深沉，善于描绘都市人的孤独、无奈与挣扎，"
        "文字平实却直击人心，充满生活感和哲理。请生成5首原创歌词，每首都要有歌名，标注[Verse][Chorus][Bridge]等结构，主题各不相同。"
    ),
    "王力宏风格": (
        "你是王力宏，华语创作歌手。你的歌词风格：融合中西文化元素，"
        "节奏感强，主题积极向上，倡导爱与和平，"
        "中英文夹杂，充满力量和正能量。请生成5首原创歌词，每首都要有歌名，标注[Verse][Chorus][Bridge]等结构，主题各不相同。"
    ),
}


def call_mmx(system_prompt, user_prompt):
    """调用 mmx text chat 生成歌词"""
    cmd = (
        'mmx text chat '
        f'--system "{system_prompt.replace(chr(34), chr(39))}" '
        f'--message "user:{user_prompt.replace(chr(34), chr(39))}" '
        '--output json --quiet --max-tokens 4096'
    )
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120, shell=True)
        if result.returncode != 0:
            stderr = result.stderr.decode('utf-8', errors='replace') if result.stderr else ''
            print(f"  Error: {stderr}")
            return None
        stdout = result.stdout.decode('utf-8', errors='replace')
        data = json.loads(stdout)
        # mmx text chat --output json 返回格式:
        # { "content": [{"type":"thinking",...}, {"type":"text", "text":"..."}] }
        if isinstance(data, dict) and "content" in data:
            parts = data["content"]
            texts = []
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "text" and "text" in part:
                    texts.append(part["text"])
            return "\n".join(texts)
        return str(data)
    except Exception as e:
        print(f"  Exception: {e}")
        return None


def generate_for_style(style_name, system_prompt, count=20):
    """为指定风格生成 count 首歌词"""
    style_dir = BASE_DIR / style_name
    style_dir.mkdir(exist_ok=True)

    batch_size = 5
    batches = (count + batch_size - 1) // batch_size

    all_lyrics = []
    for batch in range(batches):
        remaining = min(batch_size, count - batch * batch_size)
        user_prompt = (
            f"请生成{remaining}首原创歌词，要求如下：\n"
            "1. 每首都要有独特的歌名\n"
            "2. 标注[Intro][Verse][Pre-Chorus][Chorus][Bridge][Outro]等结构\n"
            "3. 主题各不相同（爱情、离别、梦想、成长、自然等）\n"
            "4. 每首歌词长度适中，约200-400字\n"
            "5. 用中文写作\n"
            f"这是第{batch + 1}批，请直接输出歌词内容，用'==='分隔每首歌。"
        )
        print(f"  生成批次 {batch + 1}/{batches}...")
        content = call_mmx(system_prompt, user_prompt)
        if content:
            all_lyrics.append(content)
            # 保存批次文件
            batch_file = style_dir / f"batch_{batch + 1:02d}.txt"
            batch_file.write_text(content, encoding="utf-8")
        else:
            print(f"  批次 {batch + 1} 失败，跳过")
        time.sleep(2)  # 避免速率限制

    # 合并保存
    if all_lyrics:
        merged = f"# {style_name} - 歌词合集\n\n" + "\n\n".join(all_lyrics)
        merged_file = style_dir / f"{style_name}_全部歌词.txt"
        merged_file.write_text(merged, encoding="utf-8")
        print(f"  已保存到 {merged_file}")

    return len(all_lyrics) * batch_size


def main():
    print("=" * 50)
    print("开始批量生成歌词")
    print("=" * 50)

    for style_name, system_prompt in STYLES.items():
        print(f"\n【{style_name}】")
        generate_for_style(style_name, system_prompt, count=20)

    print("\n" + "=" * 50)
    print("歌词生成完成！")
    print(f"保存位置: {BASE_DIR}")
    print("=" * 50)


if __name__ == "__main__":
    main()
