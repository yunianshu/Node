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
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from core.novel_config import configure_stdio, load_config, resolve_project_dir
from core.push_notifier import (
    push_task_complete as _push_task_complete,
    push_interrupted as _push_interrupted,
    push_error as _push_error,
)
from core.workflow_state import (
    aggregate_review_scores,
    highest_contiguous,
    outline_review_dir,
    report_path,
    review_dir,
    scan_chapter_status,
    write_status_file,
)
from pipeline import draft_gate as draft_gate_module
from pipeline import outline_gate as outline_gate_module
configure_stdio()

PIPELINE_SCRIPTS = {
    "planner.py": TOOLS_ROOT / "pipeline" / "planner.py",
    "outliner.py": TOOLS_ROOT / "pipeline" / "outliner.py",
    "outline_reviewer.py": TOOLS_ROOT / "pipeline" / "outline_reviewer.py",
    "writer.py": TOOLS_ROOT / "pipeline" / "writer.py",
    "reviewer.py": TOOLS_ROOT / "pipeline" / "reviewer.py",
    "polisher.py": TOOLS_ROOT / "pipeline" / "polisher.py",
    "media_generator.py": TOOLS_ROOT / "pipeline" / "media_generator.py",
}

MAINTENANCE_SCRIPTS = {
    "wechat_pusher_lane.py": TOOLS_ROOT / "maintenance" / "wechat_pusher_lane.py",
    "gate_watchdog.py": TOOLS_ROOT / "maintenance" / "gate_watchdog.py",
    "outline_book_reviewer.py": TOOLS_ROOT / "maintenance" / "outline_book_reviewer.py",
    "foreshadowing_audit.py": TOOLS_ROOT / "maintenance" / "foreshadowing_audit.py",
}

def resolve_script_path(script_name: str) -> Path:
    try:
        return PIPELINE_SCRIPTS[script_name]
    except KeyError as exc:
        raise ValueError(f"Unsupported pipeline script: {script_name}") from exc

NOVELS_DIR = None
CHAPTERS_DIR = None
REVIEWS_DIR = None
LOGS_DIR = None
WORLD_FILE = None
CHARACTERS_FILE = None
PROGRESS_FILE = None
LOG_FILE = None
CONFIG = None

_reviewer_run_lock = threading.Lock()

class CoordinatorRuntime:
    @property
    def CONFIG(self):
        return CONFIG

    @property
    def NOVELS_DIR(self):
        return NOVELS_DIR

    @property
    def LOGS_DIR(self):
        return LOGS_DIR

    @property
    def MAINTENANCE_SCRIPTS(self):
        return MAINTENANCE_SCRIPTS

    def resolve_script_path(self, script_name: str) -> Path:
        return resolve_script_path(script_name)

    def run_script(self, script_name: str, *args) -> int:
        if script_name == "reviewer.py" and "--review-file" not in args and "--chapter-file" not in args:
            with _reviewer_run_lock:
                return run_script(script_name, *args)
        return run_script(script_name, *args)

    def run_cancellable_process(self, cmd: list[str], child_log: Path, stop_event: threading.Event) -> int:
        return run_cancellable_process(cmd, child_log, stop_event)

    def run_streaming_process(self, cmd: list[str], child_log: Path) -> int:
        return run_streaming_process(cmd, child_log)

    def log(self, msg: str) -> None:
        log(msg)

    def _load_json_file(self, path: Path) -> dict:
        return _load_json_file(path)

    def _safe_unlink(self, path: Path) -> None:
        _safe_unlink(path)

    def _outline_review_file(self, chapter: int) -> Path:
        return _outline_review_file(chapter)

    def _draft_file(self, chapter: int) -> Path:
        return _draft_file(chapter)

    def _review_file(self, chapter: int) -> Path:
        return _review_file(chapter)

    def _final_file(self, chapter: int) -> Path:
        return _final_file(chapter)

    def _candidate_draft_file(self, chapter: int, candidate_id: int) -> Path:
        return _candidate_draft_file(chapter, candidate_id)

    def _candidate_review_file(self, chapter: int, candidate_id: int) -> Path:
        return _candidate_review_file(chapter, candidate_id)

    def _drop_text_artifacts(self, chapter: int, *, reason: str) -> None:
        _drop_text_artifacts(chapter, reason=reason)

    def _review_feedback(self, review_data: dict, chapter: int, gate: str, round_no: int, attempt: int, label: str = "") -> dict:
        return _review_feedback(review_data, chapter, gate, round_no, attempt, label=label)

    def _score_from_review(self, review_data: dict) -> float:
        return _score_from_review(review_data)

    def _failure_analysis(self, chapter: int, gate: str, reviews: list[dict]) -> dict:
        return _failure_analysis(chapter, gate, reviews)

    def _write_gate_feedback(self, chapter: int, gate: str, reviews: list[dict], round_no: int) -> Path:
        return _write_gate_feedback(chapter, gate, reviews, round_no)

    def _write_failure_report(self, chapter: int, gate: str, reviews: list[dict]) -> dict:
        return _write_failure_report(chapter, gate, reviews)

    def _push_gate_failure(self, chapter: int, gate: str, report: dict) -> None:
        _push_gate_failure(chapter, gate, report)

    def _load_best_score(self, chapter: int) -> float:
        return _load_best_score(chapter)

    def _save_best_draft(self, chapter: int, score: float, source: Path) -> None:
        _save_best_draft(chapter, score, source)

    def _restore_best_draft(self, chapter: int) -> float:
        return _restore_best_draft(chapter)

