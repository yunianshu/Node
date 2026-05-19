#!/usr/bin/env python3
"""
Coordinator - 小说生成主控协调器 (60 Agent 版本)
协调 Planner、Writer、Reviewer 三个 Agent 的工作
支持断点续传、配额监控、进度追踪、企业微信Webhook推送
"""

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))


import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
import urllib.request
import urllib.error

from core.novel_config import configure_stdio, get_webhook_url, load_config
from core.workflow_state import highest_contiguous, scan_chapter_status, write_status_file
from tool_paths import script_path as resolve_script_path

configure_stdio()

NOVELS_DIR = None
MMX_CLI_PATH = "C:/Users/Administrator/AppData/Roaming/npm/node_modules/mmx-cli/dist/mmx.mjs"
CHAPTERS_DIR = None
REVIEWS_DIR = None
LOGS_DIR = None
WORLD_FILE = None
OUTLINE_FILE = None
CHARACTERS_FILE = None
PROGRESS_FILE = None
LOG_FILE = None
CONFIG = None
SCRIPTS_DIR = None
WECHAT_WEBHOOK = ""

_progress_lock = threading.Lock()
_last_push_time = 0
_active_writers = 0
_pusher_started = False

# 动态并发控制
default_num_workers = None
num_workers = None
default_review_workers = None
review_workers = None


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, CHAPTERS_DIR, REVIEWS_DIR, LOGS_DIR, WORLD_FILE, OUTLINE_FILE
    global CHARACTERS_FILE, PROGRESS_FILE, LOG_FILE, CONFIG, SCRIPTS_DIR, WECHAT_WEBHOOK
    global default_num_workers, num_workers, default_review_workers, review_workers
    NOVELS_DIR = Path(project_dir).resolve()
    CHAPTERS_DIR = NOVELS_DIR / "chapters" / "draft"
    REVIEWS_DIR = NOVELS_DIR / "reviews"
    LOGS_DIR = NOVELS_DIR / "logs"
    WORLD_FILE = NOVELS_DIR / "world.json"
    OUTLINE_FILE = NOVELS_DIR / "outline.json"
    CHARACTERS_FILE = NOVELS_DIR / "characters.json"
    PROGRESS_FILE = NOVELS_DIR / "progress.json"
    LOG_FILE = LOGS_DIR / "coordinator.log"
    CONFIG = load_config(NOVELS_DIR)
    SCRIPTS_DIR = TOOLS_ROOT
    WECHAT_WEBHOOK = get_webhook_url(CONFIG)

    # 初始化动态并发控制
    default_num_workers = CONFIG["coordinator"].get("num_workers", 5)
    num_workers = default_num_workers
    default_review_workers = CONFIG["coordinator"].get("review_workers", 2)
    review_workers = default_review_workers


def get_book_title():
    if WORLD_FILE.exists():
        try:
            with open(WORLD_FILE, "r", encoding="utf-8") as f:
                world = json.load(f)
            return world.get("title", "书生武道通神")
        except Exception:
            pass
    return "书生武道通神"


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def push_wechat(msg: str):
    if not WECHAT_WEBHOOK:
        return
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
    statuses = scan_chapter_status(NOVELS_DIR, 1, CONFIG["total_chapters"])
    total_words = sum(s.draft_words for s in statuses.values() if s.draft_exists)
    draft_count = sum(1 for s in statuses.values() if s.draft_exists)
    reviewed = sum(1 for s in statuses.values() if s.review_ok)
    final_count = sum(1 for s in statuses.values() if s.final_ok)

    outline_count = 0
    if OUTLINE_FILE.exists():
        try:
            with open(OUTLINE_FILE, "r", encoding="utf-8") as f:
                o = json.load(f)
            outline_count = len(o.get("chapters", []))
        except Exception:
            pass

    return {
        "outline": outline_count,
        "draft": draft_count,
        "reviewed": reviewed,
        "final": final_count,
        "total_words": total_words
    }


