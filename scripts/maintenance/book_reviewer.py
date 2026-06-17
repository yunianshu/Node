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
from core.workflow_state import aggregate_review_scores, analyze_chapter_text, load_quality_rules, scan_chapter_status


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


def local_full_scan(project: Path, total: int, min_score: float, start_ch: int = 1) -> dict:
    rules = load_quality_rules(project)
    statuses = scan_chapter_status(project, start_ch, total, use_cache=False)
    issues: list[dict] = []
    chapter_rows: list[dict] = []
    hashes: dict[str, list[int]] = {}
    fingerprints: dict[int, set[str]] = {}
    final_texts: dict[int, str] = {}

    for chapter in range(start_ch, total + 1):
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

    for chapter in range(start_ch, total + 1):
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
    # 单章评分常被软封顶在 8.5 附近，算术平均会被低分章拖低并锚定终审；
    # 这里同时输出中位数与分位数，让终审 AI 能看到真实质量分布。
    score_metrics = aggregate_review_scores(scores, min_score=min_score)
    return {
        "status": "completed",
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_chapters": total,
        "artifact_counts": {
            name: sum(1 for chapter in range(start_ch, total + 1) if chapter_paths(project, chapter)[name].exists())
            for name in ("outline", "outline_review", "draft", "review", "final")
        },
        "final_ok_count": sum(1 for status in statuses.values() if status.final_ok),
        "review_ok_count": sum(1 for status in statuses.values() if status.review_ok),
        "total_words": sum(words),
        "average_words": round(sum(words) / len(words), 2) if words else 0,
        "min_words": min(words) if words else 0,
        "max_words": max(words) if words else 0,
        # 向后兼容的旧字段（算术平均/极值）
        "average_review_score": score_metrics["average"],
        "min_review_score": score_metrics["min"],
        "max_review_score": score_metrics["max"],
        # 分位数口径新字段（抗异常，反映真实分布）
        "median_review_score": score_metrics["median"],
        "p75_review_score": score_metrics["p75"],
        "p25_review_score": score_metrics["p25"],
        "review_pass_rate": score_metrics["pass_rate"],
        "review_score_count": score_metrics["count"],
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
        # 救猫咪结构维度（旧大纲可能缺失，空字符串兜底）
        "story_beat": str(outline.get("story_beat", "")),
        "chapter_goal": str(outline.get("chapter_goal", ""))[:200],
        "main_antagonist": str(outline.get("main_antagonist", ""))[:100],
    }


