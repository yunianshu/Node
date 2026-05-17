#!/usr/bin/env python3
"""
企业微信机器人通知脚本 - 推送小说生成进度
"""
import json
import subprocess
import time
import urllib.request
from pathlib import Path

WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=22ea4574-b1f0-4c36-af2d-b13c6c2d471b"
STATE_FILE = Path("D:/AiProject/Node/novels2/.last_report_state.json")


def get_progress():
    """获取小说生成进度"""
    NOVELS_DIR = Path("D:/AiProject/Node/novels2")
    DRAFT = NOVELS_DIR / "chapters" / "draft"
    REVIEWS = NOVELS_DIR / "reviews"
    FINAL = NOVELS_DIR / "chapters" / "final"
    OUTLINE_FILE = NOVELS_DIR / "outline.json"

    # 大纲
    outline_count = 0
    if OUTLINE_FILE.exists():
        try:
            outline = json.load(open(OUTLINE_FILE, encoding="utf-8"))
            outline_count = len(outline.get("chapters", []))
        except:
            pass

    # 初稿
    draft_count = 0
    draft_words = 0
    if DRAFT.exists():
        for c in DRAFT.glob("chapter_*.txt"):
            if c.stat().st_size > 1000:
                draft_count += 1
                try:
                    draft_words += len(c.read_text(encoding="utf-8"))
                except:
                    pass

    # 审查
    review_count = 0
    if REVIEWS.exists():
        review_count = len(list(REVIEWS.glob("chapter_*_review.json")))

    # 终稿
    final_count = 0
    if FINAL.exists():
        final_count = len(list(FINAL.glob("chapter_*.txt")))

    # 进程
    writer_count = 0
    coord_count = 0
    try:
        result = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "ProcessId,CommandLine"],
            capture_output=True, text=True, encoding="utf-8"
        )
        for line in result.stdout.split("\n"):
            if "writer.py" in line:
                writer_count += 1
            elif "coordinator.py" in line:
                coord_count += 1
    except:
        pass

    return {
        "outline": outline_count,
        "draft": draft_count,
        "draft_words": draft_words,
        "reviews": review_count,
        "final": final_count,
        "coordinators": coord_count,
        "writers": writer_count,
    }


def send_wechat(msg: str):
    """发送企业微信消息"""
    data = {
        "msgtype": "text",
        "text": {"content": msg}
    }
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[Notify] 发送失败: {e}")
        return False


def main():
    p = get_progress()
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    # 读取上次状态并计算新增
    last = {"draft": 0, "reviews": 0}
    if STATE_FILE.exists():
        try:
            last = json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception:
            pass

    draft_new = p["draft"] - last.get("draft", 0)
    review_new = p["reviews"] - last.get("reviews", 0)

    # 保存当前状态
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"draft": p["draft"], "reviews": p["reviews"]}, f)

    draft_new_str = f" (+{draft_new})" if draft_new > 0 else ""
    review_new_str = f" (+{review_new})" if review_new > 0 else ""

    msg = f"""📖 《复苏之夜》生成进度 ({timestamp})
━━━━━━━━━━━━━━━━━━━━
📋 大纲: {p['outline']}/2000 章
✍️ 初稿: {p['draft']}/2000 章{draft_new_str}
📝 字数: {p['draft_words']:,}
🔍 审查: {p['reviews']}/2000 章{review_new_str}
📤 终稿: {p['final']}/2000 章
🤖 进程: {p['coordinators']} Coordinator + {p['writers']} Writer
━━━━━━━━━━━━━━━━━━━━"""

    if send_wechat(msg):
        print(f"[Notify] {timestamp} 推送成功 初稿+{draft_new} 审查+{review_new}")
    else:
        print(f"[Notify] {timestamp} 推送失败")


if __name__ == "__main__":
    main()
