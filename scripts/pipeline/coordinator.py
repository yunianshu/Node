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
import re
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
    load_outline_review_status,
    load_review_status,
    outline_chapter_path,
    outline_completed_count,
    outline_index_path,
    outline_review_dir,
    outlines_complete,
    report_path,
    review_dir,
    scan_chapter_status,
    write_status_file,
)
configure_stdio()

PIPELINE_SCRIPTS = {
    "planner.py": TOOLS_ROOT / "pipeline" / "planner.py",
    "outliner.py": TOOLS_ROOT / "pipeline" / "outliner.py",
    "outline_reviewer.py": TOOLS_ROOT / "pipeline" / "outline_reviewer.py",
    "writer.py": TOOLS_ROOT / "pipeline" / "writer.py",
    "reviewer.py": TOOLS_ROOT / "pipeline" / "reviewer.py",
    "media_generator.py": TOOLS_ROOT / "pipeline" / "media_generator.py",
}

MAINTENANCE_SCRIPTS = {
    "wechat_pusher_lane.py": TOOLS_ROOT / "maintenance" / "wechat_pusher_lane.py",
}


def resolve_script_path(script_name: str) -> Path:
    try:
        return PIPELINE_SCRIPTS[script_name]
    except KeyError as exc:
        raise ValueError(f"Unsupported pipeline script: {script_name}") from exc

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
    global NOVELS_DIR, CHAPTERS_DIR, REVIEWS_DIR, OUTLINE_REVIEW_DIR, LOGS_DIR, WORLD_FILE, OUTLINE_FILE
    global CHARACTERS_FILE, PROGRESS_FILE, LOG_FILE, CONFIG, SCRIPTS_DIR, WECHAT_WEBHOOK
    global default_num_workers, num_workers, default_review_workers, review_workers
    NOVELS_DIR = Path(project_dir).resolve()
    CHAPTERS_DIR = NOVELS_DIR / "chapters" / "draft"
    REVIEWS_DIR = review_dir(NOVELS_DIR)
    OUTLINE_REVIEW_DIR = outline_review_dir(NOVELS_DIR)
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
    outline_reviewed = sum(1 for s in statuses.values() if s.outline_review_ok)

    outline_count = outline_completed_count(NOVELS_DIR, 1, CONFIG["total_chapters"])

    return {
        "outline": outline_count,
        "outline_reviewed": outline_reviewed,
        "draft": draft_count,
        "reviewed": reviewed,
        "final": final_count,
        "total_words": total_words
    }


def progress_pusher_thread(interval_seconds: int = 120):
    """跨进程单例的进度推送线程：使用文件锁确保只有第一个 Coordinator 进程推送。"""
    global _last_push_time, _active_writers
    import os as _os

    def _process_exists(pid_text: str) -> bool:
        try:
            pid = int(str(pid_text).strip())
        except (TypeError, ValueError):
            return False
        if pid <= 0:
            return False
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            return result.returncode == 0 and str(pid) in result.stdout
        try:
            _os.kill(pid, 0)
            return True
        except OSError:
            return False

    # 文件锁：跨进程单例
    lock_dir = NOVELS_DIR / "logs"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / "wechat_pusher.lock"
    try:
        fd = _os.open(str(lock_file), _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY)
        _os.write(fd, str(_os.getpid()).encode("utf-8"))
    except FileExistsError:
        lock_pid = lock_file.read_text().strip() if lock_file.exists() else "?"
        if lock_file.exists() and not _process_exists(lock_pid):
            try:
                lock_file.unlink()
                log(f"[WeChat] 清理过期推送锁（PID {lock_pid}）")
                fd = _os.open(str(lock_file), _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY)
                _os.write(fd, str(_os.getpid()).encode("utf-8"))
            except Exception as exc:
                log(f"[WeChat] 清理过期推送锁失败: {exc}")
                return
        else:
            # 已有别的进程在推
            log(f"[WeChat] 已有其他 Coordinator 进程在推送（PID {lock_pid}），本进程跳过")
            return
    try:
        _os.close(fd)
    except Exception:
        pass
    try:
        lock_file.write_text(str(_os.getpid()), encoding="utf-8")
    except Exception:
        pass
    log(f"[WeChat] 本进程获得推送锁 (PID {_os.getpid()})，开始每{interval_seconds}秒推送")

    while True:
        time.sleep(interval_seconds)

        p = get_progress_summary()
        title = get_book_title()
        ok = _push_progress(
            config=CONFIG,
            title=title,
            outline=p["outline"],
            outline_reviewed=p["outline_reviewed"],
            draft=p["draft"],
            reviewed=p["reviewed"],
            final=p["final"],
            total_words=p["total_words"],
            total_chapters=CONFIG["total_chapters"],
            active_writers=_active_writers,
        )
        status = "已推送" if ok else "跳过(无webhook)"
        log(f"[WeChat] 进度{status}: 大纲审{p["outline_reviewed"]}/{CONFIG["total_chapters"]}, 初稿{p["draft"]}/{CONFIG["total_chapters"]}, 审查{p["reviewed"]}/{CONFIG["total_chapters"]}")


