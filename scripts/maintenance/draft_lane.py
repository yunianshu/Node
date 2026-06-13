#!/usr/bin/env python3
"""Background draft lane that waits for approved outlines before writing."""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from core.novel_config import load_config, resolve_project_dir
from core.workflow_state import (
    load_outline_review_status,
    load_review_status,
    read_text_length,
    scan_one_chapter,
    outline_review_path,
    review_dir,
)
from pipeline import coordinator


_LOG_LOCK = threading.Lock()
_REVIEW_LOCK = threading.Lock()


def log(project: Path, message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [DraftLane] {message}"
    with _LOG_LOCK:
        print(line, flush=True)
        log_file = project / "logs" / "draft_lane.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def run_script(project: Path, script_name: str, chapter: int) -> int:
    script = TOOLS_ROOT / "pipeline" / script_name
    cmd = [sys.executable, str(script), "--project", str(project), "--chapter", str(chapter)]
    return subprocess.run(cmd, check=False).returncode


def run_cancellable_script(project: Path, args: list[str], child_log: Path, stop_event: threading.Event) -> int:
    child_log.parent.mkdir(parents=True, exist_ok=True)
    with open(child_log, "a", encoding="utf-8") as lf:
        lf.write(f"$ {' '.join(args)}\n")
        lf.flush()
        proc = subprocess.Popen(
            args,
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
                    terminate_process_tree(proc)
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
                                lf.write(line.rstrip("\n") + "\n")
                        except queue.Empty:
                            pass
                    return rc
                time.sleep(0.2)
        finally:
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except Exception:
                pass


def terminate_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            proc.terminate()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def unlink_with_retry(path: Path, *, attempts: int = 8, delay: float = 0.5) -> None:
    for attempt in range(1, attempts + 1):
        try:
            path.unlink(missing_ok=True)
            return
        except OSError:
            if attempt >= attempts:
                raise
            time.sleep(delay)


def promote_to_final(project: Path, chapter: int) -> None:
    source = project / "chapters" / "draft" / f"chapter_{chapter:04d}.txt"
    target = project / "chapters" / "final" / f"chapter_{chapter:04d}.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")


def publish_candidate(project: Path, chapter: int, draft_file: Path, review_file: Path, draft_min_score: float) -> tuple[bool, str]:
    exists, words, text_ok = read_text_length(draft_file)
    if not exists or not text_ok:
        return False, f"candidate_text_quality_failed words={words}"
    _, status, score, review_ok = load_review_status(review_file, draft_min_score)
    if not review_ok:
        return False, f"candidate_review_failed status={status} score={score} min_score={draft_min_score:g}"

    target_draft = project / "chapters" / "draft" / f"chapter_{chapter:04d}.txt"
    target_review = review_dir(project) / f"chapter_{chapter:04d}_review.json"
    target_draft.parent.mkdir(parents=True, exist_ok=True)
    target_review.parent.mkdir(parents=True, exist_ok=True)
    target_draft.write_text(draft_file.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
    target_review.write_text(review_file.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
    promote_to_final(project, chapter)
    if not final_ready(project, chapter):
        return False, "final_quality_failed"
    return True, f"review_score={score} words={words}"


def outline_approved(project: Path, chapter: int, min_score: float) -> tuple[bool, float | None]:
    config = load_config(project)
    gate_cfg = config.get("outline_quality_gate", {})
    require_quality_gate = bool(
        isinstance(gate_cfg, dict) and gate_cfg.get("enabled", False)
    )
    _, _, score, ok = load_outline_review_status(
        outline_review_path(project, chapter),
        min_score,
        require_quality_gate=require_quality_gate,
    )
    return ok, score


def outline_review_detail(project: Path, chapter: int, min_score: float) -> str:
    path = outline_review_path(project, chapter)
    if not path.exists():
        return f"review=missing min_score={min_score:g}"
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception as exc:
        return f"review=invalid_json error={exc} min_score={min_score:g}"

    status = data.get("status", "unknown")
    score = data.get("overall_score")
    verdict = data.get("verdict", "unknown")
    return f"status={status} score={score} verdict={verdict} min_score={min_score:g}"


def draft_review_passed(project: Path, chapter: int, min_score: float) -> tuple[bool, float | None]:
    review_file = review_dir(project) / f"chapter_{chapter:04d}_review.json"
    _, _, score, ok = load_review_status(review_file, min_score)
    return ok, score


def draft_review_detail(project: Path, chapter: int, min_score: float) -> str:
    path = review_dir(project) / f"chapter_{chapter:04d}_review.json"
    if not path.exists():
        return f"review=missing min_score={min_score:g}"
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception as exc:
        return f"review=invalid_json error={exc} min_score={min_score:g}"

    status = data.get("status", "unknown")
    score = data.get("overall_score")
    verdict = data.get("verdict", "unknown")
    return f"status={status} score={score} verdict={verdict} min_score={min_score:g}"


def draft_race_config(config: dict) -> dict:
    cfg = config.get("draft_race", {}) if isinstance(config.get("draft_race"), dict) else {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "candidates": max(1, int(cfg.get("candidates", 3) or 3)),
        "max_workers": max(1, int(cfg.get("max_workers", cfg.get("candidates", 3)) or 3)),
        "stop_on_first_pass": bool(cfg.get("stop_on_first_pass", True)),
    }


def candidate_paths(project: Path, chapter: int, round_no: int, attempt: int, candidate_no: int) -> tuple[Path, Path, Path]:
    root = project / "logs" / "draft_candidates" / f"ch{chapter:04d}" / f"round{round_no}_attempt{attempt}"
    prefix = f"candidate_{candidate_no:02d}"
    return root / f"{prefix}.txt", root / f"{prefix}_review.json", root / f"{prefix}.log"


def candidate_feedback(review_data: dict, chapter: int, candidate_no: int, round_no: int, attempt: int) -> dict:
    item = coordinator._review_feedback(review_data, chapter, "draft", round_no, attempt)
    item["candidate"] = candidate_no
    return item


def candidate_score(result: dict) -> float:
    try:
        return float(result.get("overall_score"))
    except (TypeError, ValueError):
        return -1.0


def run_draft_candidate(
    project: Path,
    chapter: int,
    round_no: int,
    attempt: int,
    candidate_no: int,
    feedback_file: Path | None,
    stop_event: threading.Event,
    draft_min_score: float,
) -> dict:
    draft_file, candidate_review_file, child_log = candidate_paths(project, chapter, round_no, attempt, candidate_no)
    draft_file.parent.mkdir(parents=True, exist_ok=True)
    unlink_with_retry(draft_file)
    unlink_with_retry(candidate_review_file)

    writer_args = [
        sys.executable,
        str(TOOLS_ROOT / "pipeline" / "writer.py"),
        "--project",
        str(project),
        "--chapter",
        str(chapter),
        "--output-file",
        str(draft_file),
    ]
    if feedback_file:
        writer_args += ["--review-feedback", str(feedback_file)]
    log(project, f"第{chapter}章正文候选{candidate_no}开始生成")
    rc = run_cancellable_script(project, writer_args, child_log, stop_event)
    if rc != 0:
        return {
            "chapter": chapter,
            "candidate": candidate_no,
            "status": "writer_failed" if rc != -9 else "cancelled",
            "weaknesses": ["候选正文生成失败、被取消或输出不完整"],
            "suggestions": ["重新生成时必须严格执行大纲、满足字数并强化冲突推进"],
        }

    exists, words, text_ok = read_text_length(draft_file)
    if not exists or not text_ok:
        return {
            "chapter": chapter,
            "candidate": candidate_no,
            "status": "candidate_text_quality_failed",
            "weaknesses": [f"候选正文未通过本地质量门，字数={words}"],
            "suggestions": ["下一次必须写到5000字以上，确保结尾完整、无截断、无重复段落"],
            "candidate_file": str(draft_file),
            "review_file": str(candidate_review_file),
        }

    reviewer_args = [
        sys.executable,
        str(TOOLS_ROOT / "pipeline" / "reviewer.py"),
        "--project",
        str(project),
        "--chapter",
        str(chapter),
        "--chapter-file",
        str(draft_file),
        "--review-file",
        str(candidate_review_file),
    ]
    log(project, f"第{chapter}章正文候选{candidate_no}开始审查")
    rc = run_cancellable_script(project, reviewer_args, child_log, stop_event)
    if rc != 0:
        return {
            "chapter": chapter,
            "candidate": candidate_no,
            "status": "reviewer_failed" if rc != -9 else "cancelled",
            "weaknesses": ["候选正文审查器执行失败或被取消"],
            "suggestions": ["检查候选日志、模型响应和审查JSON写入"],
        }

    review_data = {}
    try:
        review_data = json.loads(candidate_review_file.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        pass
    result = candidate_feedback(review_data, chapter, candidate_no, round_no, attempt)
    _, _, _, ok = load_review_status(candidate_review_file, draft_min_score)
    result["passed"] = ok
    result["candidate_file"] = str(draft_file)
    result["review_file"] = str(candidate_review_file)
    return result


def final_exists(project: Path, chapter: int) -> bool:
    return (project / "chapters" / "final" / f"chapter_{chapter:04d}.txt").exists()


def final_ready(project: Path, chapter: int) -> bool:
    return scan_one_chapter(project, chapter).final_ok


def highest_contiguous_final(project: Path, start: int, end: int) -> int:
    last = start - 1
    for chapter in range(start, end + 1):
        if not final_ready(project, chapter):
            break
        last = chapter
    return last


def process_chapter(
    project: Path,
    chapter: int,
    draft_min_score: float,
    max_rounds: int,
    attempts_per_round: int,
) -> tuple[bool, str]:
    if final_ready(project, chapter):
        return True, "final_ok"

    total_attempts = max(1, max_rounds) * max(1, attempts_per_round)
    last_detail = ""
    for attempt in range(1, total_attempts + 1):
        log(project, f"第{chapter}章初稿生成/审查 attempt={attempt}/{total_attempts}")
        writer_rc = run_script(project, "writer.py", chapter)
        if writer_rc != 0:
            last_detail = f"writer_rc={writer_rc}"
            log(project, f"第{chapter}章 writer 失败({last_detail})，继续重试")
            continue

        review_file = review_dir(project) / f"chapter_{chapter:04d}_review.json"
        try:
            unlink_with_retry(review_file)
        except OSError as exc:
            last_detail = f"review_unlink_failed={exc}"
            log(project, f"第{chapter}章旧审查文件删除失败({last_detail})，继续重试")
            continue

        # Reviewer updates shared progress files; keep that part serialized to avoid progress tmp-file races.
        with _REVIEW_LOCK:
            reviewer_rc = run_script(project, "reviewer.py", chapter)
        if reviewer_rc != 0:
            last_detail = f"reviewer_rc={reviewer_rc}"
            log(project, f"第{chapter}章 reviewer 失败({last_detail})，继续重试")
            continue

        draft_ok, review_score = draft_review_passed(project, chapter, draft_min_score)
        if not draft_ok:
            last_detail = draft_review_detail(project, chapter, draft_min_score)
            log(project, f"第{chapter}章初稿未达标({last_detail})，下一次将带审查意见重写")
            continue

        promote_to_final(project, chapter)
        if not final_ready(project, chapter):
            last_detail = "final_quality_failed"
            log(project, f"第{chapter}章终稿本地质量检查失败，继续重写")
            continue
        return True, f"review_score={review_score} attempts={attempt}"

    return False, last_detail or "draft_attempts_exhausted"


def process_chapter_race(
    project: Path,
    chapter: int,
    draft_min_score: float,
    max_rounds: int,
    attempts_per_round: int,
    race_cfg: dict,
) -> tuple[bool, str]:
    if final_ready(project, chapter):
        return True, "final_ok"

    candidates = int(race_cfg.get("candidates", 3))
    max_workers = min(candidates, int(race_cfg.get("max_workers", candidates)))
    reviews: list[dict] = []
    feedback_file: Path | None = None

    existing_review_file = review_dir(project) / f"chapter_{chapter:04d}_review.json"
    if existing_review_file.exists():
        try:
            existing_review = json.loads(existing_review_file.read_text(encoding="utf-8", errors="ignore"))
            reviews.append(coordinator._review_feedback(existing_review, chapter, "draft", 1, 0))
            feedback_file = coordinator._write_gate_feedback(chapter, "draft", reviews, 1)
            log(project, f"第{chapter}章读取现有初稿审查意见，反馈给正文候选: {feedback_file}")
        except Exception as exc:
            log(project, f"第{chapter}章读取现有初稿审查意见失败: {exc}")

    for round_no in range(1, max_rounds + 1):
        if reviews:
            feedback_file = coordinator._write_gate_feedback(chapter, "draft", reviews, round_no)
            log(project, f"第{chapter}章正文赛马进入第{round_no}轮原因调整: {feedback_file}")

        for attempt in range(1, attempts_per_round + 1):
            log(project, f"第{chapter}章正文候选赛马 {round_no}.{attempt} candidates={candidates}")
            stop_event = threading.Event()
            accepted: dict | None = None
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_candidates = {
                    executor.submit(
                        run_draft_candidate,
                        project,
                        chapter,
                        round_no,
                        attempt,
                        candidate_no,
                        feedback_file,
                        stop_event,
                        draft_min_score,
                    ): candidate_no
                    for candidate_no in range(1, candidates + 1)
                }
                pending = set(future_candidates)
                while pending:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        candidate_no = future_candidates.get(future, 0)
                        try:
                            result = future.result()
                        except Exception as exc:
                            result = {
                                "chapter": chapter,
                                "candidate": candidate_no,
                                "status": "candidate_exception",
                                "weaknesses": [f"候选进程异常: {exc}"],
                                "suggestions": ["检查候选日志、模型响应和候选文件写入"],
                            }
                        reviews.append(result)
                        if not result.get("passed"):
                            continue
                        if race_cfg.get("stop_on_first_pass", True):
                            accepted = result
                            stop_event.set()
                            break
                        if accepted is None or candidate_score(result) > candidate_score(accepted):
                            accepted = result
                    if stop_event.is_set():
                        break
                if stop_event.is_set():
                    wait(pending, timeout=10)

            if accepted:
                ok, detail = publish_candidate(
                    project,
                    chapter,
                    Path(str(accepted["candidate_file"])),
                    Path(str(accepted["review_file"])),
                    draft_min_score,
                )
                if ok:
                    return True, f"{detail} candidates={len(reviews)}"
                reviews.append({
                    "chapter": chapter,
                    "status": "candidate_publish_failed",
                    "weaknesses": [detail],
                    "suggestions": ["重新生成候选并检查正式目录写入和本地质量门"],
                })
                feedback_file = coordinator._write_gate_feedback(chapter, "draft", reviews, round_no)
                log(project, f"第{chapter}章正文候选发布失败，下一次使用反馈: {feedback_file}")
                continue

            feedback_file = coordinator._write_gate_feedback(chapter, "draft", reviews, round_no)
            best = coordinator._failure_analysis(chapter, "draft", reviews).get("best_score")
            log(project, f"第{chapter}章本轮正文候选均未通过，最佳分数={best}，下一次使用汇总反馈: {feedback_file}")

    report = coordinator._write_failure_report(chapter, "draft", reviews)
    coordinator._push_gate_failure(chapter, "初稿审查", report)
    return False, f"draft_race_attempts_exhausted best={report.get('best_score')}"


def main() -> int:
    parser = argparse.ArgumentParser(description="等待大纲过审后生成初稿")
    parser.add_argument("--project", "-p", type=str, default=os.getenv("NOVEL_PROJECT_DIR", ""))
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=0)
    parser.add_argument("--wait-seconds", type=float, default=5.0)
    parser.add_argument("--workers", type=int, default=0, help="章节级初稿 worker 数，默认读取 coordinator.draft_workers 或 1；draft_race 只控制同章候选并发")
    parser.add_argument(
        "--continuity-window",
        type=int,
        default=0,
        help="最多领先已完成终稿多少章，默认等于 workers；设为1则严格顺序",
    )
    args = parser.parse_args()

    try:
        project = resolve_project_dir(args.project)
    except ValueError as exc:
        print(f"错误: {exc}")
        return 1

    config = load_config(project)
    end = args.end or int(config["total_chapters"])
    outline_min_score = float(config.get("outline_reviewer", {}).get("min_score", 8.5))
    draft_min_score = float(config.get("reviewer", {}).get("min_score", 7.0))
    max_rounds = int(config.get("coordinator", {}).get("draft_analysis_rounds", 3) or 3)
    attempts_per_round = int(config.get("coordinator", {}).get("draft_attempts_per_round", 3) or 3)
    workers = args.workers or int(config.get("coordinator", {}).get("draft_workers", 1) or 1)
    workers = max(1, workers)
    continuity_window = args.continuity_window or int(config.get("coordinator", {}).get("draft_continuity_window", 1) or 1)
    continuity_window = max(1, continuity_window)
    race_cfg = draft_race_config(config)

    coordinator.init_project(project)
    push_interval = int(config.get("coordinator", {}).get("push_interval_seconds", 120))
    coordinator.ensure_wechat_pusher_process(push_interval)
    coordinator.ensure_gate_watchdog_process("draft")

    log(
        project,
        f"启动: 第{args.start}-{end}章，workers={workers}，continuity_window={continuity_window}，"
        f"正文门槛={draft_min_score:g}，每章最多{max_rounds * attempts_per_round}次重写，"
        f"draft_race={race_cfg['enabled']} candidates={race_cfg['candidates']}，"
        f"未过审大纲每{args.wait_seconds:g}秒检查一次",
    )
    next_chapter = max(1, args.start)
    in_flight = {}
    wait_seconds = max(1.0, args.wait_seconds)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        while next_chapter <= end or in_flight:
            scheduled = False
            while next_chapter <= end and len(in_flight) < workers:
                final_frontier = highest_contiguous_final(project, args.start, end)
                if next_chapter > final_frontier + continuity_window:
                    break

                outline_ok, outline_score = outline_approved(project, next_chapter, outline_min_score)
                if not outline_ok:
                    detail = outline_review_detail(project, next_chapter, outline_min_score)
                    log(project, f"第{next_chapter}章大纲未过审({detail})，等待{wait_seconds:g}秒")
                    break

                log(project, f"第{next_chapter}章大纲已过审(score={outline_score})，提交初稿 worker")
                if race_cfg["enabled"]:
                    future = executor.submit(
                        process_chapter_race,
                        project,
                        next_chapter,
                        draft_min_score,
                        max_rounds,
                        attempts_per_round,
                        race_cfg,
                    )
                else:
                    future = executor.submit(
                        process_chapter,
                        project,
                        next_chapter,
                        draft_min_score,
                        max_rounds,
                        attempts_per_round,
                    )
                in_flight[future] = next_chapter
                next_chapter += 1
                scheduled = True

            if not in_flight:
                time.sleep(wait_seconds)
                continue

            timeout = 0 if scheduled else wait_seconds
            done, _ = wait(in_flight, timeout=timeout, return_when=FIRST_COMPLETED)
            for future in done:
                chapter = in_flight.pop(future)
                try:
                    ok, detail = future.result()
                except Exception as exc:
                    log(project, f"第{chapter}章初稿 worker 异常: {exc}")
                    next_chapter = min(next_chapter, chapter)
                    continue
                if not ok:
                    log(project, f"第{chapter}章初稿处理失败({detail})，停止 draft lane")
                    return 1
                log(project, f"第{chapter}章初稿审查通过，已写入 final ({detail})")

    log(project, "全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
