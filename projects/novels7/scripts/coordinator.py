#!/usr/bin/env python3
"""
Coordinator - 小说生成统一调度器 (novels7版)
协调 Planner -> Writer -> Reviewer -> Rewrite 四个阶段
支持断点续传、配额监控、统一微信推送
"""
import concurrent.futures
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from novels.core.config import NovelConfig
from novels.core.notifier import WeChatNotifier

PROJECT_DIR = Path("D:/AiProject/Node/projects/novels7")
OUTPUT_DIR = PROJECT_DIR / "output"
DRAFT_DIR = OUTPUT_DIR / "chapters/draft"
FINAL_DIR = OUTPUT_DIR / "chapters/final"
REVIEWS_DIR = OUTPUT_DIR / "reviews"
OUTLINE_CHAPTERS_DIR = OUTPUT_DIR / "outline_chapters"
WORLD_FILE = OUTPUT_DIR / "world.json"
OUTLINE_FILE = OUTPUT_DIR / "outline.json"
CHARACTERS_FILE = OUTPUT_DIR / "characters.json"
CONFIG_FILE = PROJECT_DIR / "config.json"
LOG_FILE = OUTPUT_DIR / "logs/coordinator.log"

SCRIPTS_DIR = Path(__file__).parent


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_script(script_name: str, *args) -> int:
    """运行子Agent脚本"""
    script_path = SCRIPTS_DIR / script_name
    cmd = [sys.executable, str(script_path)] + list(args)
    log(f"[Coordinator] 执行: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=False, text=True, encoding="utf-8")
    return result.returncode


def check_all_quotas():
    """检查所有MiniMax API配额"""
    config = NovelConfig.load(CONFIG_FILE)
    result = subprocess.run(
        ["node", config.mmx_path, "quota", "show", "--quiet", "--output", "json"],
        capture_output=True, text=True, encoding="utf-8"
    )
    quotas = {}
    if result.returncode == 0:
        try:
            quota_list = json.loads(result.stdout)
            for item in quota_list.get("model_remains", []):
                name = item.get("model_name", "unknown")
                used = item.get("current_interval_usage_count", 0)
                limit = item.get("current_interval_total_count", 0)
                quotas[name] = {"used": used, "limit": limit, "remaining": limit - used}
        except Exception as e:
            log(f"[Coordinator] 配额解析失败: {e}")
    if not quotas:
        quotas["MiniMax-M*"] = {"used": 0, "limit": 4500, "remaining": 4500}
    return quotas


def load_progress():
    """加载进度"""
    progress_file = OUTPUT_DIR / "progress.json"
    if progress_file.exists():
        with open(progress_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "planner_done": False,
        "writer_done": False,
        "reviewer_done": False,
        "rewrite_done": False,
        "last_generated_chapter": 0,
        "last_reviewed_chapter": 0,
        "failed_chapters": [],
        "rewrite_queue": []
    }


def save_progress(progress):
    """保存进度"""
    progress_file = OUTPUT_DIR / "progress.json"
    progress_file.parent.mkdir(parents=True, exist_ok=True)
    with open(progress_file, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def collect_stats():
    """收集项目统计信息"""
    stats = {
        "outline": 0, "draft": 0, "review": 0, "final": 0,
        "words": 0, "score": 0.0
    }
    if OUTLINE_CHAPTERS_DIR.exists():
        stats["outline"] = len(list(OUTLINE_CHAPTERS_DIR.glob("chapter_*.json")))
    if DRAFT_DIR.exists():
        draft_files = list(DRAFT_DIR.glob("chapter_*.txt"))
        stats["draft"] = len(draft_files)
        for f in draft_files:
            try:
                stats["words"] += len(f.read_text(encoding="utf-8"))
            except:
                pass
    if REVIEWS_DIR.exists():
        review_files = list(REVIEWS_DIR.glob("*_review.json"))
        stats["review"] = len(review_files)
        scores = []
        for f in review_files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                s = data.get("overall_score", 0)
                if s > 0:
                    scores.append(s)
            except:
                pass
        if scores:
            stats["score"] = sum(scores) / len(scores)
    if FINAL_DIR.exists():
        stats["final"] = len(list(FINAL_DIR.glob("chapter_*.txt")))
    return stats


def push_progress(notifier, title: str, stats: dict, total: int, agents: int = 0):
    """推送统一格式进度到微信"""
    if not notifier:
        return
    msg = notifier.format_progress(
        title=title,
        outline=stats["outline"],
        draft=stats["draft"],
        review=stats["review"],
        final=stats["final"],
        total=total,
        total_words=stats["words"],
        score=stats["score"],
        agents=agents,
    )
    notifier.push(msg)


# ============================================================
# 阶段1: Planner
# ============================================================
def run_planner_phase(progress, config, notifier):
    """运行Planner阶段"""
    log("=" * 60)
    log("[Coordinator] 阶段1: Planner (大纲生成)")
    log("=" * 60)

    # 检查大纲是否完整
    outline_count = 0
    if OUTLINE_CHAPTERS_DIR.exists():
        outline_count = len(list(OUTLINE_CHAPTERS_DIR.glob("chapter_*.json")))

    if outline_count >= config.total_chapters and WORLD_FILE.exists() and CHARACTERS_FILE.exists():
        log(f"[Coordinator] 大纲已完整 ({outline_count}章)，跳过Planner")
        progress["planner_done"] = True
        save_progress(progress)
        return True

    log("[Coordinator] 启动 Planner Agent...")
    rc = run_script("planner.py")
    if rc != 0:
        log("[ERROR] Planner执行失败")
        return False

    progress["planner_done"] = True
    save_progress(progress)
    push_progress(notifier, config.title, collect_stats(), config.total_chapters)
    log("[Coordinator] Planner阶段完成")
    return True


# ============================================================
# 阶段2: Writer
# ============================================================
def run_writer_phase(progress, config, notifier):
    """运行Writer阶段"""
    log("=" * 60)
    log("[Coordinator] 阶段2: Writer (初稿生成)")
    log("=" * 60)

    if progress.get("writer_done"):
        draft_count = 0
        if DRAFT_DIR.exists():
            draft_count = len(list(DRAFT_DIR.glob("chapter_*.txt")))
        if draft_count >= config.total_chapters:
            log(f"[Coordinator] 初稿已完整 ({draft_count}章)，跳过Writer")
            return True

    # 使用 writer_scheduler.py 批量生成
    log("[Coordinator] 启动 Writer Scheduler...")
    rc = run_script("writer_scheduler.py", "--parallel", "30", "--per-agent", "2",
                    "--push-interval", str(config.coordinator.push_interval_seconds))
    if rc != 0:
        log("[WARNING] Writer Scheduler返回非零退出码")

    # 检查实际完成数
    draft_count = 0
    if DRAFT_DIR.exists():
        draft_count = len(list(DRAFT_DIR.glob("chapter_*.txt")))

    if draft_count >= config.total_chapters:
        progress["writer_done"] = True
        save_progress(progress)
        push_progress(notifier, config.title, collect_stats(), config.total_chapters)
        log(f"[Coordinator] Writer阶段完成 ({draft_count}章)")
        return True
    else:
        log(f"[WARNING] 初稿不完整 ({draft_count}/{config.total_chapters})")
        return False


# ============================================================
# 阶段3: Reviewer
# ============================================================
def run_reviewer_phase(progress, config, notifier):
    """运行Reviewer阶段"""
    log("=" * 60)
    log("[Coordinator] 阶段3: Reviewer (审查评分)")
    log("=" * 60)

    if progress.get("reviewer_done"):
        review_count = 0
        if REVIEWS_DIR.exists():
            review_count = len(list(REVIEWS_DIR.glob("*_review.json")))
        if review_count >= config.total_chapters:
            log(f"[Coordinator] 审查已完整 ({review_count}章)，跳过Reviewer")
            return True

    log("[Coordinator] 启动 Reviewer Scheduler...")
    rc = run_script("reviewer_scheduler.py", "--parallel", "20", "--per-agent", "2",
                    "--push-interval", str(config.coordinator.push_interval_seconds))
    if rc != 0:
        log("[WARNING] Reviewer Scheduler返回非零退出码")

    review_count = 0
    if REVIEWS_DIR.exists():
        review_count = len(list(REVIEWS_DIR.glob("*_review.json")))

    if review_count >= config.total_chapters:
        progress["reviewer_done"] = True
        save_progress(progress)
        push_progress(notifier, config.title, collect_stats(), config.total_chapters)
        log(f"[Coordinator] Reviewer阶段完成 ({review_count}章)")
        return True
    else:
        log(f"[WARNING] 审查不完整 ({review_count}/{config.total_chapters})")
        return False


# ============================================================
# 阶段4: Rewrite
# ============================================================
def run_rewrite_phase(progress, config, notifier):
    """运行Rewrite阶段"""
    log("=" * 60)
    log("[Coordinator] 阶段4: Rewrite (终稿生成)")
    log("=" * 60)

    if progress.get("rewrite_done"):
        final_count = 0
        if FINAL_DIR.exists():
            final_count = len(list(FINAL_DIR.glob("chapter_*.txt")))
        if final_count >= config.total_chapters:
            log(f"[Coordinator] 终稿已完整 ({final_count}章)，跳过Rewrite")
            return True

    log("[Coordinator] 启动 Rewrite Agent (--all)...")
    rc = run_script("rewrite_agent.py", "--all", "--workers", "20")
    if rc != 0:
        log("[WARNING] Rewrite Agent返回非零退出码")

    final_count = 0
    if FINAL_DIR.exists():
        final_count = len(list(FINAL_DIR.glob("chapter_*.txt")))

    if final_count >= config.total_chapters:
        progress["rewrite_done"] = True
        save_progress(progress)
        push_progress(notifier, config.title, collect_stats(), config.total_chapters)
        log(f"[Coordinator] Rewrite阶段完成 ({final_count}章)")
        return True
    else:
        log(f"[WARNING] 终稿不完整 ({final_count}/{config.total_chapters})")
        return False


# ============================================================
# 主控
# ============================================================
def main():
    print("=" * 70)
    print("  《凡尘逆仙》小说生成 - 统一Coordinator")
    print("=" * 70)

    config = NovelConfig.load(CONFIG_FILE)
    notifier = None
    if config.coordinator.wechat_webhook:
        notifier = WeChatNotifier(
            webhook_url=config.coordinator.wechat_webhook,
            min_interval=config.coordinator.push_interval_seconds,
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    FINAL_DIR.mkdir(parents=True, exist_ok=True)
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)

    progress = load_progress()
    log(f"[Coordinator] 当前进度: Planner={progress.get('planner_done')}, "
        f"Writer={progress.get('writer_done')}, "
        f"Reviewer={progress.get('reviewer_done')}, "
        f"Rewrite={progress.get('rewrite_done')}")

    # 阶段1: Planner
    if not progress.get("planner_done"):
        if not run_planner_phase(progress, config, notifier):
            log("[ERROR] Planner阶段失败，Coordinator终止")
            return
    else:
        log("[Coordinator] Planner已跳过")

    # 阶段2: Writer
    if not progress.get("writer_done"):
        if not run_writer_phase(progress, config, notifier):
            log("[WARNING] Writer阶段未完成，等待下次启动")
            return
    else:
        log("[Coordinator] Writer已跳过")

    # 阶段3: Reviewer
    if not progress.get("reviewer_done"):
        if not run_reviewer_phase(progress, config, notifier):
            log("[WARNING] Reviewer阶段未完成，等待下次启动")
            return
    else:
        log("[Coordinator] Reviewer已跳过")

    # 阶段4: Rewrite
    if not progress.get("rewrite_done"):
        if not run_rewrite_phase(progress, config, notifier):
            log("[WARNING] Rewrite阶段未完成，等待下次启动")
            return
    else:
        log("[Coordinator] Rewrite已跳过")

    # 全部完成
    stats = collect_stats()
    log("=" * 60)
    log("[Coordinator] 全部阶段已完成！")
    log(f"[Coordinator] 大纲: {stats['outline']}/2000")
    log(f"[Coordinator] 初稿: {stats['draft']}/2000 ({stats['words']:,}字)")
    log(f"[Coordinator] 审查: {stats['review']}/2000 (评分{stats['score']:.2f})")
    log(f"[Coordinator] 终稿: {stats['final']}/2000")
    log("=" * 60)

    if notifier:
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
        notifier.push(msg + "\n🎉 全部完成！")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("[Coordinator] 用户中断，保存进度...")
        progress = load_progress()
        save_progress(progress)
        log("[Coordinator] 进度已保存，可断点续传")
    except Exception as e:
        log(f"[ERROR] Coordinator异常: {e}")
        import traceback
        log(traceback.format_exc())