def progress_pusher_thread(interval_seconds: int = 120):
    global _last_push_time, _active_writers
    while True:
        time.sleep(interval_seconds)
        with _progress_lock:
            now = time.time()
            if now - _last_push_time < interval_seconds:
                continue
            _last_push_time = now

        p = get_progress_summary()
        title = get_book_title()
        writer_count = _active_writers
        separator = "━━━━━━━━━━━━━━━━━━━━"
        msg = (
            f"📖 《{title}》生成进度 ({time.strftime('%Y-%m-%d %H:%M:%S')})\n"
            f"{separator}\n"
            f"📋 大纲: {p['outline']}/{CONFIG['total_chapters']} 章\n"
            f"✍ 初稿: {p['draft']}/{CONFIG['total_chapters']} 章\n"
            f"📝 字数: {p['total_words']:,}\n"
            f"🔍 审查: {p['reviewed']}/{CONFIG['total_chapters']} 章\n"
            f"📤 终稿: {p['final']}/{CONFIG['total_chapters']} 章\n"
            f"🤖 进程: 1 Coordinator + {writer_count} Writer\n"
            f"{separator}"
        )
        push_wechat(msg)
        log(f"[WeChat] 进度已推送: 初稿{p['draft']}/{CONFIG['total_chapters']}, 审查{p['reviewed']}/{CONFIG['total_chapters']}")


def run_script(script_name: str, *args) -> int:
    script_file = resolve_script_path(script_name)
    cmd = [sys.executable, str(script_file), "--project", str(NOVELS_DIR)] + list(args)
    log(f"[Coordinator] 执行: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=False, text=True, encoding="utf-8")
    return result.returncode


def check_all_quotas():
    result = subprocess.run(
        ["node", MMX_CLI_PATH, "quota", "show", "--quiet", "--output", "json"],
        capture_output=True, text=True, encoding="utf-8"
    )
    quotas = {}
    if result.returncode == 0:
        try:
            quota_list = json.loads(result.stdout)
            if isinstance(quota_list, list):
                for item in quota_list:
                    if isinstance(item, dict) and "model_name" in item:
                        name = item.get("model_name", "unknown")
                        used = item.get("current_interval_usage_count", 0)
                        limit = item.get("current_interval_total_count", 0)
                        remaining = limit - used
                        quotas[name] = {"used": used, "limit": limit, "remaining": remaining}
                        log(f"[Coordinator] 配额 {name}: {used}/{limit}, 剩余 {remaining}")
            elif isinstance(quota_list, dict) and "model_remains" in quota_list:
                for item in quota_list["model_remains"]:
                    name = item.get("model_name", "unknown")
                    used = item.get("current_interval_usage_count", 0)
                    limit = item.get("current_interval_total_count", 0)
                    remaining = limit - used
                    quotas[name] = {"used": used, "limit": limit, "remaining": remaining}
                    log(f"[Coordinator] 配额 {name}: {used}/{limit}, 剩余 {remaining}")
        except Exception as e:
            log(f"[Coordinator] 配额解析失败: {e}")
    if not quotas:
        log("[Coordinator] 无法获取配额信息，默认继续")
        quotas["MiniMax-M*"] = {"used": 0, "limit": 4500, "remaining": 4500}
    return quotas


def check_quota():
    quotas = check_all_quotas()
    for name, info in quotas.items():
        if "MiniMax-M" in name or "M*" in name:
            return info["remaining"]
    return quotas.get("MiniMax-M*", {}).get("remaining", 1000)


def load_progress():
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "planner_done": False,
        "last_generated_chapter": 0,
        "last_reviewed_chapter": 0,
        "failed_chapters": [],
        "rewrite_queue": []
    }


def save_progress(progress):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def get_completed_chapters():
    completed = []
    if CHAPTERS_DIR.exists():
        for f in CHAPTERS_DIR.glob("chapter_*.txt"):
            try:
                num = int(f.stem.split("_")[1])
                if f.stat().st_size > 1000:
                    completed.append(num)
            except (ValueError, IndexError):
                pass
    return sorted(completed)


def check_outline_complete():
    if not OUTLINE_FILE.exists():
        return False
    try:
        with open(OUTLINE_FILE, "r", encoding="utf-8") as f:
            outline = json.load(f)
        count = len(outline.get("chapters", []))
        log(f"[Coordinator] 当前大纲: {count}/{CONFIG['total_chapters']} 章")
        return count >= CONFIG["total_chapters"]
    except Exception:
        return False


