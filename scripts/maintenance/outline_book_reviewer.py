#!/usr/bin/env python3
"""Hierarchical whole-book review for per-chapter novel outlines."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import sys
import time
import statistics
from pathlib import Path

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from core.json_repair import fix_inner_quotes, fix_truncated_json
from core.mmx_client import MmxError, call_mmx
from core.novel_config import load_config, resolve_project_dir
from core.outline_batch_lock import lock_batch
from core.workflow_state import (
    atomic_write_json,
    load_outline_review_status,
    outline_chapter_path,
    outline_review_path,
)


def log(project: Path, message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [OutlineBookReviewer] {message}"
    print(line, flush=True)
    path = project / "logs" / "outline_book_reviewer.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def parse_json(raw: str) -> dict:
    text = raw.strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()
    candidates = [text]
    start = text.find("{")
    if start >= 0:
        value = text[start:]
        candidates.extend((
            fix_inner_quotes(value),
            fix_truncated_json(fix_inner_quotes(value)),
            fix_truncated_json(value),
        ))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return {"status": "parse_error", "raw_response": raw[:3000]}


def ai_call(project: Path, config: dict, prompt: str, raw_name: str) -> dict:
    cfg = config.get("outline_book_reviewer", {})
    try:
        raw = call_mmx(
            "你是长篇中文网络小说的总编。只依据输入证据审查，输出合法紧凑JSON，不使用Markdown。",
            prompt,
            model=config["model"],
            mmx_path=config["mmx_path"],
            max_tokens=int(cfg.get("max_tokens", 4096)),
            temperature=float(cfg.get("temperature", 0.2)),
            retries=int(cfg.get("retries", 2)),
            retry_delay=float(cfg.get("retry_delay", 5.0)),
            timeout=int(cfg.get("timeout_seconds", 300)),
            log_dir=project / "logs" / "raw_responses",
            raw_name=raw_name,
            qps=float(config.get("api_qps", 5.0)),
            rate_state_dir=project / "logs" / "rate_limit",
        )
    except MmxError as exc:
        return {"status": "failed", "error": str(exc)}
    result = parse_json(raw)
    result.setdefault("status", "completed")
    return result


def aggregate_reports(
    reports: list[dict],
    *,
    min_score: float,
    required_rounds: int,
    required_votes: int,
    score_key: str = "score",
) -> dict:
    scores = []
    for report in reports:
        try:
            scores.append(float(report.get(score_key)))
        except (TypeError, ValueError):
            pass
    complete = len(reports) == required_rounds and len(scores) == required_rounds
    median_score = float(statistics.median(scores)) if complete else 0.0
    pass_votes = 0
    for report in reports:
        try:
            score_value = float(report.get(score_key))
        except (TypeError, ValueError):
            continue
        if (
            report.get("status") == "completed"
            and report.get("verdict") == "通过"
            and score_value >= min_score
        ):
            pass_votes += 1
    passed = complete and median_score >= min_score and pass_votes >= required_votes
    issues = [
        issue
        for report in reports
        for issue in report.get("issues", [])
        if isinstance(issue, dict)
    ]
    best = max(reports, key=lambda item: float(item.get(score_key, 0) or 0), default={})
    result = dict(best)
    result.update({
        "status": "completed" if complete else "incomplete_rounds",
        score_key: round(median_score, 3),
        "verdict": "通过" if passed else "需修订",
        "quality_gate": {
            "passed": passed,
            "rounds": required_rounds,
            "pass_votes": pass_votes,
            "required_votes": required_votes,
            "round_scores": scores,
        },
        "review_rounds": reports,
        "issues": issues[:15],
    })
    return result


def outline_context(project: Path, chapter: int) -> dict:
    item = load_json(outline_chapter_path(project, chapter))
    return {
        "chapter": chapter,
        "title": item.get("title", ""),
        "summary": str(item.get("summary", ""))[:500],
        "key_events": item.get("key_events", [])[:7] if isinstance(item.get("key_events"), list) else item.get("key_events"),
        "foreshadowing": item.get("foreshadowing", ""),
        "power_progression": item.get("power_progression", ""),
        "hook": item.get("chapter_hook", ""),
        "emotional_arc": item.get("emotional_arc", ""),
    }


def local_scan(project: Path, total: int, chapter_min_score: float) -> dict:
    issues = []
    scores = []
    config = load_config(project)
    gate_cfg = config.get("outline_quality_gate", {})
    require_quality_gate = bool(
        isinstance(gate_cfg, dict) and gate_cfg.get("enabled", False)
    )
    for chapter in range(1, total + 1):
        outline = outline_chapter_path(project, chapter)
        review = outline_review_path(project, chapter)
        if not outline.exists():
            issues.append({"severity": "critical", "chapters": [chapter], "category": "missing_outline", "detail": "缺少单章大纲"})
            continue
        exists, status, score, ok = load_outline_review_status(
            review,
            chapter_min_score,
            require_quality_gate=require_quality_gate,
        )
        if isinstance(score, (int, float)):
            scores.append(float(score))
        if not exists or not ok:
            issues.append({
                "severity": "critical",
                "chapters": [chapter],
                "category": "outline_review_gate",
                "detail": f"大纲审查未通过 status={status} score={score}",
            })
    return {
        "status": "completed",
        "total_chapters": total,
        "issue_count": len(issues),
        "average_chapter_score": round(sum(scores) / len(scores), 3) if scores else 0,
        "min_chapter_score": min(scores) if scores else None,
        "issues": issues,
    }


def source_fingerprint(project: Path, total: int) -> str:
    digest = hashlib.sha256()
    for chapter in range(1, total + 1):
        for path in (
            outline_chapter_path(project, chapter),
            outline_review_path(project, chapter),
        ):
            digest.update(str(path.relative_to(project)).encode("utf-8"))
            if not path.exists():
                digest.update(b"missing")
                continue
            stat = path.stat()
            digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode("ascii"))
    return digest.hexdigest()


def segment_review(
    project: Path,
    config: dict,
    start: int,
    end: int,
    output: Path,
    force: bool,
    lock_on_pass: bool = True,
) -> dict:
    if output.exists() and not force:
        existing = load_json(output)
        if (
            existing.get("status") == "completed"
            and isinstance(existing.get("quality_gate"), dict)
        ):
            return existing
    chapters = [outline_context(project, chapter) for chapter in range(start, end + 1)]
    prompt = f"""审查第{start}-{end}章连续大纲：
{json.dumps(chapters, ensure_ascii=False)}

