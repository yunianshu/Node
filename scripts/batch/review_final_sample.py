#!/usr/bin/env python3
"""Review final chapters by temporarily swapping draft/final dirs"""
import json, random, sys, shutil, subprocess, os, time
from pathlib import Path

project = sys.argv[1] if len(sys.argv) > 1 else "projects/novels1"
sample_size = int(sys.argv[2]) if len(sys.argv) > 2 else 100

novels_dir = Path(project).resolve()
draft_dir = novels_dir / "chapters" / "draft"
final_dir = novels_dir / "chapters" / "final"
review_dir = novels_dir / "chapters" / "review"

# Backup draft dir
backup_dir = novels_dir / "chapters" / "draft_backup"
if backup_dir.exists():
    shutil.rmtree(backup_dir)
shutil.copytree(draft_dir, backup_dir)

# Get all final files
final_files = sorted(final_dir.glob("chapter_*.txt"))
random.seed(42)
sample = random.sample(final_files, min(sample_size, len(final_files)))

print(f"Sampling {len(sample)} final chapters for review...")

# For each sampled chapter, swap its draft with final, review it, then restore
scores = []
failed = []

for f in sample:
    ch_num = int(f.stem.split("_")[1])
    draft_file = draft_dir / f"chapter_{ch_num:04d}.txt"
    backup_file = backup_dir / f"chapter_{ch_num:04d}.txt"
    
    # Delete old review file to force re-review
    review_file = review_dir / f"chapter_{ch_num:04d}_review.json"
    if review_file.exists():
        review_file.unlink()
    
    # Swap: put final content in draft position
    if draft_file.exists():
        draft_file.unlink()
    shutil.copy2(f, draft_file)
    
    # Run reviewer for this chapter
    env = os.environ.copy()
    env["NOVEL_PROJECT_DIR"] = str(novels_dir)
    
    try:
        result = subprocess.run(
            [sys.executable, "scripts/pipeline/reviewer.py", str(ch_num), "--project", str(novels_dir)],
            cwd=novels_dir.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=120
        )
        
        # Read the review file
        review_file = review_dir / f"chapter_{ch_num:04d}_review.json"
        if review_file.exists():
            review = json.loads(review_file.read_text(encoding="utf-8"))
            score = review.get("overall_score", "N/A")
            verdict = review.get("verdict", "N/A")
            try:
                s = float(score)
                scores.append(s)
                print(f"  Ch{ch_num:04d}: {s} ({verdict})")
            except:
                failed.append(ch_num)
                print(f"  Ch{ch_num:04d}: N/A")
        else:
            failed.append(ch_num)
            print(f"  Ch{ch_num:04d}: no review file")
    except Exception as e:
        failed.append(ch_num)
        print(f"  Ch{ch_num:04d}: ERROR {e}")
    
    # Restore original draft
    if draft_file.exists():
        draft_file.unlink()
    if backup_file.exists():
        shutil.copy2(backup_file, draft_file)
    
    time.sleep(0.5)

# Restore any remaining files from backup
for backup_file in backup_dir.glob("chapter_*.txt"):
    target = draft_dir / backup_file.name
    if not target.exists():
        shutil.copy2(backup_file, target)

shutil.rmtree(backup_dir)

if scores:
    avg = sum(scores) / len(scores)
    gte85 = sum(1 for s in scores if s >= 8.5)
    gte8 = sum(1 for s in scores if s >= 8.0)
    lt7 = sum(1 for s in scores if s < 7.0)
    print(f"\n=== Final Sample Review ({len(scores)} chapters) ===")
    print(f"Average: {avg:.2f}")
    print(f">=8.5: {gte85} ({gte85/len(scores)*100:.1f}%)")
    print(f">=8.0: {gte8} ({gte8/len(scores)*100:.1f}%)")
    print(f"<7.0: {lt7} ({lt7/len(scores)*100:.1f}%)")
    print(f"Failed: {len(failed)}")
else:
    print("No valid scores")