def check_all_files_exist():
    return WORLD_FILE.exists() and OUTLINE_FILE.exists() and CHARACTERS_FILE.exists()


def run_planner():
    log("=" * 60)
    log("[Coordinator] 启动 Planner Agent")
    log("=" * 60)
    return run_script("planner.py")


def run_writer_batch(start: int, end: int) -> list:
    log("=" * 60)
    log(f"[Coordinator] 启动 Writer Agent: 第{start}-{end}章")
    log("=" * 60)
    return run_script("writer.py", "--start", str(start), "--end", str(end))


def run_reviewer_batch(start: int, end: int) -> list:
    log("=" * 60)
    log(f"[Coordinator] 启动 Reviewer Agent: 第{start}-{end}章")
    log("=" * 60)
    return run_script("reviewer.py", "--start", str(start), "--end", str(end))


def adjust_workers(agent_name: str, results: list):
    """根据失败率动态调整并发数"""
    global num_workers, review_workers, default_num_workers, default_review_workers
    total = len(results)
    if total == 0:
        return
    failures = sum(1 for _, _, rc in results if rc != 0)
    failure_rate = failures / total

    if agent_name == "writer.py":
        current = num_workers
        max_workers = default_num_workers
        if failure_rate > 0.3 and current > 1:
            num_workers = max(1, current - 2)
            log(f"[Coordinator] Writer 失败率 {failure_rate:.0%}，并发从 {current} 降至 {num_workers}")
        elif failure_rate < 0.05 and current < max_workers:
            num_workers = min(max_workers, current + 1)
            log(f"[Coordinator] Writer 失败率 {failure_rate:.0%}，并发从 {current} 升至 {num_workers}")
    elif agent_name == "reviewer.py":
        current = review_workers
        max_workers = default_review_workers
        if failure_rate > 0.3 and current > 1:
            review_workers = max(1, current - 2)
            log(f"[Coordinator] Reviewer 失败率 {failure_rate:.0%}，并发从 {current} 降至 {review_workers}")
        elif failure_rate < 0.05 and current < max_workers:
            review_workers = min(max_workers, current + 1)
            log(f"[Coordinator] Reviewer 失败率 {failure_rate:.0%}，并发从 {current} 升至 {review_workers}")


def run_parallel_agents(agent_name: str, start: int, end: int, agent_workers: int = None) -> list:
    global _active_writers, num_workers, review_workers
    total = end - start + 1
    if total <= 0:
        return []

    # 使用动态并发数
    if agent_workers is not None:
        use_workers = agent_workers
    elif agent_name == "writer.py":
        use_workers = num_workers
    elif agent_name == "reviewer.py":
        use_workers = review_workers
    else:
        use_workers = 2

    chunk_size = max(1, total // use_workers)
    ranges = []
    for i in range(use_workers):
        s = start + i * chunk_size
        e = min(s + chunk_size - 1, end)
        if s > end:
            break
        ranges.append((s, e))

    log(f"[Coordinator] 并行启动 {len(ranges)} 个 {agent_name} 实例 (并发={use_workers})")

    if agent_name == "writer.py":
        _active_writers = len(ranges)

    procs = []
    script_file = resolve_script_path(agent_name)
    for s, e in ranges:
        cmd = [sys.executable, str(script_file), "--project", str(NOVELS_DIR), "--start", str(s), "--end", str(e)]
        log(f"[Coordinator] 执行: {' '.join(cmd)}")
        try:
            child_log = LOGS_DIR / f"{Path(agent_name).stem}_{s:04d}_{e:04d}.log"
            lf = open(child_log, "a", encoding="utf-8")
            proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT)
            procs.append((s, e, proc, lf))
        except Exception as exc:
            log(f"[Coordinator] {agent_name} {s}-{e} 启动失败: {exc}")
            procs.append((s, e, None, None))

    results = []
    active = [(s, e, p, lf) for s, e, p, lf in procs if p is not None]
    while active:
        new_active = []
        for s, e, proc, lf in active:
            try:
                ret = proc.poll()
                if ret is not None:
                    if lf:
                        lf.close()
                    results.append((s, e, ret))
                    log(f"[Coordinator] {agent_name} {s}-{e} 完成 (rc={ret})")
                else:
                    new_active.append((s, e, proc, lf))
            except Exception as exc:
                if lf:
                    lf.close()
                log(f"[Coordinator] {agent_name} {s}-{e} 轮询异常: {exc}")
                results.append((s, e, -1))
        active = new_active
        if active:
            time.sleep(3)

    for s, e, p, lf in procs:
        if p is None:
            results.append((s, e, -1))

    if agent_name == "writer.py":
        _active_writers = 0

    # 动态调整并发数
    adjust_workers(agent_name, results)

    return results


