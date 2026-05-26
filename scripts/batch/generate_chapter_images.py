#!/usr/bin/env python3
"""为每章生成一张插图"""
import json, os, subprocess, sys, time, concurrent.futures
from pathlib import Path

project = sys.argv[1] if len(sys.argv) > 1 else "projects/novels5"
max_workers = int(sys.argv[2]) if len(sys.argv) > 2 else 5

novels_dir = Path(project).resolve()
outline_dir = novels_dir / "chapters" / "outline"
output_dir = novels_dir / "media" / "images" / "chapters"
output_dir.mkdir(parents=True, exist_ok=True)

# mmx CLI 完整路径（避免 subprocess 中找不到 mmx 命令）
MMX_CLI = Path("C:/Users/59290/AppData/Roaming/npm/node_modules/mmx-cli/dist/mmx.mjs")
NODE_CMD = "node"
# 验证路径
if not MMX_CLI.exists():
    print(f"ERROR: mmx.mjs not found at {MMX_CLI}")
    sys.exit(1)

# 加载世界观
world = {}
world_file = novels_dir / "world.json"
if world_file.exists():
    with open(world_file, "r", encoding="utf-8") as f:
        world = json.load(f)
world_desc = world.get("world_description", "")[:200]
style_hint = world.get("genre", "Chinese fantasy")

# 获取所有 outline 文件
outline_files = sorted(outline_dir.glob("chapter_*.json"))
print(f"Found {len(outline_files)} chapter outlines")

# 找出尚未生成图片的章节
todo = []
for f in outline_files:
    ch_num = int(f.stem.split("_")[1])
    img_file = output_dir / f"chapter_{ch_num:04d}.jpg"
    if not img_file.exists():
        todo.append((ch_num, f))

print(f"Need to generate: {len(todo)} images")
if not todo:
    print("All images already generated!")
    sys.exit(0)

def generate_image(ch_num: int, outline_file: Path) -> str:
    try:
        with open(outline_file, "r", encoding="utf-8") as f:
            outline = json.load(f)
        
        title = outline.get("title", "")
        summary = outline.get("summary", "")[:300]
        
        prompt = (
            f"Chinese fantasy novel illustration, chapter {ch_num} scene: {title}. "
            f"Scene description: {summary}. "
            f"World setting: {world_desc}. "
            f"Rich vibrant colors, dramatic cinematic lighting, highly detailed, "
            f"anime-meets-realistic art style, 16:9 composition, no text"
        )
        
        # 限制 prompt 长度
        prompt = prompt[:900]
        
        out_path = output_dir / f"chapter_{ch_num:04d}.jpg"
        
        result = subprocess.run(
            [
                NODE_CMD, str(MMX_CLI), "image", "generate",
                "--prompt", prompt,
                "--aspect-ratio", "16:9",
                "--out-dir", str(output_dir),
                "--out-prefix", f"chapter_{ch_num:04d}",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            shell=False,
        )
        
        if result.returncode == 0:
            return f"OK ch{ch_num}"
        else:
            return f"FAIL ch{ch_num}: {result.stderr[:100]}"
    except Exception as e:
        return f"ERROR ch{ch_num}: {e}"

completed = 0
failed = 0
start_time = time.time()

with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
    futures = {executor.submit(generate_image, ch, f): (ch, f) for ch, f in todo}
    
    for future in concurrent.futures.as_completed(futures):
        result = future.result()
        completed += 1
        
        if result.startswith("OK"):
            pass
        else:
            failed += 1
            print(result)
        
        if completed % 10 == 0:
            elapsed = time.time() - start_time
            rate = completed / elapsed if elapsed > 0 else 0
            remaining = (len(todo) - completed) / rate if rate > 0 else 0
            print(f"Progress: {completed}/{len(todo)} ({failed} failed), rate: {rate:.2f} img/s, ETA: {remaining/60:.1f}min")

print(f"\nDone! Total: {completed}, Failed: {failed}")
