#!/usr/bin/env python3
"""Review final chapters that had low draft scores"""
import json, random, sys, shutil, subprocess, os, time
from pathlib import Path

project = sys.argv[1] if len(sys.argv) > 1 else "projects/novels9"
sample_size = int(sys.argv[2]) if len(sys.argv) > 2 else 50
threshold = float(sys.argv[3]) if len(sys.argv) > 3 else 7.5

novels_dir = Path(project).resolve()
draft_dir = novels_dir / "chapters" / "draft"
final_dir = novels_dir / "chapters" / "final"
review_dir = novels_dir / "chapters" / "review"

# Backup draft dir
backup_dir = novels_dir / "chapters" / "draft_backup_low"
if backup_dir.exists():
    shutil.rmtree(backup_dir)
shutil.copytree(draft_dir, backup_dir)

# Find chapters with draft score < threshold
low_score_chapters = []
for review_file in review_dir.glob("chapter_*_review.json"):
    try:
        ch_num = int(review_file.stem.split("_")[1])
        r = json.loads(review_file.read_text(encoding="utf-8"))
        score = float(r.get("overall_score", 10))
        if score < threshold:
            low_score_chapters.append((ch_num, score))
    except:
        pass

low_score_chapters.sort()
print(f"Found {len(low_score_chapters)} chapters with draft score < {threshold}")

# Sample from low-score chapters
random.seed(123)
sample = random.sample(low_score_chapters, min(sample_size, len(low_score_chapters)))

scores = []
failed = []

for ch_num, draft_score in sample:
    draft_file = draft_dir / f"chapter_{ch_num:04d}.txt"
    final_file = final_dir / f"chapter_{ch_num:04d}.txt"
    backup_file = backup_dir / f"chapter_{ch_num:04d}.txt"
    
    if not final_file.exists():
        print(f"  Ch{ch_num:04d}: no final file (draft_score={draft_score})")
        failed.append(ch_num)
        continue
    
    # Delete old review file to force re-review
    review_file = review_dir / f"chapter_{ch_num:04d}_review.json"
    if review_file.exists():
        review_file.unlink()
    
    # Swap: put final content in draft position
    if draft_file.exists():
        draft_file.unlink()
    shutil.copy2(final_file, draft_file)
    
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
        
        review_file = review_dir / f"chapter_{ch_num:04d}_review.json"
        if review_file.exists():
            review = json.loads(review_file.read_text(encoding="utf-8"))
            score = review.get("overall_score", "N/A")
            verdict = review.get("verdict", "N/A")
            try:
                s = float(score)
                scores.append((ch_num, draft_score, s))
                delta = s - draft_score
                print(f"  Ch{ch_num:04d}: final={s} (draft={draft_score}, delta={delta:+.1f}) {verdict}")
            except:
                failed.append(ch_num)
                print(f"  Ch{ch_num:04d}: N/A (draft={draft_score})")
        else:
            failed.append(ch_num)
            print(f"  Ch{ch_num:04d}: no review file (draft={draft_score})")
    except Exception as e:
        failed.append(ch_num)
        print(f"  Ch{ch_num:04d}: ERROR {e} (draft={draft_score})")
    
    # Restore original draft
    if draft_file.exists():
        draft_file.unlink()
    if backup_file.exists():
        shutil.copy2(backup_file, draft_file)
    
    time.sleep(0.5)

# Restore any remaining files
for backup_file in backup_dir.glob("chapter_*.txt"):
    target = draft_dir / backup_file.name
    if not target.exists():
        shutil.copy2(backup_file, target)
shutil.rmtree(backup_dir)

if scores:
    final_scores = [s[2] for s in scores]
    draft_scores = [s[1] for s in scores]
    avg_final = sum(final_scores) / len(final_scores)
    avg_draft = sum(draft_scores) / len(draft_scores)
    gte85 = sum(1 for s in final_scores if s >= 8.5)
    gte8 = sum(1 for s in final_scores if s >= 8.0)
    lt7 = sum(1 for s in final_scores if s < 7.0)
    improved = sum(1 for s in scores if s[2] > s[1])
    worsened = sum(1 for s in scores if s[2] < s[1])
    print(f"\n=== Low-Score Chapter Review ({len(scores)} chapters, draft < {threshold}) ===")
    print(f"Draft avg: {avg_draft:.2f} -> Final avg: {avg_final:.2f} (delta: {avg_final-avg_draft:+.2f})")
    print(f">=8.5: {gte85} ({gte85/len(scores)*100:.1f}%)")
    print(f">=8.0: {gte8} ({gte8/len(scores)*100:.1f}%)")
    print(f"<7.0: {lt7} ({lt7/len(scores)*100:.1f}%)")
    print(f"Improved: {improved}, Worsened: {worsened}, Same: {len(scores)-improved-worsened}")
    print(f"Failed: {len(failed)}")
else:
    print("No valid scores")