def check_rewrites(start: int, end: int) -> list:
    rewrite_list = []
    for ch in range(start, end + 1):
        review_file = REVIEWS_DIR / f"chapter_{ch:04d}_review.json"
        if review_file.exists():
            with open(review_file, "r", encoding="utf-8") as f:
                review = json.load(f)
            verdict = review.get("verdict", "")
            score = review.get("overall_score", 10)
            if verdict == "需重写" or score < 7:
                rewrite_list.append(ch)
                log(f"[Coordinator] 第{ch}章评分{score}， verdict: {verdict}，标记为需重写")
    return rewrite_list


def generate_summary_report():
    log("=" * 60)
    log("[Coordinator] 生成总结报告")
    log("=" * 60)

    statuses = scan_chapter_status(NOVELS_DIR, 1, CONFIG["total_chapters"])
    completed = [ch for ch, status in statuses.items() if status.final_ok]
    total_words = sum(status.draft_words for status in statuses.values() if status.draft_exists)
    review_scores = [status.review_score for status in statuses.values() if status.review_score is not None]
    rewrite_count = sum(1 for status in statuses.values() if status.review_exists and not status.review_ok)

    report = {
        "total_chapters": len(completed),
        "target_chapters": CONFIG["total_chapters"],
        "total_words": total_words,
        "average_words_per_chapter": total_words // len(completed) if completed else 0,
        "average_score": sum(review_scores) / len(review_scores) if review_scores else 0,
        "rewrite_count": rewrite_count,
        "completed_chapters": completed
    }

    report_file = NOVELS_DIR / "summary_report.json"
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    log(f"[Coordinator] 总结报告: 已完成 {report['total_chapters']}/{CONFIG['total_chapters']} 章")
    log(f"[Coordinator] 总字数: {report['total_words']:,} 字")
    log(f"[Coordinator] 平均评分: {report['average_score']:.2f}")
    log(f"[Coordinator] 需重写: {rewrite_count} 章")
    return report


