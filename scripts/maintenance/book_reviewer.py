#!/usr/bin/env python3
"""Run whole-book validation and hierarchical AI review."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
import hashlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from core.mmx_client import MmxError, call_mmx
from core.json_repair import fix_inner_quotes, fix_truncated_json
from core.novel_config import load_config, resolve_project_dir
from core.workflow_state import analyze_chapter_text, load_quality_rules, scan_chapter_status


LOG_LOCK = threading.Lock()


def log(project: Path, message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [BookReviewer] {message}"
    with LOG_LOCK:
        print(line, flush=True)
        path = project / "logs" / "book_reviewer.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{threading.get_ident()}")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def parse_json_response(raw: str) -> dict:
    text = raw.strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {"status": "invalid_shape", "raw_response": raw[:4000]}
    except Exception:
        pass
    start = text.find("{")
    if start >= 0:
        candidate = text[start:]
        for repaired in (
            fix_inner_quotes(candidate),
            fix_truncated_json(fix_inner_quotes(candidate)),
            fix_truncated_json(candidate),
        ):
            try:
                data = json.loads(repaired)
                if isinstance(data, dict):
                    data.setdefault("repair_applied", True)
                    return data
            except Exception:
                continue
    return {"status": "parse_error", "raw_response": raw[:4000]}


def chapter_paths(project: Path, chapter: int) -> dict[str, Path]:
    return {
        "outline": project / "chapters" / "outline" / f"chapter_{chapter:04d}.json",
        "outline_review": project / "chapters" / "outline_review" / f"chapter_{chapter:04d}_review.json",
        "draft": project / "chapters" / "draft" / f"chapter_{chapter:04d}.txt",
        "review": project / "chapters" / "review" / f"chapter_{chapter:04d}_review.json",
        "final": project / "chapters" / "final" / f"chapter_{chapter:04d}.txt",
    }


def compact_text(text: str) -> str:
    return re.sub(r"\s+", "", text)


def shingles(text: str, size: int = 12, step: int = 4) -> set[str]:
    value = compact_text(text)
    if len(value) < size:
        return {value} if value else set()
    return {value[index:index + size] for index in range(0, len(value) - size + 1, step)}


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def make_issue(severity: str, category: str, chapters: list[int], detail: str, evidence: str = "") -> dict:
    return {
        "severity": severity,
        "category": category,
        "chapters": chapters,
        "detail": detail,
        "evidence": evidence,
    }


def local_full_scan(project: Path, total: int, min_score: float) -> dict:
    rules = load_quality_rules(project)
    statuses = scan_chapter_status(project, 1, total, use_cache=False)
    issues: list[dict] = []
    chapter_rows: list[dict] = []
    hashes: dict[str, list[int]] = {}
    fingerprints: dict[int, set[str]] = {}
    final_texts: dict[int, str] = {}

    for chapter in range(1, total + 1):
        paths = chapter_paths(project, chapter)
        missing = [name for name, path in paths.items() if not path.exists()]
        if missing:
            issues.append(make_issue("critical", "missing_artifact", [chapter], f"缺少产物: {', '.join(missing)}"))
            continue

        text = paths["final"].read_text(encoding="utf-8", errors="ignore")
        final_texts[chapter] = text
        length, grade, ok, text_issues = analyze_chapter_text(text, rules=rules)
        review = load_json(paths["review"])
        outline = load_json(paths["outline"])
        outline_review = load_json(paths["outline_review"])
        score = review.get("overall_score")
        outline_score = outline_review.get("overall_score")
        title = str(outline.get("title", "")).strip()
        digest = hashlib.sha256(compact_text(text).encode("utf-8")).hexdigest()
        hashes.setdefault(digest, []).append(chapter)
        fingerprints[chapter] = shingles(text)

        if not ok:
            issues.append(make_issue("critical", "final_quality", [chapter], f"终稿本地质量门失败: {grade}", ", ".join(text_issues)))
        if not isinstance(score, (int, float)) or float(score) < min_score:
            issues.append(make_issue("critical", "review_score", [chapter], f"正文审查分未达标: {score}", f"门槛={min_score:g}"))
        if review.get("verdict") in {"需修改", "需重写"}:
            issues.append(make_issue("critical", "review_verdict", [chapter], f"正文审查结论异常: {review.get('verdict')}"))
        if not title:
            issues.append(make_issue("major", "missing_title", [chapter], "大纲缺少章节标题"))
        markdown_hits = []
        for marker in ("```", "**", "### ", "# "):
            if marker in text:
                markdown_hits.append(marker)
        if markdown_hits:
            issues.append(make_issue("minor", "markdown_residue", [chapter], "终稿包含Markdown标记", ", ".join(markdown_hits)))
        if re.search(r"(作为AI|无法继续生成|以下是正文|字数[:：]|本章完)", text):
            issues.append(make_issue("major", "meta_text", [chapter], "终稿疑似包含元叙述或生成痕迹"))

        chapter_rows.append({
            "chapter": chapter,
            "title": title,
            "words": length,
            "grade": grade,
            "final_ok": ok,
            "review_score": score,
            "review_verdict": review.get("verdict"),
            "outline_score": outline_score,
            "text_issues": text_issues,
        })

    for group in hashes.values():
        if len(group) > 1:
            issues.append(make_issue("critical", "exact_duplicate_chapter", group, "多个章节正文完全相同"))

    for chapter in range(1, total + 1):
        for other in range(chapter + 1, min(total, chapter + 5) + 1):
            similarity = jaccard(fingerprints.get(chapter, set()), fingerprints.get(other, set()))
            if similarity >= 0.72:
                issues.append(make_issue(
                    "major",
                    "near_duplicate_chapter",
                    [chapter, other],
                    f"相邻范围章节文本高度相似: {similarity:.3f}",
                ))
        if chapter < total:
            ending = compact_text(final_texts.get(chapter, ""))[-300:]
            opening = compact_text(final_texts.get(chapter + 1, ""))[:300]
            overlap = jaccard(shingles(ending, size=8, step=2), shingles(opening, size=8, step=2))
            if overlap >= 0.65:
                issues.append(make_issue(
                    "major",
                    "boundary_repetition",
                    [chapter, chapter + 1],
                    f"前章结尾与后章开头疑似大段重复: {overlap:.3f}",
                ))

    scores = [float(row["review_score"]) for row in chapter_rows if isinstance(row.get("review_score"), (int, float))]
    words = [int(row["words"]) for row in chapter_rows]
    severity_counts = {key: sum(1 for issue in issues if issue["severity"] == key) for key in ("critical", "major", "minor")}
    return {
        "status": "completed",
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_chapters": total,
        "artifact_counts": {
            name: sum(1 for chapter in range(1, total + 1) if chapter_paths(project, chapter)[name].exists())
            for name in ("outline", "outline_review", "draft", "review", "final")
        },
        "final_ok_count": sum(1 for status in statuses.values() if status.final_ok),
        "review_ok_count": sum(1 for status in statuses.values() if status.review_ok),
        "total_words": sum(words),
        "average_words": round(sum(words) / len(words), 2) if words else 0,
        "min_words": min(words) if words else 0,
        "max_words": max(words) if words else 0,
        "average_review_score": round(sum(scores) / len(scores), 4) if scores else 0,
        "min_review_score": min(scores) if scores else None,
        "max_review_score": max(scores) if scores else None,
        "severity_counts": severity_counts,
        "issues": issues,
        "chapters": chapter_rows,
    }


def chapter_review_context(project: Path, chapter: int) -> dict:
    paths = chapter_paths(project, chapter)
    outline = load_json(paths["outline"])
    review = load_json(paths["review"])
    text = paths["final"].read_text(encoding="utf-8", errors="ignore")

    def compact_items(value: Any, limit: int) -> list[str]:
        if isinstance(value, list):
            return [str(item)[:300] for item in value[:limit]]
        if isinstance(value, dict):
            return [f"{key}: {item}"[:300] for key, item in list(value.items())[:limit]]
        if value:
            return [str(value)[:300]]
        return []

    return {
        "chapter": chapter,
        "title": outline.get("title", ""),
        "outline_summary": str(outline.get("summary", ""))[:700],
        "key_events": compact_items(outline.get("key_events"), 7),
        "foreshadowing": compact_items(outline.get("foreshadowing"), 5),
        "power_progression": outline.get("power_progression", ""),
        "review_score": review.get("overall_score"),
        "review_summary": str(review.get("summary", ""))[:300],
        "opening": text[:650],
        "ending": text[-650:],
    }


def ai_call(project: Path, config: dict, system: str, prompt: str, raw_name: str) -> dict:
    review_cfg = config.get("book_reviewer", {})
    try:
        raw = call_mmx(
            system,
            prompt,
            model=config["model"],
            mmx_path=config["mmx_path"],
            max_tokens=int(review_cfg.get("max_tokens", 4096)),
            temperature=float(review_cfg.get("temperature", 0.2)),
            retries=int(review_cfg.get("retries", 2)),
            retry_delay=float(review_cfg.get("retry_delay", 5.0)),
            timeout=int(review_cfg.get("timeout_seconds", 240)),
            log_dir=project / "logs" / "raw_responses",
            raw_name=raw_name,
            qps=float(config.get("api_qps", 5.0)),
            rate_state_dir=project / "logs" / "rate_limit",
        )
    except MmxError as exc:
        return {"status": "failed", "error": str(exc)}
    data = parse_json_response(raw)
    data.setdefault("status", "completed" if data.get("status") not in {"parse_error", "invalid_shape"} else data.get("status"))
    return data


def segment_review(project: Path, config: dict, start: int, end: int, output: Path) -> dict:
    if output.exists():
        existing = load_json(output)
        if existing.get("status") == "completed":
            return existing
    chapters = [chapter_review_context(project, chapter) for chapter in range(start, end + 1)]
    system = """你是长篇中文网络小说的资深总编，负责10章连续剧情审查。
