#!/usr/bin/env python3
"""并行补全缺失的初稿章节 - 高效版本"""
import concurrent.futures
import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

NOVELS_DIR = Path("D:/AiProject/Node/novels3")
CHAPTERS_DIR = NOVELS_DIR / "chapters" / "draft"
SCRIPTS_DIR = Path(__file__).parent
LOG_FILE = NOVELS_DIR / "logs" / "fill_draft_parallel.log"

WECHAT_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=22ea4574-b1f0-4c36-af2d-b13c6c2d471b"

# 全局状态
_progress_lock = threading.Lock()
_last_push_time = 0
_completed_count = 0
_total_missing = 0
_pusher_started = False


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def push_wechat(msg: str):
    try:
        data = json.dumps({"msgtype": "text", "text": {"content": msg}}).encode("utf-8")
        req = urllib.request.Request(
            WECHAT_WEBHOOK,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as e:
        log(f"[WeChat] 推送失败: {e}")


def get_progress_summary():
    completed = 0
    total_words = 0
    if CHAPTERS_DIR.exists():
        for f in CHAPTERS_DIR.glob("chapter_*.txt"):
            if f.stat().st_size > 1000:
                completed += 1
                total_words += len(f.read_text(encoding="utf-8"))
    return {"draft": completed, "total_words": total_words}


def progress_pusher_thread(interval_seconds: int = 120):
    global _last_push_time
    while True:
        time.sleep(interval_seconds)
        with _progress_lock:
            now = time.time()
            if now - _last_push_time < interval_seconds:
                continue
            _last_push_time = now

        p = get_progress_summary()
        separator = "━━━━━━━━━━━━━━━━━━━━"
        msg = (
            f"📖 《书生武道通神》生成进度 ({time.strftime('%Y-%m-%d %H:%M:%S')})\n"
            f"{separator}\n"
            f"✍ 初稿: {p['draft']}/2000 章\n"
            f"📝 字数: {p['total_words']:,}\n"
            f"🔧 状态: 并行补全缺失章节中...\n"
            f"{separator}"
        )
        push_wechat(msg)
        log(f"[WeChat] 进度已推送: 初稿{p['draft']}/2000")


def get_missing_ranges():
    existing = set()
    for f in CHAPTERS_DIR.glob("chapter_*.txt"):
        if f.stat().st_size > 1000:
            num = int(f.stem.split("_")[1])
            existing.add(num)

    missing = [i for i in range(1, 2001) if i not in existing]
    if not missing:
        return []

    groups = []
    start = missing[0]
    prev = missing[0]
    for n in missing[1:]:
        if n == prev + 1:
            prev = n
        else:
            groups.append((start, prev))
            start = n
            prev = n
    groups.append((start, prev))
    return groups


def run_writer(start: int, end: int) -> tuple:
    """运行writer.py生成指定范围"""
    global _completed_count
    log(f"[Worker] 开始生成第{start}-{end}章")
    cmd = [
        sys.executable, str(SCRIPTS_DIR / "writer.py"),
        "--start", str(start), "--end", str(end)
    ]
    result = subprocess.run(cmd, capture_output=False, text=True, encoding="utf-8")

    # 计算本范围实际生成了多少章
    newly_completed = 0
    for ch in range(start, end + 1):
        f = CHAPTERS_DIR / f"chapter_{ch:04d}.txt"
        if f.exists() and f.stat().st_size > 1000:
            newly_completed += 1

    with _progress_lock:
        _completed_count += newly_completed

    if result.returncode == 0:
        log(f"[Worker] 第{start}-{end}章完成，本范围生成 {newly_completed} 章")
    else:
        log(f"[Worker] 第{start}-{end}章返回非零码 (rc={result.returncode})")

    return (start, end, result.returncode, newly_completed)


def main():
    print("=" * 60)
    print("并行补全缺失初稿 - 高效版本")
    print("=" * 60)

    # 启动推送线程
    global _pusher_started
    if not _pusher_started:
        _pusher_started = True
        pusher = threading.Thread(target=progress_pusher_thread, args=(120,), daemon=True)
        pusher.start()
        log("[Main] 企业微信进度推送已启动（每2分钟）")

    missing_ranges = get_missing_ranges()
    total_missing = sum(e - s + 1 for s, e in missing_ranges)
    log(f"缺失: {total_missing} 章, 共 {len(missing_ranges)} 段")

    for s, e in missing_ranges:
        log(f"  {s}-{e} ({e-s+1}章)")

    if not missing_ranges:
        log("初稿已完整!")
        return

    global _total_missing
    _total_missing = total_missing

    # 并行生成，限制并发数避免API限流
    max_workers = min(15, len(missing_ranges))
    log(f"[Main] 启动 {max_workers} 个并发 Worker 生成 {len(missing_ranges)} 段缺失章节")

    completed = 0
    failed = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(run_writer, s, e): (s, e)
            for s, e in missing_ranges
        }
        for future in concurrent.futures.as_completed(futures):
            s, e = futures[future]
            try:
                start, end, rc, count = future.result()
                if rc == 0:
                    completed += 1
                else:
                    failed += 1
            except Exception as exc:
                log(f"[Main] 第{s}-{e}章异常: {exc}")
                failed += 1

    # 最终进度
    p = get_progress_summary()
    log(f"[Main] 补全完成: 初稿 {p['draft']}/2000 章, 总字数 {p['total_words']:,}")

    separator = "━━━━━━━━━━━━━━━━━━━━"
    push_wechat(
        f"📖 《书生武道通神》补全完成 ({time.strftime('%Y-%m-%d %H:%M:%S')})\n"
        f"{separator}\n"
        f"✍ 初稿: {p['draft']}/2000 章\n"
        f"📝 字数: {p['total_words']:,}\n"
        f"✅ 成功段: {completed}\n"
        f"❌ 失败段: {failed}\n"
        f"{separator}"
    )


if __name__ == "__main__":
    main()