def main():
    parser = argparse.ArgumentParser(description="小说生成协调器")
    parser.add_argument("--project", "-p", type=str,
                        default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录（默认从环境变量 NOVEL_PROJECT_DIR 读取）")
    parser.add_argument("--batch-size", type=int, default=0, help="每批生成的章节数")
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=0, help="结束章节")
    parser.add_argument("--skip-planner", action="store_true", help="跳过Planner阶段")
    parser.add_argument("--skip-review", action="store_true", help="跳过Review阶段")
    parser.add_argument("--rewrite-only", action="store_true", help="只运行重写")
    parser.add_argument("--planner-parallel", action="store_true", help="使用60 Agent并行生成大纲")
    args = parser.parse_args()

    if not args.project:
        print("错误: 必须指定 --project 或设置 NOVEL_PROJECT_DIR 环境变量")
        sys.exit(1)

    init_project(args.project)

    print("=" * 70)
    print("  小说Agent系统 - Coordinator")
    print(f"  项目: {NOVELS_DIR}")
    print("=" * 70)

    NOVELS_DIR.mkdir(parents=True, exist_ok=True)
    CHAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    global _pusher_started
    if not _pusher_started:
        _pusher_started = True
        pusher = threading.Thread(target=progress_pusher_thread, args=(120,), daemon=True)
        pusher.start()
        log("[Coordinator] 企业微信进度推送已启动（每2分钟）")
    else:
        log("[Coordinator] 企业微信进度推送线程已存在，跳过")

    progress = load_progress()
    log(f"[Coordinator] 当前进度: 已生成 {progress['last_generated_chapter']} 章，已审查 {progress['last_reviewed_chapter']} 章")

    batch_size = args.batch_size or CONFIG["coordinator"]["batch_size"]
    end_chapter = args.end or CONFIG["total_chapters"]

    need_planner = not args.skip_planner and not check_all_files_exist()
    if need_planner:
        log("[Coordinator] 检测到必要文件缺失，启动Planner...")
        if args.planner_parallel:
            log("[Coordinator] 使用60 Agent并行生成大纲...")
            rc = run_script("planner_parallel.py")
            if rc != 0:
                log("[ERROR] Planner并行执行失败，请检查日志")
                return
        else:
            if run_planner() != 0:
                log("[ERROR] Planner执行失败，请检查日志")
                return
        progress["planner_done"] = True
        save_progress(progress)
    elif check_all_files_exist():
        outline_count = 0
        if OUTLINE_FILE.exists():
            try:
                with open(OUTLINE_FILE, "r", encoding="utf-8") as f:
                    o = json.load(f)
                outline_count = len(o.get("chapters", []))
            except Exception:
                pass
        log(f"[Coordinator] 世界观、大纲({outline_count}章)、角色档案已存在")
        progress["planner_done"] = True
        save_progress(progress)

    if not progress["planner_done"]:
        log("[ERROR] Planner未完成且跳过标志未设置")
        return

    actual_outline_count = 0
    if OUTLINE_FILE.exists():
        try:
            with open(OUTLINE_FILE, "r", encoding="utf-8") as f:
                o = json.load(f)
            actual_outline_count = max(ch.get("chapter_number", 0) for ch in o.get("chapters", [])) if o.get("chapters") else 0
        except Exception:
            pass

    end_chapter = min(end_chapter, actual_outline_count) if actual_outline_count > 0 else end_chapter
    existing_statuses = scan_chapter_status(NOVELS_DIR, args.start, end_chapter)
    write_status_file(NOVELS_DIR, existing_statuses.values())
    first_invalid = next(
        (ch for ch in range(args.start, end_chapter + 1) if not existing_statuses[ch].draft_ok),
        end_chapter + 1,
    )
    start_chapter = max(args.start, first_invalid)

    log(f"[Coordinator] 生成范围: 第{start_chapter}-{end_chapter}章（大纲共{actual_outline_count}章），批次大小: {batch_size}")

    for batch_start in range(start_chapter, end_chapter + 1, batch_size):
        batch_end = min(batch_start + batch_size - 1, end_chapter)

        all_quotas = check_all_quotas()
        text_remaining = check_quota()

        current_batch_size = batch_end - batch_start + 1
        calls_needed = current_batch_size * (1 if args.skip_review else 2)

        if text_remaining < calls_needed:
            log(f"[Coordinator] 文本配额不足（剩余{text_remaining}，需要{calls_needed}）")
            other_quotas = []
            for name, info in all_quotas.items():
                if name != "MiniMax-M*" and info["remaining"] > 0:
                    other_quotas.append(f"{name}: {info['remaining']}/{info['limit']}")
            if other_quotas:
                log(f"[Coordinator] 其他可用配额: {', '.join(other_quotas)}")
                log(f"[Coordinator] 注意：其他API（语音/视频/音乐等）无法生成文本内容，需等待文本配额重置")
            log(f"[Coordinator] 等待配额重置...")
            wait_minutes = 12
            log(f"[Coordinator] 等待 {wait_minutes} 分钟...")
            time.sleep(wait_minutes * 60)
            all_quotas = check_all_quotas()
            text_remaining = check_quota()
            if text_remaining < calls_needed:
                log(f"[Coordinator] 配额仍然不足，本次批次暂停，下次启动将从第{batch_start}章继续")
                break
            remaining_quota = check_quota()
            if remaining_quota < batch_size * 3:
                log("[Coordinator] 配额仍然不足，生成暂停")
                break

        log(f"[Coordinator] ===== 开始第 {batch_start}-{batch_end} 章 =====")

        writer_results = run_parallel_agents("writer.py", batch_start, batch_end)
        failed_writers = [r for r in writer_results if r[2] != 0]
        if failed_writers:
            for s, e, rc in failed_writers:
                log(f"[WARNING] Writer Agent {s}-{e} 返回非零退出码: {rc}")

        batch_statuses = scan_chapter_status(NOVELS_DIR, args.start, end_chapter)
        write_status_file(NOVELS_DIR, batch_statuses.values())
        progress["last_generated_chapter"] = highest_contiguous(batch_statuses, args.start, "draft_ok")
        save_progress(progress)

        if not args.skip_review:
            reviewer_results = run_parallel_agents("reviewer.py", batch_start, batch_end)
            failed_reviewers = [r for r in reviewer_results if r[2] != 0]
            if failed_reviewers:
                for s, e, rc in failed_reviewers:
                    log(f"[WARNING] Reviewer Agent {s}-{e} 返回非零退出码: {rc}")

            rewrites = check_rewrites(batch_start, batch_end)
            queue = set(progress.get("rewrite_queue", []))
            queue.update(rewrites)
            progress["rewrite_queue"] = sorted(queue)

            reviewed_statuses = scan_chapter_status(NOVELS_DIR, args.start, end_chapter)
            write_status_file(NOVELS_DIR, reviewed_statuses.values())
            progress["last_reviewed_chapter"] = highest_contiguous(reviewed_statuses, args.start, "review_ok")
            save_progress(progress)

        if batch_start % (batch_size * 10) == 1 or batch_end == end_chapter:
            generate_summary_report()

        pause_seconds = CONFIG["coordinator"]["pause_between_batches"]
        log(f"[Coordinator] 第 {batch_start}-{batch_end} 章完成，暂停{pause_seconds}秒...")
        time.sleep(pause_seconds)

    if progress["rewrite_queue"] and not args.rewrite_only:
        log("=" * 60)
        log(f"[Coordinator] 处理重写队列: {len(progress['rewrite_queue'])} 章")
        log("=" * 60)
        rc = run_script("rewrite_agent.py")
        log(f"[Coordinator] Rewrite Agent 完成 (rc={rc})")

        progress["rewrite_queue"] = []
        final_statuses = scan_chapter_status(NOVELS_DIR, args.start, end_chapter)
        write_status_file(NOVELS_DIR, final_statuses.values())
        save_progress(progress)

    report = generate_summary_report()
    log("=" * 60)
    log("[Coordinator] 全部任务完成")
    log(f"[Coordinator] 总进度: {report['total_chapters']}/{CONFIG['total_chapters']} 章")
    log(f"[Coordinator] 总字数: {report['total_words']:,} 字")
    log("=" * 60)

    title = get_book_title()
    separator = "━━━━━━━━━━━━━━━━━━━━"
    push_wechat(
        f"📖 《{title}》生成任务完成! ({time.strftime('%Y-%m-%d %H:%M:%S')})\n"
        f"{separator}\n"
        f"📋 大纲: {CONFIG['total_chapters']}/{CONFIG['total_chapters']} 章\n"
        f"✍ 初稿: {report['total_chapters']}/{CONFIG['total_chapters']} 章\n"
        f"📝 字数: {report['total_words']:,}\n"
        f"🔍 审查: {report['total_chapters']}/{CONFIG['total_chapters']} 章\n"
        f"📤 终稿: {report['total_chapters']}/{CONFIG['total_chapters']} 章\n"
        f"⭐ 平均评分: {report['average_score']:.2f}\n"
        f"{separator}"
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("[Coordinator] 用户中断，保存进度...")
        progress = load_progress()
        save_progress(progress)
        log("[Coordinator] 进度已保存，可断点续传")
        title = get_book_title()
        separator = "━━━━━━━━━━━━━━━━━━━━"
        push_wechat(
            f"⚠️ 《{title}》生成任务中断 ({time.strftime('%Y-%m-%d %H:%M:%S')})\n"
            f"{separator}\n"
            f"进度已保存，可断点续传\n"
            f"{separator}"
        )
    except Exception as e:
        log(f"[ERROR] 发生异常: {e}")
        import traceback
        log(traceback.format_exc())
        title = get_book_title()
        separator = "━━━━━━━━━━━━━━━━━━━━"
        push_wechat(
            f"❌ 《{title}》生成任务异常 ({time.strftime('%Y-%m-%d %H:%M:%S')})\n"
            f"{separator}\n"
            f"错误: {str(e)[:200]}\n"
            f"{separator}"
        )
