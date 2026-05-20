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

from core.novel_config import configure_stdio, get_webhook_url, load_config
from core.push_notifier import (
    push_progress as _push_progress,
    push_stage_event as _push_stage_event,
    push_task_complete as _push_task_complete,
    push_interrupted as _push_interrupted,
    push_error as _push_error,
)
from core.workflow_state import (
    highest_contiguous,
    outline_completed_count,
    outline_index_path,
    outlines_complete,
    report_path,
    review_dir,
    scan_chapter_status,
    write_status_file,
)
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
    REVIEWS_DIR = review_dir(NOVELS_DIR)
    LOGS_DIR = NOVELS_DIR / "logs"
    WORLD_FILE = NOVELS_DIR / "world.json"
    OUTLINE_FILE = outline_index_path(NOVELS_DIR)
    CHARACTERS_FILE = NOVELS_DIR / "characters.json"
    PROGRESS_FILE = report_path(NOVELS_DIR, "progress.json")
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


def get_progress_summary():
    statuses = scan_chapter_status(NOVELS_DIR, 1, CONFIG["total_chapters"])
    total_words = sum(s.draft_words for s in statuses.values() if s.draft_exists)
    draft_count = sum(1 for s in statuses.values() if s.draft_exists)
    reviewed = sum(1 for s in statuses.values() if s.review_ok)
    final_count = sum(1 for s in statuses.values() if s.final_ok)

    outline_count = outline_completed_count(NOVELS_DIR, 1, CONFIG["total_chapters"])

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
        ok = _push_progress(
            config=CONFIG,
            title=title,
            outline=p["outline"],
            draft=p["draft"],
            reviewed=p["reviewed"],
            final=p["final"],
            total_words=p["total_words"],
            total_chapters=CONFIG["total_chapters"],
            active_writers=_active_writers,
        )
        status = "已推送" if ok else "跳过(无webhook)"
        log(f"[WeChat] 进度{status}: 初稿{p['draft']}/{CONFIG['total_chapters']}, 审查{p['reviewed']}/{CONFIG['total_chapters']}")


def notify_stage(stage: str, status: str, start: int | None = None, end: int | None = None,
                 processed: int = 0, failed: int = 0, error: str = "") -> None:
    """记录阶段事件（不再推送微信，由 progress_pusher_thread 定时推送）。"""
    scope = f" 第{start}-{end}章" if start is not None and end is not None else ""
    extra = f", 成功{processed}章" if processed > 0 else ""
    extra += f", 失败{failed}章" if failed > 0 else ""
    extra += f", 错误: {error}" if error else ""
    log(f"[{stage}] {status}{scope}{extra}")


def run_streaming_process(cmd: list[str], child_log: Path) -> int:
    """运行子进程，并将输出同时写入子日志和 Coordinator CLI 日志。"""
    child_log.parent.mkdir(parents=True, exist_ok=True)
    with open(child_log, "a", encoding="utf-8") as lf:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                text = line.rstrip("\n")
                lf.write(text + "\n")
                lf.flush()
                if text:
                    log(f"[Child] {text}")
        finally:
            proc.stdout.close()
        return proc.wait()


def _stream_child_output(proc: subprocess.Popen, child_log: Path) -> None:
    child_log.parent.mkdir(parents=True, exist_ok=True)
    with open(child_log, "a", encoding="utf-8") as lf:
        if proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                text = line.rstrip("\n")
                lf.write(text + "\n")
                lf.flush()
                if text:
                    log(f"[Child] {text}")
        finally:
            proc.stdout.close()


def run_script(script_name: str, *args) -> int:
    script_file = resolve_script_path(script_name)
    cmd = [sys.executable, str(script_file), "--project", str(NOVELS_DIR)] + list(args)
    log(f"[Coordinator] 执行: {' '.join(cmd)}")
    child_log = LOGS_DIR / f"coordinator_{Path(script_name).stem}.log"
    return run_streaming_process(cmd, child_log)


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
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
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
    count = outline_completed_count(NOVELS_DIR, 1, CONFIG["total_chapters"])
    log(f"[Coordinator] 当前单章大纲: {count}/{CONFIG['total_chapters']} 章")
    return outlines_complete(NOVELS_DIR, 1, CONFIG["total_chapters"])


def check_base_files_exist():
    return WORLD_FILE.exists() and CHARACTERS_FILE.exists()


