#!/usr/bin/env python3
"""企业微信机器人进度推送"""
import json
import urllib.request
import os
import time
from pathlib import Path

WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=22ea4574-b1f0-4c36-af2d-b13c6c2d471b"
NOVELS_DIR = Path("D:/AiProject/Node/novels5")

def get_progress():
    lines = []

    # 大纲进度
    outline_file = NOVELS_DIR / "outline.json"
    outline_count = 0
    if outline_file.exists():
        try:
            with open(outline_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            chapters = data.get("chapters", [])
            outline_count = len(chapters)
        except Exception:
            pass

    # 初稿进度
    draft_dir = NOVELS_DIR / "chapters" / "draft"
    draft_count = 0
    total_words = 0
    if draft_dir.exists():
        drafts = [f for f in draft_dir.glob("chapter_*.txt") if f.stat().st_size > 1000]
        draft_count = len(drafts)
        for d in drafts:
            try:
                total_words += len(d.read_text(encoding="utf-8"))
            except Exception:
                pass

    # 终稿进度
    final_dir = NOVELS_DIR / "chapters" / "final"
    final_count = 0
    if final_dir.exists():
        finals = [f for f in final_dir.glob("chapter_*.txt") if f.stat().st_size > 1000]
        final_count = len(finals)

    # 审查进度
    reviews_dir = NOVELS_DIR / "reviews"
    review_count = 0
    avg_score = 0
    scores = []
    if reviews_dir.exists():
        reviews = list(reviews_dir.glob("chapter_*_review.json"))
        review_count = len(reviews)
        for r in reviews:
            try:
                with open(r, "r", encoding="utf-8") as f:
                    data = json.load(f)
                score = data.get("overall_score", 0)
                if score:
                    scores.append(score)
            except Exception:
                pass
        if scores:
            avg_score = sum(scores) / len(scores)

    # 构建消息
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    msg = f"""《长生武道：虚空万界行》生成任务完成! ({now})
━━━━━━━━━━━━━━━━━━━━
📋 大纲: {outline_count}/2000 章
✍ 初稿: {draft_count}/2000 章
📝 字数: {total_words:,}
🔍 审查: {review_count}/2000 章
📤 终稿: {final_count}/2000 章
⭐ 平均评分: {avg_score:.2f}
━━━━━━━━━━━━━━━━━━━━"""

    return msg

def send_wechat(msg: str):
    data = json.dumps({"msgtype": "text", "text": {"content": msg}}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(WEBHOOK_URL, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        return str(e)

if __name__ == "__main__":
    msg = get_progress()
    result = send_wechat(msg)
    print(result)
