#!/usr/bin/env python3
"""修复 parse_error 的审查报告"""
import json
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import subprocess

NOVELS_DIR = Path("D:/AiProject/Node/novels2")
REVIEWS_DIR = NOVELS_DIR / "reviews"
LOG_FILE = NOVELS_DIR / "logs" / "fix_parse_errors.log"
SCRIPT = Path("D:/AiProject/Node/novels2/scripts/reviewer.py")


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_parse_error_chapters():
    errors = []
    for r in REVIEWS_DIR.glob("chapter_*_review.json"):
        try:
            d = json.loads(r.read_text(encoding="utf-8"))
            if d.get("status") == "parse_error":
                errors.append(int(r.stem.split("_")[1]))
        except:
            pass
    return sorted(errors)


def fix_one(chapter: int) -> dict:
    review_file = REVIEWS_DIR / f"chapter_{chapter:04d}_review.json"
    # 删除旧的 parse_error 文件
    if review_file.exists():
        review_file.unlink()

    try:
        result = subprocess.run(
            ["python", str(SCRIPT), "--chapter", str(chapter)],
            capture_output=True, text=True, encoding="utf-8", timeout=120
        )
        if result.returncode == 0:
            # 验证新生成的文件
            if review_file.exists():
                try:
                    d = json.loads(review_file.read_text(encoding="utf-8"))
                    if d.get("status") == "completed":
                        return {"chapter": chapter, "status": "success", "score": d.get("overall_score")}
                    else:
                        return {"chapter": chapter, "status": "still_error", "detail": d.get("status")}
                except Exception as e:
                    return {"chapter": chapter, "status": "json_error", "err": str(e)}
            else:
                return {"chapter": chapter, "status": "no_file"}
        else:
            return {"chapter": chapter, "status": "failed", "err": result.stderr[:200]}
    except Exception as e:
        return {"chapter": chapter, "status": "error", "err": str(e)}


def main():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    chapters = get_parse_error_chapters()
    log(f"[FixParse] 发现 {len(chapters)} 个 parse_error 章节: {chapters}")

    if not chapters:
        log("[FixParse] 无 parse_error，退出")
        return

    success = 0
    failed = 0
    workers = min(3, len(chapters))  # 保守并行，避免API问题

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(fix_one, ch): ch for ch in chapters}
        for future in as_completed(future_map):
            ch = future_map[future]
            try:
                result = future.result()
                if result["status"] == "success":
                    success += 1
                    log(f"[FixParse] 第{ch}章修复成功，评分: {result.get('score')} ({success}/{len(chapters)})")
                else:
                    failed += 1
                    log(f"[FixParse] 第{ch}章修复失败: {result['status']} - {result.get('err', '')}")
            except Exception as e:
                failed += 1
                log(f"[FixParse] 第{ch}章异常: {e}")

    log(f"[FixParse] 完成: 成功 {success}, 失败 {failed}")


if __name__ == "__main__":
    main()