重点检查跨章重复、时间地点和人物状态、能力成长、伏笔推进、高潮分布、钩子承接。
只输出：
{{"status":"completed","range":"{start}-{end}","score":0到10,"verdict":"通过/需修订/严重问题",
"summary":"180字内","end_state":"段末人物、地点、能力与主线状态",
"open_threads":["未解决伏笔"],"issues":[{{"severity":"critical/major/minor","chapters":[章节号],
"category":"类别","detail":"问题","evidence":"证据","suggestion":"建议"}}]}}
issues最多8条。"""
    cfg = config.get("outline_book_reviewer", {})
    rounds = int(cfg.get("review_rounds", 3) or 3)
    votes = int(cfg.get("required_votes", rounds // 2 + 1) or 2)
    min_score = float(cfg.get("min_score", 9.0))
    reports = [
        ai_call(project, config, prompt, f"outline_segment_{start:04d}_{end:04d}_round{round_no}")
        for round_no in range(1, rounds + 1)
    ]
    result = aggregate_reports(
        reports,
        min_score=min_score,
        required_rounds=rounds,
        required_votes=votes,
    )
    result.update({"start": start, "end": end})
    atomic_write_json(output, result)
    if lock_on_pass and result.get("quality_gate", {}).get("passed") is True:
        lock_batch(
            project,
            start,
            end,
            score=float(result.get("score", 0) or 0),
            report=str(output.relative_to(project)),
        )
    return result


def volume_review(project: Path, config: dict, start: int, end: int, segments: list[dict], output: Path, force: bool) -> dict:
    if output.exists() and not force:
        existing = load_json(output)
        if (
            existing.get("status") == "completed"
            and isinstance(existing.get("quality_gate"), dict)
        ):
            return existing
    prompt = f"""审查第{start}-{end}章卷级大纲结构：
{json.dumps(segments, ensure_ascii=False)}