重点发现跨章问题，而不是重复逐章评分。必须输出合法JSON，不使用Markdown。
严重度仅使用critical/major/minor。证据必须指明章节。"""
    prompt = f"""审查第{start}-{end}章的连续剧情资料：
{json.dumps(chapters, ensure_ascii=False)}

检查：
1. 前后章衔接、时间线、地点、人物状态是否连续；
2. 事件是否重复、后章是否重演前章；
3. 人物动机、能力等级、世界观规则是否一致；
4. 伏笔是否推进或遗忘，节奏是否长期停滞；
5. 本段起点和终点是否形成明确推进。

只输出：
{{
  "status":"completed",
  "range":"{start}-{end}",
  "score":0到10,
  "verdict":"通过/需修订/严重问题",
  "summary":"不超过180字",
  "timeline_state":"本段结束时的关键时间、地点、人物和能力状态",
  "open_threads":["尚未解决的伏笔或支线"],
  "issues":[{{"severity":"critical/major/minor","chapters":[章节号],"category":"类别","detail":"问题","evidence":"证据","suggestion":"修订建议"}}]
}}
issues最多8条，只保留有明确跨章证据的问题；所有字符串保持紧凑。"""
    result = ai_call(project, config, system, prompt, f"book_segment_{start:04d}_{end:04d}")
    if result.get("status") == "completed" and str(result.get("range", "")) != f"{start}-{end}":
        result = {
            "status": "range_mismatch",
            "expected_range": f"{start}-{end}",
            "actual_range": result.get("range"),
        }
    result["start"] = start
    result["end"] = end
    atomic_json(output, result)
    return result


def volume_review(project: Path, config: dict, start: int, end: int, segments: list[dict], output: Path) -> dict:
    if output.exists():
        existing = load_json(output)
        if existing.get("status") == "completed":
            return existing
    system = """你是长篇网络小说卷级主编。根据连续5个十章审查结果，评估整卷结构。
