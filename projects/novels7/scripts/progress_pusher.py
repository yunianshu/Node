#!/usr/bin/env python3
"""
进度推送服务 - 统一格式版
持续监控项目进度并通过企业微信推送
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from novels.core.config import NovelConfig
from novels.core.notifier import WeChatNotifier


def collect_stats(config: NovelConfig):
    """收集项目统计信息"""
    stats = {
        "outline": 0,
        "draft": 0,
        "review": 0,
        "final": 0,
        "words": 0,
        "score": 0.0,
    }

    # 大纲
    outline_dir = config.output_dir / "outline_chapters"
    if outline_dir.exists():
        stats["outline"] = len(list(outline_dir.glob("chapter_*.json")))

    # 初稿 + 字数
    if config.draft_dir.exists():
        draft_files = list(config.draft_dir.glob("chapter_*.txt"))
        stats["draft"] = len(draft_files)
        for f in draft_files:
            try:
                stats["words"] += len(f.read_text(encoding="utf-8"))
            except:
                pass

    # 审查
    review_dir = config.output_dir / "reviews"
    if review_dir.exists():
        stats["review"] = len(list(review_dir.glob("*_review.json")))

    # 终稿
    final_dir = config.output_dir / "chapters/final"
    if final_dir.exists():
        stats["final"] = len(list(final_dir.glob("chapter_*.txt")))

    # 平均评分
    scores = []
    if review_dir.exists():
        for f in review_dir.glob("*_review.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                s = data.get("overall_score", 0)
                if s > 0:
                    scores.append(s)
            except:
                pass
    if scores:
        stats["score"] = sum(scores) / len(scores)

    return stats


def main():
    config_path = Path(__file__).parent.parent / "config.json"
    config = NovelConfig.load(config_path)

    notifier = WeChatNotifier(
        webhook_url=config.coordinator.wechat_webhook,
        min_interval=config.coordinator.push_interval_seconds,
    )

    print(f"[{config.title}] 推送服务启动，每{config.coordinator.push_interval_seconds}秒推送一次")

    last_progress = ""
    total = config.total_chapters

    while True:
        time.sleep(config.coordinator.push_interval_seconds)

        stats = collect_stats(config)

        # 构建统一格式消息
        msg = notifier.format_progress(
            title=config.title,
            outline=stats["outline"],
            draft=stats["draft"],
            review=stats["review"],
            final=stats["final"],
            total=total,
            total_words=stats["words"],
            score=stats["score"],
        )

        # 只在进度变化时推送
        if msg == last_progress:
            continue
        last_progress = msg

        notifier.push(msg)
        print(f"[{time.strftime('%H:%M:%S')}] 推送进度: 初稿{stats['draft']}/{total}")


if __name__ == "__main__":
    main()
