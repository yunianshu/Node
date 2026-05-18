#!/usr/bin/env python3
"""
Writer Scheduler - 持续并行调度器
维持固定数量的并行writer agent，每个agent只生成2章后退出
支持微信进度推送
"""
import subprocess
import sys
import time
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from novels.core.config import NovelConfig
from novels.core.notifier import WeChatNotifier


PROJECT_DIR = Path("D:/AiProject/Node/projects/novels7")
DRAFT_DIR = PROJECT_DIR / "output/chapters/draft"
SCRIPT = PROJECT_DIR / "scripts/writer.py"
CONFIG = PROJECT_DIR / "config.json"
LOG_FILE = PROJECT_DIR / "logs/writer_scheduler.log"


def log(msg):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


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

    outline_dir = config.output_dir / "outline_chapters"
    if outline_dir.exists():
        stats["outline"] = len(list(outline_dir.glob("chapter_*.json")))

    if config.draft_dir.exists():
        draft_files = list(config.draft_dir.glob("chapter_*.txt"))
        stats["draft"] = len(draft_files)
        for f in draft_files:
            try:
                stats["words"] += len(f.read_text(encoding="utf-8"))
            except:
                pass

    review_dir = config.output_dir / "reviews"
    if review_dir.exists():
        stats["review"] = len(list(review_dir.glob("*_review.json")))

    final_dir = config.output_dir / "chapters/final"
    if final_dir.exists():
        stats["final"] = len(list(final_dir.glob("chapter_*.txt")))

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


def get_missing_chapters():
    """获取缺失的章节列表"""
    existing = set()
    if DRAFT_DIR.exists():
        for f in DRAFT_DIR.glob("chapter_*.txt"):
            try:
                num = int(f.stem.split("_")[1])
                if f.stat().st_size > 1000:
                    existing.add(num)
            except (ValueError, IndexError):
                pass
    return [i for i in range(1, 2001) if i not in existing]


def run_writer(chapter):
    """运行writer.py生成单章"""
    cmd = [sys.executable, str(SCRIPT), "--chapter", str(chapter)]
    start = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=300)
        elapsed = time.time() - start
        if result.returncode == 0:
            log(f"[OK] 第{chapter}章完成 ({elapsed:.1f}s)")
            return chapter, "success", elapsed
        else:
            err = result.stderr[-200:] if result.stderr else "unknown"
            log(f"[FAIL] 第{chapter}章失败: {err}")
            return chapter, "failed", elapsed
    except subprocess.TimeoutExpired:
        log(f"[TIMEOUT] 第{chapter}章超时")
        return chapter, "timeout", 300
    except Exception as e:
        log(f"[ERROR] 第{chapter}章异常: {e}")
        return chapter, "error", 0


def worker_task(chapters):
    """一个worker任务：顺序生成2章"""
    results = []
    for ch in chapters:
        result = run_writer(ch)
        results.append(result)
    return results


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--parallel", type=int, default=15, help="并行agent数量（默认15）")
    parser.add_argument("--per-agent", type=int, default=2, help="每个agent生成的章节数（默认2）")
    parser.add_argument("--push-interval", type=int, default=300, help="微信推送间隔秒数（默认300=5分钟）")
    parser.add_argument("--no-push", action="store_true", help="禁用微信推送")
    args = parser.parse_args()

    config = NovelConfig.load(CONFIG)
    notifier = None
    if not args.no_push and config.coordinator.wechat_webhook:
        notifier = WeChatNotifier(
            webhook_url=config.coordinator.wechat_webhook,
            min_interval=args.push_interval,
        )

    log("=" * 60)
    log(f"Writer Scheduler 启动 | 并行: {args.parallel} | 每agent: {args.per_agent}章")
    log("=" * 60)

    total_success = 0
    total_failed = 0
    start_time = time.time()
    last_push = 0
    last_progress_msg = ""

    while True:
        missing = get_missing_chapters()
        if not missing:
            log("全部章节已生成完毕！")
            break

        batch = missing[:args.parallel * args.per_agent]
        if not batch:
            break

        tasks = []
        for i in range(0, len(batch), args.per_agent):
            chunk = batch[i:i + args.per_agent]
            tasks.append(chunk)

        log(f"本轮调度: {len(tasks)}个agent, 共{len(batch)}章, 剩余缺失{len(missing)}章")

        with ThreadPoolExecutor(max_workers=args.parallel) as executor:
            futures = {executor.submit(worker_task, task): task for task in tasks}
            for future in as_completed(futures):
                results = future.result()
                for ch, status, elapsed in results:
                    if status == "success":
                        total_success += 1
                    else:
                        total_failed += 1

        elapsed_total = time.time() - start_time
        rate = total_success / (elapsed_total / 60) if elapsed_total > 0 else 0
        log(f"累计: 成功{total_success} | 失败{total_failed} | 速率{rate:.1f}章/分钟")

        # 微信推送（使用统一格式）
        if notifier and time.time() - last_push >= args.push_interval:
            stats = collect_stats(config)
            msg = notifier.format_progress(
                title=config.title,
                outline=stats["outline"],
                draft=stats["draft"],
                review=stats["review"],
                final=stats["final"],
                total=config.total_chapters,
                total_words=stats["words"],
                score=stats["score"],
                agents=args.parallel,
            )
            if msg != last_progress_msg:
                notifier.push(msg)
                last_progress_msg = msg
            last_push = time.time()

        time.sleep(1)

    # 最终推送
    if notifier:
        stats = collect_stats(config)
        msg = notifier.format_progress(
            title=config.title,
            outline=stats["outline"],
            draft=stats["draft"],
            review=stats["review"],
            final=stats["final"],
            total=config.total_chapters,
            total_words=stats["words"],
            score=stats["score"],
            agents=0,
        )
        notifier.push(msg + "\n[完成] Writer 阶段全部结束！")

    log("=" * 60)
    log(f"Scheduler 结束 | 成功: {total_success} | 失败: {total_failed}")
    log("=" * 60)


if __name__ == "__main__":
    main()