必须输出合法JSON，不使用Markdown；问题必须指明章节范围。"""
    prompt = f"""审查第{start}-{end}章卷级结构：
{json.dumps(segments, ensure_ascii=False)}

检查主线推进、高潮分布、人物弧线、能力成长、重复桥段、伏笔管理和卷末收束。
只输出：
{{
  "status":"completed",
  "range":"{start}-{end}",
  "score":0到10,
  "verdict":"通过/需修订/严重问题",
  "summary":"不超过250字",
  "arc_progression":"本卷主线与人物弧线",
  "open_threads":["卷末未解决事项"],
  "issues":[{{"severity":"critical/major/minor","chapters":[章节号],"category":"类别","detail":"问题","evidence":"证据","suggestion":"修订建议"}}]
}}
issues最多10条，只保留影响整卷结构的问题；所有字符串保持紧凑。"""
    result = ai_call(project, config, system, prompt, f"book_volume_{start:04d}_{end:04d}")
    if result.get("status") == "completed" and str(result.get("range", "")) != f"{start}-{end}":
        result = {
            "status": "range_mismatch",
            "expected_range": f"{start}-{end}",
            "actual_range": result.get("range"),
        }
    result["start"] = start
    result["end"] = end
    atomic_json(output, result)
    return result


def final_review(project: Path, config: dict, local_scan: dict, volumes: list[dict], output: Path) -> dict:
    world = load_json(project / "world.json")
    local_category_counts: dict[str, int] = {}
    local_examples: list[dict] = []
    for issue in local_scan.get("issues", []):
        category = str(issue.get("category", "unknown"))
        local_category_counts[category] = local_category_counts.get(category, 0) + 1
        if len(local_examples) < 12:
            local_examples.append(issue)
    compact_local = {
        key: local_scan.get(key)
        for key in (
            "total_chapters", "artifact_counts", "final_ok_count", "review_ok_count", "total_words",
            "average_words", "min_words", "max_words", "average_review_score", "min_review_score",
            "max_review_score", "severity_counts",
        )
    }
    compact_local["issue_category_counts"] = local_category_counts
    compact_local["issue_examples"] = local_examples
    system = """你是长篇中文网络小说的终审总编。根据本地全量扫描和10个卷级报告做整本终审。