def run_planner():
    log("=" * 60)
    log("[Coordinator] 启动 Planner Agent")
    log("=" * 60)
    notify_stage("世界观/角色", "开始")
    rc = run_script("planner.py")
    notify_stage("世界观/角色", "完成" if rc == 0 else "异常", error="" if rc == 0 else f"退出码 {rc}")
    return rc


def run_outliner():
    log("=" * 60)
    log("[Coordinator] 启动 Outliner Agent")
    log("=" * 60)
    notify_stage("大纲", "开始")
    rc = run_script("outliner.py")
    notify_stage("大纲", "完成" if rc == 0 else "异常", error="" if rc == 0 else f"退出码 {rc}")
    return rc


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


def adjust_workers_by_quota(batch_size: int, text_remaining: int, skip_review: bool) -> tuple[int, int]:
    """根据剩余API配额动态调整Writer和Reviewer并发数

    策略：
    - 每章Writer约1次调用，Reviewer约1次，Rewrite约1次
    - 如果配额紧张，按比例缩减并发，避免触发限流
    - 最少保持1个worker，最多不超过默认值
    - 返回值: (writer_workers, reviewer_workers)
    """
    global num_workers, review_workers, default_num_workers, default_review_workers

    calls_per_chapter = 1 if skip_review else 2  # writer + reviewer
    calls_needed = batch_size * calls_per_chapter

    if calls_needed <= 0 or text_remaining <= 0:
        return 1, 1

    # 配额充足，使用默认配置
    if text_remaining >= calls_needed * 3:
        return default_num_workers, default_review_workers

    # 配额紧张，按比例缩减
    ratio = text_remaining / (calls_needed * 1.5)  # 留50%余量给重试和rewrite
    ratio = max(0.1, min(1.0, ratio))

    new_writer = max(1, int(default_num_workers * ratio))
    new_reviewer = max(1, int(default_review_workers * ratio))

    # 如果当前值已经更低（被失败率调低），取较小值
    new_writer = min(new_writer, num_workers if num_workers > 0 else default_num_workers)
    new_reviewer = min(new_reviewer, review_workers if review_workers > 0 else default_review_workers)

    if new_writer != num_workers or new_reviewer != review_workers:
        log(f"[Coordinator] 配额紧张（剩余{text_remaining}，需约{calls_needed}），"
            f"并发调整 Writer={num_workers}->{new_writer}, Reviewer={review_workers}->{new_reviewer} "
            f"(比例{ratio:.0%})")
        num_workers = new_writer
        review_workers = new_reviewer

    return new_writer, new_reviewer


def collect_failed_writer_chapters(writer_results: list, statuses: dict, start: int, end: int) -> list[int]:
    failed = set()
    for s, e, rc in writer_results:
        if rc != 0:
            failed.update(range(max(start, s), min(end, e) + 1))
    for ch in range(start, end + 1):
        status = statuses.get(ch)
        if status is None or not status.draft_ok:
            failed.add(ch)
    return sorted(failed)


def run_writer_repair_queue(chapters: list[int], max_passes: int = 2) -> list[int]:
    remaining = sorted(set(chapters))
    if not remaining:
        return []

    for attempt in range(1, max_passes + 1):
        log(f"[Coordinator] Writer补偿第{attempt}/{max_passes}轮，待补齐 {len(remaining)} 章: {remaining}")
        # 并行补偿：传入整个区间，writer.py 内部会跳过已存在的有效章节
        # 使用当前动态并发数（由配额和失败率共同控制），不再强制单章单进程
        # 补偿队列为内部机制，不发送企业微信阶段通知
        run_parallel_agents("writer.py", min(remaining), max(remaining), notify=False)

        statuses = scan_chapter_status(NOVELS_DIR, min(remaining), max(remaining))
        remaining = [chapter for chapter in remaining if not statuses.get(chapter) or not statuses[chapter].draft_ok]
        if not remaining:
            log("[Coordinator] Writer补偿完成，失败章节已补齐")
            return []

        time.sleep(CONFIG["coordinator"]["pause_between_batches"])

    log(f"[WARNING] Writer补偿后仍未完成章节: {remaining}")
    return remaining