def select_key_chapters(project: Path, total: int, volumes: list[dict], max_full: int = 10) -> dict:
    """选出终审需要喂【全文】的关键章节，其余只给摘要。

    真正的整本评审必须让 AI 读到关键节点的原文，而不是只看统计数字。选取规则按优先级：
    1. 开篇（第 1、2 章）——建立世界观、主角动机、核心悬念
    2. 结局（倒数第 1、2 章）——验证主线闭环、伏笔回收、结局满意度
    3. 卷级报告标记的 critical/major 章节——结构硬伤集中点
    4. 全书大纲中埋设伏笔的章节（均匀采样）——验证伏笔是否回收
    5. 1/4、1/2、3/4 进度位置——检测注水腰（sagging middle）与节奏曲线

    Args:
        project: 项目目录。
        total: 总章数。
        volumes: 卷级审查报告列表（来自 volume_review）。
        max_full: 喂全文的章节数上限，控制 prompt 篇幅。

    Returns:
        {chapter_number: {"full": True, "role": "..."}}，仅包含选中的章节。
    """
    selected: dict[int, dict] = {}
    pending: list[tuple[int, str, int]] = []  # (chapter, role, priority)，priority 越小越优先

    def add(chapter: int, role: str, priority: int) -> None:
        if 1 <= chapter <= total and chapter not in selected and chapter not in {c for c, _, _ in pending}:
            pending.append((chapter, role, priority))

    # 优先级 1：开篇与结局
    add(1, "开篇（建立世界观与核心悬念）", 1)
    add(2, "开篇（主角动机与初始处境）", 1)
    add(total, "结局（主线闭环验证）", 1)
    add(total - 1, "结局（收束前的高潮）", 1)

    # 优先级 2：卷级报告中的 critical/major 章节
    flagged: set[int] = set()
    for vol in volumes or []:
        for issue in vol.get("issues", []) if isinstance(vol.get("issues"), list) else []:
            if str(issue.get("severity", "")) in {"critical", "major"}:
                for ch in issue.get("chapters", []) if isinstance(issue.get("chapters"), list) else []:
                    if isinstance(ch, int):
                        flagged.add(ch)
    for ch in sorted(flagged):
        add(ch, "卷级审查标记的结构问题章", 2)

    # 优先级 3：进度位置（注水腰检测）
    for ratio, label in ((0.25, "前1/4进度点"), (0.5, "中点（注水腰检测）"), (0.75, "后1/4进度点")):
        add(max(1, round(total * ratio)), label, 3)

    # 优先级 4：埋设伏笔的章节（均匀采样，避免过多）
    foreshadow_chapters: list[int] = []
    for chapter in range(1, total + 1):
        outline = load_json(chapter_paths(project, chapter)["outline"])
        if str(outline.get("foreshadowing", "")).strip():
            foreshadow_chapters.append(chapter)
    if foreshadow_chapters:
        sample_step = max(1, len(foreshadow_chapters) // 5)  # 最多取约5个伏笔章
        sampled = foreshadow_chapters[::sample_step][:5]
        for ch in sampled:
            add(ch, "伏笔埋设章（验证是否回收）", 4)

    # 按优先级截断到 max_full
    pending.sort(key=lambda item: (item[2], item[0]))
    for chapter, role, _ in pending[:max_full]:
        selected[chapter] = {"full": True, "role": role}
    return selected


def _sample_outlines_for_final(project: Path, total: int, max_chars: int = 20000) -> list[dict]:
    """采样全书逐章大纲摘要，控制总篇幅不超 max_chars。

    小书（≤120章）逐章全取；大书按固定步长均匀采样，保证终审 AI 能看到全书剧情骨架，
    同时把 prompt 控制在可接受范围内（给关键章节原文留出空间）。
    单条 summary/foreshadowing 截短，避免少数超长摘要挤占篇幅。
    """
    target_count = min(120, total)  # 目标约 120 条以内
    step = max(1, total // target_count)
    compact: list[dict] = []
    accumulated = 0
    for chapter in range(1, total + 1):
        if (chapter - 1) % step != 0 and chapter != total:
            continue
        outline = load_json(chapter_paths(project, chapter)["outline"])
        if not outline:
            continue
        summary = str(outline.get("summary", ""))[:120]
        foreshadowing = str(outline.get("foreshadowing", ""))[:80]
        story_beat = str(outline.get("story_beat", "")).strip()
        entry = {
            "ch": chapter,
            "t": str(outline.get("title", ""))[:24],
            "s": summary,
        }
        if story_beat:
            entry["b"] = story_beat  # 结构功能标签，供终审分析全书节奏曲线
        if foreshadowing:
            entry["f"] = foreshadowing
        entry_text = summary + foreshadowing + story_beat + entry["t"]
        if accumulated + len(entry_text) > max_chars:
            break
        compact.append(entry)
        accumulated += len(entry_text)
    return compact


def build_whole_book_context(project: Path, total: int, volumes: list[dict], local_scan: dict) -> dict:
    """构建终审所需的"全书内容"上下文，让 AI 真正读到小说而不是只看统计数字。

    这是本次重构的核心：把 world.json 的主线/三幕结构、关键章节原文、全书大纲采样、
    伏笔清单整合在一起，供终审 AI 做基于阅读体验的整本评分。
    """
    world = load_json(project / "world.json")

    # 全书主线（world.json 的 overall_arc / three_act_structure 此前完全没用）
    world_arc = {
        "title": world.get("title", ""),
        "overall_arc": str(world.get("overall_arc", ""))[:2000],
        "themes": world.get("themes", [])[:8] if isinstance(world.get("themes"), list) else [],
    }
    three_act = world.get("three_act_structure")
    if isinstance(three_act, dict):
        world_arc["three_act_structure"] = {
            key: str(value)[:800] for key, value in three_act.items()
        }
    power_system = world.get("power_system", {})
    if isinstance(power_system, dict):
        levels = power_system.get("levels")
        if isinstance(levels, list):
            world_arc["power_system_levels"] = [str(lv)[:60] for lv in levels[:15]]

    # 关键章节原文
    key_chapters_map = select_key_chapters(project, total, volumes)
    key_chapters_fulltext: list[dict] = []
    for chapter in sorted(key_chapters_map):
        info = key_chapters_map[chapter]
        final_file = chapter_paths(project, chapter)["final"]
        if not final_file.exists():
            continue
        text = final_file.read_text(encoding="utf-8", errors="ignore")
        key_chapters_fulltext.append({
            "chapter": chapter,
            "role": info["role"],
            "title": load_json(chapter_paths(project, chapter)["outline"]).get("title", ""),
            "opening": text[:1500],
            "ending": text[-800:] if len(text) > 1500 else "",
        })

    # 全书大纲采样（控制篇幅）
    all_outlines_compact = _sample_outlines_for_final(project, total)

    # 伏笔清单（从每章 foreshadowing 字段聚合，用于让 AI 判断回收情况）。
    # 全量聚合在大书上可达上千条、上百k字，超出 prompt 容量；这里先统计总数，
    # 再均匀采样出代表性的伏笔条目喂给 AI（数量已在 final_review prompt 中二次截断到 60）。
    foreshadowing_inventory: list[dict] = []
    foreshadowing_total = 0
    sample_step = max(1, total // 120)  # 与大纲采样同步，目标 ≤120 条
    for chapter in range(1, total + 1):
        outline = load_json(chapter_paths(project, chapter)["outline"])
        setup = str(outline.get("foreshadowing", "")).strip()
        if not setup:
            continue
        foreshadowing_total += 1
        if (chapter - 1) % sample_step != 0:
            continue
        foreshadowing_inventory.append({
            "planted_at": chapter,
            "setup": setup[:200],
        })

    # 统计指标作为辅助参考（不再是主依据）
    score_distribution = {
        key: local_scan.get(key)
        for key in (
            "total_chapters", "final_ok_count", "review_ok_count", "total_words",
            "average_review_score", "median_review_score", "review_pass_rate", "severity_counts",
        )
    }

    return {
        "world_arc": world_arc,
        "key_chapters_fulltext": key_chapters_fulltext,
        "all_outlines_compact": all_outlines_compact,
        "foreshadowing_inventory": foreshadowing_inventory,
        "foreshadowing_total": foreshadowing_total,
        "volume_findings": [
            {
                "range": f"{v.get('start')}-{v.get('end')}",
                "score": v.get("score"),
                "verdict": v.get("verdict"),
                "summary": str(v.get("summary", ""))[:200],
            }
            for v in volumes if isinstance(v, dict)
        ],
        "score_distribution": score_distribution,
        "key_chapter_count": len(key_chapters_fulltext),
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
    total = int(local_scan.get("total_chapters", 0)) or int(config.get("total_chapters", 0))
    # 构建真正的"全书内容"上下文：主线、关键章节原文、大纲采样、伏笔清单。
    # 这是本次重构的核心——让终审 AI 读到小说，而不是只看统计数字。
    context = build_whole_book_context(project, total, volumes, local_scan)

    system = """你是长篇中文网络小说的终审总编。你刚刚通读了全书的关键章节原文、全书大纲摘要和伏笔清单。
你的评分必须基于【整本阅读体验】，绝不等于逐章评分的平均值——单章审查口径严格，逐章均分被低分章拉低，
不能代表整本质量。你要回答的核心问题是：一位读者从头读到尾，这本书整体上是否值得出版/推荐？

整本评审必须重点核查这些【只在整本层面才会暴露】的问题：
- 主线是否闭环：开篇抛出的核心目标/悬念，到结局是否真正达成或合理转化？
- 伏笔是否回收：前文埋下的线，后面有没有兑现？（参考伏笔清单）
- 是否存在"注水腰"：中段是否拖沓、重复、原地踏步？（参考剧情骨架的 b 字段结构功能分布——
  若中段连续多章是 transition/rising_action 而无 catalyst/midpoint/all_is_lost 式转折，即为注水腰）
- 结局是否兑现：是水到渠成还是强行收束/烂尾？
- 人物弧线是否完整：主角从开篇到结局是否有真实的成长/转变？
- 节奏曲线是否合理：催化事件(catalyst)是否在开头出现？中点(midpoint)赌注是否升级？
  谷底(all_is_lost)是否在3/4处？高潮(finale)是否兑现主线承诺？（参考剧情骨架 b 字段分布）

输出合法JSON，不使用Markdown。所有评分必须引用具体章节号、伏笔或事件作为依据。"""
    prompt = f"""## 全书设定与主线
{json.dumps(context["world_arc"], ensure_ascii=False)}

## 关键章节原文（共 {context["key_chapter_count"]} 章，含开篇/结局/伏笔埋设/结构问题章）
{json.dumps(context["key_chapters_fulltext"], ensure_ascii=False)}

## 全书剧情骨架（逐章大纲摘要采样，字段：ch=章号/t=标题/s=摘要/f=伏笔/b=结构功能节拍）
{json.dumps(context["all_outlines_compact"], ensure_ascii=False)}

## 全书伏笔清单（全书共 {context["foreshadowing_total"]} 条，此处为均匀采样，请判断哪些已回收、哪些悬空）
{json.dumps(context["foreshadowing_inventory"][:60], ensure_ascii=False)}

## 卷级审查发现（结构参考，非评分主依据）
{json.dumps(context["volume_findings"], ensure_ascii=False)}

## 本地质量扫描（仅供参考，不可作为评分主依据；逐章均分受单章口径压制，会偏低）
{json.dumps(context["score_distribution"], ensure_ascii=False)}

## 【评分要求】
请从以下 8 个【整本专属维度】逐项打分（0-10），并给出依据（必须引用章节/伏笔/事件）：
1. main_arc_closure 主线闭环（权重20%）：开篇核心目标→结局是否真正达成或合理转化？
2. foreshadowing_payoff 伏笔回收（权重15%）：伏笔清单中回收比例与质量？是否有重大悬空？
3. character_arc 人物弧线（权重15%）：主角从开篇到结局是否有真实的成长/转变？
4. pacing_curve 节奏曲线（权重15%）：高潮分布是否合理？是否存在注水腰/烂尾？
5. world_consistency 世界观自洽（权重10%）：力量体系/规则/设定前后是否矛盾？
6. ending_satisfaction 结局满意度（权重10%）：是否兑现读者期待？有无强行收束？
7. emotional_resonance 情感共鸣（权重10%）：整本情感张力？是否有记忆点？
8. overall_readability 整体可读性（权重5%）：通读体验？是否有大量水文？

最终 score 是 8 维度的【加权综合分】（不是简单平均），请自行按权重计算。
【重要】score 不要参考 score_distribution 中的逐章均分——那是单章审查口径，不是整本质量。

只输出：
{{
  "status": "completed",
  "score": 0到10,
  "dimension_scores": {{
    "main_arc_closure": {{"score": 0到10, "rationale": "依据，引用章节/事件"}},
    "foreshadowing_payoff": {{"score": 0到10, "rationale": "依据，引用伏笔"}},
    "character_arc": {{"score": 0到10, "rationale": "依据"}},
    "pacing_curve": {{"score": 0到10, "rationale": "依据，指出注水腰位置"}},
    "world_consistency": {{"score": 0到10, "rationale": "依据"}},
    "ending_satisfaction": {{"score": 0到10, "rationale": "依据"}},
    "emotional_resonance": {{"score": 0到10, "rationale": "依据"}},
    "overall_readability": {{"score": 0到10, "rationale": "依据"}}
  }},
  "verdict": "通过发布/修订后发布/不建议发布",
  "executive_summary": "不超过400字，必须明确说明：主线是否闭环、伏笔回收情况、是否存在注水腰",
  "foreshadowing_analysis": {{
    "planted": "埋设总数",
    "resolved": "判断已回收数",
    "unresolved": ["未回收的重要伏笔"],
    "payoff_quality": "回收质量评价"
  }},
  "character_arc_analysis": "主角从X到Y的成长/转变分析",
  "strengths": ["整本优势"],
  "unresolved_threads": ["未回收伏笔或支线"],
  "issues": [{{"severity": "critical/major/minor", "chapters": [章节号], "category": "类别", "detail": "问题", "evidence": "证据", "suggestion": "修订建议"}}],
  "publication_recommendation": "具体发布建议"
}}
issues最多15条，只保留最重要且有全书证据的问题；不要把格式问题擅自升级为critical。"""

    result = ai_call(project, config, system, prompt, "book_final_review")
    # 兜底：若 AI 未返回 dimension_scores，补一个空结构，保证下游 schema 稳定
    if isinstance(result, dict) and "score" in result and not result.get("dimension_scores"):
        result["dimension_scores"] = {}
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
        f"- 整本评分：**{final.get('score', 'N/A')} / 10**（基于整本阅读体验的加权综合分，非逐章平均）",
        f"- 总字数：{local_scan.get('total_words', 0):,}",
        f"- 逐章均分：{local_scan.get('average_review_score', 0)}（单章口径，仅供参考，不代表整本质量）",
        f"- 逐章中位分：{local_scan.get('median_review_score', 0)} · 过审率：{local_scan.get('review_pass_rate', 0) * 100:.1f}%",
        "",
        "## 执行摘要",
        "",
        str(final.get("executive_summary", "终审未返回有效摘要。")),
        "",
    ]

    # 整本专属维度评分表（8 维度）
    dimension_scores = final.get("dimension_scores")
    if isinstance(dimension_scores, dict) and dimension_scores:
        dimension_labels = {
            "main_arc_closure": "主线闭环（20%）",
            "foreshadowing_payoff": "伏笔回收（15%）",
            "character_arc": "人物弧线（15%）",
            "pacing_curve": "节奏曲线（15%）",
            "world_consistency": "世界观自洽（10%）",
            "ending_satisfaction": "结局满意度（10%）",
            "emotional_resonance": "情感共鸣（10%）",
            "overall_readability": "整体可读性（5%）",
        }
        lines.extend(["## 整本专属维度评分", "", "| 维度 | 分数 | 依据 |", "|---|---:|---|"])
        for key, label in dimension_labels.items():
            entry = dimension_scores.get(key)
            if isinstance(entry, dict):
                score = entry.get("score", "N/A")
                rationale = str(entry.get("rationale", "")).replace("|", "｜").replace("\n", " ")[:200]
                lines.append(f"| {label} | {score} | {rationale} |")
        lines.append("")

    # 伏笔回收分析
    foreshadow = final.get("foreshadowing_analysis")
    if isinstance(foreshadow, dict) and foreshadow:
        lines.extend([
            "## 伏笔回收分析",
            "",
            f"- 埋设：{foreshadow.get('planted', 'N/A')} 条",
            f"- 已回收：{foreshadow.get('resolved', 'N/A')} 条",
            f"- 回收质量：{foreshadow.get('payoff_quality', 'N/A')}",
        ])
        unresolved = foreshadow.get("unresolved", [])
        if isinstance(unresolved, list) and unresolved:
            lines.append("- 未回收：")
            for item in unresolved:
                lines.append(f"  - {item}")
        lines.append("")

    # 人物弧线分析
    arc_analysis = final.get("character_arc_analysis")
    if arc_analysis:
        lines.extend(["## 人物弧线分析", "", str(arc_analysis), ""])

    lines.extend([
        "## 完整性与本地质量门",
        "",
        f"- 产物数量：{json.dumps(local_scan.get('artifact_counts', {}), ensure_ascii=False)}",
        f"- Final 本地通过：{local_scan.get('final_ok_count')}/{local_scan.get('total_chapters')}",
        f"- 逐章审查通过：{local_scan.get('review_ok_count')}/{local_scan.get('total_chapters')}",
        f"- 单章字数：最少 {local_scan.get('min_words')}，最多 {local_scan.get('max_words')}，平均 {local_scan.get('average_words')}",
        f"- 逐章评分：最低 {local_scan.get('min_review_score')}，最高 {local_scan.get('max_review_score')}，中位 {local_scan.get('median_review_score', 0)}",
        f"- 本地问题统计：{json.dumps(local_scan.get('severity_counts', {}), ensure_ascii=False)}",
        "",
        "## 卷级结果",
        "",
        "| 范围 | 评分 | 结论 | 摘要 |",
        "|---|---:|---|---|",
    ])
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
    parser.add_argument("--start", type=int, default=0, help="起始章节（默认1，用于小批量验证）")
    parser.add_argument("--end", type=int, default=0, help="结束章节（默认读config.total_chapters，用于小批量验证）")
    args = parser.parse_args()

    project = resolve_project_dir(args.project)
    config = load_config(project)
    config_total = int(config.get("total_chapters", 0))
    # 支持范围参数：未生成全本正文时，只审已有章节验证终审分数
    start_ch = args.start if args.start > 0 else 1
    total = args.end if args.end > 0 else config_total
    min_score = float(config.get("reviewer", {}).get("min_score", 8.5))
    # 范围模式：用独立报告目录，避免覆盖全本终审结果
    if start_ch != 1 or total != config_total:
        reports = project / "reports" / f"book_review_{start_ch:04d}_{total:04d}"
        log(project, f"范围终审模式: 第{start_ch}-{total}章 (全本{config_total}章)，报告写入独立目录")
    else:
        reports = project / "reports" / "book_review"
    segments_dir = reports / "segments"
    volumes_dir = reports / "volumes"
    reports.mkdir(parents=True, exist_ok=True)

    log(project, f"开始整本终审: {start_ch}-{total}章")
    local_path = reports / "local_full_scan.json"
    local_scan = local_full_scan(project, total, min_score, start_ch=start_ch)
    atomic_json(local_path, local_scan)
    log(project, f"本地全量扫描完成: issues={len(local_scan['issues'])}")

    if args.local_only:
        return 0

    workers = args.workers or int(config.get("book_reviewer", {}).get("workers", 5) or 5)
    segment_ranges = [(start, min(total, start + 9)) for start in range(start_ch, total + 1, 10)]
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

    volume_size = 50
    volume_ranges = [(start, min(total, start + volume_size - 1)) for start in range(start_ch, total + 1, volume_size)]
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
        "range": f"{start_ch}-{total}",
        "local_scan": str(local_path),
        "segments": len(segment_list),
        "segment_completed": sum(item.get("status") == "completed" for item in segment_list),
        "volumes": len(volume_list),
        "volume_completed": sum(item.get("status") == "completed" for item in volume_list),
        "final_review": str(final_path),
        "markdown_report": str(reports / "final_book_review.md"),
    }
    atomic_json(reports / "manifest.json", manifest)
    log(project, f"整本终审完成({start_ch}-{total}): verdict={final.get('verdict')} score={final.get('score')}")
    return 0 if manifest["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
