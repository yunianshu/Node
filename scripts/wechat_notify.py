#!/usr/bin/env python3
"""
企业微信小说进度推送 - 全局多小说管理版

用法:
    python wechat_notify.py --list                    # 列出所有小说
    python wechat_notify.py --push --book novels5     # 推送指定小说进度
    python wechat_notify.py --push --all              # 推送所有小说进度
    python wechat_notify.py --push --all --webhook KEY # 使用指定 webhook
"""
import argparse
import io
import json
import sys
import time
import urllib.request
from pathlib import Path

# 修复 Windows 控制台 UTF-8 编码
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# 默认配置
DEFAULT_ROOT = Path("D:/AiProject/Node")
DEFAULT_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=22ea4574-b1f0-4c36-af2d-b13c6c2d471b"


def discover_books(root_dir):
    """扫描目录下所有小说项目"""
    books = []
    root = Path(root_dir)
    if not root.exists():
        return books
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        if entry.name in ("audio", "logs", "scripts", "node_modules", ".claude"):
            continue
        outline_file = entry / "outline.json"
        chapters_dir = entry / "chapters"
        if not outline_file.exists() or not chapters_dir.exists():
            continue
        try:
            with open(outline_file, "r", encoding="utf-8") as f:
                outline = json.load(f)
            chapter_count = len(outline.get("chapters", []))
            # 获取书名
            book_title = entry.name
            first_ch = outline.get("chapters", [{}])[0]
            if first_ch and "series_title" in first_ch:
                book_title = first_ch["series_title"]
            elif first_ch and "title" in first_ch:
                # 尝试从 world.json 获取书名
                world_file = entry / "world.json"
                if world_file.exists():
                    try:
                        with open(world_file, "r", encoding="utf-8") as f:
                            world = json.load(f)
                        book_title = world.get("title", book_title)
                    except Exception:
                        pass
            books.append({
                "id": entry.name,
                "title": book_title,
                "path": str(entry),
                "chapter_count": chapter_count,
            })
        except Exception:
            continue
    return sorted(books, key=lambda x: x["id"])


def get_book_progress(book_dir):
    """获取单本小说的详细进度"""
    book_dir = Path(book_dir)
    result = {
        "id": book_dir.name,
        "title": book_dir.name,
        "outline": 0,
        "outline_total": 0,
        "draft": 0,
        "draft_total": 0,
        "final": 0,
        "final_total": 0,
        "review": 0,
        "review_total": 0,
        "total_words": 0,
        "avg_score": 0,
    }

    # 读取 world.json 获取书名
    world_file = book_dir / "world.json"
    if world_file.exists():
        try:
            with open(world_file, "r", encoding="utf-8") as f:
                world = json.load(f)
            result["title"] = world.get("title", book_dir.name)
        except Exception:
            pass

    # 大纲
    outline_file = book_dir / "outline.json"
    if outline_file.exists():
        try:
            with open(outline_file, "r", encoding="utf-8") as f:
                outline = json.load(f)
            chapters = outline.get("chapters", [])
            result["outline"] = len(chapters)
            result["outline_total"] = len(chapters)
            result["draft_total"] = len(chapters)
            result["final_total"] = len(chapters)
            result["review_total"] = len(chapters)
        except Exception:
            pass

    # 初稿
    draft_dir = book_dir / "chapters" / "draft"
    if draft_dir.exists():
        drafts = [f for f in draft_dir.glob("chapter_*.txt") if f.stat().st_size > 1000]
        result["draft"] = len(drafts)
        for d in drafts:
            try:
                result["total_words"] += len(d.read_text(encoding="utf-8"))
            except Exception:
                pass

    # 终稿
    final_dir = book_dir / "chapters" / "final"
    if final_dir.exists():
        finals = [f for f in final_dir.glob("chapter_*.txt") if f.stat().st_size > 1000]
        result["final"] = len(finals)

    # 审查
    reviews_dir = book_dir / "reviews"
    if reviews_dir.exists():
        reviews = list(reviews_dir.glob("chapter_*_review.json"))
        result["review"] = len(reviews)
        scores = []
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
            result["avg_score"] = sum(scores) / len(scores)

    return result


def format_progress_line(label, current, total):
    """格式化进度行"""
    if total == 0:
        return f"  {label}: --"
    pct = current / total * 100
    bar_len = 20
    filled = int(bar_len * current / total)
    bar = "█" * filled + "░" * (bar_len - filled)
    return f"  {label}: {bar} {current}/{total} ({pct:.1f}%)"