检查主线推进、人物弧线、能力曲线、高潮密度、桥段重复、伏笔管理和卷末收束。
只输出：
{{"status":"completed","range":"{start}-{end}","score":0到10,"verdict":"通过/需修订/严重问题",
"summary":"250字内","arc_progression":"本卷结构","open_threads":["卷末事项"],
"issues":[{{"severity":"critical/major/minor","chapters":[章节号],"category":"类别",
"detail":"问题","evidence":"证据","suggestion":"建议"}}]}}
issues最多10条。"""
    cfg = config.get("outline_book_reviewer", {})
    rounds = int(cfg.get("review_rounds", 3) or 3)
    votes = int(cfg.get("required_votes", rounds // 2 + 1) or 2)
    min_score = float(cfg.get("min_score", 9.0))
    reports = [
        ai_call(project, config, prompt, f"outline_volume_{start:04d}_{end:04d}_round{round_no}")
        for round_no in range(1, rounds + 1)
    ]
    result = aggregate_reports(
        reports,
        min_score=min_score,
        required_rounds=rounds,
        required_votes=votes,
    )
    result.update({"start": start, "end": end})
    atomic_write_json(output, result)
    return result


def final_review(
    project: Path,
    config: dict,
    scan: dict,
    volumes: list[dict],
    output: Path,
    *,
    preflight: bool = False,
) -> dict:
    world = load_json(project / "world.json")
    min_score = float(config.get("outline_book_reviewer", {}).get("min_score", 9.0))
    prompt = f"""对整本大纲做最终审查。

书籍设定：
{json.dumps({"title": world.get("title"), "overall_arc": world.get("overall_arc"), "three_act_structure": world.get("three_act_structure")}, ensure_ascii=False)}

本地完整性：
{json.dumps({key: scan.get(key) for key in ("total_chapters", "issue_count", "average_chapter_score", "min_chapter_score")}, ensure_ascii=False)}

卷级报告：
{json.dumps(volumes, ensure_ascii=False)}

