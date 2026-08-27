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

from core.json_repair import fix_inner_quotes, fix_truncated_json
from core.novel_config import (
    load_config,
    resolve_project_dir,
)
from core.review_ai_client import ReviewAIError, call_review_ai
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
    except Exception as exc:
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
    except Exception as exc:
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
            except Exception as exc:
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


def _keyword_hits(text: str, keywords: tuple[str, ...]) -> list[str]:
    return [keyword for keyword in keywords if keyword and keyword in text]


def _human_warmth_chapter_signals(text: str) -> dict:
    """Extract cheap cross-chapter signals for lived-in storytelling."""
    daily_object_keywords = (
        "碗", "杯", "筷", "饭盒", "药", "药瓶", "账单", "零钱", "钥匙", "伞", "照片", "纸条",
        "旧衣", "袖口", "鞋", "门", "灯", "手机", "屏幕", "毛巾", "茶", "粥", "面", "菜",
        "伤口", "手心", "指节", "汗", "烟", "酒", "房租", "工资", "工钱",
    )
    side_choice_keywords = (
        "拦住", "递给", "塞给", "替他", "替她", "挡在", "拉住", "扶住", "留下", "转身",
        "摇头", "点头", "藏起", "拿出", "放下", "推开", "收回", "护住", "等你", "别怕",
        "别告诉", "我来", "算了", "不用还", "先吃", "先走",
    )
    object_hits = _keyword_hits(text, daily_object_keywords)
    side_choice_hits = _keyword_hits(text, side_choice_keywords)
    question_count = len(re.findall(r"[？?]", text))
    dialogue_markers = text.count("\u201c") + text.count('"') + text.count("：")
    return {
        "object_hits": object_hits[:10],
        "object_hit_count": len(object_hits),
        "question_count": question_count,
        "dialogue_markers": dialogue_markers,
        "side_choice_hits": side_choice_hits[:10],
        "side_choice_hit_count": len(side_choice_hits),
        "missing_object": len(object_hits) < 2,
        "missing_question_dialogue": question_count < 1 or dialogue_markers < 4,
        "missing_side_choice": len(side_choice_hits) < 1,
    }


def _human_warmth_streak_issues(chapter_signals: dict[int, dict], *, window: int = 3) -> list[dict]:
    issues: list[dict] = []
    checks = (
        ("missing_object", "连续多章缺少可触摸生活物件", "生活物件命中不足"),
        ("missing_question_dialogue", "连续多章缺少问句对白或互动密度", "问句对白/互动不足"),
        ("missing_side_choice", "连续多章缺少配角主动选择", "配角主动动作不足"),
    )
    chapters = sorted(chapter_signals)
    for key, detail, evidence_label in checks:
        run: list[int] = []
        previous: int | None = None
        for chapter in chapters + [10**9]:
            signal = chapter_signals.get(chapter, {})
            contiguous = previous is None or chapter == previous + 1
            if chapter != 10**9 and contiguous and signal.get(key) is True:
                run.append(chapter)
                previous = chapter
                continue
            if len(run) >= window:
                selected = run[:window]
                snippets = []
                for value in selected:
                    sig = chapter_signals.get(value, {})
                    snippets.append(
                        f"第{value}章 object={sig.get('object_hit_count', 0)} "
                        f"question={sig.get('question_count', 0)} "
                        f"side_choice={sig.get('side_choice_hit_count', 0)}"
                    )
                issues.append(make_issue(
                    "major",
                    "human_warmth_streak",
                    selected,
                    detail,
                    f"{evidence_label}: " + "; ".join(snippets),
                ))
            run = [chapter] if chapter != 10**9 and signal.get(key) is True else []
            previous = chapter if chapter != 10**9 else None
    return issues[:12]


