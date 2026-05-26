#!/usr/bin/env python3
"""抽样 review 终稿，使用 reviewer.py --final 参数"""
import json, random, sys, subprocess, os, time
from pathlib import Path

project = sys.argv[1] if len(sys.argv) > 1 else "projects/novels9"
sample_size = int(sys.argv[2]) if len(sys.argv) > 2 else 100

novels_dir = Path(project).resolve()
final_dir = novels_dir / "chapters" / "final"
review_final_dir = novels_dir / "chapters" / "review_final"
review_final_dir.mkdir(exist_ok=True)

# 获取所有终稿文件
final_files = sorted(final_dir.glob("chapter_*.txt"))
if len(final_files) == 0:
    print("No final chapters found")
    sys.exit(1)

# 随机抽样
random.seed(42)
sample = random.sample(final_files, min(sample_size, len(final_files)))
print(f"Sampling {len(sample)} final chapters for review...")

scores = []
failed = []

for f in sample:
    ch_num = int(f.stem.split("_")[1])
    env = os.environ.copy()
    env["NOVEL_PROJECT_DIR"] = str(novels_dir)
    
    try:
        result = subprocess.run(
            [sys.executable, "scripts/pipeline/reviewer.py", 
             "--chapter", str(ch_num), 
             "--final",
             "--project", str(novels_dir)],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180
        )
        
        review_file = review_final_dir / f"chapter_{ch_num:04d}_review.json"
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
            if result.returncode != 0:
                print(f"    stderr: {result.stderr[:200]}")
    except Exception as e:
        failed.append(ch_num)
        print(f"  Ch{ch_num:04d}: ERROR {e}")
    
    time.sleep(0.5)

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