_RUNTIME = CoordinatorRuntime()

def runtime_context() -> CoordinatorRuntime:
    return _RUNTIME

def _runtime() -> CoordinatorRuntime:
    return _RUNTIME

def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, CHAPTERS_DIR, REVIEWS_DIR, OUTLINE_REVIEW_DIR, LOGS_DIR, WORLD_FILE
    global CHARACTERS_FILE, PROGRESS_FILE, LOG_FILE, CONFIG
    NOVELS_DIR = Path(project_dir).resolve()
    CHAPTERS_DIR = NOVELS_DIR / "chapters" / "draft"
    REVIEWS_DIR = review_dir(NOVELS_DIR)
    OUTLINE_REVIEW_DIR = outline_review_dir(NOVELS_DIR)
    LOGS_DIR = NOVELS_DIR / "logs"
    WORLD_FILE = NOVELS_DIR / "world.json"
    CHARACTERS_FILE = NOVELS_DIR / "characters.json"
    PROGRESS_FILE = report_path(NOVELS_DIR, "progress.json")
    LOG_FILE = LOGS_DIR / "coordinator.log"
    CONFIG = load_config(NOVELS_DIR)

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

def ensure_gate_watchdog_process(
    mode: str,
    *,
    interval_seconds: int | None = None,
    stale_threshold: int | None = None,
    notify_cooldown_seconds: int | None = None,
) -> None:
    """启动只读质量门 watchdog；实际单例由 watchdog 自己的 lock 文件保证。"""
    if NOVELS_DIR is None or CONFIG is None:
        return
    if mode not in {"outline", "draft"}:
        raise ValueError(f"unsupported watchdog mode: {mode}")
    script = MAINTENANCE_SCRIPTS["gate_watchdog.py"]
    if not script.exists():
        log(f"[Watchdog] 监控脚本不存在，跳过: {script}")
        return

    coordinator_cfg = CONFIG.get("coordinator", {})
    interval = interval_seconds or int(coordinator_cfg.get("watchdog_interval_seconds", 300) or 300)
    threshold = stale_threshold or int(coordinator_cfg.get("watchdog_stale_threshold", 2) or 2)
    cooldown = notify_cooldown_seconds or int(coordinator_cfg.get("watchdog_notify_cooldown_seconds", 900) or 900)
    cmd = [
        sys.executable,
        str(script),
        "--project",
        str(NOVELS_DIR),
        "--mode",
        mode,
        "--interval",
        str(interval),
        "--stale-threshold",
        str(threshold),
        "--notify-cooldown",
        str(cooldown),
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
        log(f"[Watchdog] 已确保{mode}质量门监控进程运行（每{interval}秒）")
    except Exception as exc:
        log(f"[Watchdog] 启动{mode}质量门监控失败: {exc}")

def ensure_gate_watchdog_processes() -> None:
    ensure_gate_watchdog_process("outline")
    ensure_gate_watchdog_process("draft")

def notify_stage(stage: str, status: str, start: int | None = None, end: int | None = None,
                 processed: int = 0, failed: int = 0, error: str = "") -> None:
    """记录阶段事件；企业微信进度由独立 wechat_pusher_lane 进程推送。"""
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

def _terminate_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        else:
            proc.terminate()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

def run_cancellable_process(cmd: list[str], child_log: Path, stop_event: threading.Event) -> int:
    """运行可取消子进程；stop_event 设置后终止进程树。"""
    child_log.parent.mkdir(parents=True, exist_ok=True)
    with open(child_log, "a", encoding="utf-8") as lf:
        lf.write(f"$ {' '.join(cmd)}\n")
        lf.flush()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output_queue: queue.Queue[str | None] = queue.Queue()

        def _reader() -> None:
            try:
                if proc.stdout is None:
                    return
                for line in proc.stdout:
                    output_queue.put(line)
            finally:
                output_queue.put(None)

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        try:
            stream_done = False
            while True:
                if stop_event.is_set():
                    lf.write("[cancelled] stop_event set, terminating process tree\n")
                    lf.flush()
                    _terminate_process_tree(proc)
                    return -9
                try:
                    while True:
                        line = output_queue.get_nowait()
                        if line is None:
                            stream_done = True
                            break
                        text = line.rstrip("\n")
                        lf.write(text + "\n")
                        lf.flush()
                        if text:
                            log(f"[Child] {text}")
                except queue.Empty:
                    pass
                rc = proc.poll()
                if rc is not None:
                    if not stream_done:
                        reader.join(timeout=1)
                        try:
                            while True:
                                line = output_queue.get_nowait()
                                if line is None:
                                    break
                                text = line.rstrip("\n")
                                lf.write(text + "\n")
                                lf.flush()
                                if text:
                                    log(f"[Child] {text}")
                        except queue.Empty:
                            pass
                    return rc
                time.sleep(0.2)
        finally:
            if proc.stdout:
                try:
                    proc.stdout.close()
                except Exception:
                    pass

def run_script(script_name: str, *args) -> int:
    script_file = resolve_script_path(script_name)
    cmd = [sys.executable, str(script_file), "--project", str(NOVELS_DIR)] + list(args)
    log(f"[Coordinator] 执行: {' '.join(cmd)}")
    child_log = LOGS_DIR / f"coordinator_{Path(script_name).stem}.log"
    return run_streaming_process(cmd, child_log)

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
    }