def ensure_wechat_pusher_process(interval_seconds: int | None = None) -> None:
    """启动独立企业微信推送进程；实际单例由推送进程自己的 lock 文件保证。"""
    if NOVELS_DIR is None or CONFIG is None:
        return
    script = MAINTENANCE_SCRIPTS["wechat_pusher_lane.py"]
    if not script.exists():
        log(f"[WeChat] 推送脚本不存在，跳过: {script}")
        return
    interval = interval_seconds or int(CONFIG["coordinator"].get("push_interval_seconds", 120))
    cmd = [
        sys.executable,
        str(script),
        "--project",
        str(NOVELS_DIR),
        "--interval",
        str(interval),
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        subprocess.Popen(
            cmd,
            cwd=str(TOOLS_ROOT.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        log(f"[WeChat] 已确保独立进度推送进程运行（每{interval}秒）")
    except Exception as exc:
        log(f"[WeChat] 启动独立进度推送进程失败: {exc}")


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
        "last_outline_reviewed_chapter": 0,
        "failed_chapters": [],
        "rewrite_queue": [],
        "outline_rewrite_queue": []
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


def check_outline_complete(start: int = 1, end: int | None = None):
    end = end if end is not None else CONFIG["total_chapters"]
    count = outline_completed_count(NOVELS_DIR, start, end)
    log(f"[Coordinator] 当前单章大纲: {count}/{end - start + 1} 章 (请求范围 {start}-{end})")
    return outlines_complete(NOVELS_DIR, start, end)


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


def media_prompts_ready() -> bool:
    if not WORLD_FILE.exists():
        return False
    try:
        world = json.loads(WORLD_FILE.read_text(encoding="utf-8"))
    except Exception:
        return False
    prompts = world.get("media_prompts", {})
    if not isinstance(prompts, dict):
        return False
    return all(prompts.get(key) for key in ("cover_prompt", "video_prompt", "song_prompt", "song_lyrics"))


def media_assets_ready() -> bool:
    image_exts = {".png", ".jpg", ".jpeg", ".webp"}
    cover = any(
        path.is_file() and path.suffix.lower() in image_exts
        for path in (NOVELS_DIR / "media" / "images").glob("cover*")
    )
    video = (NOVELS_DIR / "media" / "videos" / "world_video.mp4").exists()
    song = any((NOVELS_DIR / "media" / "music").glob("theme_song.*"))
    return cover and video and song


def run_media_generator():
    if not CONFIG.get("media", {}).get("enabled", True):
        log("[Coordinator] media.enabled=false，跳过媒体生成")
        return 0
    if media_assets_ready():
        log("[Coordinator] 封面、世界观视频、主题歌已存在，跳过媒体生成")
        return 0

    log("=" * 60)
    log("[Coordinator] 启动 Media Generator: 封面/世界观视频/主题歌")
    log("=" * 60)
    notify_stage("媒体资产", "开始")
    rc = run_script("media_generator.py")
    notify_stage("媒体资产", "完成" if rc == 0 else "异常", error="" if rc == 0 else f"退出码 {rc}")
    return rc


def run_outliner():
    log("=" * 60)
    log("[Coordinator] 启动 Outliner Agent")
    log("=" * 60)
    notify_stage("大纲", "开始")
    rc = run_script("outliner.py", "--fill-gaps")
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


def summarize_agent_results(results: list[tuple[int, int, int]]) -> tuple[int, int]:
    """兼容旧测试与旧调用方：按章节范围汇总处理量与失败量。"""
    processed = 0
    failed = 0
    for start, end, rc in results:
        span = max(0, end - start + 1)
        if rc != 0:
            failed += span
        else:
            processed += span
    return processed, failed


def check_rewrites(start: int, end: int) -> list:
    rewrite_list = []
    for ch in range(start, end + 1):
        review_file = REVIEWS_DIR / f"chapter_{ch:04d}_review.json"
        if review_file.exists():
            with open(review_file, "r", encoding="utf-8") as f:
                review = json.load(f)
            verdict = str(review.get("verdict", ""))
            score_raw = review.get("overall_score", 10)
            try:
                score = float(score_raw)
            except (TypeError, ValueError):
                score = 10.0
            threshold = float(CONFIG.get("reviewer", {}).get("min_score", 7.0))
            if verdict == "需重写" or score < threshold:
                rewrite_list.append(ch)
                log(f"[Coordinator] 第{ch}章评分{score}， verdict: {verdict}，标记为需重写")
    return rewrite_list


def run_outline_reviewer_batch(start: int, end: int) -> list:
    log("=" * 60)
    log(f"[Coordinator] 启动 Outline Reviewer Agent: 第{start}-{end}章")
    log("=" * 60)
    return run_parallel_agents("outline_reviewer.py", start, end)


def check_outline_rewrites(start: int, end: int) -> list:
    rewrite_list = []
    min_score = float(CONFIG.get("outline_reviewer", {}).get("min_score", 8.5))
    for ch in range(start, end + 1):
        review_file = OUTLINE_REVIEW_DIR / f"chapter_{ch:04d}_review.json"
        exists, status, score, ok = load_outline_review_status(review_file, min_score)
        if exists and not ok:
            rewrite_list.append(ch)
            log(f"[Coordinator] 第{ch}章大纲评分{score}（门槛{min_score}），状态{status}，标记为需重生成")
    return rewrite_list


def run_outline_repair_queue(chapters: list[int], max_passes: int = 2) -> list[int]:
    remaining = sorted(set(chapters))
    if not remaining:
        return []

    for attempt in range(1, max_passes + 1):
        log(f"[Coordinator] Outline补偿第{attempt}/{max_passes}轮，待补齐 {len(remaining)} 章")

        # 收集旧审查意见作为反馈，然后删除旧报告，确保新大纲会被重新审查。
        review_feedback_data = {}
        for ch in remaining:
            outline_file = outline_chapter_path(NOVELS_DIR, ch)
            review_file = OUTLINE_REVIEW_DIR / f"chapter_{ch:04d}_review.json"
            if outline_file.exists():
                outline_file.unlink()
                log(f"[Coordinator] 删除第{ch}章不合格大纲")
            if review_file.exists():
                try:
                    review_data = json.loads(review_file.read_text(encoding="utf-8"))
                    feedback = {
                        "chapter": ch,
                        "overall_score": review_data.get("overall_score"),
                        "verdict": review_data.get("verdict"),
                        "weaknesses": review_data.get("weaknesses", []),
                        "suggestions": review_data.get("suggestions", []),
                        "continuity_issues": review_data.get("continuity_issues", []),
                        "summary": review_data.get("summary", ""),
                    }
                    review_feedback_data[str(ch)] = feedback
                except Exception:
                    pass
                try:
                    review_file.unlink()
                    log(f"[Coordinator] 删除第{ch}章旧大纲审查报告，等待重审")
                except Exception as exc:
                    log(f"[WARNING] 删除第{ch}章旧大纲审查报告失败: {exc}")

        # 将审查意见写入临时文件
        review_feedback_file = NOVELS_DIR / "logs" / "outline_review_feedback.json"
        review_feedback_file.parent.mkdir(parents=True, exist_ok=True)
        review_feedback_file.write_text(json.dumps(review_feedback_data, ensure_ascii=False, indent=2), encoding="utf-8")

        # 使用 fill-gaps 模式重新生成缺失大纲，并传入审查意见
        rc = run_script(
            "outliner.py",
            "--start", str(min(remaining)),
            "--end", str(max(remaining)),
            "--fill-gaps",
            "--review-feedback", str(review_feedback_file),
        )
        if rc != 0:
            log(f"[WARNING] Outliner 填补失败 (rc={rc})")

        # 重新审查
        run_outline_reviewer_batch(min(remaining), max(remaining))

        # 检查还有多少不通过
        remaining = check_outline_rewrites(min(remaining), max(remaining))
        remaining = [ch for ch in remaining if ch in chapters]
        if not remaining:
            log("[Coordinator] 大纲补偿完成，所有章节已通过审查")
            return []

        time.sleep(CONFIG["coordinator"]["pause_between_batches"])

    log(f"[WARNING] 大纲补偿后仍有 {len(remaining)} 章未通过: {remaining}")
    return remaining


def all_reviews_finished(statuses: dict, start: int, end: int) -> bool:
    for ch in range(start, end + 1):
        status = statuses.get(ch)
        if not status or not status.review_exists or status.review_status != "completed":
            return False
    return True


def _load_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except Exception as exc:
        log(f"[WARNING] 删除文件失败 {path}: {exc}")


def _outline_review_file(chapter: int) -> Path:
    return OUTLINE_REVIEW_DIR / f"chapter_{chapter:04d}_review.json"


def _draft_file(chapter: int) -> Path:
    return CHAPTERS_DIR / f"chapter_{chapter:04d}.txt"


def _review_file(chapter: int) -> Path:
    return REVIEWS_DIR / f"chapter_{chapter:04d}_review.json"


def _final_file(chapter: int) -> Path:
    return NOVELS_DIR / "chapters" / "final" / f"chapter_{chapter:04d}.txt"


def _review_feedback(review_data: dict, chapter: int, gate: str, round_no: int, attempt: int) -> dict:
    return {
        "chapter": chapter,
        "gate": gate,
        "round": round_no,
        "attempt": attempt,
        "overall_score": review_data.get("overall_score"),
        "verdict": review_data.get("verdict"),
        "weaknesses": review_data.get("weaknesses", []),
        "suggestions": review_data.get("suggestions", []),
        "continuity_issues": review_data.get("continuity_issues", []),
        "summary": review_data.get("summary", ""),
    }


def _failure_analysis(chapter: int, gate: str, reviews: list[dict]) -> dict:
    scores = []
    weaknesses = []
    suggestions = []
    statuses = []
    for item in reviews:
        try:
            if item.get("overall_score") is not None:
                scores.append(float(item.get("overall_score")))
        except (TypeError, ValueError):
            pass
        statuses.append(str(item.get("status", "")))
        weaknesses.extend([str(v) for v in item.get("weaknesses", []) if v])
        suggestions.extend([str(v) for v in item.get("suggestions", []) if v])
    top_weaknesses = list(dict.fromkeys(weaknesses))[:8]
    top_suggestions = list(dict.fromkeys(suggestions))[:8]
    return {
        "chapter": chapter,
        "gate": gate,
        "attempts": len(reviews),
        "scores": scores,
        "best_score": max(scores) if scores else None,
        "statuses": statuses,
        "likely_reasons": top_weaknesses or statuses or ["no_valid_review"],
        "adjustments": top_suggestions or top_weaknesses or ["提高剧情完整度、人物动机、节奏和可写性"],
    }


def _write_gate_feedback(chapter: int, gate: str, reviews: list[dict], round_no: int) -> Path:
    analysis = _failure_analysis(chapter, gate, reviews)
    payload = {
        f"{chapter}": {
            "chapter": chapter,
            "gate": gate,
            "analysis_round": round_no,
            "failure_analysis": analysis,
            "reviews": reviews[-3:],
        }
    }
    feedback_file = LOGS_DIR / f"{gate}_feedback_ch{chapter:04d}_round{round_no}.json"
    feedback_file.parent.mkdir(parents=True, exist_ok=True)
    feedback_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return feedback_file


def _write_failure_report(chapter: int, gate: str, reviews: list[dict]) -> dict:
    report = _failure_analysis(chapter, gate, reviews)
    report["failed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    report_file = report_path(NOVELS_DIR, f"{gate}_failure_chapter_{chapter:04d}.json")
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _push_gate_failure(chapter: int, gate: str, report: dict) -> None:
    reason = "; ".join(str(item) for item in report.get("likely_reasons", [])[:5])
    message = (
        f"第{chapter}章{gate}三轮修正仍未通过。"
        f"最佳分数: {report.get('best_score')}; 原因: {reason}"
    )
    log(f"[ERROR] {message}")
    _push_error(config=CONFIG, title=get_book_title(), error=message)


def _outline_gate_passed(chapter: int) -> bool:
    min_score = float(CONFIG.get("outline_reviewer", {}).get("min_score", 8.5))
    _, _, _, ok = load_outline_review_status(_outline_review_file(chapter), min_score)
    return ok


def _draft_gate_passed(chapter: int) -> bool:
    min_score = float(CONFIG.get("reviewer", {}).get("min_score", 7.0))
    _, _, _, ok = load_review_status(_review_file(chapter), min_score)
    return ok


def _promote_draft_to_final(chapter: int) -> bool:
    source = _draft_file(chapter)
    target = _final_file(chapter)
    if not source.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
    log(f"[Coordinator] 第{chapter}章初稿审查通过，已写入终稿 -> {target}")
    return True


def process_outline_gate(chapter: int, *, push_on_failure: bool = True) -> bool:
    if outline_chapter_path(NOVELS_DIR, chapter).exists() and _outline_gate_passed(chapter):
        log(f"[Coordinator] 第{chapter}章大纲和大纲审已通过，跳过")
        return True

    max_rounds = int(CONFIG.get("coordinator", {}).get("outline_analysis_rounds", 3) or 3)
    attempts_per_round = int(CONFIG.get("coordinator", {}).get("outline_attempts_per_round", 3) or 3)
    reviews: list[dict] = []
    feedback_file: Path | None = None

    for round_no in range(1, max_rounds + 1):
        if reviews:
            feedback_file = _write_gate_feedback(chapter, "outline", reviews, round_no)
            log(f"[Coordinator] 第{chapter}章大纲进入第{round_no}轮原因调整: {feedback_file}")

        for attempt in range(1, attempts_per_round + 1):
            log(f"[Coordinator] 第{chapter}章大纲生成/初审 {round_no}.{attempt}")
            existing_review = _load_json_file(_outline_review_file(chapter))
            if existing_review:
                reviews.append(_review_feedback(existing_review, chapter, "outline", round_no, attempt))
                feedback_file = _write_gate_feedback(chapter, "outline", reviews, round_no)
                log(f"[Coordinator] 第{chapter}章读取现有大纲审查意见，反馈给本次重生成: {feedback_file}")
            _drop_text_artifacts(chapter, reason="大纲正在重生成")
            _safe_unlink(outline_chapter_path(NOVELS_DIR, chapter))
            _safe_unlink(_outline_review_file(chapter))

            args = ["--chapter", str(chapter)]
            if feedback_file:
                args += ["--review-feedback", str(feedback_file)]
            if len(reviews) >= 8:
                args.append("--rescue")
                log(f"[Coordinator] 第{chapter}章大纲累计失败{len(reviews)}次，启用卡章救援模式")
            if run_script("outliner.py", *args) != 0:
                reviews.append({
                    "chapter": chapter,
                    "status": "outliner_failed",
                    "weaknesses": ["大纲生成失败、JSON解析失败或结构字段不完整"],
                    "suggestions": ["重新生成时必须补齐summary、key_events、foreshadowing、power_progression等必填字段"],
                })
                feedback_file = _write_gate_feedback(chapter, "outline", reviews, round_no)
                log(f"[Coordinator] 第{chapter}章大纲生成失败，下一次重生成将使用反馈: {feedback_file}")
                continue
            if run_script("outline_reviewer.py", "--chapter", str(chapter)) != 0:
                reviews.append({
                    "chapter": chapter,
                    "status": "outline_reviewer_failed",
                    "weaknesses": ["大纲审查器执行失败"],
                    "suggestions": ["重新生成大纲并确保结构完整、剧情冲突明确、伏笔和能力进展具体"],
                })
                feedback_file = _write_gate_feedback(chapter, "outline", reviews, round_no)
                log(f"[Coordinator] 第{chapter}章大纲审查失败，下一次重生成将使用反馈: {feedback_file}")
                continue

            review_data = _load_json_file(_outline_review_file(chapter))
            reviews.append(_review_feedback(review_data, chapter, "outline", round_no, attempt))
            if _outline_gate_passed(chapter):
                log(f"[Coordinator] 第{chapter}章大纲初审通过")
                return True
            feedback_file = _write_gate_feedback(chapter, "outline", reviews, round_no)
            log(f"[Coordinator] 第{chapter}章大纲未通过，下一次重生成将使用反馈: {feedback_file}")

    report = _write_failure_report(chapter, "outline", reviews)
    if push_on_failure:
        _push_gate_failure(chapter, "大纲初审", report)
    return False


def process_draft_gate(chapter: int) -> bool:
    if _draft_file(chapter).exists() and _draft_gate_passed(chapter):
        return _promote_draft_to_final(chapter)

    max_rounds = int(CONFIG.get("coordinator", {}).get("draft_analysis_rounds", 3) or 3)
    attempts_per_round = int(CONFIG.get("coordinator", {}).get("draft_attempts_per_round", 3) or 3)
    reviews: list[dict] = []

    for round_no in range(1, max_rounds + 1):
        if reviews:
            feedback_file = _write_gate_feedback(chapter, "draft", reviews, round_no)
            log(f"[Coordinator] 第{chapter}章初稿进入第{round_no}轮原因调整: {feedback_file}")

        for attempt in range(1, attempts_per_round + 1):
            log(f"[Coordinator] 第{chapter}章初稿生成/审查 {round_no}.{attempt}")
            if run_script("writer.py", "--chapter", str(chapter)) != 0:
                reviews.append({"chapter": chapter, "status": "writer_failed"})
                continue
            _safe_unlink(_review_file(chapter))
            if run_script("reviewer.py", "--chapter", str(chapter)) != 0:
                reviews.append({"chapter": chapter, "status": "reviewer_failed"})
                continue
            review_data = _load_json_file(_review_file(chapter))
            reviews.append(_review_feedback(review_data, chapter, "draft", round_no, attempt))
            if _draft_gate_passed(chapter):
                return _promote_draft_to_final(chapter)

    report = _write_failure_report(chapter, "draft", reviews)
    _push_gate_failure(chapter, "初稿审查", report)
    return False


def _outline_lookahead_window(chapter: int, end: int, lookahead: int) -> tuple[int, int]:
    window = max(1, lookahead)
    return chapter, min(end, chapter + window - 1)


def _load_outline_failure_report(chapter: int) -> dict:
    return _load_json_file(report_path(NOVELS_DIR, f"outline_failure_chapter_{chapter:04d}.json"))


def _extract_later_overlap_chapter(chapter: int, report: dict) -> int | None:
    texts: list[str] = []
    for key in ("likely_reasons", "adjustments", "statuses"):
        values = report.get(key, [])
        if isinstance(values, list):
            texts.extend(str(item) for item in values if item)
        elif values:
            texts.append(str(values))

    candidate_chapters: list[int] = []
    overlap_markers = ("重叠", "重复", "冲突", "断裂", "冲突", "不自洽", "bug")
    for text in texts:
        if not any(marker in text for marker in overlap_markers):
            continue
        for match in re.finditer(r"第\s*(\d+)\s*章", text):
            try:
                target = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if target > chapter:
                candidate_chapters.append(target)
        for match in re.finditer(r"chapter[_\s-]?(\d+)", text, re.IGNORECASE):
            try:
                target = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if target > chapter:
                candidate_chapters.append(target)

    if candidate_chapters:
        return min(candidate_chapters)
    return None


def _drop_outline_artifacts(chapter: int) -> None:
    outline_file = outline_chapter_path(NOVELS_DIR, chapter)
    review_file = _outline_review_file(chapter)
    if outline_file.exists():
        outline_file.unlink()
        log(f"[Coordinator] 删除第{chapter}章大纲，准备按后章重叠规则重写")
    if review_file.exists():
        review_file.unlink()
        log(f"[Coordinator] 删除第{chapter}章大纲审查报告，准备按后章重叠规则重写")
    _drop_text_artifacts(chapter, reason="大纲已排除/重写")


def _drop_text_artifacts(chapter: int, *, reason: str) -> None:
    for path, label in (
        (_draft_file(chapter), "初稿"),
        (_review_file(chapter), "正文审查报告"),
        (_final_file(chapter), "终稿"),
    ):
        if path.exists():
            path.unlink()
            log(f"[Coordinator] 删除第{chapter}章{label}，原因: {reason}")


def _repair_later_overlap(chapter: int, report: dict) -> int | None:
    overlap_chapter = _extract_later_overlap_chapter(chapter, report)
    if overlap_chapter is None:
        return None
    _drop_outline_artifacts(overlap_chapter)
    log(f"[Coordinator] 第{chapter}章审查指向第{overlap_chapter}章存在重叠，已先排除后章，后续将重生成第{overlap_chapter}章大纲")
    return overlap_chapter


def ensure_outline_lookahead(chapter: int, end: int, lookahead: int) -> tuple[bool, int | None]:
    start_chapter, end_chapter = _outline_lookahead_window(chapter, end, lookahead)
    log(f"[Coordinator] 写第{chapter}章前，确保第{start_chapter}-{end_chapter}章大纲已通过")
    for outline_chapter in range(start_chapter, end_chapter + 1):
        if not process_outline_gate(outline_chapter, push_on_failure=False):
            return False, outline_chapter
    return True, None


def run_serial_quality_workflow(start: int, end: int, outline_lookahead: int | None = None) -> bool:
    if outline_lookahead is None:
        outline_lookahead = int(CONFIG.get("coordinator", {}).get("outline_lookahead_chapters", 10) or 10)

    for chapter in range(start, end + 1):
        log("=" * 60)
        log(f"[Coordinator] 单章质量门开始: 第{chapter}章")
        log("=" * 60)
        ok, failed_chapter = ensure_outline_lookahead(chapter, end, outline_lookahead)
        if not ok:
            failed_report_chapter = failed_chapter or chapter
            report = _load_outline_failure_report(failed_report_chapter)
            overlap_chapter = _repair_later_overlap(failed_report_chapter, report)
            if overlap_chapter is not None:
                log(f"[Coordinator] 重新校验第{chapter}章前，先让后章第{overlap_chapter}章让位")
                ok, failed_chapter = ensure_outline_lookahead(chapter, end, outline_lookahead)
                if not ok:
                    failed_report_chapter = failed_chapter or chapter
                    report = _load_outline_failure_report(failed_report_chapter)
                    _push_gate_failure(chapter, "大纲初审", report)
                    return False
            else:
                _push_gate_failure(failed_report_chapter, "大纲初审", report)
                return False
        if not process_draft_gate(chapter):
            return False

        statuses = scan_chapter_status(NOVELS_DIR, 1, CONFIG["total_chapters"])
        write_status_file(NOVELS_DIR, statuses.values())
        progress = load_progress()
        progress["last_outline_reviewed_chapter"] = highest_contiguous(statuses, 1, "outline_review_ok")
        progress["last_generated_chapter"] = highest_contiguous(statuses, 1, "draft_ok")
        progress["last_reviewed_chapter"] = highest_contiguous(statuses, 1, "review_ok")
        progress["failed_chapters"] = []
        progress["rewrite_queue"] = []
        progress["outline_rewrite_queue"] = []
        save_progress(progress)
    return True


def generate_summary_report():
    log("=" * 60)
    log("[Coordinator] 生成总结报告")
    log("=" * 60)

    statuses = scan_chapter_status(NOVELS_DIR, 1, CONFIG["total_chapters"])
    completed = [ch for ch, status in statuses.items() if status.final_ok]
    total_words = sum(status.draft_words for status in statuses.values() if status.draft_exists)
    review_scores = [status.review_score for status in statuses.values() if status.final_ok and status.review_score is not None]
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
    parser.add_argument("--outline-lookahead", type=int, default=0,
                        help="写正文前预先通过审查的大纲章数，默认读取 coordinator.outline_lookahead_chapters")
    parser.add_argument("--skip-planner", action="store_true", help="跳过Planner阶段")
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

    push_interval = int(CONFIG["coordinator"].get("push_interval_seconds", 120))
    ensure_wechat_pusher_process(push_interval)

    progress = load_progress()
    log(f"[Coordinator] 当前进度: 大纲审 {progress.get('last_outline_reviewed_chapter', 0)} 章，已生成 {progress['last_generated_chapter']} 章，已审查 {progress['last_reviewed_chapter']} 章")

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

    media_cfg = CONFIG.get("media", {})
    if media_cfg.get("enabled", True) and media_cfg.get("generate_after_planner", True):
        if not media_prompts_ready() and not args.skip_planner:
            log("[Coordinator] 检测到媒体提示词缺失，重新运行Planner补齐 media_prompts...")
            if run_planner() != 0:
                log("[ERROR] Planner补齐媒体提示词失败，请检查日志")
                return
        if not media_prompts_ready():
            log("[ERROR] world.json 缺少 media_prompts，无法生成封面/视频/主题歌")
            return
        if run_media_generator() != 0:
            log("[ERROR] 媒体资产生成失败，请检查 media_generator.log")
            return

    outline_lookahead = args.outline_lookahead or int(CONFIG["coordinator"].get("outline_lookahead_chapters", 10) or 10)
    outline_lookahead = max(1, outline_lookahead)
    log(f"[Coordinator] 单章质量门范围: 第{args.start}-{end_chapter}章；大纲提前窗口: {outline_lookahead}章")
    ok = run_serial_quality_workflow(args.start, end_chapter, outline_lookahead)
    if not ok:
        log("[Coordinator] 单章质量门失败，流程已停止")
        return

    report = generate_summary_report()
    log("=" * 60)
    log("[Coordinator] 全部任务完成")
    log(f"[Coordinator] 总进度: {report['total_chapters']}/{CONFIG['total_chapters']} 章")
    log(f"[Coordinator] 总字数: {report['total_words']:,} 字")
    log("=" * 60)

    title = get_book_title()
    # 用实际扫描的 draft/review/final 数（不能都用 total_chapters）
    statuses = scan_chapter_status(NOVELS_DIR, 1, CONFIG["total_chapters"])
    actual_draft = sum(1 for s in statuses.values() if s.draft_exists)
    actual_review = sum(1 for s in statuses.values() if s.review_exists)
    actual_final = sum(1 for s in statuses.values() if s.final_ok)
    _push_task_complete(
        config=CONFIG,
        title=title,
        total_chapters=CONFIG["total_chapters"],
        draft=actual_draft,
        review=actual_review,
        final=actual_final,
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
