#!/usr/bin/env python3
"""
并行补全缺失的审查报告
"""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

NOVELS_DIR = Path("D:/AiProject/Node/projects/novels6")
REVIEWS_DIR = NOVELS_DIR / "reviews"
LOG_FILE = NOVELS_DIR / "logs" / "fill_reviews_parallel.log"
SCRIPTS_DIR = NOVELS_DIR / "scripts"

# 企业微信Webhook
WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=22ea4574-b1f0-4c36-af2d-b13c6c2d471b"


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()


def push_wechat(msg: str):
    """推送消息到企业微信"""
    import urllib.request
    data = json.dumps({"msgtype": "text", "text": {"content": msg}}).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"[WeChat] 推送失败: {e}")


def get_missing_reviews():
    """获取需要重新审查的章节列表（缺失或解析失败）"""
    missing = []
    for ch in range(1, 2001):
        review_file = REVIEWS_DIR / f"chapter_{ch:04d}_review.json"
        if not review_file.exists():
            missing.append(ch)
            continue
        try:
            data = json.load(open(review_file, encoding="utf-8"))
            if data.get("status") in ("failed", "parse_error"):
                missing.append(ch)
        except Exception:
            missing.append(ch)
    return missing


def run_reviewer(start: int, end: int) -> tuple:
    """运行 reviewer.py 处理一个范围"""
    cmd = [
        sys.executable, str(SCRIPTS_DIR / "reviewer.py"),
        "--start", str(start), "--end", str(end)
    ]
    result = subprocess.run(cmd, capture_output=False, text=True, encoding="utf-8")
    newly_completed = 0
    for ch in range(start, end + 1):
        f = REVIEWS_DIR / f"chapter_{ch:04d}_review.json"
        if f.exists():
            try:
                data = json.load(open(f, encoding="utf-8"))
                if data.get("status") == "completed":
                    newly_completed += 1
            except Exception:
                pass
    return (start, end, result.returncode, newly_completed)


def chunk_ranges(missing: list, chunk_size: int = 30) -> list:
    """将缺失章节列表拆分为连续的范围"""
    if not missing:
        return []
    ranges = []
    start = missing[0]
    prev = missing[0]
    for ch in missing[1:]:
        if ch == prev + 1:
            prev = ch
        else:
            ranges.append((start, prev))
            start = ch
            prev = ch
    ranges.append((start, prev))
    # 如果范围太大，进一步拆分
    final_ranges = []
    for s, e in ranges:
        while s <= e:
            seg_end = min(s + chunk_size - 1, e)
            final_ranges.append((s, seg_end))
            s = seg_end + 1
    return final_ranges


def pusher_thread():
    """每2分钟推送一次进度"""
    while True:
        time.sleep(120)
        missing = get_missing_reviews()
        completed = 2000 - len(missing)
        msg = f"📊 小说生成进度\n审查: {completed}/2000 章\n缺失: {len(missing)} 章"
        push_wechat(msg)
        log(f"[WeChat] 进度已推送: 审查{completed}/2000")


def main():
    print("=" * 60)
    print("Fill Reviews Parallel Agent 启动")
    print("=" * 60)

    missing = get_missing_reviews()
    log(f"需要审查的章节: {len(missing)} 章")
    if not missing:
        log("所有审查报告已完成！")
        return

    ranges = chunk_ranges(missing, chunk_size=30)
    log(f"拆分为 {len(ranges)} 个任务范围")

    # 启动微信推送线程
    pusher = threading.Thread(target=pusher_thread, daemon=True)
    pusher.start()

    # 限制并发数为15
    max_workers = min(15, len(ranges))
    completed_total = 2000 - len(missing)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(run_reviewer, s, e): (s, e) for s, e in ranges}
        for future in as_completed(futures):
            s, e = futures[future]
            try:
                start_r, end_r, rc, newly = future.result()
                completed_total += newly
                log(f"[Worker] 第{s}-{e}章审查完成，本范围完成 {newly} 章，总完成 {completed_total}/2000")
            except Exception as exc:
                log(f"[Worker] 第{s}-{e}章审查异常: {exc}")

    # 最终检查
    missing = get_missing_reviews()
    log(f"最终审查: {2000 - len(missing)}/2000 章")
    if missing:
        log(f"仍有缺失: {len(missing)} 章: {missing[:20]}...")
    else:
        log("全部审查完成！")
        push_wechat("🎉 小说审查全部完成！2000/2000 章")


if __name__ == "__main__":
    main()