def build_single_message(book):
    """构建单本小说的推送消息"""
    p = get_book_progress(book["path"])
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    msg = f"【{p['title']}】生成进度 ({now})\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"📋 大纲: {p['outline']}/{p['outline_total']} 章\n"
    msg += f"✍ 初稿: {p['draft']}/{p['draft_total']} 章\n"
    msg += f"📝 字数: {p['total_words']:,}\n"
    msg += f"🔍 审查: {p['review']}/{p['review_total']} 章\n"
    msg += f"📤 终稿: {p['final']}/{p['final_total']} 章\n"
    if p["avg_score"] > 0:
        msg += f"⭐ 平均评分: {p['avg_score']:.2f}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━"
    return msg


def build_all_message(books):
    """构建所有小说的汇总推送消息"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    msg = f"📚 小说生成进度汇总 ({now})\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"

    total_outline = 0
    total_draft = 0
    total_final = 0
    total_words = 0

    for book in books:
        p = get_book_progress(book["path"])
        total_outline += p["outline"]
        total_draft += p["draft"]
        total_final += p["final"]
        total_words += p["total_words"]

        # 简写状态
        status = []
        if p["outline"] == p["outline_total"] and p["outline_total"] > 0:
            status.append("✅大纲")
        else:
            status.append(f"📋{p['outline']}/{p['outline_total']}")

        if p["draft"] == p["draft_total"] and p["draft_total"] > 0:
            status.append("✅初稿")
        else:
            status.append(f"✍{p['draft']}/{p['draft_total']}")

        if p["review"] == p["review_total"] and p["review_total"] > 0:
            status.append("✅审查")
        elif p["review"] > 0:
            status.append(f"🔍{p['review']}/{p['review_total']}")

        if p["final"] == p["final_total"] and p["final_total"] > 0:
            status.append("✅终稿")
        elif p["final"] > 0:
            status.append(f"📤{p['final']}/{p['final_total']}")

        status_str = " | ".join(status)
        word_wan = p["total_words"] / 10000
        msg += f"《{p['title']}》\n"
        msg += f"  {status_str}\n"
        msg += f"  📝 {word_wan:.1f}万字"
        if p["avg_score"] > 0:
            msg += f" | ⭐ {p['avg_score']:.2f}"
        msg += "\n\n"

    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"📊 总计: {len(books)} 本小说\n"
    msg += f"📝 总字数: {total_words / 10000:.1f} 万字"
    return msg


def send_wechat(msg, webhook_url):
    """发送企业微信消息"""
    data = json.dumps({"msgtype": "text", "text": {"content": msg}}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(webhook_url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        return str(e)


def main():
    parser = argparse.ArgumentParser(description="企业微信小说进度推送")
    parser.add_argument("--root", type=str, default=str(DEFAULT_ROOT), help="小说根目录")
    parser.add_argument("--webhook", type=str, default=DEFAULT_WEBHOOK, help="企业微信 Webhook URL")
    parser.add_argument("--list", action="store_true", help="列出所有小说")
    parser.add_argument("--push", action="store_true", help="推送进度")
    parser.add_argument("--book", type=str, help="指定小说ID推送")
    parser.add_argument("--all", action="store_true", help="推送所有小说")
    args = parser.parse_args()

    books = discover_books(args.root)

    if args.list:
        print(f"发现 {len(books)} 本小说:")
        for b in books:
            p = get_book_progress(b["path"])
            status = []
            if p["outline"] == p["outline_total"] and p["outline_total"] > 0:
                status.append("大纲✅")
            if p["draft"] == p["draft_total"] and p["draft_total"] > 0:
                status.append("初稿✅")
            if p["final"] == p["final_total"] and p["final_total"] > 0:
                status.append("终稿✅")
            status_str = " | ".join(status) if status else "进行中"
            print(f"  [{b['id']}] 《{b['title']}》 - {status_str} - {p['total_words'] / 10000:.1f}万字")
        return

    if args.push:
        if args.book:
            # 推送指定小说
            book = next((b for b in books if b["id"] == args.book), None)
            if not book:
                print(f"错误: 未找到小说 '{args.book}'")
                print(f"可用小说: {[b['id'] for b in books]}")
                return
            msg = build_single_message(book)
            print(msg)
            result = send_wechat(msg, args.webhook)
            print(result)
        elif args.all:
            # 推送所有小说
            if not books:
                print("未发现任何小说项目")
                return
            msg = build_all_message(books)
            print(msg)
            result = send_wechat(msg, args.webhook)
            print(result)
        else:
            # 默认推送第一个小说（兼容旧行为）
            if books:
                msg = build_single_message(books[0])
                print(msg)
                result = send_wechat(msg, args.webhook)
                print(result)
            else:
                print("未发现任何小说项目")
        return

    # 默认行为：推送第一个小说
    if books:
        msg = build_single_message(books[0])
        print(msg)
        result = send_wechat(msg, args.webhook)
        print(result)
    else:
        print("未发现任何小说项目")


if __name__ == "__main__":
    main()
