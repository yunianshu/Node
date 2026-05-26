import time, subprocess, sys
from pathlib import Path

project = "projects/novels1"
review_dir = Path(project) / "chapters" / "review_final"

def count_reviews():
    return len(list(review_dir.glob("chapter_*.json")))

print("[Monitor] Starting review_final monitor...")
while True:
    cnt = count_reviews()
    print(f"[Monitor] {time.strftime('%H:%M:%S')} review_final: {cnt}/2000")
    if cnt >= 2000:
        print("[Monitor] review_final complete! Starting polish...")
        # 启动 polish（低分优先，limit 500 章以控制额度）
        subprocess.Popen([
            sys.executable, "scripts/batch/polish_to_85.py",
            "--project", project,
            "--workers", "10",
            "--wechat",
            "--limit", "500"
        ], cwd=str(Path.cwd()))
        break
    time.sleep(120)