整本通过门槛为{min_score:g}分。检查开篇到终局闭环、主线和人物弧线、力量体系、伏笔回收、高潮分布及重复桥段。
只输出：
{{"status":"completed","score":0到10,"verdict":"通过/需修订/严重问题",
"executive_summary":"400字内","strengths":["优势"],"unresolved_threads":["未闭环事项"],
"issues":[{{"severity":"critical/major/minor","chapters":[章节号],"category":"类别",
"detail":"问题","evidence":"证据","suggestion":"建议"}}]}}
issues最多15条。"""
    cfg = config.get("outline_book_reviewer", {})
    rounds = int(cfg.get("review_rounds", 3) or 3)
    votes = int(cfg.get("required_votes", rounds // 2 + 1) or 2)
    reports = [
        ai_call(project, config, prompt, f"outline_final_review_round{round_no}")
        for round_no in range(1, rounds + 1)
    ]
    result = aggregate_reports(
        reports,
        min_score=min_score,
        required_rounds=rounds,
        required_votes=votes,
    )
    score = result.get("score")
    try:
        score_value = float(score)
    except (TypeError, ValueError):
        score_value = 0.0
    result["gate_min_score"] = min_score
    missing_outline = any(
        issue.get("category") == "missing_outline"
        for issue in scan.get("issues", [])
        if isinstance(issue, dict)
    )
    result["preflight"] = preflight
    result["preflight_passed"] = (
        result.get("status") == "completed"
        and result.get("verdict") == "通过"
        and score_value >= min_score
        and not missing_outline
    )
    result["gate_passed"] = (
        result["preflight_passed"]
        and not scan.get("issues")
        and all(
            volume.get("quality_gate", {}).get("passed") is True
            for volume in volumes
        )
    )
    atomic_write_json(output, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="整本大纲分段、卷级、全书三级审查")
    parser.add_argument("--project", "-p", default=os.getenv("NOVEL_PROJECT_DIR", ""))
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="允许逐章评分未达门槛，先完成跨章一致性总审；只审查，不修改大纲",
    )
    args = parser.parse_args()

    project = resolve_project_dir(args.project)
    config = load_config(project)
    cfg = config.get("outline_book_reviewer", {})
    total = int(config["total_chapters"])
    segment_size = max(5, int(cfg.get("segment_size", 25)))
    volume_size = max(segment_size, int(cfg.get("volume_size", 100)))
    workers = max(1, int(cfg.get("workers", 3)))
    reports = project / "reports" / "outline_book_review"
    segments_dir = reports / "segments"
    volumes_dir = reports / "volumes"

    fingerprint = source_fingerprint(project, total)
    previous_manifest = load_json(reports / "manifest.json")
    cached_final = load_json(reports / "final_outline_review.json")
    if (
        not args.force
        and previous_manifest.get("source_fingerprint") == fingerprint
        and cached_final.get("status") == "completed"
        and isinstance(cached_final.get("quality_gate"), dict)
        and bool(cached_final.get("preflight", False)) == args.preflight
    ):
        log(
            project,
            f"大纲与逐章审查未变化，复用整本总审 score={cached_final.get('score')} "
            f"gate_passed={cached_final.get('gate_passed')}",
        )
        passed_key = "preflight_passed" if args.preflight else "gate_passed"
        return 0 if cached_final.get(passed_key) else 1
    force = args.force or previous_manifest.get("source_fingerprint") != fingerprint
    scan = local_scan(project, total, float(config.get("outline_reviewer", {}).get("min_score", 8.5)))
    atomic_write_json(reports / "local_scan.json", scan)
    missing_issues = [
        issue for issue in scan["issues"]
        if issue.get("category") == "missing_outline"
    ]
    if missing_issues or (scan["issues"] and not args.preflight):
        log(project, f"本地门失败，发现 {len(scan['issues'])} 个缺失或未过审章节")
        atomic_write_json(reports / "final_outline_review.json", {
            "status": "blocked",
            "gate_passed": False,
            "reason": "per_chapter_outline_gate_incomplete",
            "issue_count": len(scan["issues"]),
        })
        return 1
    if args.preflight and scan["issues"]:
        log(
            project,
            f"预审模式继续执行：{len(scan['issues'])} 章逐章评分尚未达门槛，"
            "将作为全局审查证据，不阻断分段和卷级检查",
        )

    segment_ranges = [(start, min(total, start + segment_size - 1)) for start in range(1, total + 1, segment_size)]
    segments: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                segment_review, project, config, start, end,
                segments_dir / f"segment_{start:04d}_{end:04d}.json", force,
                not args.preflight,
            ): start
            for start, end in segment_ranges
        }
        for future in as_completed(futures):
            start = futures[future]
            segments[start] = future.result()
            log(project, f"分段审查完成: {start}")
    failed_segments = [
        value for value in segments.values()
        if value.get("quality_gate", {}).get("passed") is not True
    ]
    if failed_segments and not args.preflight:
        atomic_write_json(reports / "final_outline_review.json", {
            "status": "blocked",
            "gate_passed": False,
            "reason": "segment_quality_gate_failed",
            "failed_ranges": [item.get("range") or f"{item.get('start')}-{item.get('end')}" for item in failed_segments],
            "issues": [issue for item in failed_segments for issue in item.get("issues", [])][:30],
        })
        return 1

    volume_ranges = [(start, min(total, start + volume_size - 1)) for start in range(1, total + 1, volume_size)]
    volumes: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for start, end in volume_ranges:
            source = [segments[item_start] for item_start, item_end in segment_ranges if start <= item_start <= end]
            future = executor.submit(
                volume_review, project, config, start, end, source,
                volumes_dir / f"volume_{start:04d}_{end:04d}.json", force,
            )
            futures[future] = start
        for future in as_completed(futures):
            start = futures[future]
            volumes[start] = future.result()
            log(project, f"卷级审查完成: {start}")
    failed_volumes = [
        value for value in volumes.values()
        if value.get("quality_gate", {}).get("passed") is not True
    ]
    if failed_volumes and not args.preflight:
        atomic_write_json(reports / "final_outline_review.json", {
            "status": "blocked",
            "gate_passed": False,
            "reason": "volume_quality_gate_failed",
            "failed_ranges": [item.get("range") or f"{item.get('start')}-{item.get('end')}" for item in failed_volumes],
            "issues": [issue for item in failed_volumes for issue in item.get("issues", [])][:30],
        })
        return 1

    final = final_review(
        project,
        config,
        scan,
        [volumes[start] for start, _ in volume_ranges],
        reports / "final_outline_review.json",
        preflight=args.preflight,
    )
    atomic_write_json(reports / "manifest.json", {
        "status": "completed",
        "segments": len(segments),
        "volumes": len(volumes),
        "score": final.get("score"),
        "verdict": final.get("verdict"),
        "gate_passed": final.get("gate_passed", False),
        "preflight": args.preflight,
        "preflight_passed": final.get("preflight_passed", False),
        "source_fingerprint": fingerprint,
    })
    log(
        project,
        f"整本大纲审查完成 score={final.get('score')} "
        f"preflight_passed={final.get('preflight_passed')} gate_passed={final.get('gate_passed')}",
    )
    passed_key = "preflight_passed" if args.preflight else "gate_passed"
    return 0 if final.get(passed_key) else 1


if __name__ == "__main__":
    raise SystemExit(main())
