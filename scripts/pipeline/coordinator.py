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
    atomic_write_json,
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
    "book_reviewer.py": TOOLS_ROOT / "maintenance" / "book_reviewer.py",
    "foreshadowing_audit.py": TOOLS_ROOT / "maintenance" / "foreshadowing_audit.py",
    "story_flow_audit.py": TOOLS_ROOT / "maintenance" / "story_flow_audit.py",
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
        except Exception as exc:
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
    except Exception as exc:
        try:
            proc.kill()
        except Exception as exc:
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
                except Exception as exc:
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


def refresh_progress_from_status(progress: dict | None = None) -> dict:
    """Refresh progress from authoritative chapter artifacts, not stale progress.json."""
    progress = dict(progress or load_progress())
    statuses = scan_chapter_status(NOVELS_DIR, 1, CONFIG["total_chapters"])
    write_status_file(NOVELS_DIR, statuses.values())
    progress["last_outline_reviewed_chapter"] = highest_contiguous(statuses, 1, "outline_review_ok")
    progress["last_generated_chapter"] = highest_contiguous(statuses, 1, "draft_ok")
    progress["last_reviewed_chapter"] = highest_contiguous(statuses, 1, "review_ok")
    progress["last_final_chapter"] = highest_contiguous(statuses, 1, "final_ok")
    progress.setdefault("failed_chapters", [])
    save_progress(progress)
    return progress

def check_base_files_exist():
    return WORLD_FILE.exists() and CHARACTERS_FILE.exists()

def run_planner(*extra_args):
    log("=" * 60)
    log("[Coordinator] 启动 Planner Agent")
    log("=" * 60)
    notify_stage("世界观/角色", "开始")
    rc = run_script("planner.py", *extra_args)
    notify_stage("世界观/角色", "完成" if rc == 0 else "异常", error="" if rc == 0 else f"退出码 {rc}")
    return rc