def save_progress(progress):
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)

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

def _best_draft_file(chapter: int) -> Path:
    return CHAPTERS_DIR / f"chapter_{chapter:04d}_best.txt"

def _best_score_file(chapter: int) -> Path:
    return CHAPTERS_DIR / f"chapter_{chapter:04d}_best.json"

def _candidate_draft_file(chapter: int, candidate_id: int) -> Path:
    return CHAPTERS_DIR / f"chapter_{chapter:04d}_polish_{candidate_id}.txt"

def _candidate_review_file(chapter: int, candidate_id: int) -> Path:
    return REVIEWS_DIR / f"chapter_{chapter:04d}_polish_{candidate_id}_review.json"

def _load_best_score(chapter: int) -> float:
    path = _best_score_file(chapter)
    if not path.exists():
        return 0.0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return float(data.get("score", 0.0))
    except Exception:
        return 0.0

def _save_best_draft(chapter: int, score: float, source: Path) -> None:
    best_draft = _best_draft_file(chapter)
    best_score = _best_score_file(chapter)
    best_draft.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    best_score.write_text(
        json.dumps({"score": score, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False),
        encoding="utf-8",
    )
    log(f"[Coordinator] 第{chapter}章保存最佳草稿 {score} 分 -> {best_draft}")

def _restore_best_draft(chapter: int) -> float:
    best_draft = _best_draft_file(chapter)
    if not best_draft.exists():
        return 0.0
    current = _draft_file(chapter)
    current.write_text(best_draft.read_text(encoding="utf-8"), encoding="utf-8")
    score = _load_best_score(chapter)
    log(f"[Coordinator] 第{chapter}章从 {best_draft} 恢复最佳草稿 {score} 分")
    return score

def _review_feedback(review_data: dict, chapter: int, gate: str, round_no: int, attempt: int, label: str = "") -> dict:
    return {
        "chapter": chapter,
        "gate": gate,
        "round": round_no,
        "attempt": attempt,
        "label": label,
        "overall_score": review_data.get("overall_score"),
        "verdict": review_data.get("verdict"),
        "weaknesses": review_data.get("weaknesses", []),
        "suggestions": review_data.get("suggestions", []),
        "continuity_issues": review_data.get("continuity_issues", []),
        "summary": review_data.get("summary", ""),
    }

def _score_from_review(review_data: dict) -> float:
    score = review_data.get("overall_score")
    if score is None:
        return 0.0
    try:
        return float(score)
    except (TypeError, ValueError):
        return 0.0

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
    return outline_gate_module.outline_gate_passed(_runtime(), chapter)

def _outline_quality_gate_config() -> dict:
    return outline_gate_module.outline_quality_gate_config(_runtime())

def _run_outline_review_rounds(
    chapter: int,
    outline_file: Path,
    aggregate_file: Path,
    *,
    child_log: Path | None = None,
    stop_event: threading.Event | None = None,
) -> tuple[int, dict]:
    return outline_gate_module.run_outline_review_rounds(
        _runtime(),
        chapter,
        outline_file,
        aggregate_file,
        child_log=child_log,
        stop_event=stop_event,
    )

def _outline_race_config() -> dict:
    return outline_gate_module.outline_race_config(_runtime())

def _draft_min_score() -> float:
    return draft_gate_module.draft_min_score(_runtime())

def _draft_gate_passed(chapter: int) -> bool:
    return draft_gate_module.draft_gate_passed(_runtime(), chapter)

def _promote_draft_to_final(chapter: int) -> bool:
    return draft_gate_module.promote_draft_to_final(_runtime(), chapter)

def process_outline_gate(
    chapter: int,
    *,
    push_on_failure: bool = True,
    initial_feedback: Path | None = None,
) -> bool:
    return outline_gate_module.process_outline_gate(
        _runtime(),
        chapter,
        push_on_failure=push_on_failure,
        initial_feedback=initial_feedback,
    )

def process_draft_gate(chapter: int) -> bool:
    return draft_gate_module.process_draft_gate(_runtime(), chapter)

def _outline_lookahead_window(chapter: int, end: int, lookahead: int) -> tuple[int, int]:
    return outline_gate_module.outline_lookahead_window(chapter, end, lookahead)

def _load_outline_failure_report(chapter: int) -> dict:
    return outline_gate_module.load_outline_failure_report(_runtime(), chapter)

def _drop_outline_artifacts(chapter: int) -> None:
    return outline_gate_module.drop_outline_artifacts(_runtime(), chapter)

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
    return outline_gate_module.repair_later_overlap(_runtime(), chapter, report)

def ensure_outline_lookahead(chapter: int, end: int, lookahead: int) -> tuple[bool, int | None]:
    return outline_gate_module.ensure_outline_lookahead(_runtime(), chapter, end, lookahead)

def run_serial_quality_workflow(start: int, end: int, outline_lookahead: int | None = None) -> bool:
    if outline_lookahead is None:
        outline_lookahead = int(CONFIG.get("coordinator", {}).get("outline_lookahead_chapters", 10) or 10)
    overlap_repair_passes = max(
        1,
        int(CONFIG.get("coordinator", {}).get("outline_overlap_repair_passes", 1) or 1),
    )

    for chapter in range(start, end + 1):
        log("=" * 60)
        log(f"[Coordinator] 单章质量门开始: 第{chapter}章")
        log("=" * 60)
        ok, failed_chapter = ensure_outline_lookahead(chapter, end, outline_lookahead)
        if not ok:
            failed_report_chapter = failed_chapter or chapter
            for repair_pass in range(1, overlap_repair_passes + 1):
                report = _load_outline_failure_report(failed_report_chapter)
                overlap_chapter = _repair_later_overlap(failed_report_chapter, report)
                if overlap_chapter is None:
                    break
                log(
                    f"[Coordinator] 重新校验第{chapter}章前，先让后章第{overlap_chapter}章让位 "
                    f"({repair_pass}/{overlap_repair_passes})"
                )
                ok, failed_chapter = ensure_outline_lookahead(chapter, end, outline_lookahead)
                if ok:
                    break
                failed_report_chapter = failed_chapter or chapter
            if not ok:
                report = _load_outline_failure_report(failed_report_chapter)
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
        progress.pop("rewrite_queue", None)
        progress.pop("outline_rewrite_queue", None)
        save_progress(progress)
    return True

def run_outline_book_review(force: bool = False) -> bool:
    return outline_gate_module.run_outline_book_review(_runtime(), force=force)

def _outline_book_review_report() -> dict:
    return outline_gate_module._outline_book_review_report(_runtime())

def _global_outline_feedback(chapter: int, issues: list[dict], round_no: int) -> Path:
    return outline_gate_module._global_outline_feedback(_runtime(), chapter, issues, round_no)

def repair_outline_book_review(round_no: int) -> bool:
    return outline_gate_module.repair_outline_book_review(_runtime(), round_no)

def prepare_all_outlines_and_book_review(end: int, force_review: bool = False) -> bool:
    return outline_gate_module.prepare_all_outlines_and_book_review(_runtime(), end, force_review=force_review)


def run_foreshadowing_audit(*, apply_patches: bool = True) -> bool:
    """A2 伏笔闭环门禁：writer 前检查 dangling 伏笔，必要时自动补丁回收。

    仅在全量大纲就绪后（outline-first 路径）执行。critical_dangling 超阈值则阻断 writer。
    返回 True 表示通过（可继续 writer），False 表示阻断。
    """
    script = MAINTENANCE_SCRIPTS["foreshadowing_audit.py"]
    cmd = [sys.executable, str(script), "--project", str(NOVELS_DIR),
           "--block-threshold", "0"]
    if apply_patches:
        cmd.append("--apply-patches")
    child_log = LOGS_DIR / "foreshadowing_audit_child.log"
    rc = run_streaming_process(cmd, child_log)
    if rc != 0:
        log(f"[Coordinator] 伏笔闭环门禁未通过 (rc={rc})，阻断 writer 阶段")
        return False
    log("[Coordinator] 伏笔闭环门禁通过，进入正文阶段")
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

    # 单章评分常被软封顶在 8.5 附近，算术平均会被低分章拖低；改用分位数口径，
    # 让中位数与过审率反映真实质量分布，避免整本分被单一均分锚定。
    min_score = float(CONFIG.get("reviewer", {}).get("min_score", 8.5))
    score_metrics = aggregate_review_scores(review_scores, min_score=min_score)

    report = {
        "total_chapters": len(completed),
        "target_chapters": CONFIG["total_chapters"],
        "total_words": total_words,
        "average_words_per_chapter": total_words // len(completed) if completed else 0,
        "average_score": score_metrics["average"],
        "score_metrics": score_metrics,
        "rewrite_count": rewrite_count,
        "completed_chapters": completed
    }

    report_file = report_path(NOVELS_DIR, "summary_report.json")
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    log(f"[Coordinator] 总结报告: 已完成 {report['total_chapters']}/{CONFIG['total_chapters']} 章")
    log(f"[Coordinator] 总字数: {report['total_words']:,} 字")
    log(f"[Coordinator] 评分: 均分 {score_metrics['average']:.2f} / 中位 {score_metrics['median']:.2f} / 过审率 {score_metrics['pass_rate'] * 100:.1f}%")
    log(f"[Coordinator] 需重写: {rewrite_count} 章")
    return report

def main():
    parser = argparse.ArgumentParser(description="小说生成协调器")
    parser.add_argument("--project", "-p", type=str,
                        default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录（默认从环境变量 NOVEL_PROJECT_DIR 读取）")
    parser.add_argument("--batch-size", type=int, default=0, help="兼容参数；当前单章质量门模式不按批次切分")
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=0, help="结束章节")
    parser.add_argument("--outline-lookahead", type=int, default=0,
                        help="写正文前预先通过审查的大纲章数，默认读取 coordinator.outline_lookahead_chapters")
    parser.add_argument("--skip-planner", action="store_true", help="跳过Planner阶段")
    parser.add_argument(
        "--outline-first",
        action="store_true",
        help="先完成全书逐章大纲及整本大纲总审，通过后再生成正文",
    )
    parser.add_argument(
        "--force-outline-book-review",
        action="store_true",
        help="忽略整本大纲审查缓存并重新审查",
    )
    args = parser.parse_args()

    try:
        project = resolve_project_dir(args.project)
    except ValueError as exc:
        print(f"错误: {exc}")
        sys.exit(1)

    init_project(project)

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
    ensure_gate_watchdog_processes()

    progress = load_progress()
    log(f"[Coordinator] 当前进度: 大纲审 {progress.get('last_outline_reviewed_chapter', 0)} 章，已生成 {progress['last_generated_chapter']} 章，已审查 {progress['last_reviewed_chapter']} 章")

    if args.batch_size:
        log("[Coordinator] 当前使用单章质量门主流程，--batch-size 仅保留兼容，不会改变章节推进方式")
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
    outline_book_cfg = CONFIG.get("outline_book_reviewer", {})
    outline_first = bool(
        args.outline_first
        or outline_book_cfg.get("required_before_draft", False)
    )
    if outline_first:
        if not prepare_all_outlines_and_book_review(
            end_chapter,
            force_review=args.force_outline_book_review,
        ):
            log("[Coordinator] 全量大纲或整本大纲总审未通过，正文阶段未启动")
            return
        outline_lookahead = 1
        log("[Coordinator] 全量大纲已过审，正文阶段仅做当前章大纲校验")
        # A2 伏笔闭环门禁：全量大纲就绪后，writer 前强制回收 dangling 伏笔
        if not run_foreshadowing_audit(apply_patches=True):
            log("[Coordinator] 伏笔闭环门禁未通过，正文阶段未启动")
            return
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
        avg_score=report["score_metrics"]["median"],
        pass_rate=report["score_metrics"]["pass_rate"],
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