def run_parallel_agents(agent_name: str, start: int, end: int, agent_workers: int = None, notify: bool = True) -> list:
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
    stage_name = "初稿" if agent_name == "writer.py" else "审查" if agent_name == "reviewer.py" else Path(agent_name).stem
    if notify:
        notify_stage(stage_name, "开始", start, end)

    if agent_name == "writer.py":
        _active_writers = len(ranges)

    procs = []
    stream_threads = []
    script_file = resolve_script_path(agent_name)
    for s, e in ranges:
        cmd = [sys.executable, str(script_file), "--project", str(NOVELS_DIR), "--start", str(s), "--end", str(e)]
        log(f"[Coordinator] 执行: {' '.join(cmd)}")
        try:
            child_log = LOGS_DIR / f"{Path(agent_name).stem}_{s:04d}_{e:04d}.log"
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            thread = threading.Thread(target=_stream_child_output, args=(proc, child_log), daemon=True)
            thread.start()
            stream_threads.append(thread)
            procs.append((s, e, proc))
        except Exception as exc:
            log(f"[Coordinator] {agent_name} {s}-{e} 启动失败: {exc}")
            procs.append((s, e, None))

    results = []
    active = [(s, e, p) for s, e, p in procs if p is not None]
    while active:
        new_active = []
        for s, e, proc in active:
            try:
                ret = proc.poll()
                if ret is not None:
                    results.append((s, e, ret))
                    log(f"[Coordinator] {agent_name} {s}-{e} 完成 (rc={ret})")
                else:
                    new_active.append((s, e, proc))
            except Exception as exc:
                log(f"[Coordinator] {agent_name} {s}-{e} 轮询异常: {exc}")
                results.append((s, e, -1))
        active = new_active
        if active:
            time.sleep(3)

    for thread in stream_threads:
        thread.join(timeout=5)

    for s, e, p in procs:
        if p is None:
            results.append((s, e, -1))

    if agent_name == "writer.py":
        _active_writers = 0

    # 动态调整并发数
    adjust_workers(agent_name, results)
    success_count = sum(1 for _, _, rc in results if rc == 0)
    failed_count = len(results) - success_count
    if notify:
        notify_stage(
            stage_name,
            "完成" if failed_count == 0 else "异常",
            start,
            end,
            processed=success_count,
            failed=failed_count,
            error="" if failed_count == 0 else f"{failed_count} 个子进程失败",
        )

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