def _excerpt_around_hits(text: str, hits: list[str], *, limit: int = 120) -> str:
    clean = re.sub(r"\s+", "", str(text or ""))
    if not clean:
        return ""
    positions = [clean.find(hit) for hit in hits if hit and clean.find(hit) >= 0]
    center = min(positions) if positions else 0
    start = max(0, center - limit // 3)
    excerpt = clean[start:start + limit]
    if start > 0:
        excerpt = "..." + excerpt
    if start + limit < len(clean):
        excerpt += "..."
    return excerpt


def _human_warmth_exemplars(chapter_rows: list[dict], final_texts: dict[int, str] | None = None, *, limit: int = 5) -> dict:
    """Rank chapters by cheap lived-in storytelling signals for human review."""
    final_texts = final_texts or {}
    ranked: list[dict] = []
    for row in chapter_rows:
        if not isinstance(row, dict):
            continue
        signals = row.get("human_warmth_signals")
        if not isinstance(signals, dict):
            continue
        object_count = int(signals.get("object_hit_count", 0) or 0)
        question_count = int(signals.get("question_count", 0) or 0)
        side_choice_count = int(signals.get("side_choice_hit_count", 0) or 0)
        dialogue_markers = int(signals.get("dialogue_markers", 0) or 0)
        score = (
            min(object_count, 5) * 2
            + min(question_count, 3) * 2
            + min(side_choice_count, 4) * 3
            + min(dialogue_markers, 8)
        )
        missing = [
            label
            for key, label in (
                ("missing_object", "生活物件不足"),
                ("missing_question_dialogue", "问句对白不足"),
                ("missing_side_choice", "配角主动选择不足"),
            )
            if signals.get(key)
        ]
        hit_terms = (signals.get("object_hits", [])[:3] or []) + (signals.get("side_choice_hits", [])[:3] or [])
        chapter_no = int(row.get("chapter") or 0)
        ranked.append({
            "chapter": row.get("chapter"),
            "title": row.get("title", ""),
            "human_warmth_signal_score": score,
            "review_score": row.get("review_score"),
            "object_hits": signals.get("object_hits", [])[:5],
            "side_choice_hits": signals.get("side_choice_hits", [])[:5],
            "question_count": question_count,
            "dialogue_markers": dialogue_markers,
            "missing": missing,
            "sample_excerpt": _excerpt_around_hits(final_texts.get(chapter_no, ""), hit_terms),
        })
    best = sorted(
        ranked,
        key=lambda item: (
            item.get("human_warmth_signal_score", 0),
            float(item.get("review_score") or 0),
            -int(item.get("chapter") or 0),
        ),
        reverse=True,
    )[:limit]
    weakest = sorted(
        ranked,
        key=lambda item: (
            item.get("human_warmth_signal_score", 0),
            float(item.get("review_score") or 0),
            int(item.get("chapter") or 0),
        ),
    )[:limit]
    return {"most_lived_in": best, "most_flat": weakest}


def local_full_scan(project: Path, total: int, min_score: float, start_ch: int = 1) -> dict:
    rules = load_quality_rules(project)
    statuses = scan_chapter_status(project, start_ch, total, use_cache=False)
    issues: list[dict] = []
    chapter_rows: list[dict] = []
    hashes: dict[str, list[int]] = {}
    fingerprints: dict[int, set[str]] = {}
    final_texts: dict[int, str] = {}
    human_warmth_signals: dict[int, dict] = {}

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
        human_warmth = _human_warmth_chapter_signals(text)
        human_warmth_signals[chapter] = human_warmth

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
            "human_warmth_signals": human_warmth,
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

    issues.extend(_human_warmth_streak_issues(human_warmth_signals))

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
        "human_warmth_exemplars": _human_warmth_exemplars(chapter_rows, final_texts),
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


def select_key_chapters(project: Path, total: int, volumes: list[dict], max_full: int = 16) -> dict:
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
        max_full: 喂全文的章节数上限（配置驱动，默认 16），控制 prompt 篇幅。

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


def _load_foreshadowing_ledger(project: Path, total: int) -> dict:
    """从 reports/foreshadowing_ledger.json 读取权威伏笔台账，供终审判断回收情况。

    优先用台账（包含 plant/resolve 章号、状态、类别），找不到则回退到空。
    """
    ledger_path = project / "reports" / "foreshadowing_ledger.json"
    ledger = load_json(ledger_path)
    if not ledger:
        return {}
    return ledger


def _summarize_foreshadowing_ledger(ledger: dict, total: int) -> dict:
    """把伏笔台账压缩成终审可读的结构化摘要（统计 + 悬空线程明细）。"""
    threads = ledger.get("threads", [])
    if not isinstance(threads, list):
        threads = []
    stats = ledger.get("stats", {}) if isinstance(ledger.get("stats"), dict) else {}
    # 重新统计，确保准确（台账 stats 可能陈旧）
    planted = [t for t in threads if isinstance(t, dict)]
    resolved = [t for t in planted if str(t.get("status", "")).lower() in {"resolved", "paid_off", "closed"}]
    dangling = [t for t in planted if str(t.get("status", "")).lower() in {"planted", "open", "dangling", ""}]
    # 悬空线程中按优先级排序，取最重要的 30 条供终审核查
    priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    dangling_sorted = sorted(
        dangling,
        key=lambda t: (priority_order.get(str(t.get("priority", "")).lower(), 9), int(t.get("planted_at", 9999))),
    )
    dangling_detail = []
    for t in dangling_sorted[:30]:
        dangling_detail.append({
            "id": t.get("id", ""),
            "planted_at": t.get("planted_at"),
            "must_resolve_by": t.get("must_resolve_by"),
            "category": t.get("category", ""),
            "priority": t.get("priority", ""),
            "setup": str(t.get("setup", ""))[:160],
        })
    return {
        "planted_total": len(planted),
        "resolved_total": len(resolved),
        "dangling_total": len(dangling),
        "dangling_detail": dangling_detail,
        "ledger_stats": stats,
    }


def _load_pacing_ledgers(project: Path) -> dict:
    """从 build_outline_ledgers.py 产物读取节奏/重复信号，供终审核查注水腰。

    读取 reports/outline_reconstruction/ledgers/ 下的 repetition_blacklist 与 timeline。
    不存在则返回空（终审退化为仅靠大纲骨架判断）。
    """
    base = project / "reports" / "outline_reconstruction" / "ledgers"
    result: dict[str, Any] = {}
    repetition = load_json(base / "repetition_blacklist.json")
    if repetition:
        items = repetition.get("items") or repetition.get("entries") or []
        if isinstance(items, list):
            result["repetition_issues"] = [
                {"chapters": it.get("chapters", []), "phrase": str(it.get("phrase", it.get("text", "")))[:80], "count": it.get("count")}
                for it in items[:20]
                if isinstance(it, dict)
            ]
    return result


def _relationship_pressure_text(item: dict[str, Any]) -> str:
    return "；".join(
        str(item.get(field, "") or "").strip()
        for field in ("debt", "misunderstanding", "promise", "unsaid", "next_pressure")
        if str(item.get(field, "") or "").strip()
    )


def _relationship_response_text(item: dict[str, Any]) -> str:
    return "；".join(
        str(item.get(field, "") or "").strip()
        for field in ("change", "care_action")
        if str(item.get(field, "") or "").strip()
    )


def _relationship_pressure_signature(item: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(item.get(field, "") or "").strip()[:40]
        for field in ("debt", "misunderstanding", "promise", "unsaid", "next_pressure")
        if str(item.get(field, "") or "").strip()
    )


def _relationship_trajectory_evidence(pair: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    chapters = [int(item.get("chapter", 0) or 0) for item in items]
    pressure_items = [item for item in items if _relationship_pressure_text(item)]
    response_items = [item for item in items if _relationship_response_text(item) or item.get("resolved")]
    carried_over = [item for item in items if item.get("carried_over")]
    signatures = {_relationship_pressure_signature(item) for item in pressure_items}
    signatures.discard(())
    first = items[0]
    last = items[-1]
    span = max(chapters) - min(chapters) + 1 if chapters else 0
    resolved = any(item.get("resolved") is True for item in items)
    response_count = len(response_items)
    pressure_count = len(pressure_items)
    stagnant = (
        span >= 3
        and pressure_count >= 2
        and response_count == 0
        and len(signatures) <= 1
    )
    open_without_payoff = (
        span >= 5
        and pressure_count >= 2
        and not resolved
        and response_count < 2
    )
    status = "resolved" if resolved else "active"
    if stagnant:
        status = "stagnant"
    elif open_without_payoff:
        status = "open_without_payoff"
    return {
        "pair": pair,
        "chapters": chapters[:12],
        "first_chapter": first.get("chapter"),
        "last_chapter": last.get("chapter"),
        "span": span,
        "pressure_count": pressure_count,
        "response_count": response_count,
        "carried_over_count": len(carried_over),
        "carried_over_ratio": round(len(carried_over) / len(items), 3) if items else 0.0,
        "distinct_pressure_states": len(signatures),
        "resolved": resolved,
        "status": status,
        "first_pressure": (_relationship_pressure_text(first) or str(first.get("change", "") or ""))[:160],
        "latest_pressure": (_relationship_pressure_text(last) or str(last.get("change", "") or ""))[:160],
        "latest_response": (_relationship_response_text(last) or ("resolved=true" if last.get("resolved") else ""))[:160],
    }


def _summarize_relationship_states(project: Path, total: int) -> dict:
    """读取 relationship_states，压缩成人物温度曲线证据。"""
    base = project / "chapters" / "relationship_states"
    if not base.exists():
        return {}
    all_items: list[dict[str, Any]] = []
    by_pair: dict[str, list[dict[str, Any]]] = {}
    for chapter in range(1, total + 1):
        path = base / f"chapter_{chapter:04d}.json"
        data = load_json(path)
        rels = data.get("relationships", []) if isinstance(data, dict) else []
        if not isinstance(rels, list):
            continue
        for raw in rels:
            if not isinstance(raw, dict):
                continue
            pair = str(raw.get("pair", "")).strip()
            if not pair:
                a = str(raw.get("from", "")).strip()
                b = str(raw.get("to", "")).strip()
                pair = f"{a}->{b}" if a or b else ""
            if not pair:
                continue
            item = {
                "chapter": chapter,
                "pair": pair,
                "change": str(raw.get("change", ""))[:80],
                "debt": str(raw.get("debt", ""))[:80],
                "misunderstanding": str(raw.get("misunderstanding", ""))[:80],
                "promise": str(raw.get("promise", ""))[:80],
                "care_action": str(raw.get("care_action", ""))[:80],
                "unsaid": str(raw.get("unsaid", ""))[:80],
                "next_pressure": str(raw.get("next_pressure", ""))[:120],
                "resolved": raw.get("resolved") is True,
                "carried_over": raw.get("carried_over") is True,
            }
            all_items.append(item)
            by_pair.setdefault(pair, []).append(item)
    if not all_items:
        return {}

    long_open: list[dict[str, Any]] = []
    trajectories: list[dict[str, Any]] = []
    trajectory_issues: list[dict[str, Any]] = []
    for pair, items in by_pair.items():
        items.sort(key=lambda x: int(x.get("chapter", 0) or 0))
        open_items = [it for it in items if not it.get("resolved")]
        first = items[0]
        last = items[-1]
        if open_items and int(last["chapter"]) - int(first["chapter"]) >= 5:
            long_open.append({
                "pair": pair,
                "first_chapter": first["chapter"],
                "last_chapter": last["chapter"],
                "span": int(last["chapter"]) - int(first["chapter"]) + 1,
                "latest_pressure": last.get("next_pressure") or last.get("debt") or last.get("unsaid"),
            })
        if len(items) >= 2:
            trajectory = _relationship_trajectory_evidence(pair, items)
            trajectories.append(trajectory)
            if trajectory.get("status") in {"stagnant", "open_without_payoff"}:
                trajectory_issues.append({
                    "pair": pair,
                    "status": trajectory.get("status"),
                    "first_chapter": trajectory.get("first_chapter"),
                    "last_chapter": trajectory.get("last_chapter"),
                    "span": trajectory.get("span"),
                    "evidence": (
                        f"pressure_count={trajectory.get('pressure_count')} "
                        f"response_count={trajectory.get('response_count')} "
                        f"carried_over={trajectory.get('carried_over_count')}; "
                        f"latest={trajectory.get('latest_pressure')}"
                    )[:260],
                })
    long_open.sort(key=lambda x: (-int(x.get("span", 0)), x.get("first_chapter", 0)))
    trajectories.sort(key=lambda x: (
        0 if x.get("status") in {"stagnant", "open_without_payoff"} else 1,
        -int(x.get("span", 0) or 0),
        x.get("first_chapter", 0),
    ))
    trajectory_issues.sort(key=lambda x: (-int(x.get("span", 0) or 0), x.get("first_chapter", 0)))
    return {
        "relationship_event_total": len(all_items),
        "pair_count": len(by_pair),
        "carried_over_total": sum(1 for item in all_items if item.get("carried_over")),
        "resolved_total": sum(1 for item in all_items if item.get("resolved")),
        "long_open_threads": long_open[:20],
        "trajectory_status_counts": {
            "resolved": sum(1 for item in trajectories if item.get("status") == "resolved"),
            "active": sum(1 for item in trajectories if item.get("status") == "active"),
            "open_without_payoff": sum(1 for item in trajectories if item.get("status") == "open_without_payoff"),
            "stagnant": sum(1 for item in trajectories if item.get("status") == "stagnant"),
        },
        "relationship_trajectory_issues": trajectory_issues[:20],
        "sample_trajectories": trajectories[:20],
    }


def _relationship_repair_targets_from_context(context: dict, total: int) -> list[dict]:
    """Build deterministic repair targets from long-open relationship debts."""
    signals = context.get("relationship_signals")
    if not isinstance(signals, dict):
        return []
    long_open = signals.get("long_open_threads")
    if not isinstance(long_open, list):
        long_open = []
    trajectory_issues = signals.get("relationship_trajectory_issues")
    if not isinstance(trajectory_issues, list):
        trajectory_issues = []

    targets: list[dict] = []
    for item in long_open:
        if not isinstance(item, dict):
            continue
        pair = str(item.get("pair", "")).strip()
        first = item.get("first_chapter")
        last = item.get("last_chapter")
        span = item.get("span")
        try:
            first_chapter = int(first)
            last_chapter = int(last)
        except (TypeError, ValueError):
            continue
        if not pair or first_chapter <= 0 or last_chapter <= 0:
            continue

        target_chapter = min(max(last_chapter, 1), max(total, 1))
        latest_pressure = str(item.get("latest_pressure", "")).strip()
        problem = f"{pair} 从第{first_chapter}章到第{last_chapter}章持续承压，但缺少可见回声或收束"
        evidence = latest_pressure or f"关系跨度约 {span} 章，终审关系台账仍显示未解决压力"
        targets.append({
            "target_chapter": target_chapter,
            "pair": pair,
            "problem": problem,
            "evidence": evidence[:220],
            "acceptance": (
                "在目标章或其后相邻章节补一次明确的关系动作：选择、照料、摊牌、误会解除或代价兑现；"
                "正文中必须有可触摸物件/生活压力和潜台词对白，不能只用内心总结交代。"
            ),
        })
    existing = {
        (str(item.get("pair", "")).strip(), int(item.get("target_chapter", 0) or 0))
        for item in targets
        if isinstance(item, dict)
    }
    for item in trajectory_issues:
        if not isinstance(item, dict):
            continue
        pair = str(item.get("pair", "")).strip()
        try:
            first_chapter = int(item.get("first_chapter"))
            last_chapter = int(item.get("last_chapter"))
        except (TypeError, ValueError):
            continue
        if not pair or first_chapter <= 0 or last_chapter <= 0:
            continue
        target_chapter = min(max(last_chapter, 1), max(total, 1))
        key = (pair, target_chapter)
        if key in existing:
            continue
        status = str(item.get("status", "")).strip()
        evidence = str(item.get("evidence", "")).strip()
        targets.append({
            "target_chapter": target_chapter,
            "pair": pair,
            "problem": f"{pair} 第{first_chapter}-{last_chapter}章关系轨迹状态为 {status}，缺少欠账后的回声或收束",
            "evidence": evidence[:220] or f"关系轨迹跨度约 {item.get('span')} 章，终审本地轨迹扫描判定为 {status}",
            "acceptance": (
                "补出这条关系的轨迹节点：至少一次具体照料/回避/补偿/摊牌动作，"
                "并让 next_pressure 发生变化或 resolved=true，不能继续只 carried_over 同一笔欠账。"
            ),
        })
        existing.add(key)
    return targets[:10]


def build_whole_book_context(project: Path, total: int, volumes: list[dict], local_scan: dict, *, max_full: int = 16) -> dict:
    """构建终审所需的"全书内容"上下文，让 AI 真正读到小说而不是只看统计数字。

    这是本次重构的核心：把 world.json 的主线/三幕结构、关键章节原文、全书大纲采样、
    【权威伏笔台账】、【节奏重复信号】整合在一起，供终审 AI 做基于阅读体验的整本评分。
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

    # 关键章节原文（max_full 由配置驱动，默认 16 章，让终审能验证更多结构节点）
    key_chapters_map = select_key_chapters(project, total, volumes, max_full=max_full)
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

    # 权威伏笔台账：优先用 reports/foreshadowing_ledger.json（含 plant/resolve 状态），
    # 这是让终审能"按 ID 核查回收"的关键。回退到大纲聚合仅作兜底。
    ledger = _load_foreshadowing_ledger(project, total)
    if ledger and ledger.get("threads"):
        foreshadowing_summary = _summarize_foreshadowing_ledger(ledger, total)
        foreshadowing_inventory: list[dict] = []
        foreshadowing_total = foreshadowing_summary["planted_total"]
    else:
        # 兜底：从每章 foreshadowing 字段聚合（无 resolve 状态，质量较差）
        foreshadowing_inventory = []
        foreshadowing_total = 0
        sample_step = max(1, total // 120)
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
        foreshadowing_summary = None

    # 节奏/重复信号：接入 build_outline_ledgers.py 产物，让终审能定位注水腰
    pacing_signals = _load_pacing_ledgers(project)
    relationship_signals = _summarize_relationship_states(project, total)

    # 统计指标作为辅助参考（不再是主依据）
    score_distribution = {
        key: local_scan.get(key)
        for key in (
            "total_chapters", "final_ok_count", "review_ok_count", "total_words",
            "average_review_score", "median_review_score", "review_pass_rate", "severity_counts",
        )
    }
    human_warmth_scan = {
        "streak_issues": [
            issue
            for issue in local_scan.get("issues", [])
            if isinstance(issue, dict) and issue.get("category") == "human_warmth_streak"
        ][:20],
        "weak_chapters": [
            {
                "chapter": row.get("chapter"),
                "object_hit_count": signals.get("object_hit_count"),
                "question_count": signals.get("question_count"),
                "side_choice_hit_count": signals.get("side_choice_hit_count"),
            }
            for row in local_scan.get("chapters", [])
            if isinstance(row, dict)
            for signals in [row.get("human_warmth_signals")]
            if isinstance(signals, dict)
            and (
                signals.get("missing_object")
                or signals.get("missing_question_dialogue")
                or signals.get("missing_side_choice")
            )
        ][:40],
        "exemplars": local_scan.get("human_warmth_exemplars", {}),
    }

    context = {
        "world_arc": world_arc,
        "key_chapters_fulltext": key_chapters_fulltext,
        "all_outlines_compact": all_outlines_compact,
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
        "human_warmth_scan": human_warmth_scan,
        "key_chapter_count": len(key_chapters_fulltext),
    }
    if foreshadowing_summary:
        context["foreshadowing_ledger"] = foreshadowing_summary
    else:
        context["foreshadowing_inventory"] = foreshadowing_inventory
    if pacing_signals:
        context["pacing_signals"] = pacing_signals
    if relationship_signals:
        context["relationship_signals"] = relationship_signals
    return context


def ai_call(project: Path, config: dict, system: str, prompt: str, raw_name: str) -> dict:
    review_cfg = config.get("book_reviewer", {})
    try:
        raw = call_review_ai(
            config,
            project,
            "book_reviewer",
            system,
            prompt,
            max_tokens=int(review_cfg.get("max_tokens", 4096)),
            temperature=float(review_cfg.get("temperature", 0.2)),
            timeout=int(review_cfg.get("timeout_seconds", 240)),
            raw_name=raw_name,
            fallback_retries=int(review_cfg.get("retries", 2)),
            fallback_retry_delay=float(review_cfg.get("retry_delay", 5.0)),
        )
    except ReviewAIError as exc:
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


def final_review(project: Path, config: dict, local_scan: dict, volumes: list[dict], output: Path, *, force: bool = False) -> dict:
    # 缓存复用：终审输出已存在且 force=False 时直接复用，避免每次都跑 3 轮 LLM
    if output.exists() and not force:
        cached = load_json(output)
        if cached.get("status") == "completed" and cached.get("score") is not None:
            log(project, "终审已有缓存且未要求 force，直接复用")
            return cached
    total = int(local_scan.get("total_chapters", 0)) or int(config.get("total_chapters", 0))
    review_cfg = config.get("book_reviewer", {})
    max_full = int(review_cfg.get("max_full_chapters", 16) or 16)
    # 构建真正的"全书内容"上下文：主线、关键章节原文、大纲采样、伏笔清单。
    # 这是本次重构的核心——让终审 AI 读到小说，而不是只看统计数字。
    context = build_whole_book_context(project, total, volumes, local_scan, max_full=max_full)

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
"""
    # 权威伏笔台账（含悬空线程明细）优先于旧式采样清单
    if context.get("foreshadowing_ledger"):
        ledger = context["foreshadowing_ledger"]
        prompt += f"""
## 全书伏笔台账（权威数据，含回收状态与悬空明细）
全书共 {ledger['planted_total']} 条伏笔，已回收 {ledger['resolved_total']} 条，悬空 {ledger['dangling_total']} 条。
悬空线程明细（按优先级，已截断到 30 条，请逐条判断是否构成烂尾）：
{json.dumps(ledger['dangling_detail'], ensure_ascii=False)}
"""
    else:
        prompt += f"""
## 全书伏笔清单（全书共 {context["foreshadowing_total"]} 条，此处为均匀采样，请判断哪些已回收、哪些悬空）
{json.dumps(context.get("foreshadowing_inventory", [])[:60], ensure_ascii=False)}
"""
    if context.get("pacing_signals"):
        prompt += f"""
## 节奏/重复信号（来自大纲 ledger 重建，用于定位注水腰与原地踏步）
{json.dumps(context["pacing_signals"], ensure_ascii=False)}
"""
    if context.get("relationship_signals"):
        prompt += f"""
## 人物温度与关系欠账轨迹（来自 chapters/relationship_states，用于判断人情味是否跨章延续）
{json.dumps(context["relationship_signals"], ensure_ascii=False)}
"""
    if context.get("human_warmth_scan"):
        prompt += f"""
## 烟火气连续性本地扫描（生活物件/问句对白/配角主动选择的连续缺失信号）
{json.dumps(context["human_warmth_scan"], ensure_ascii=False)}
"""
    prompt += f"""
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
7. emotional_resonance 情感共鸣（权重10%）：整本情感张力？关系欠账是否有回声和收束？是否连续缺少生活物件、问句对白或配角主动选择？是否有记忆点？
8. overall_readability 整体可读性（权重5%）：通读体验？是否有大量水文？

最终 score 是 8 维度的【加权综合分】（不是简单平均），请自行按权重计算。
【重要】score 不要参考 score_distribution 中的逐章均分——那是单章审查口径，不是整本质量。
【重要】若伏笔台账显示悬空线程已被回收（在关键章节原文或大纲中找到兑现），应判定为已回收而非悬空——
不要因台账 status 字段未更新就盲目扣分，以正文/大纲实际证据为准。

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
    "emotional_resonance": {{"score": 0到10, "rationale": "依据，引用关系欠账/人物反应/章节"}},
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
  "relationship_arc_analysis": "关键关系欠账是否持续发酵、产生回声并收束；若缺失则指出章节范围",
  "relationship_repair_targets": [
    {{"target_chapter": 章节号, "pair": "人物A->人物B", "problem": "关系线问题", "evidence": "章节证据", "acceptance": "修复后必须满足的验收标准"}}
  ],
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
    if isinstance(result, dict):
        fallback_targets = _relationship_repair_targets_from_context(context, total)
        targets = result.get("relationship_repair_targets")
        if not isinstance(targets, list):
            result["relationship_repair_targets"] = fallback_targets
        elif fallback_targets and not targets:
            result["relationship_repair_targets"] = fallback_targets
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

    relationship_arc = final.get("relationship_arc_analysis")
    if relationship_arc:
        lines.extend(["## 关系欠账与人物温度", "", str(relationship_arc), ""])

    relationship_signals = _summarize_relationship_states(project, int(local_scan.get("total_chapters", 0) or 0))
    trajectory_issues = relationship_signals.get("relationship_trajectory_issues") if isinstance(relationship_signals, dict) else []
    if isinstance(trajectory_issues, list) and trajectory_issues:
        lines.extend(["## 本地关系轨迹问题", ""])
        for item in trajectory_issues[:10]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- {item.get('pair', '未指定关系')}：第{item.get('first_chapter')}-{item.get('last_chapter')}章，"
                f"状态 `{item.get('status')}`，{item.get('evidence', '')}"
            )
        lines.append("")

    relationship_targets = final.get("relationship_repair_targets")
    if isinstance(relationship_targets, list) and relationship_targets:
        lines.extend(["## 关系线修复目标", ""])
        for index, target in enumerate(relationship_targets, 1):
            if not isinstance(target, dict):
                continue
            lines.extend([
                f"### {index}. 第{target.get('target_chapter', 'N/A')}章：{target.get('pair', '未指定关系')}",
                "",
                f"- 问题：{target.get('problem', '')}",
                f"- 证据：{target.get('evidence', '')}",
                f"- 验收：{target.get('acceptance', '')}",
                "",
            ])

    exemplars = local_scan.get("human_warmth_exemplars")
    if isinstance(exemplars, dict) and (exemplars.get("most_lived_in") or exemplars.get("most_flat")):
        lines.extend(["## 烟火气章节样例（本地信号）", ""])
        lived_in = exemplars.get("most_lived_in") if isinstance(exemplars.get("most_lived_in"), list) else []
        flat = exemplars.get("most_flat") if isinstance(exemplars.get("most_flat"), list) else []
        if lived_in:
            lines.extend(["### 最有人味章节样例", "", "| 章节 | 标题 | 信号分 | 物件/动作命中 | 摘录 |", "|---:|---|---:|---|---|"])
            for item in lived_in[:5]:
                hits = "、".join((item.get("object_hits") or [])[:3] + (item.get("side_choice_hits") or [])[:3])
                excerpt = str(item.get("sample_excerpt", "")).replace("|", "｜")
                lines.append(
                    f"| {item.get('chapter')} | {str(item.get('title', '')).replace('|', '｜')} | "
                    f"{item.get('human_warmth_signal_score')} | {hits or 'N/A'} | {excerpt or 'N/A'} |"
                )
            lines.append("")
        if flat:
            lines.extend(["### 最空泛章节样例", "", "| 章节 | 标题 | 信号分 | 缺口 | 摘录 |", "|---:|---|---:|---|---|"])
            for item in flat[:5]:
                missing = "、".join(item.get("missing") or [])
                excerpt = str(item.get("sample_excerpt", "")).replace("|", "｜")
                lines.append(
                    f"| {item.get('chapter')} | {str(item.get('title', '')).replace('|', '｜')} | "
                    f"{item.get('human_warmth_signal_score')} | {missing or 'N/A'} | {excerpt or 'N/A'} |"
                )
            lines.append("")

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
    parser.add_argument("--force", action="store_true", help="忽略缓存并强制重新终审")
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
    final = final_review(project, config, local_scan, volume_list, final_path, force=args.force)
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
