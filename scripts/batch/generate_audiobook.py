#!/usr/bin/env python3
"""批量生成有声书（语音合成）"""
import os, subprocess, sys, time, concurrent.futures
from pathlib import Path

project = sys.argv[1] if len(sys.argv) > 1 else "projects/novels5"
max_workers = int(sys.argv[2]) if len(sys.argv) > 2 else 3
batch_size = int(sys.argv[3]) if len(sys.argv) > 3 else 50  # 先生成前N章

novels_dir = Path(project).resolve()
final_dir = novels_dir / "chapters" / "final"
output_dir = novels_dir / "media" / "audio" / "chapters"
output_dir.mkdir(parents=True, exist_ok=True)

final_files = sorted(final_dir.glob("chapter_*.txt"))
# 只取前 batch_size 章
todo_files = final_files[:batch_size]

print(f"Generating audiobook for {len(todo_files)} chapters...")

def synthesize_chapter(final_file: Path) -> str:
    ch_num = int(final_file.stem.split("_")[1])
    out_file = output_dir / f"chapter_{ch_num:04d}.mp3"
    
    if out_file.exists():
        return f"SKIP ch{ch_num}"
    
    try:
        result = subprocess.run(
            [
                "mmx", "speech", "synthesize",
                "--text-file", str(final_file),
                "--voice", "Chinese (Mandarin)_Male_Announcer",
                "--speed", "1.1",
                "--out", str(out_file),
                "--quiet",
            ],
            capture_output=True,
            text=True,
            timeout=120,
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
    futures = [executor.submit(synthesize_chapter, f) for f in todo_files]
    for future in concurrent.futures.as_completed(futures):
        result = future.result()
        completed += 1
        if result.startswith("OK"):
            pass
        else:
            failed += 1
            print(result)
        
        if completed % 5 == 0:
            elapsed = time.time() - start_time
            rate = completed / elapsed if elapsed > 0 else 0
            remaining = (len(todo_files) - completed) / rate if rate > 0 else 0
            print(f"Audio: {completed}/{len(todo_files)} ({failed} failed), rate: {rate:.2f} ch/s, ETA: {remaining/60:.1f}min")

print(f"\nAudiobook done! Total: {completed}, Failed: {failed}")