必须忠于证据，不得虚构已读内容。输出合法JSON，不使用Markdown。"""
    prompt = f"""书籍设定：
{json.dumps({"title": world.get("title"), "subtitle": world.get("subtitle"), "world_description": world.get("world_description"), "power_system": world.get("power_system")}, ensure_ascii=False)}

本地全量扫描：
{json.dumps(compact_local, ensure_ascii=False)}

卷级报告：
{json.dumps(volumes, ensure_ascii=False)}

判断整本是否达到发布标准，检查开篇到终局是否闭环、人物/世界观/能力体系是否一致、主要伏笔是否回收。
只输出：
{{
  "status":"completed",
  "score":0到10,
  "verdict":"通过发布/修订后发布/不建议发布",
  "executive_summary":"不超过400字",
  "strengths":["整本优势"],
  "unresolved_threads":["未回收伏笔或支线"],
  "issues":[{{"severity":"critical/major/minor","chapters":[章节号],"category":"类别","detail":"问题","evidence":"证据","suggestion":"修订建议"}}],
  "publication_recommendation":"具体发布建议"
}}
issues最多15条，只保留最重要且有卷级证据的问题；不要把格式问题擅自升级为critical。"""
    result = ai_call(project, config, system, prompt, "book_final_review")
    atomic_json(output, result)
    return result


def collect_ai_issues(items: list[dict]) -> list[dict]:
    issues = []
    for item in items:
        for issue in item.get("issues", []) if isinstance(item.get("issues"), list) else []:
            if isinstance(issue, dict):
                issues.append(issue)
    return issues


def write_markdown_report(project: Path, local_scan: dict, segments: list[dict], volumes: list[dict], final: dict, output: Path) -> None:
    world = load_json(project / "world.json")
    all_issues = local_scan.get("issues", []) + collect_ai_issues(segments) + collect_ai_issues(volumes) + collect_ai_issues([final])
    rank = {"critical": 0, "major": 1, "minor": 2}
    all_issues.sort(key=lambda item: (rank.get(str(item.get("severity")), 9), item.get("chapters", [9999])[0] if item.get("chapters") else 9999))

    lines = [
        f"# 《{world.get('title', project.name)}》整本终审报告",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 审查范围：第1-{local_scan.get('total_chapters')}章",
        f"- 终审结论：**{final.get('verdict', '未知')}**",
        f"- 整本评分：**{final.get('score', 'N/A')} / 10**",
        f"- 总字数：{local_scan.get('total_words', 0):,}",
        f"- 逐章平均分：{local_scan.get('average_review_score', 0)}",
        "",
        "## 执行摘要",
        "",
        str(final.get("executive_summary", "终审未返回有效摘要。")),
        "",
        "## 完整性与本地质量门",
        "",
        f"- 产物数量：{json.dumps(local_scan.get('artifact_counts', {}), ensure_ascii=False)}",
        f"- Final 本地通过：{local_scan.get('final_ok_count')}/{local_scan.get('total_chapters')}",
        f"- 逐章审查通过：{local_scan.get('review_ok_count')}/{local_scan.get('total_chapters')}",
        f"- 单章字数：最少 {local_scan.get('min_words')}，最多 {local_scan.get('max_words')}，平均 {local_scan.get('average_words')}",
        f"- 逐章评分：最低 {local_scan.get('min_review_score')}，最高 {local_scan.get('max_review_score')}",
        f"- 本地问题统计：{json.dumps(local_scan.get('severity_counts', {}), ensure_ascii=False)}",
        "",
        "## 卷级结果",
        "",
        "| 范围 | 评分 | 结论 | 摘要 |",
        "|---|---:|---|---|",
    ]
    for item in volumes:
        lines.append(f"| {item.get('start')}-{item.get('end')} | {item.get('score', 'N/A')} | {item.get('verdict', '')} | {str(item.get('summary', '')).replace('|', '｜')} |")

    lines.extend(["", "## 问题清单", ""])
    if not all_issues:
        lines.append("未发现需要修订的问题。")
    else:
        for index, issue in enumerate(all_issues, 1):
            chapters = ",".join(str(value) for value in issue.get("chapters", []))
            lines.extend([
                f"### {index}. [{str(issue.get('severity', 'unknown')).upper()}] {issue.get('category', '未分类')}（章节 {chapters or '未指定'}）",
                "",
                f"- 问题：{issue.get('detail', '')}",
                f"- 证据：{issue.get('evidence', '')}",
                f"- 建议：{issue.get('suggestion', '')}",
                "",
            ])

    lines.extend(["## 未解决伏笔与支线", ""])
    threads = final.get("unresolved_threads", [])
    if threads:
        lines.extend(f"- {item}" for item in threads)
    else:
        lines.append("- 终审未识别到明确未回收项。")

    lines.extend(["", "## 发布建议", "", str(final.get("publication_recommendation", "无有效发布建议。")), ""])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="整本小说分层终审")
    parser.add_argument("--project", "-p", default=os.getenv("NOVEL_PROJECT_DIR", ""))
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--local-only", action="store_true")
    args = parser.parse_args()

    project = resolve_project_dir(args.project)
    config = load_config(project)
    total = int(config.get("total_chapters", 0))
    min_score = float(config.get("reviewer", {}).get("min_score", 8.5))
    reports = project / "reports" / "book_review"
    segments_dir = reports / "segments"
    volumes_dir = reports / "volumes"
    reports.mkdir(parents=True, exist_ok=True)

    log(project, f"开始整本终审: 1-{total}章")
    local_path = reports / "local_full_scan.json"
    local_scan = local_full_scan(project, total, min_score)
    atomic_json(local_path, local_scan)
    log(project, f"本地全量扫描完成: issues={len(local_scan['issues'])}")

    if args.local_only:
        return 0

    workers = args.workers or int(config.get("book_reviewer", {}).get("workers", 5) or 5)
    segment_ranges = [(start, min(total, start + 9)) for start in range(1, total + 1, 10)]
    segments: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(
                segment_review,
                project,
                config,
                start,
                end,
                segments_dir / f"segment_{start:04d}_{end:04d}.json",
            ): (start, end)
            for start, end in segment_ranges
        }
        for future in as_completed(futures):
            start, end = futures[future]
            try:
                segments[start] = future.result()
            except Exception as exc:
                segments[start] = {"status": "failed", "start": start, "end": end, "error": str(exc)}
            log(project, f"分段审查完成: {start}-{end} status={segments[start].get('status')}")
    segment_list = [segments[start] for start, _ in segment_ranges]

    volume_ranges = [(start, min(total, start + 49)) for start in range(1, total + 1, 50)]
    volumes: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=min(max(1, workers), 5)) as executor:
        futures = {}
        for start, end in volume_ranges:
            selected = [item for item in segment_list if start <= int(item.get("start", 0)) <= end]
            futures[executor.submit(
                volume_review,
                project,
                config,
                start,
                end,
                selected,
                volumes_dir / f"volume_{start:04d}_{end:04d}.json",
            )] = (start, end)
        for future in as_completed(futures):
            start, end = futures[future]
            try:
                volumes[start] = future.result()
            except Exception as exc:
                volumes[start] = {"status": "failed", "start": start, "end": end, "error": str(exc)}
            log(project, f"卷级审查完成: {start}-{end} status={volumes[start].get('status')}")
    volume_list = [volumes[start] for start, _ in volume_ranges]

    final_path = reports / "final_book_review.json"
    final = final_review(project, config, local_scan, volume_list, final_path)
    write_markdown_report(project, local_scan, segment_list, volume_list, final, reports / "final_book_review.md")
    manifest = {
        "status": "completed" if final.get("status") == "completed" else "incomplete",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "local_scan": str(local_path),
        "segments": len(segment_list),
        "segment_completed": sum(item.get("status") == "completed" for item in segment_list),
        "volumes": len(volume_list),
        "volume_completed": sum(item.get("status") == "completed" for item in volume_list),
        "final_review": str(final_path),
        "markdown_report": str(reports / "final_book_review.md"),
    }
    atomic_json(reports / "manifest.json", manifest)
    log(project, f"整本终审完成: verdict={final.get('verdict')} score={final.get('score')}")
    return 0 if manifest["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