def media_prompts_ready() -> bool:
    if not WORLD_FILE.exists():
        return False
    try:
        world = json.loads(WORLD_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
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
    except Exception as exc:
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
    except Exception as exc:
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
    def first_list(value) -> list:
        return value[:1] if isinstance(value, list) else []

    def targeted_repairs() -> list[str]:
        if gate != "draft":
            return []
        local = review_data.get("local_analysis")
        if not isinstance(local, dict):
            return []
        human = local.get("human_warmth_detection")
        repairs = []
        if isinstance(human, dict) and human.get("passed") is False:
            issues = [str(v).strip() for v in human.get("issues", []) if str(v).strip()]
            if any("human_anchor" in issue or "锚点" in issue for issue in issues):
                repairs.append("重写时必须把大纲 human_anchor 落进一个具体现场，用生活物件、行动和对白推动剧情，不能只写心理旁白。")
            if any("生活压力" in issue for issue in issues):
                repairs.append("补出本章现实生活压力：钱、饭、病痛、工作、身份成本或体面损失必须影响人物选择。")
            if any("关系" in issue or "牵挂" in issue for issue in issues):
                repairs.append("补出关系牵挂或亏欠回声：至少一名配角要有主动选择、误会变化、照料动作或未说出口的真话。")
            if any("物件" in issue for issue in issues):
                repairs.append("加入一个可触摸生活物件，并让它承载旧账、承诺、亏欠或告别的潜台词。")
            if any("互动" in issue or "对白" in issue for issue in issues):
                repairs.append("增加带问句或试探意味的对白，让人物不把动机一次说透，而是通过回避、追问或停顿露出潜台词。")
        origin_fact = local.get("origin_fact_reference_detection")
        if isinstance(origin_fact, dict) and origin_fact.get("needs_attention") is True:
            missing_clauses = [
                str(item.get("clause", "") if isinstance(item, dict) else item).strip()
                for item in origin_fact.get("missing_fact_clauses_sample", [])
                if str(item.get("clause", "") if isinstance(item, dict) else item).strip()
            ][:2]
            fact_terms = [
                str(v).strip()
                for v in origin_fact.get("fact_terms_sample", [])
                if str(v).strip()
            ][:6]
            if missing_clauses:
                repairs.append(
                    "重写时必须兑现 origin/facts 的事实短句，不能只借用人名/地名。"
                    f"优先落地：{'；'.join(missing_clauses)}。"
                )
            elif fact_terms:
                repairs.append(
                    "重写时必须引用 origin/facts 的事实素材，至少让其中一个人物、地点、旧事或物件进入正文现场。"
                    f"可用事实线索：{'、'.join(fact_terms)}。"
                )
            else:
                repairs.append("重写时必须引用 origin/facts 的事实素材，不能只模仿 style 风格样本。")
        relationship = local.get("relationship_obligation_detection")
        if (
            isinstance(relationship, dict)
            and relationship.get("required") is True
            and relationship.get("passed") is False
        ):
            pair = str(relationship.get("pair", "")).strip() or "上一章延续关系"
            pressure = str(relationship.get("pressure", "")).strip()
            repairs.append(
                f"重写时必须兑现关系任务【{pair}】：{pressure[:120]}。"
                "安排双方同场或明确互动，写出潜台词对白、照料/回避/补偿动作和关系变化结果。"
            )
        if not repairs and isinstance(human, dict) and human.get("passed") is False:
            repairs.append("优先修复 human_warmth_detection 失败项：让本章有生活压力、关系牵挂、具体物件和人的反应。")
        return list(dict.fromkeys(repairs))[:3]

    return {
        "chapter": chapter,
        "gate": gate,
        "round": round_no,
        "attempt": attempt,
        "label": label,
        "status": review_data.get("status", ""),
        "overall_score": review_data.get("overall_score"),
        "verdict": review_data.get("verdict"),
        "strengths": first_list(review_data.get("strengths", [])),
        "weaknesses": first_list(review_data.get("weaknesses", [])),
        "suggestions": first_list(review_data.get("suggestions", [])),
        "continuity_issues": first_list(review_data.get("continuity_issues", [])),
        "summary": review_data.get("summary", ""),
        "edits": first_list(review_data.get("edits", [])),
        "local_analysis": review_data.get("local_analysis", {}),
        "targeted_repairs": targeted_repairs(),
        "raw_response": review_data.get("raw_response", ""),
        "review_contract_errors": review_data.get("review_contract_errors", []),
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
    targeted_repairs = []
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
        targeted_repairs.extend([str(v) for v in item.get("targeted_repairs", []) if v])
    top_weaknesses = list(dict.fromkeys(weaknesses))[:1]
    top_suggestions = list(dict.fromkeys(suggestions))[:1]
    top_repairs = list(dict.fromkeys(targeted_repairs))[:3]
    return {
        "chapter": chapter,
        "gate": gate,
        "attempts": len(reviews),
        "scores": scores,
        "best_score": max(scores) if scores else None,
        "statuses": statuses,
        "likely_reasons": top_weaknesses or statuses or ["no_valid_review"],
        "adjustments": top_repairs or top_suggestions or top_weaknesses or ["提高剧情完整度、人物动机、节奏和可写性"],
        "targeted_repairs": top_repairs,
    }


def _feedback_review_score(review: dict) -> float | None:
    status = str(review.get("status", "")).strip().lower()
    if status and status != "completed":
        return None
    try:
        return float(review.get("overall_score"))
    except (TypeError, ValueError):
        return None


def _feedback_review_window(reviews: list[dict], limit: int = 3) -> list[dict]:
    valid = [
        (index, score, review)
        for index, review in enumerate(reviews)
        if isinstance(review, dict)
        and (score := _feedback_review_score(review)) is not None
    ]
    if not valid:
        return [review for review in reviews[-limit:] if isinstance(review, dict)]

    best = max(valid, key=lambda item: (item[1], item[0]))
    selected_indices = {best[0]}
    for index in range(max(0, len(reviews) - limit), len(reviews)):
        if isinstance(reviews[index], dict):
            selected_indices.add(index)
    selected = [reviews[index] for index in sorted(selected_indices)]
    if len(selected) > limit:
        best_review = best[2]
        latest = selected[-(limit - 1):]
        selected = [best_review] + [review for review in latest if review is not best_review]
    return selected[-limit:]


def _write_gate_feedback(chapter: int, gate: str, reviews: list[dict], round_no: int) -> Path:
    analysis = _failure_analysis(chapter, gate, reviews)
    payload = {
        f"{chapter}": {
            "chapter": chapter,
            "gate": gate,
            "analysis_round": round_no,
            "failure_analysis": analysis,
            "reviews": _feedback_review_window(reviews),
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

def _read_json_safely(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = _load_json_file(path)
    except Exception as exc:
        return {}
    return data if isinstance(data, dict) else {}

def _relationship_state_summary(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}
    relationships = data.get("relationships")
    if not isinstance(relationships, list):
        relationships = []
    open_items = []
    resolved_items = []
    for item in relationships:
        if not isinstance(item, dict):
            continue
        compact = {
            "pair": str(item.get("pair", "")).strip(),
            "debt": str(item.get("debt", "")).strip(),
            "misunderstanding": str(item.get("misunderstanding", "")).strip(),
            "promise": str(item.get("promise", "")).strip(),
            "care_action": str(item.get("care_action", "")).strip(),
            "next_pressure": str(item.get("next_pressure", "")).strip(),
            "resolved": bool(item.get("resolved")),
        }
        if compact["resolved"]:
            resolved_items.append(compact)
        else:
            open_items.append(compact)
    return {
        "relationship_count": len(relationships),
        "open_count": len(open_items),
        "resolved_count": len(resolved_items),
        "open_items": open_items[:5],
        "resolved_items": resolved_items[:5],
    }

def _human_warmth_summary_from_review(review: dict) -> dict:
    if not isinstance(review, dict):
        return {}
    local_analysis = review.get("local_analysis")
    if not isinstance(local_analysis, dict):
        return {}
    human_warmth = local_analysis.get("human_warmth_detection")
    if not isinstance(human_warmth, dict):
        return {}
    return {
        "passed": human_warmth.get("passed"),
        "matched_anchor": human_warmth.get("matched_anchor"),
        "signals": human_warmth.get("signals", {}),
        "issues": human_warmth.get("issues", []),
    }

def _artifact_snapshot(path: Path, label: str) -> dict | None:
    if not path.exists():
        return None
    if label == "正文审查报告":
        return {"human_warmth_detection": _human_warmth_summary_from_review(_read_json_safely(path))}
    if label == "关系欠账状态":
        return {"relationship_state": _relationship_state_summary(_read_json_safely(path))}
    return None

def _drop_text_artifacts(chapter: int, *, reason: str) -> None:
    records = []
    for path, label in (
        (_draft_file(chapter), "初稿"),
        (_review_file(chapter), "正文审查报告"),
        (_final_file(chapter), "终稿"),
        (NOVELS_DIR / "chapters" / "character_states" / f"chapter_{chapter:04d}.json", "角色状态"),
        (NOVELS_DIR / "chapters" / "arc_states" / f"chapter_{chapter:04d}.json", "成长弧线状态"),
        (NOVELS_DIR / "chapters" / "relationship_states" / f"chapter_{chapter:04d}.json", "关系欠账状态"),
    ):
        record = {"label": label, "path": str(path), "existed": path.exists(), "deleted": False}
        snapshot = _artifact_snapshot(path, label)
        if snapshot:
            record["pre_delete_snapshot"] = snapshot
        if path.exists():
            try:
                path.unlink()
                record["deleted"] = True
            except Exception as exc:
                record["error"] = str(exc)
                log(f"[WARNING] 删除第{chapter}章{label}失败 {path}: {exc}")
            else:
                log(f"[Coordinator] 删除第{chapter}章{label}，原因: {reason}")
        records.append(record)
    _write_text_cleanup_audit(chapter, reason=reason, records=records)


def _write_text_cleanup_audit(chapter: int, *, reason: str, records: list[dict]) -> None:
    path = LOGS_DIR / "text_artifact_cleanup.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "chapter": chapter,
        "reason": reason,
        "records": records,
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

def _repair_later_overlap(chapter: int, report: dict) -> int | None:
    return outline_gate_module.repair_later_overlap(_runtime(), chapter, report)

def ensure_outline_lookahead(chapter: int, end: int, lookahead: int) -> tuple[bool, int | None]:
    return outline_gate_module.ensure_outline_lookahead(_runtime(), chapter, end, lookahead)


def _extract_post_chapter_states(chapter: int) -> None:
    """正文通过门禁后抽取角色状态、成长弧线和关系欠账，供下一章 writer 注入。

    三个抽取彼此无依赖——并行执行以省一轮往返延迟。
    """
    final_file = NOVELS_DIR / "chapters" / "final" / f"chapter_{chapter:04d}.txt"
    draft_file = NOVELS_DIR / "chapters" / "draft" / f"chapter_{chapter:04d}.txt"
    src = final_file if final_file.exists() else draft_file
    if not src.exists():
        log(f"[Coordinator] 第{chapter}章正文文件不存在，跳过状态抽取")
        return
    text = src.read_text(encoding="utf-8")
    characters_meta = {}
    chars_file = NOVELS_DIR / "characters.json"
    if chars_file.exists():
        try:
            import json as _json
            characters_meta = _json.loads(chars_file.read_text(encoding="utf-8"))
        except Exception as exc:
            pass

    def _extract_character_states() -> None:
        try:
            from core.character_state import extract_character_states
            extract_character_states(NOVELS_DIR, CONFIG, chapter, text, characters_meta)
            log(f"[Coordinator] 第{chapter}章角色状态快照已抽取")
        except Exception as _cse:
            log(f"[Coordinator] 角色状态抽取异常（忽略）: {_cse}")

    def _extract_arc_progress() -> None:
        try:
            from core.arc_state import extract_arc_progress
            extract_arc_progress(NOVELS_DIR, CONFIG, chapter, text, characters_meta)
            log(f"[Coordinator] 第{chapter}章成长弧线进度已抽取")
        except Exception as _ase:
            log(f"[Coordinator] 弧线进度抽取异常（忽略）: {_ase}")

    def _extract_relationship_states() -> None:
        try:
            from core.relationship_state import extract_relationship_states
            extract_relationship_states(NOVELS_DIR, CONFIG, chapter, text, characters_meta)
            log(f"[Coordinator] 第{chapter}章关系欠账快照已抽取")
        except Exception as _rse:
            log(f"[Coordinator] 关系欠账抽取异常（忽略）: {_rse}")

    # 三个抽取互相独立，并行跑以省一轮 LLM 往返
    char_thread = threading.Thread(target=_extract_character_states, daemon=True)
    arc_thread = threading.Thread(target=_extract_arc_progress, daemon=True)
    rel_thread = threading.Thread(target=_extract_relationship_states, daemon=True)
    char_thread.start()
    arc_thread.start()
    rel_thread.start()
    char_thread.join()
    arc_thread.join()
    rel_thread.join()


def _final_ai_flavor_check(chapter: int) -> None:
    """G14: 终稿落盘后复检 AI 味，严重时记录告警（不阻断，仅报告）。"""
    final_file = NOVELS_DIR / "chapters" / "final" / f"chapter_{chapter:04d}.txt"
    if not final_file.exists():
        return
    try:
        from core.ai_flavor_detector import detect_ai_flavor
        text = final_file.read_text(encoding="utf-8")
        result = detect_ai_flavor(text, project=NOVELS_DIR)
        score = result.get("ai_flavor_score", 10) if isinstance(result, dict) else 10
        if score < 7:
            log(f"[Coordinator] ⚠️ 第{chapter}章终稿AI味复检偏低(score={score})，建议人工复核")
        else:
            log(f"[Coordinator] 第{chapter}章终稿AI味复检通过(score={score})")
    except Exception as _afe:
        log(f"[Coordinator] 终稿AI味复检异常（忽略）: {_afe}")


def _ending_integrity_check(final_chapter: int) -> None:
    """G16: 全书完结时检查结尾收束完整性（防烂尾）。

    检查项：①主线是否收束 ②伏笔是否全回收 ③角色弧线是否到达终点
    仅报告不阻断（结尾已生成，阻断无意义，但为后续修订提供方向）。
    """
    log("=" * 60)
    log("[Coordinator] G16 结尾收束完整性检查（防烂尾）")
    log("=" * 60)
    issues = []

    # 检查1：伏笔回收率
    try:
        from core.foreshadowing_ledger import load_ledger, dangling_threads
        ledger = load_ledger(NOVELS_DIR)
        dangling = dangling_threads(ledger, as_of_chapter=final_chapter)
        if dangling:
            issues.append(f"⚠️ 仍有 {len(dangling)} 条伏笔未回收（烂尾风险）")
            for t in dangling[:5]:
                issues.append(f"  - {t.get('id', '?')}: {t.get('setup', '')[:60]}")
    except Exception as exc:
        log(f"[Coordinator] 伏笔检查异常（忽略）: {exc}")

    # 检查2：角色弧线是否到达终点
    try:
        from core.arc_state import load_arc
        arc = load_arc(NOVELS_DIR, final_chapter)
        stage = arc.get("current_stage", "")
        if stage and stage != "destination":
            issues.append(f"⚠️ 主角弧线未到达终点（当前: {stage}，期望: destination）")
    except Exception as exc:
        log(f"[Coordinator] 弧线检查异常（忽略）: {exc}")

    if issues:
        log("[Coordinator] 结尾收束检查发现问题：")
        for issue in issues:
            log(f"  {issue}")
    else:
        log("[Coordinator] ✅ 结尾收束检查通过：主线收束、伏笔回收、弧线到位")


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

        # G3: 正文通过门禁后，抽取角色状态快照和成长弧线进度（供下一章 writer 注入）
        _extract_post_chapter_states(chapter)

        # G14: 终稿AI味复检——final 落盘后做一次 ai_flavor 检测，严重时记录告警
        _final_ai_flavor_check(chapter)

        progress = refresh_progress_from_status()
        progress["failed_chapters"] = []
        progress.pop("rewrite_queue", None)
        progress.pop("outline_rewrite_queue", None)
        save_progress(progress)

    # G16: 全书最后一章完成后，执行结尾收束检查（防烂尾）
    if end >= CONFIG.get("total_chapters", 9999):
        _ending_integrity_check(end)

    return True

def _global_outline_feedback(
    chapter: int,
    issues: list[dict],
    round_no: int,
    *,
    source: str = "outline_book_review",
    path_prefix: str = "outline_book_feedback",
    summary: str = "整本大纲总审要求修复跨章结构问题",
) -> Path:
    return outline_gate_module._global_outline_feedback(
        _runtime(),
        chapter,
        issues,
        round_no,
        source=source,
        path_prefix=path_prefix,
        summary=summary,
    )

def repair_book_relationship_targets(round_no: int = 1) -> list[int]:
    return outline_gate_module.repair_book_relationship_targets(_runtime(), round_no)

def repair_book_human_warmth_streaks(round_no: int = 1) -> list[int]:
    return outline_gate_module.repair_book_human_warmth_streaks(_runtime(), round_no)


def run_story_flow_audit_report(start: int, end: int) -> bool:
    """Generate a deterministic story-flow audit report without blocking the workflow."""
    script = MAINTENANCE_SCRIPTS["story_flow_audit.py"]
    cmd = [
        sys.executable,
        str(script),
        "--project",
        str(NOVELS_DIR),
        "--start",
        str(max(1, int(start or 1))),
        "--end",
        str(max(1, int(end or CONFIG.get("total_chapters", 1)))),
    ]
    child_log = LOGS_DIR / "story_flow_audit_child.log"
    rc = run_streaming_process(cmd, child_log)
    if rc != 0:
        log(f"[Coordinator] story_flow_audit 生成失败 (rc={rc})，不阻断主流程")
        return False
    log("[Coordinator] story_flow_audit 已生成 reports/story_flow_audit.json")
    return True

def _book_review_report() -> dict:
    """读取整本终审最终报告，不存在或未完成返回 {}。"""
    final_path = report_path(NOVELS_DIR, "book_review") / "final_book_review.json"
    if not final_path.exists():
        return {}
    try:
        return _load_json_file(final_path)
    except Exception as exc:
        return {}

def book_review_gate_passed() -> bool:
    """整本终审门禁：final_book_review.score >= book_reviewer.min_score 且 verdict 非 需重写。"""
    cfg = CONFIG.get("book_reviewer", {})
    if not cfg.get("enabled", True):
        return True
    report = _book_review_report()
    if not report or report.get("status") != "completed":
        return False
    score = report.get("score")
    if score is None:
        return False
    try:
        score = float(score)
    except (TypeError, ValueError):
        return False
    min_score = float(cfg.get("min_score", 8.5))
    verdict = str(report.get("verdict", "")).strip()
    if verdict in {"需重写", "reject", "rejected"}:
        return False
    return score >= min_score


def _book_repair_resume_end(chapters: list[int], requested_end: int | None = None) -> int | None:
    valid = []
    for ch in chapters:
        try:
            chapter = int(ch)
        except (TypeError, ValueError):
            continue
        if chapter > 0:
            valid.append(chapter)
    valid = sorted(set(valid))
    if not valid:
        return None
    total = int(CONFIG.get("total_chapters", max(valid)) or max(valid))
    upper = total
    if requested_end is not None:
        upper = min(upper, int(requested_end))
    cfg = CONFIG.get("book_reviewer", {}) if isinstance(CONFIG.get("book_reviewer"), dict) else {}
    if cfg.get("auto_resume_to_total_chapters", False):
        return upper
    followup = max(0, int(cfg.get("auto_resume_followup_chapters", 3) or 0))
    return min(upper, max(valid) + followup)


def _write_book_repair_manifest(
    report: dict,
    *,
    relationship_chapters: list[int],
    human_warmth_chapters: list[int],
) -> Path | None:
    targets = sorted(set(relationship_chapters + human_warmth_chapters))
    if not targets:
        return None
    def _chapter_repair_record(chapter: int) -> dict:
        feedback_files = []
        repair_types = []
        if chapter in relationship_chapters:
            repair_types.append("relationship")
            feedback_files.append(
                str(LOGS_DIR / f"book_relationship_feedback_ch{chapter:04d}_round1.json")
            )
        if chapter in human_warmth_chapters:
            repair_types.append("human_warmth")
            feedback_files.append(
                str(LOGS_DIR / f"book_human_warmth_feedback_ch{chapter:04d}_round1.json")
            )
        return {
            "chapter": chapter,
            "repair_types": repair_types,
            "feedback_files": feedback_files,
            "cleared_artifacts": [
                str(NOVELS_DIR / "chapters" / "draft" / f"chapter_{chapter:04d}.txt"),
                str(NOVELS_DIR / "chapters" / "review" / f"chapter_{chapter:04d}_review.json"),
                str(NOVELS_DIR / "chapters" / "final" / f"chapter_{chapter:04d}.txt"),
                str(NOVELS_DIR / "chapters" / "character_states" / f"chapter_{chapter:04d}.json"),
                str(NOVELS_DIR / "chapters" / "arc_states" / f"chapter_{chapter:04d}.json"),
                str(NOVELS_DIR / "chapters" / "relationship_states" / f"chapter_{chapter:04d}.json"),
            ],
            "post_resume_expected_artifacts": [
                str(NOVELS_DIR / "chapters" / "outline" / f"chapter_{chapter:04d}.json"),
                str(NOVELS_DIR / "chapters" / "outline_review" / f"chapter_{chapter:04d}_review.json"),
                str(NOVELS_DIR / "chapters" / "draft" / f"chapter_{chapter:04d}.txt"),
                str(NOVELS_DIR / "chapters" / "review" / f"chapter_{chapter:04d}_review.json"),
                str(NOVELS_DIR / "chapters" / "final" / f"chapter_{chapter:04d}.txt"),
                str(NOVELS_DIR / "chapters" / "relationship_states" / f"chapter_{chapter:04d}.json"),
            ],
            "post_resume_checks": [
                "outline_review.design_gates.repair_feedback_closed 必须通过",
                "review.local_analysis.human_warmth_detection.passed 不得为 false",
                "relationship_states 应记录修复后的关系欠账、照料、误会或承诺变化",
                "重新运行 book_reviewer 后不应再出现同一关系线或同一 human_warmth_streak 目标",
            ],
        }

    resume_from = min(targets)
    resume_to = _book_repair_resume_end(targets, CONFIG["total_chapters"]) or CONFIG["total_chapters"]
    book_cfg = CONFIG.get("book_reviewer", {}) if isinstance(CONFIG.get("book_reviewer"), dict) else {}
    manifest = {
        "status": "repair_targets_generated",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "book_review_score": report.get("score"),
        "book_review_verdict": report.get("verdict", ""),
        "resume_from_chapter": resume_from,
        "resume_to_chapter": resume_to,
        "resume_range_basis": {
            "target_chapters": targets,
            "followup_chapters": max(0, int(book_cfg.get("auto_resume_followup_chapters", 3) or 0)),
            "auto_resume_to_total_chapters": bool(book_cfg.get("auto_resume_to_total_chapters", False)),
        },
        "relationship_repair_chapters": relationship_chapters,
        "human_warmth_repair_chapters": human_warmth_chapters,
        "all_repair_chapters": targets,
        "repair_baseline": {
            "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "human_warmth_streaks": _local_scan_human_warmth_summary(),
        },
        "repair_records": [_chapter_repair_record(chapter) for chapter in targets],
        "next_action": (
            "断点续跑 coordinator；目标章节的大纲已按终审反馈重过门，"
            "对应 draft/review/final 与派生状态已清理，将从最早目标章按受限范围重新生成正文。"
        ),
        "post_resume_verification": {
            "command": f'python "scripts/pipeline/coordinator.py" --project "{NOVELS_DIR}" --start {resume_from} --end {resume_to}',
            "then_run": f'python "scripts/maintenance/book_reviewer.py" --project "{NOVELS_DIR}" --force',
            "success_evidence": [
                "目标章 final 重新生成且 review 达到 reviewer.min_score",
                "目标章 relationship_states 重新生成",
                "reports/book_review/final_book_review.json 达到 book_reviewer.min_score",
                "reports/book_review/local_full_scan.json 不再包含同一连续烟火气缺口",
            ],
        },
        "reports": {
            "final_book_review": str(report_path(NOVELS_DIR, "book_review") / "final_book_review.json"),
            "local_full_scan": str(report_path(NOVELS_DIR, "book_review") / "local_full_scan.json"),
            "text_artifact_cleanup_log": str(LOGS_DIR / "text_artifact_cleanup.jsonl"),
        },
    }
    path = report_path(NOVELS_DIR, "book_review") / "repair_manifest.json"
    atomic_write_json(path, manifest)
    return path


def _latest_cleanup_snapshots(chapters: list[int]) -> dict[int, dict]:
    wanted = set(chapters)
    path = LOGS_DIR / "text_artifact_cleanup.jsonl"
    if not path.exists():
        return {}
    snapshots: dict[int, dict] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        return {}
    for line in lines:
        try:
            entry = json.loads(line)
        except Exception as exc:
            continue
        try:
            chapter = int(entry.get("chapter"))
        except (TypeError, ValueError):
            continue
        if chapter not in wanted:
            continue
        chapter_snapshot = snapshots.setdefault(chapter, {})
        records = entry.get("records")
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            pre_delete = record.get("pre_delete_snapshot")
            if not isinstance(pre_delete, dict):
                continue
            if "human_warmth_detection" in pre_delete:
                chapter_snapshot["before_human_warmth"] = pre_delete["human_warmth_detection"]
            if "relationship_state" in pre_delete:
                chapter_snapshot["before_relationship_state"] = pre_delete["relationship_state"]
        chapter_snapshot["cleanup_reason"] = entry.get("reason", "")
        chapter_snapshot["cleanup_timestamp"] = entry.get("timestamp", "")
    return snapshots


def _relationship_improved(before: dict, after: dict) -> bool | None:
    if not before and not after:
        return None
    before_open = before.get("open_count") if isinstance(before, dict) else None
    after_open = after.get("open_count") if isinstance(after, dict) else None
    before_resolved = before.get("resolved_count") if isinstance(before, dict) else None
    after_resolved = after.get("resolved_count") if isinstance(after, dict) else None
    if isinstance(before_open, int) and isinstance(after_open, int) and after_open < before_open:
        return True
    if isinstance(before_resolved, int) and isinstance(after_resolved, int) and after_resolved > before_resolved:
        return True
    if before != after:
        return True
    return False


def _relationship_items_by_pair(summary: dict) -> dict[str, dict]:
    if not isinstance(summary, dict):
        return {}
    result: dict[str, dict] = {}
    for key in ("open_items", "resolved_items"):
        items = summary.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            pair = str(item.get("pair", "")).strip()
            if pair:
                result[pair] = item
    return result


def _relationship_pair_diffs(before: dict, after: dict) -> list[dict]:
    before_by_pair = _relationship_items_by_pair(before)
    after_by_pair = _relationship_items_by_pair(after)
    diffs = []
    for pair in sorted(set(before_by_pair) | set(after_by_pair)):
        old = before_by_pair.get(pair, {})
        new = after_by_pair.get(pair, {})
        changes = []
        field_changes = {}
        for field in ("debt", "misunderstanding", "promise", "care_action", "next_pressure", "resolved"):
            old_value = old.get(field)
            new_value = new.get(field)
            if old_value == new_value:
                continue
            field_changes[field] = {"before": old_value, "after": new_value}
            if field == "resolved" and new_value is True:
                changes.append("resolved")
            elif field == "care_action" and new_value:
                changes.append("care_action_added")
            elif field in {"debt", "misunderstanding"} and old_value and not new_value:
                changes.append(f"{field}_cleared")
            elif field == "next_pressure" and old_value != new_value:
                changes.append("pressure_changed")
            elif field == "promise" and old_value != new_value:
                changes.append("promise_changed")
            else:
                changes.append(f"{field}_changed")
        if changes:
            diffs.append({
                "pair": pair,
                "changes": changes,
                "field_changes": field_changes,
            })
    return diffs[:10]


def _human_warmth_streak_summary(local_scan: dict) -> dict:
    if not isinstance(local_scan, dict):
        return {"count": 0, "issues": []}
    issues = []
    for issue in local_scan.get("issues", []):
        if not isinstance(issue, dict) or issue.get("category") != "human_warmth_streak":
            continue
        chapters = []
        for chapter in issue.get("chapters", []):
            try:
                chapters.append(int(chapter))
            except (TypeError, ValueError):
                continue
        issues.append({
            "severity": issue.get("severity", ""),
            "chapters": chapters,
            "detail": issue.get("detail", ""),
            "evidence": issue.get("evidence", ""),
            "suggestion": issue.get("suggestion", ""),
        })
    return {
        "checked_at": local_scan.get("checked_at", ""),
        "count": len(issues),
        "issues": issues[:12],
    }


def _local_scan_human_warmth_summary() -> dict:
    local_scan_path = report_path(NOVELS_DIR, "book_review") / "local_full_scan.json"
    return _human_warmth_streak_summary(_read_json_safely(local_scan_path))


def _streaks_touching_chapters(summary: dict, chapters: list[int]) -> list[dict]:
    chapter_set = set(chapters)
    if not chapter_set or not isinstance(summary, dict):
        return []
    matches = []
    for issue in summary.get("issues", []):
        if not isinstance(issue, dict):
            continue
        issue_chapters = set()
        for chapter in issue.get("chapters", []):
            try:
                issue_chapters.add(int(chapter))
            except (TypeError, ValueError):
                continue
        if issue_chapters & chapter_set:
            matches.append(issue)
    return matches


def _verification_candidate_issues(candidate: dict) -> list[dict]:
    if not isinstance(candidate, dict) or candidate.get("chapter") is None:
        return []
    try:
        chapter = int(candidate.get("chapter"))
    except (TypeError, ValueError):
        return []
    issues = []
    for index, check in enumerate(candidate.get("failed_checks", []), start=1):
        text = str(check or "").strip()
        if not text:
            continue
        repair_types = candidate.get("repair_types", [])
        repair_text = "、".join(str(item) for item in repair_types if item) or "book_review_verification"
        issues.append({
            "severity": "major",
            "category": f"book_verification_retry_{index}",
            "chapters": [chapter],
            "detail": text,
            "evidence": f"repair_manifest verification failed for chapter {chapter}: {text}",
            "suggestion": (
                f"本章必须针对复核失败项重修（类型: {repair_text}）。"
                "大纲需写明可落地的剧情动作、关系回声或人情味触点，后续正文重写必须能被 Reviewer 本地检测到。"
            ),
        })
    return issues


def _materialize_next_repair_feedback(next_repair_candidates: list[dict]) -> list[dict]:
    generated = []
    for candidate in next_repair_candidates:
        if not isinstance(candidate, dict) or candidate.get("chapter") is None:
            continue
        try:
            chapter = int(candidate.get("chapter"))
        except (TypeError, ValueError):
            continue
        issues = _verification_candidate_issues(candidate)
        if not issues:
            continue
        feedback = _global_outline_feedback(
            chapter,
            issues,
            1,
            source="book_verification_retry",
            path_prefix="book_verification_feedback",
            summary="整本终审修复复核失败，要求对目标章生成下一轮精确修复大纲反馈",
        )
        candidate.setdefault("generated_feedback_files", []).append(str(feedback))
        generated.append({
            "chapter": chapter,
            "feedback_file": str(feedback),
            "failed_checks": candidate.get("failed_checks", []),
        })
    return generated


def _verify_book_repair_manifest(report: dict) -> None:
    """Mark an existing repair manifest verified after a passing book review."""
    path = report_path(NOVELS_DIR, "book_review") / "repair_manifest.json"
    if not path.exists():
        return
    manifest = _load_json_file(path)
    if not isinstance(manifest, dict) or not manifest.get("all_repair_chapters"):
        return
    chapters = []
    for chapter in manifest.get("all_repair_chapters", []):
        try:
            chapter_no = int(chapter)
        except (TypeError, ValueError):
            continue
        if chapter_no > 0:
            chapters.append(chapter_no)
    if not chapters:
        return
    cfg = CONFIG.get("book_reviewer", {}) if isinstance(CONFIG.get("book_reviewer"), dict) else {}
    min_book_score = float(cfg.get("min_score", 8.5))
    reviewer_min = float(CONFIG.get("reviewer", {}).get("min_score", 8.5))
    statuses = scan_chapter_status(NOVELS_DIR, min(chapters), max(chapters))
    cleanup_snapshots = _latest_cleanup_snapshots(chapters)
    chapter_results = []
    verified = True
    for chapter in chapters:
        status = statuses.get(chapter)
        review = _load_json_file(review_dir(NOVELS_DIR) / f"chapter_{chapter:04d}_review.json")
        human_warmth = {}
        local_analysis = review.get("local_analysis") if isinstance(review, dict) else {}
        if isinstance(local_analysis, dict):
            human_warmth = local_analysis.get("human_warmth_detection") or {}
        relationship_state = NOVELS_DIR / "chapters" / "relationship_states" / f"chapter_{chapter:04d}.json"
        relationship_after = _relationship_state_summary(_read_json_safely(relationship_state))
        before_snapshot = cleanup_snapshots.get(chapter, {})
        before_human_warmth = before_snapshot.get("before_human_warmth", {})
        after_human_warmth = _human_warmth_summary_from_review(review)
        before_relationship = before_snapshot.get("before_relationship_state", {})
        issues = []
        if not status or not status.final_ok:
            issues.append("final 未重新生成或未通过质量门")
        if not status or not status.review_ok or (status.review_score is not None and status.review_score < reviewer_min):
            issues.append("review 未达到 reviewer.min_score")
        if isinstance(human_warmth, dict) and human_warmth.get("passed") is False:
            issues.append("human_warmth_detection 仍未通过")
        if not relationship_state.exists():
            issues.append("relationship_states 未重新生成")
        chapter_verified = not issues
        verified = verified and chapter_verified
        chapter_results.append({
            "chapter": chapter,
            "verified": chapter_verified,
            "review_score": status.review_score if status else None,
            "final_ok": bool(status.final_ok) if status else False,
            "relationship_state_exists": relationship_state.exists(),
            "human_warmth_passed": human_warmth.get("passed") if isinstance(human_warmth, dict) else None,
            "before_after_evidence": {
                "cleanup_reason": before_snapshot.get("cleanup_reason", ""),
                "cleanup_timestamp": before_snapshot.get("cleanup_timestamp", ""),
                "human_warmth": {
                    "before": before_human_warmth,
                    "after": after_human_warmth,
                    "passed_changed": (
                        isinstance(before_human_warmth, dict)
                        and before_human_warmth.get("passed") != after_human_warmth.get("passed")
                    ),
                },
                "relationship_state": {
                    "before": before_relationship,
                    "after": relationship_after,
                    "improved_or_changed": _relationship_improved(before_relationship, relationship_after),
                    "pair_diffs": _relationship_pair_diffs(before_relationship, relationship_after),
                },
            },
            "issues": issues,
        })
    try:
        book_score = float(report.get("score"))
    except (TypeError, ValueError):
        book_score = 0.0
    book_verified = book_score >= min_book_score and str(report.get("verdict", "")).strip() not in {"需重写", "reject", "rejected"}
    verified = verified and book_verified
    baseline = manifest.get("repair_baseline") if isinstance(manifest.get("repair_baseline"), dict) else {}
    before_scan = baseline.get("human_warmth_streaks", {}) if isinstance(baseline, dict) else {}
    after_scan = _local_scan_human_warmth_summary()
    before_touching = _streaks_touching_chapters(before_scan, chapters)
    after_touching = _streaks_touching_chapters(after_scan, chapters)
    next_repair_candidates = []
    repair_records = manifest.get("repair_records", [])
    records_by_chapter = {}
    if isinstance(repair_records, list):
        for record in repair_records:
            if not isinstance(record, dict):
                continue
            try:
                record_chapter = int(record.get("chapter"))
            except (TypeError, ValueError):
                continue
            records_by_chapter[record_chapter] = record
    for result in chapter_results:
        if result.get("verified"):
            continue
        chapter = result.get("chapter")
        record = records_by_chapter.get(chapter, {})
        next_repair_candidates.append({
            "chapter": chapter,
            "repair_types": record.get("repair_types", []),
            "failed_checks": result.get("issues", []),
            "feedback_files": record.get("feedback_files", []),
            "suggested_next_action": (
                "重新回灌该章终审反馈并断点续跑正文；若 final/review 已存在但本地检测仍失败，"
                "优先把 failed_checks 转成 Writer 下一轮 targeted_repairs。"
            ),
        })
    book_issues = [] if book_verified else ["整本终审仍未达到 book_reviewer.min_score 或 verdict 仍为需重写"]
    if book_issues:
        next_repair_candidates.append({
            "chapter": None,
            "repair_types": ["book_review"],
            "failed_checks": book_issues,
            "feedback_files": [str(report_path(NOVELS_DIR, "book_review") / "final_book_review.json")],
            "suggested_next_action": "先读取终审 summary/required_fixes，再分配到最晚可改的目标章节生成新一轮反馈。",
        })
    next_feedback_files = _materialize_next_repair_feedback(next_repair_candidates)
    manifest["verification"] = {
        "verified": verified,
        "verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "book_review_score": report.get("score"),
        "book_review_verdict": report.get("verdict", ""),
        "book_review_min_score": min_book_score,
        "book_scan_evidence": {
            "human_warmth_streaks": {
                "before": before_scan,
                "after": after_scan,
                "before_touching_repair_chapters": before_touching,
                "after_touching_repair_chapters": after_touching,
                "touching_streaks_removed": bool(before_touching) and not bool(after_touching),
            },
        },
        "chapter_results": chapter_results,
        "issues": book_issues,
        "next_repair_candidates": next_repair_candidates,
        "next_feedback_files": next_feedback_files,
    }
    if verified:
        manifest["status"] = "verified"
    else:
        manifest["status"] = "verification_failed"
    atomic_write_json(path, manifest)


def _book_repair_resume_chapter() -> int | None:
    path = report_path(NOVELS_DIR, "book_review") / "repair_manifest.json"
    if not path.exists():
        return None
    manifest = _read_json_safely(path)
    try:
        chapter = int(manifest.get("resume_from_chapter"))
    except (TypeError, ValueError):
        return None
    if chapter <= 0:
        return None
    total = int(CONFIG.get("total_chapters", chapter) or chapter)
    return min(chapter, total)


def _book_repair_resume_range(requested_end: int) -> tuple[int, int] | None:
    path = report_path(NOVELS_DIR, "book_review") / "repair_manifest.json"
    if not path.exists():
        return None
    manifest = _read_json_safely(path)
    try:
        start = int(manifest.get("resume_from_chapter"))
    except (TypeError, ValueError):
        return None
    if start <= 0:
        return None
    total = int(CONFIG.get("total_chapters", requested_end) or requested_end)
    upper = min(int(requested_end), total)
    try:
        end = int(manifest.get("resume_to_chapter"))
    except (TypeError, ValueError):
        chapters = manifest.get("all_repair_chapters")
        if not isinstance(chapters, list):
            chapters = [start]
        end = _book_repair_resume_end(chapters, upper) or upper
    start = min(start, upper)
    end = min(max(end, start), upper)
    return start, end


def _record_book_auto_resume_attempt(
    *,
    cycle: int,
    resume_from: int | None,
    end_chapter: int,
    workflow_ok: bool,
    review_passed: bool | None,
) -> None:
    path = report_path(NOVELS_DIR, "book_review") / "repair_manifest.json"
    if not path.exists():
        return
    manifest = _read_json_safely(path)
    if not manifest:
        return
    attempts = manifest.get("auto_resume_attempts")
    if not isinstance(attempts, list):
        attempts = []
    attempts.append({
        "cycle": cycle,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "resume_from_chapter": resume_from,
        "end_chapter": end_chapter,
        "workflow_ok": workflow_ok,
        "book_review_passed": review_passed,
    })
    manifest["auto_resume_attempts"] = attempts[-10:]
    atomic_write_json(path, manifest)


def _verification_retry_feedback_files() -> list[dict]:
    path = report_path(NOVELS_DIR, "book_review") / "repair_manifest.json"
    if not path.exists():
        return []
    manifest = _read_json_safely(path)
    verification = manifest.get("verification") if isinstance(manifest, dict) else {}
    items = verification.get("next_feedback_files") if isinstance(verification, dict) else []
    result = []
    if not isinstance(items, list):
        return result
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            chapter = int(item.get("chapter"))
        except (TypeError, ValueError):
            continue
        feedback = Path(str(item.get("feedback_file", "")))
        if chapter > 0 and feedback.exists():
            result.append({"chapter": chapter, "feedback_file": feedback})
    return result


def _record_verification_retry_attempt(
    *,
    cycle: int,
    chapters: list[int],
    resume_to: int | None = None,
    outline_ok: bool,
    workflow_ok: bool,
    review_passed: bool | None,
) -> None:
    path = report_path(NOVELS_DIR, "book_review") / "repair_manifest.json"
    if not path.exists():
        return
    manifest = _read_json_safely(path)
    if not manifest:
        return
    attempts = manifest.get("verification_retry_attempts")
    if not isinstance(attempts, list):
        attempts = []
    attempts.append({
        "cycle": cycle,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "chapters": chapters,
        "resume_from_chapter": min(chapters) if chapters else None,
        "resume_to_chapter": resume_to,
        "outline_ok": outline_ok,
        "workflow_ok": workflow_ok,
        "book_review_passed": review_passed,
    })
    manifest["verification_retry_attempts"] = attempts[-10:]
    atomic_write_json(path, manifest)


def _retry_verification_feedback(
    *,
    end_chapter: int,
    outline_lookahead: int,
    progress: dict,
) -> bool:
    cfg = CONFIG.get("book_reviewer", {}) if isinstance(CONFIG.get("book_reviewer"), dict) else {}
    if not cfg.get("auto_retry_verification_feedback", False):
        return False
    max_cycles = max(1, int(cfg.get("max_verification_retry_cycles", 1) or 1))
    for cycle in range(1, max_cycles + 1):
        feedback_items = _verification_retry_feedback_files()
        if not feedback_items:
            log("[Coordinator] 验证失败反馈自动重试已启用，但没有可用 book_verification_feedback 文件")
            _record_verification_retry_attempt(
                cycle=cycle,
                chapters=[],
                resume_to=None,
                outline_ok=False,
                workflow_ok=False,
                review_passed=None,
            )
            return False
        chapters = sorted({item["chapter"] for item in feedback_items})
        log(f"[Coordinator] 验证失败反馈自动重试第{cycle}/{max_cycles}轮: 目标章节 {chapters}")
        outline_ok = True
        for item in feedback_items:
            chapter = item["chapter"]
            feedback = item["feedback_file"]
            if not process_outline_gate(chapter, push_on_failure=False, initial_feedback=feedback):
                log(f"[Coordinator] 第{chapter}章应用验证失败反馈后仍未通过大纲门")
                outline_ok = False
                break
            _drop_text_artifacts(chapter, reason="整本终审复核失败反馈已回灌大纲，需重写正文")
        if not outline_ok:
            _record_verification_retry_attempt(
                cycle=cycle,
                chapters=chapters,
                resume_to=None,
                outline_ok=False,
                workflow_ok=False,
                review_passed=None,
            )
            return False
        resume_from = min(chapters)
        resume_to = _book_repair_resume_end(chapters, end_chapter) or end_chapter
        log(f"[Coordinator] 验证失败反馈自动重试正文续跑范围: 第{resume_from}-{resume_to}章")
        workflow_ok = run_serial_quality_workflow(resume_from, resume_to, outline_lookahead)
        if not workflow_ok:
            refresh_progress_from_status(progress)
            _record_verification_retry_attempt(
                cycle=cycle,
                chapters=chapters,
                resume_to=resume_to,
                outline_ok=True,
                workflow_ok=False,
                review_passed=None,
            )
            return False
        review_passed = process_book_review_gate(push_on_failure=True, force=True)
        _record_verification_retry_attempt(
            cycle=cycle,
            chapters=chapters,
            resume_to=resume_to,
            outline_ok=True,
            workflow_ok=True,
            review_passed=review_passed,
        )
        if review_passed:
            refresh_progress_from_status(progress)
            log("[Coordinator] 验证失败反馈自动重试后已通过整本终审")
            return True
    return False


def _auto_resume_book_review_repair(
    *,
    end_chapter: int,
    outline_lookahead: int,
    progress: dict,
) -> bool:
    cfg = CONFIG.get("book_reviewer", {}) if isinstance(CONFIG.get("book_reviewer"), dict) else {}
    if not cfg.get("auto_resume_after_repair", False):
        return False
    max_cycles = max(1, int(cfg.get("max_auto_resume_cycles", 1) or 1))
    for cycle in range(1, max_cycles + 1):
        resume_range = _book_repair_resume_range(end_chapter)
        if resume_range is None:
            log("[Coordinator] 终审自动续跑已启用，但 repair_manifest 缺少可用 resume_from_chapter/resume_to_chapter")
            _record_book_auto_resume_attempt(
                cycle=cycle,
                resume_from=None,
                end_chapter=end_chapter,
                workflow_ok=False,
                review_passed=None,
            )
            return False
        resume_from, resume_to = resume_range
        log(
            f"[Coordinator] 终审修复自动续跑第{cycle}/{max_cycles}轮: "
            f"第{resume_from}-{resume_to}章"
        )
        workflow_ok = run_serial_quality_workflow(resume_from, resume_to, outline_lookahead)
        if not workflow_ok:
            refresh_progress_from_status(progress)
            _record_book_auto_resume_attempt(
                cycle=cycle,
                resume_from=resume_from,
                end_chapter=resume_to,
                workflow_ok=False,
                review_passed=None,
            )
            return False
        review_passed = process_book_review_gate(push_on_failure=True, force=True)
        _record_book_auto_resume_attempt(
            cycle=cycle,
            resume_from=resume_from,
            end_chapter=resume_to,
            workflow_ok=True,
            review_passed=review_passed,
        )
        if review_passed:
            refresh_progress_from_status(progress)
            log("[Coordinator] 终审修复自动续跑后已通过整本终审")
            return True
        if _retry_verification_feedback(
            end_chapter=end_chapter,
            outline_lookahead=outline_lookahead,
            progress=progress,
        ):
            return True
    return False


def run_book_review(force: bool = False) -> int:
    """运行整本分层终审（segment→volume→final）。返回子进程退出码。

    force=True 时透传 --force，强制重新审查（忽略缓存指纹）。
    """
    cfg = CONFIG.get("book_reviewer", {})
    if not cfg.get("enabled", True):
        log("[Coordinator] book_reviewer.enabled=false，跳过整本终审")
        return 0

    log("=" * 60)
    log("[Coordinator] 启动整本终审 Book Reviewer（分段→卷级→终审）")
    log("=" * 60)
    notify_stage("整本终审", "开始")
    script = MAINTENANCE_SCRIPTS["book_reviewer.py"]
    workers = int(cfg.get("workers", 5) or 5)
    cmd = [
        sys.executable,
        str(script),
        "--project",
        str(NOVELS_DIR),
        "--workers",
        str(workers),
    ]
    if force:
        cmd.append("--force")
    child_log = LOGS_DIR / "book_reviewer.log"
    rc = run_streaming_process(cmd, child_log)
    notify_stage("整本终审", "完成" if rc == 0 else "异常", error="" if rc == 0 else f"退出码 {rc}")
    return rc

def process_book_review_gate(*, push_on_failure: bool = True, force: bool = False) -> bool:
    """整本终审门禁：跑终审，未达 min_score 则阻断流程并告警。

    返回 True 表示通过（或门禁关闭），False 表示阻断。
    """
    cfg = CONFIG.get("book_reviewer", {})
    if not cfg.get("enabled", True):
        return True
    rc = run_book_review(force=force)
    if rc != 0:
        log(f"[Coordinator] 整本终审子进程异常退出 (rc={rc})")
        if push_on_failure:
            _push_error(config=CONFIG, title=get_book_title(), error="整本终审子进程异常退出")
        return False
    if book_review_gate_passed():
        report = _book_review_report()
        log(f"[Coordinator] 整本终审门禁通过，终审分 {report.get('score')}")
        _verify_book_repair_manifest(report)
        return True
    report = _book_review_report()
    score = report.get("score")
    min_score = float(cfg.get("min_score", 8.5))
    verdict = report.get("verdict", "")
    message = (
        f"整本终审未达 {min_score} 分（实际 {score}，结论 {verdict}）。"
        f"主要扣分维度见 reports/book_review/final_book_review.md"
    )
    repaired_relationship_chapters: list[int] = []
    if cfg.get("repair_relationship_targets_on_failure", True):
        repaired_relationship_chapters = repair_book_relationship_targets(round_no=1)
        if repaired_relationship_chapters:
            message += (
                " 已将关系线修复目标回灌到大纲门并清理对应正文产物，"
                f"目标章节: {repaired_relationship_chapters}。请断点续跑以重写这些章节。"
            )
    repaired_human_warmth_chapters: list[int] = []
    if cfg.get("repair_human_warmth_streaks_on_failure", True):
        repaired_human_warmth_chapters = repair_book_human_warmth_streaks(round_no=1)
        if repaired_human_warmth_chapters:
            message += (
                " 已将烟火气连续性问题回灌到大纲门并清理对应正文产物，"
                f"目标章节: {repaired_human_warmth_chapters}。请断点续跑以重写这些章节。"
            )
    manifest = _write_book_repair_manifest(
        report,
        relationship_chapters=repaired_relationship_chapters,
        human_warmth_chapters=repaired_human_warmth_chapters,
    )
    if manifest:
        message += f" 修复清单: {manifest.relative_to(NOVELS_DIR)}"
    log(f"[ERROR] {message}")
    if push_on_failure:
        _push_error(config=CONFIG, title=get_book_title(), error=message)
    return False

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

        "--skip-book-review",
        action="store_true",
        help="跳过正文阶段后的整本终审门禁（仅调试用，默认不跳过）",
    )
    parser.add_argument(
        "--force-book-review",
        action="store_true",
        help="忽略整本终审缓存并重新终审",
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

    progress = refresh_progress_from_status()
    log(f"[Coordinator] 当前进度: 大纲审 {progress.get('last_outline_reviewed_chapter', 0)} 章，已生成 {progress['last_generated_chapter']} 章，已审查 {progress['last_reviewed_chapter']} 章")

    if args.batch_size:
        log("[Coordinator] 当前使用单章质量门主流程，--batch-size 仅保留兼容，不会改变章节推进方式")
    end_chapter = args.end or CONFIG["total_chapters"]

    if not args.skip_planner:
        log("[Coordinator] 启动Planner生成或校验世界观、角色档案...")
        if run_planner() != 0:
            log("[ERROR] Planner生成或契约校验失败，请检查日志")
            return
        progress["planner_done"] = True
        save_progress(progress)
    elif check_base_files_exist():
        log("[Coordinator] 已显式跳过Planner，使用现有世界观和角色档案")
        progress["planner_done"] = True
        save_progress(progress)
    else:
        progress["planner_done"] = False
        save_progress(progress)
        log("[ERROR] 已跳过Planner，但 world.json 或 characters.json 不存在")
        return

    if not progress["planner_done"]:
        log("[ERROR] Planner未完成且跳过标志未设置")
        return

    media_cfg = CONFIG.get("media", {})
    if media_cfg.get("enabled", True) and media_cfg.get("generate_after_planner", True):
        if not media_prompts_ready() and not args.skip_planner:
            log("[Coordinator] 检测到媒体提示词缺失，重新运行Planner补齐 media_prompts...")
            if run_planner("--with-media-prompts") != 0:
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
        refresh_progress_from_status(progress)
        log("[Coordinator] 单章质量门失败，流程已停止")
        return

    # G15: 整本终审门禁——全书正文完成后做分层终审（segment→volume→final），
    # 未达 book_reviewer.min_score 则阻断，整本结构缺陷在生成时即可见。
    if not args.skip_book_review:
        book_cfg = CONFIG.get("book_reviewer", {})
        if book_cfg.get("enabled", True) and book_cfg.get("required_on_finish", True):
            if not process_book_review_gate(push_on_failure=True, force=args.force_book_review):
                if not _auto_resume_book_review_repair(
                    end_chapter=end_chapter,
                    outline_lookahead=outline_lookahead,
                    progress=progress,
                ):
                    # 终审不过：仍生成总结报告（便于诊断），但不推送"完成"
                    generate_summary_report()
                    run_story_flow_audit_report(args.start, end_chapter)
                    refresh_progress_from_status(progress)
                    log("[Coordinator] 整本终审门禁未通过，流程已停止")
                    return
            # 终审通过后强制刷新一次，确保进度与终审结果一致
            if args.force_book_review:
                refresh_progress_from_status(progress)

    report = generate_summary_report()
    run_story_flow_audit_report(args.start, end_chapter)
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