def all_reviews_finished(statuses: dict, start: int, end: int) -> bool:
    for ch in range(start, end + 1):
        status = statuses.get(ch)
        if not status or not status.review_exists or status.review_status != "completed":
            return False
    return True


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

    report_file = report_path(NOVELS_DIR, "summary_report.json")
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
        push_interval = int(CONFIG["coordinator"].get("push_interval_seconds", 120))
        pusher = threading.Thread(target=progress_pusher_thread, args=(push_interval,), daemon=True)
        pusher.start()
        log(f"[Coordinator] 企业微信进度推送已启动（每{push_interval}秒）")
    else:
        log("[Coordinator] 企业微信进度推送线程已存在，跳过")

    progress = load_progress()
    log(f"[Coordinator] 当前进度: 已生成 {progress['last_generated_chapter']} 章，已审查 {progress['last_reviewed_chapter']} 章")

    batch_size = args.batch_size or CONFIG["coordinator"]["batch_size"]
    end_chapter = args.end or CONFIG["total_chapters"]

    need_planner = not args.skip_planner and not check_base_files_exist()
    if need_planner:
        log("[Coordinator] 检测到世界观或角色档案缺失，启动Planner...")
        if run_planner() != 0:
            log("[ERROR] Planner执行失败，请检查日志")
            return
        progress["planner_done"] = True
        save_progress(progress)
    elif check_base_files_exist():
        log("[Coordinator] 世界观、角色档案已存在")
        progress["planner_done"] = True
        save_progress(progress)

    if not progress["planner_done"]:
        log("[ERROR] Planner未完成且跳过标志未设置")
        return

    if not check_outline_complete():
        if args.skip_planner:
            log("[ERROR] 大纲未完成且设置了 --skip-planner，停止后续 Writer/Reviewer/Rewrite")
            return
        if args.planner_parallel:
            log("[Coordinator] 使用批量Outliner并行生成大纲...")
            notify_stage("大纲", "开始", 1, CONFIG["total_chapters"])
            rc = run_script("planner_parallel.py")
            outline_count = outline_completed_count(NOVELS_DIR, 1, CONFIG["total_chapters"])
            notify_stage(
                "大纲",
                "完成" if rc == 0 else "异常",
                1,
                CONFIG["total_chapters"],
                processed=outline_count,
                failed=max(0, CONFIG["total_chapters"] - outline_count),
                error="" if rc == 0 else f"退出码 {rc}",
            )
        else:
            rc = run_outliner()
        if rc != 0:
            log("[ERROR] Outliner执行失败，请检查日志")
            return
        if not check_outline_complete():
            log("[ERROR] 大纲仍未完成，停止后续 Writer/Reviewer/Rewrite")
            return

    actual_outline_count = outline_completed_count(NOVELS_DIR, 1, CONFIG["total_chapters"])
    existing_statuses = scan_chapter_status(NOVELS_DIR, args.start, end_chapter)
    write_status_file(NOVELS_DIR, existing_statuses.values())
    first_invalid = next(
        (ch for ch in range(args.start, end_chapter + 1) if not existing_statuses[ch].draft_ok),
        end_chapter + 1,
    )
    start_chapter = max(args.start, first_invalid)

    log(f"[Coordinator] 生成范围: 第{start_chapter}-{end_chapter}章（单章大纲共{actual_outline_count}章），批次大小: {batch_size}")

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

        # 根据配额动态调整并发数
        writer_workers, reviewer_workers = adjust_workers_by_quota(
            batch_end - batch_start + 1, text_remaining, args.skip_review
        )

        log(f"[Coordinator] ===== 开始第 {batch_start}-{batch_end} 章 =====")

        writer_results = run_parallel_agents("writer.py", batch_start, batch_end, agent_workers=writer_workers, notify=False)
        failed_writers = [r for r in writer_results if r[2] != 0]
        if failed_writers:
            for s, e, rc in failed_writers:
                log(f"[WARNING] Writer Agent {s}-{e} 返回非零退出码: {rc}")

        batch_statuses = scan_chapter_status(NOVELS_DIR, args.start, end_chapter)
        write_status_file(NOVELS_DIR, batch_statuses.values())
        failed_writer_chapters = collect_failed_writer_chapters(writer_results, batch_statuses, batch_start, batch_end)
        if failed_writer_chapters:
            failed_writer_chapters = run_writer_repair_queue(failed_writer_chapters)
            batch_statuses = scan_chapter_status(NOVELS_DIR, args.start, end_chapter)
            write_status_file(NOVELS_DIR, batch_statuses.values())
        progress["failed_chapters"] = failed_writer_chapters
        progress["last_generated_chapter"] = highest_contiguous(batch_statuses, args.start, "draft_ok")
        save_progress(progress)

        if failed_writer_chapters:
            log(f"[WARNING] 第 {batch_start}-{batch_end} 章存在未完成初稿，跳过本批 reviewer: {failed_writer_chapters}")

        if not args.skip_review and not failed_writer_chapters:
            reviewer_results = run_parallel_agents("reviewer.py", batch_start, batch_end, agent_workers=reviewer_workers)
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

    final_review_statuses = scan_chapter_status(NOVELS_DIR, args.start, end_chapter)
    if progress["rewrite_queue"] and not all_reviews_finished(final_review_statuses, args.start, end_chapter):
        log("[Coordinator] 审查未全部完成，暂不执行 Rewrite")

    if progress["rewrite_queue"] and not args.rewrite_only and all_reviews_finished(final_review_statuses, args.start, end_chapter):
        log("=" * 60)
        log(f"[Coordinator] 处理重写队列: {len(progress['rewrite_queue'])} 章")
        log("=" * 60)
        notify_stage("终稿重写", "开始", min(progress["rewrite_queue"]), max(progress["rewrite_queue"]))
        rc = run_script("rewrite_agent.py")
        log(f"[Coordinator] Rewrite Agent 完成 (rc={rc})")
        notify_stage(
            "终稿重写",
            "完成" if rc == 0 else "异常",
            args.start,
            end_chapter,
            processed=len(progress["rewrite_queue"]) if rc == 0 else 0,
            failed=0 if rc == 0 else len(progress["rewrite_queue"]),
            error="" if rc == 0 else f"退出码 {rc}",
        )

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
    _push_task_complete(
        config=CONFIG,
        title=title,
        total_chapters=CONFIG["total_chapters"],
        total_words=report["total_words"],
        avg_score=report["average_score"],
        rewrite_count=report["rewrite_count"],
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
        _push_interrupted(config=CONFIG, title=title, reason="用户中断")
    except Exception as e:
        log(f"[ERROR] 发生异常: {e}")
        import traceback
        log(traceback.format_exc())
        title = get_book_title()
        _push_error(config=CONFIG, title=title, error=str(e))
