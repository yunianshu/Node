#!/usr/bin/env python3
"""Hierarchical whole-book review for per-chapter novel outlines."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import re
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
from core.outline_quality_gate import detect_adjacent_event_repetition
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
    semantic_retries = max(0, int(cfg.get("semantic_retries", 1) or 0))
    result = {"status": "failed", "error": "no_response"}
    for semantic_attempt in range(semantic_retries + 1):
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
                raw_name=f"{raw_name}_semantic{semantic_attempt}",
                qps=float(config.get("api_qps", 5.0)),
                rate_state_dir=project / "logs" / "rate_limit",
            )
        except MmxError as exc:
            result = {"status": "failed", "error": str(exc)}
            continue
        result = parse_json(raw)
        result.setdefault("status", "completed")
        if result.get("status") != "parse_error":
            return result
        log(
            project,
            f"{raw_name} JSON解析失败，原审查语义重试 "
            f"{semantic_attempt + 1}/{semantic_retries}",
        )
    return result


def aggregate_reports(
    reports: list[dict],
    *,
    min_score: float,
    required_rounds: int,
    required_votes: int,
    score_key: str = "score",
) -> dict:
    valid_reports = []
    scores = []
    for report in reports:
        if report.get("status") != "completed":
            continue
        try:
            score = float(report.get(score_key))
        except (TypeError, ValueError):
            continue
        valid_reports.append(report)
        scores.append(score)
    quorum = len(valid_reports) >= required_votes
    median_score = float(statistics.median(scores)) if quorum else 0.0
    pass_votes = 0
    for report in valid_reports:
        try:
            score_value = float(report.get(score_key))
        except (TypeError, ValueError):
            continue
        if (
            report.get("verdict") == "通过"
            and score_value >= min_score
        ):
            pass_votes += 1
    passed = quorum and median_score >= min_score and pass_votes >= required_votes
    issues = _aggregate_consensus_issues(valid_reports, required_votes)
    best = max(valid_reports, key=lambda item: float(item.get(score_key, 0) or 0), default={})
    result = dict(best)
    result.update({
        "status": "completed" if quorum else "incomplete_rounds",
        score_key: round(median_score, 3),
        "verdict": "通过" if passed else "需修订",
        "quality_gate": {
            "passed": passed,
            "rounds": len(valid_reports),
            "requested_rounds": required_rounds,
            "pass_votes": pass_votes,
            "required_votes": required_votes,
            "round_scores": scores,
        },
        "review_rounds": reports,
        "issues": issues[:1],
    })
    return result


def _issue_category_bucket(issue: dict) -> str:
    buckets = (
        ("identity", ("身份", "卧底", "揭露")),
        ("character_state", ("人物状态", "生死", "伤势", "昏迷", "复活")),
        ("timeline", ("时间", "时序", "先后顺序")),
        ("repetition", ("重复", "场景", "动作链", "桥段")),
        ("rescue", ("营救", "救援", "转移")),
        ("evidence", ("证据", "物证", "道具", "归属", "U盘", "密钥")),
        ("foreshadowing", ("伏笔", "钩子", "悬念")),
        ("rhythm", ("节奏", "高潮", "密度")),
    )
    category = str(issue.get("category", ""))
    detail = str(issue.get("detail", ""))
    for source in (category, detail):
        for bucket, markers in buckets:
            if any(marker in source for marker in markers):
                return bucket
    return str(issue.get("category", "")).strip().lower()


def _issue_bigrams(text: str) -> set[str]:
    compact = re.sub(r"[\W_]+", "", str(text or ""), flags=re.UNICODE)
    return {
        compact[index:index + 2]
        for index in range(max(0, len(compact) - 1))
    }


def _same_issue(left: dict, right: dict) -> bool:
    if _issue_category_bucket(left) != _issue_category_bucket(right):
        return False
    left_chapters = {
        value for value in left.get("chapters", [])
        if isinstance(value, int)
    }
    right_chapters = {
        value for value in right.get("chapters", [])
        if isinstance(value, int)
    }
    if left_chapters and right_chapters:
        overlap = len(left_chapters & right_chapters) / min(
            len(left_chapters),
            len(right_chapters),
        )
        if overlap < 0.5:
            return False
    left_tokens = _issue_bigrams(left.get("detail", ""))
    right_tokens = _issue_bigrams(right.get("detail", ""))
    if not left_tokens or not right_tokens:
        return False
    similarity = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    return similarity >= 0.08


def _dedupe_issues(issues: list[dict]) -> list[dict]:
    severity_order = {"critical": 0, "major": 1, "minor": 2}
    result: list[dict] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        duplicate_index = next(
            (index for index, current in enumerate(result) if _same_issue(current, issue)),
            None,
        )
        if duplicate_index is None:
            result.append(dict(issue))
            continue
        current = result[duplicate_index]
        current_severity = severity_order.get(str(current.get("severity", "")).lower(), 3)
        issue_severity = severity_order.get(str(issue.get("severity", "")).lower(), 3)
        if issue_severity < current_severity:
            current["severity"] = issue.get("severity")
        current["chapters"] = sorted({
            value
            for source in (current.get("chapters", []), issue.get("chapters", []))
            if isinstance(source, list)
            for value in source
            if isinstance(value, int)
        })
        for field in ("detail", "evidence", "suggestion"):
            if len(str(issue.get(field, ""))) > len(str(current.get(field, ""))):
                current[field] = issue.get(field)
    return sorted(
        result,
        key=lambda item: (
            severity_order.get(str(item.get("severity", "")).lower(), 3),
            min(
                [
                    value
                    for value in item.get("chapters", [])
                    if isinstance(value, int)
                ]
                or [10**9]
            ),
        ),
    )


def _aggregate_consensus_issues(reports: list[dict], required_votes: int) -> list[dict]:
    clusters: list[dict] = []
    for report_index, report in enumerate(reports):
        per_report = _dedupe_issues(
            report.get("issues", []) if isinstance(report.get("issues"), list) else []
        )
        for issue in per_report:
            cluster = next(
                (
                    item for item in clusters
                    if _same_issue(item["issue"], issue)
                ),
                None,
            )
            if cluster is None:
                clusters.append({"issue": dict(issue), "reports": {report_index}})
                continue
            cluster["issue"] = _dedupe_issues([cluster["issue"], issue])[0]
            cluster["reports"].add(report_index)

    selected = []
    for cluster in clusters:
        issue = dict(cluster["issue"])
        votes = len(cluster["reports"])
        severity = str(issue.get("severity", "")).lower()
        if votes >= required_votes or severity == "critical":
            issue["review_votes"] = votes
            selected.append(issue)
    if selected:
        return _dedupe_issues(selected)[:1]

    fallback = _dedupe_issues([
        issue
        for report in reports
        for issue in report.get("issues", [])
        if isinstance(issue, dict)
    ])
    return fallback[:1]


def _clip_text(value, limit: int) -> str:
    return str(value or "").strip().replace("\r", "").replace("\n", " ")[:limit]


def _compact_list(values, *, limit: int, item_limit: int) -> list[str]:
    if not isinstance(values, list):
        return [_clip_text(values, item_limit)] if str(values or "").strip() else []
    return [
        _clip_text(item, item_limit)
        for item in values[:limit]
        if str(item or "").strip()
    ]


def outline_context(project: Path, chapter: int) -> dict:
    item = load_json(outline_chapter_path(project, chapter))
    return {
        "chapter": chapter,
        "title": _clip_text(item.get("title"), 40),
        "time": _clip_text(item.get("time_progression"), 80),
        "place": _clip_text(item.get("location"), 60),
        "cast": _compact_list(item.get("characters_involved", []), limit=8, item_limit=20),
        "summary": _clip_text(item.get("summary"), 220),
        "events": _compact_list(item.get("key_events", []), limit=5, item_limit=110),
        "hook": _clip_text(item.get("chapter_hook"), 140),
        "threads": _clip_text(item.get("foreshadowing"), 160),
    }


def compact_evidence_report(report: dict) -> dict:
    return {
        "status": report.get("status"),
        "range": report.get("range") or f"{report.get('start')}-{report.get('end')}",
        "score": report.get("score"),
        "verdict": report.get("verdict"),
        "summary": _clip_text(report.get("summary"), 220),
        "quality_gate": report.get("quality_gate", {}),
        "issues": [
            {
                "severity": issue.get("severity"),
                "chapters": issue.get("chapters", []),
                "category": _clip_text(issue.get("category"), 40),
                "detail": _clip_text(issue.get("detail"), 180),
                "evidence": _clip_text(issue.get("evidence"), 160),
                "suggestion": _clip_text(issue.get("suggestion"), 160),
            }
            for issue in report.get("issues", [])[:1]
            if isinstance(issue, dict)
        ],
    }


def local_scan(project: Path, total: int, chapter_min_score: float) -> dict:
    issues = []
    scores = []
    previous_outline: dict = {}
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
            previous_outline = {}
            continue
        current_outline = load_json(outline)
        key_events = current_outline.get("key_events", [])
        if isinstance(key_events, list):
            event_items = [item for item in key_events if str(item).strip()]
            if len(event_items) > 9 or any(
                len(str(item).strip()) > 140 for item in event_items
            ):
                issues.append({
                    "severity": "critical",
                    "chapters": [chapter],
                    "category": "outline_structure",
                    "detail": (
                        f"第{chapter}章 key_events={len(event_items)}，"
                        "要求不超过9条且单条不超过140字"
                    ),
                    "suggestion": "合并重复动作，压缩为5-7个可执行核心事件。",
                })
        repetition = detect_adjacent_event_repetition(previous_outline, current_outline)
        if repetition:
            issues.append({
                "severity": "critical",
                "chapters": [chapter - 1, chapter],
                "category": "adjacent_event_repetition",
                "detail": repetition["evidence"],
                "suggestion": repetition["suggestion"],
            })
        previous_outline = current_outline
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
    """基于文件【内容】的指纹，而非 mtime。

    早期实现用 st_mtime_ns，导致任何评审文件被重写（即使内容不变）就使整本缓存失效，
    触发整本终审全量重跑。改为内容哈希后，只有大纲/评审实际变化才会重审，
    大幅减少 outline_book_reviewer 在修复循环中的重复 LLM 调用。
    """
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
            try:
                digest.update(path.read_bytes())
            except Exception:
                digest.update(b"unreadable")
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
审查时把最早出现的明确事实作为锚点：后章不得推翻前章已确认的身份、生死、伤势、证据归属和事件结果。
死亡、被捕、身份揭露、关键证据取得、营救成功、公开直播等不可逆事件只能发生一次。
同一场景中的追逐、对峙、取证、营救或直播动作链不得换标题后跨章重复。
只输出：
{{"status":"completed","range":"{start}-{end}","score":0到10,"verdict":"通过/需修订/严重问题",
"summary":"180字内","end_state":"段末人物、地点、能力与主线状态",
"open_threads":["未解决伏笔"],"issues":[{{"severity":"critical/major/minor","chapters":[章节号],
"category":"类别","detail":"问题","evidence":"证据","suggestion":"建议"}}]}}
issues最多1条，只给最影响通过的单个可执行问题。"""
    cfg = config.get("outline_book_reviewer", {})
    rounds = int(cfg.get("evidence_review_rounds", 1) or 1)
    votes = int(cfg.get("required_votes", rounds // 2 + 1) or 2)
    votes = min(votes, rounds)
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
同时检查节奏四变量管理：
- 快慢：重要高潮是否细描？次要情节是否快推？
- 密疏：高密章节后是否有缓冲（日常/对话/心理）？
- 张弛：是否有"压抑→爆发"的情绪起伏？还是全程高能或全程平淡？
- 紧缓：是否有倒计时/危机逼近制造紧迫感？
检查是否存在连续3章以上同强度高潮（疲劳风险）或连续3章以上低密度平推（流失风险）。
只输出：
{{"status":"completed","range":"{start}-{end}","score":0到10,"verdict":"通过/需修订/严重问题",
"summary":"250字内","arc_progression":"本卷结构","rhythm_analysis":"节奏四变量分析（快慢/密疏/张弛/紧缓各有何问题）","open_threads":["卷末事项"],
"issues":[{{"severity":"critical/major/minor","chapters":[章节号],"category":"类别",
"detail":"问题","evidence":"证据","suggestion":"建议"}}]}}
issues最多1条，只给最影响通过的单个可执行问题。"""
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
    evidence_reports: list[dict],
    output: Path,
    *,
    preflight: bool = False,
    force: bool = False,
) -> dict:
    # 缓存复用：终审输出已存在且 force=False 时直接复用，避免每次重跑 review_rounds 轮 LLM
    if output.exists() and not force:
        cached = load_json(output)
        if cached.get("status") == "completed" and cached.get("score") is not None:
            return cached
    world = load_json(project / "world.json")
    min_score = float(config.get("outline_book_reviewer", {}).get("min_score", 9.0))
    compact_evidence = [compact_evidence_report(report) for report in evidence_reports]
    evidence_issues = _dedupe_issues([
        issue
        for report in compact_evidence
        for issue in report.get("issues", [])
        if isinstance(issue, dict)
    ])[:1]
    prompt = f"""对整本大纲做最终审查。

书籍设定：
{json.dumps({"title": world.get("title"), "overall_arc": world.get("overall_arc"), "three_act_structure": world.get("three_act_structure")}, ensure_ascii=False)}

本地完整性：
{json.dumps({key: scan.get(key) for key in ("total_chapters", "issue_count", "average_chapter_score", "min_chapter_score")}, ensure_ascii=False)}

结构证据报告（分段/卷级只作为证据，最终是否阻断由本次整本裁决决定）：
{json.dumps(compact_evidence, ensure_ascii=False)}

结构证据中被多轮提到的问题摘要：
{json.dumps(evidence_issues, ensure_ascii=False)}

整本通过门槛为{min_score:g}分。检查开篇到终局闭环、主线和人物弧线、力量体系、伏笔回收、高潮分布及重复桥段。
只输出：
{{"status":"completed","score":0到10,"verdict":"通过/需修订/严重问题",
"executive_summary":"400字内","strengths":["优势"],"unresolved_threads":["未闭环事项"],
"issues":[{{"severity":"critical/major/minor","chapters":[章节号],"category":"类别",
"detail":"问题","evidence":"证据","suggestion":"建议"}}]}}
issues最多1条，只给最影响通过的单个可执行问题。"""
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
    )
    result["evidence_issue_count"] = len(evidence_issues)
    result["evidence_issues"] = evidence_issues
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
    enable_segment_review = bool(cfg.get("enable_segment_review", True))
    enable_volume_review = bool(cfg.get("enable_volume_review", False))
    block_on_segment_reviews = bool(cfg.get("block_on_segment_reviews", False))
    block_on_volume_reviews = bool(cfg.get("block_on_volume_reviews", False))
    review_strategy = {
        "enable_segment_review": enable_segment_review,
        "enable_volume_review": enable_volume_review,
        "block_on_segment_reviews": block_on_segment_reviews,
        "block_on_volume_reviews": block_on_volume_reviews,
    }
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
        and previous_manifest.get("review_strategy") == review_strategy
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
        log(project, f"本地门失败，发现 {len(scan['issues'])} 个缺失、未过审或确定性连续性问题")
        atomic_write_json(reports / "final_outline_review.json", {
            "status": "blocked",
            "gate_passed": False,
            "reason": "local_outline_gate_failed",
            "issue_count": len(scan["issues"]),
            "issues": scan["issues"],
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
    if enable_segment_review:
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
    else:
        segments = {
            start: {
                "status": "skipped",
                "range": f"{start}-{end}",
                "start": start,
                "end": end,
                "summary": "segment review disabled",
                "issues": [],
                "quality_gate": {"passed": True, "skipped": True},
            }
            for start, end in segment_ranges
        }
        log(project, "分段模型审查已关闭，仅使用本地扫描与最终整本审查")
    failed_segments = [
        value for value in segments.values()
        if value.get("quality_gate", {}).get("passed") is not True
    ]
    if failed_segments:
        log(
            project,
            f"分段审查发现 {len(failed_segments)} 个未通过范围，作为最终整本审查证据"
        )
    if failed_segments and block_on_segment_reviews and not args.preflight:
        atomic_write_json(reports / "final_outline_review.json", {
            "status": "blocked",
            "gate_passed": False,
            "reason": "segment_quality_gate_failed",
            "failed_ranges": [item.get("range") or f"{item.get('start')}-{item.get('end')}" for item in failed_segments],
            "issues": _dedupe_issues([
                issue
                for item in failed_segments
                for issue in item.get("issues", [])
            ])[:30],
        })
        return 1

    volume_ranges = [(start, min(total, start + volume_size - 1)) for start in range(1, total + 1, volume_size)]
    volumes: dict[int, dict] = {}
    if enable_volume_review:
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
    else:
        volumes = {}
        log(project, "卷级模型审查已关闭，分段报告直接进入最终整本审查")
    failed_volumes = [
        value for value in volumes.values()
        if value.get("quality_gate", {}).get("passed") is not True
    ]
    if failed_volumes:
        log(
            project,
            f"卷级审查发现 {len(failed_volumes)} 个未通过范围，作为最终整本审查证据"
        )
    if failed_volumes and block_on_volume_reviews and not args.preflight:
        atomic_write_json(reports / "final_outline_review.json", {
            "status": "blocked",
            "gate_passed": False,
            "reason": "volume_quality_gate_failed",
            "failed_ranges": [item.get("range") or f"{item.get('start')}-{item.get('end')}" for item in failed_volumes],
            "issues": _dedupe_issues([
                issue
                for item in failed_volumes
                for issue in item.get("issues", [])
            ])[:30],
        })
        return 1

    evidence_reports = (
        [volumes[start] for start, _ in volume_ranges]
        if enable_volume_review
        else [segments[start] for start, _ in segment_ranges]
    )
    final = final_review(
        project,
        config,
        scan,
        evidence_reports,
        reports / "final_outline_review.json",
        preflight=args.preflight,
        force=force,
    )
    atomic_write_json(reports / "manifest.json", {
        "status": "completed",
        "segments": len(segments),
        "volumes": len(volumes),
        "review_strategy": review_strategy,
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
