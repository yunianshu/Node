#!/usr/bin/env python3
"""统一企业微信推送模块 - 所有 Agent 共享的推送格式。"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

_SEPARATOR = "━━━━━━━━━━━━━━━━━━━━"
_STAGE_STATUS_ICON = {
    "开始": "🚀",
    "进度": "⏳",
    "完成": "✅",
    "异常": "❌",
}


def _get_webhook(config: dict) -> str:
    url = os.getenv("NOVEL_WEBHOOK_URL", "").strip()
    if url:
        return url
    url = config.get("webhook_url", "")
    if url:
        url = str(url).strip()
    if not url:
        url = config.get("coordinator", {}).get("wechat_webhook", "")
    return str(url).strip() if url else ""


def _send(webhook_url: str, content: str) -> bool:
    if not webhook_url:
        return False
    try:
        data = json.dumps({"msgtype": "text", "text": {"content": content}}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception:
        return False


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _latest_existing_mtime(paths: list[Path]) -> tuple[float, Path | None]:
    latest = 0.0
    latest_path: Path | None = None
    for path in paths:
        try:
            if not path.exists() or not path.is_file():
                continue
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > latest:
            latest = mtime
            latest_path = path
    return latest, latest_path


def _story_flow_input_files(project: Path) -> list[Path]:
    files = [
        project / "premise.txt",
        project / "world.json",
        project / "characters.json",
        project / "reports" / "book_review" / "final_book_review.json",
        project / "reports" / "book_review" / "local_full_scan.json",
        project / "reports" / "book_review" / "repair_manifest.json",
    ]
    for rel, pattern in (
        ("origin", "*"),
        ("chapters/outline", "chapter_*.json"),
        ("chapters/outline_review", "chapter_*_review.json"),
        ("chapters/draft", "chapter_*.txt"),
        ("chapters/review", "chapter_*_review.json"),
        ("chapters/final", "chapter_*.txt"),
        ("chapters/relationship_states", "chapter_*.json"),
    ):
        base = project / rel
        if base.exists():
            files.extend(path for path in base.rglob(pattern) if path.is_file())
    return files


def story_flow_audit_state(project: str | Path) -> dict:
    """Return parse and freshness state for reports/story_flow_audit.json."""
    project_path = Path(project)
    report = project_path / "reports" / "story_flow_audit.json"
    if not report.exists():
        return {"exists": False, "valid": False, "stale": False}

    data = _load_json(report)
    if not data:
        return {"exists": True, "valid": False, "stale": False, "path": str(report)}

    report_mtime = 0.0
    try:
        report_mtime = report.stat().st_mtime
    except OSError:
        pass
    latest_input_mtime, latest_input_path = _latest_existing_mtime(_story_flow_input_files(project_path))
    stale = bool(report_mtime and latest_input_mtime > report_mtime + 1.0)
    latest_rel = ""
    if latest_input_path is not None:
        try:
            latest_rel = str(latest_input_path.relative_to(project_path))
        except ValueError:
            latest_rel = str(latest_input_path)
    return {
        "exists": True,
        "valid": True,
        "stale": stale,
        "status": str(data.get("overall_status", "") or "unknown").strip(),
        "generated_at": str(data.get("generated_at", "") or "").strip(),
        "gaps": data.get("gaps") if isinstance(data.get("gaps"), list) else [],
        "stage_statuses": data.get("stage_statuses") if isinstance(data.get("stage_statuses"), dict) else {},
        "latest_input_path": latest_rel,
        "report_mtime": report_mtime,
        "latest_input_mtime": latest_input_mtime,
    }


def story_flow_audit_note(project: str | Path) -> str:
    """Return a compact non-ok or stale story-flow audit note for progress pushes."""
    state = story_flow_audit_state(project)
    if not state.get("exists"):
        return ""
    if not state.get("valid"):
        return "流程审计 invalid: 报告不可解析"
    if state.get("stale"):
        latest = str(state.get("latest_input_path", "") or "").strip()
        detail = f"报告早于 {latest}" if latest else "报告早于关键产物"
        return f"流程审计 stale: {detail}，需重跑"

    status = str(state.get("status", "") or "").strip()
    if not status or status == "ok":
        return ""

    labels: list[str] = []
    stages = state.get("stage_statuses")
    if isinstance(stages, dict):
        for stage in stages.values():
            if not isinstance(stage, dict) or stage.get("status") == "ok":
                continue
            label = str(stage.get("label", "") or "").strip()
            if label and label not in labels:
                labels.append(label)
            if len(labels) >= 3:
                break
    if not labels:
        gaps = state.get("gaps")
        if isinstance(gaps, list):
            labels = [str(item).strip() for item in gaps if str(item).strip()][:3]
    detail = "；".join(labels)
    if detail:
        return f"流程审计 {status}: {detail}"
    return f"流程审计 {status}"


def build_progress_message(
    *,
    title: str,
    outline: int = 0,
    outline_reviewed: int | None = None,
    outline_approved: int | None = None,
    draft: int = 0,
    reviewed: int = 0,
    final: int = 0,
    total_words: int = 0,
    total_chapters: int = 2000,
    active_writers: int = 0,
    avg_score: float | None = None,
    pass_rate: float | None = None,
    status_note: str = "",
    audit_note: str = "",
    eta_text: str = "",
) -> str:
    """构建统一的定期进度报告文本。

    avg_score 语义为"中位评分"（调用方传中位数，抗异常更稳）；pass_rate 为过审率（0-1）。
    """
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    outline_display = outline_approved if outline_approved is not None else outline_reviewed if outline_reviewed is not None else outline
    lines = [
        f"📖 《{title}》生成进度 ({ts})",
        _SEPARATOR,
        f"📋 大纲: {outline_display}/{total_chapters} 章",
    ]
    lines.extend([
        f"✍ 初稿: {draft}/{total_chapters} 章",
        f"📝 字数: {total_words:,}",
        f"🔍 审查: {reviewed}/{total_chapters} 章",
        f"📤 终稿: {final}/{total_chapters} 章",
    ])
    if eta_text:
        lines.append(f"⏱ 预计剩余: {eta_text}")
    if avg_score is not None:
        score_line = f"⭐ 中位评分: {avg_score:.2f}"
        if pass_rate is not None:
            score_line += f"（过审率 {pass_rate * 100:.1f}%）"
        lines.append(score_line)
    if status_note:
        lines.append(f"📍 当前: {status_note[:180]}")
    if audit_note:
        lines.append(f"🧭 审计: {audit_note[:180]}")
    if active_writers > 0:
        lines.append(f"🤖 活跃进程: {active_writers}")
    lines.append(_SEPARATOR)
    return "\n".join(lines)


def push_progress(
    *,
    config: dict,
    title: str,
    outline: int = 0,
    outline_reviewed: int | None = None,
    outline_approved: int | None = None,
    draft: int = 0,
    reviewed: int = 0,
    final: int = 0,
    total_words: int = 0,
    total_chapters: int = 2000,
    active_writers: int = 0,
    avg_score: float | None = None,
    pass_rate: float | None = None,
    status_note: str = "",
    audit_note: str = "",
    eta_text: str = "",
) -> bool:
    """推送定期进度报告。"""
    msg = build_progress_message(
        title=title,
        outline=outline,
        outline_reviewed=outline_reviewed,
        outline_approved=outline_approved,
        draft=draft,
        reviewed=reviewed,
        final=final,
        total_words=total_words,
        total_chapters=total_chapters,
        active_writers=active_writers,
        avg_score=avg_score,
        pass_rate=pass_rate,
        status_note=status_note,
        audit_note=audit_note,
        eta_text=eta_text,
    )
    return _send(_get_webhook(config), msg)


def push_stage_complete(
    *,
    config: dict,
    title: str,
    stage: str,
    start_chapter: int,
    end_chapter: int,
    processed: int = 0,
    failed: int = 0,
) -> bool:
    """推送阶段完成报告（Reviewer / Rewrite 等子 Agent 完成一批时使用）。"""
    msg = build_stage_message(
        title=title,
        stage=stage,
        status="完成",
        start_chapter=start_chapter,
        end_chapter=end_chapter,
        processed=processed,
        failed=failed,
    )
    return _send(_get_webhook(config), msg)


def build_stage_message(
    *,
    title: str,
    stage: str,
    status: str,
    start_chapter: int | None = None,
    end_chapter: int | None = None,
    processed: int = 0,
    failed: int = 0,
    error: str = "",
) -> str:
    """构建统一的阶段事件文本。"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    icon = _STAGE_STATUS_ICON.get(status, "ℹ")
    lines = [
        f"{icon} 《{title}》{stage}{status} ({ts})",
        _SEPARATOR,
    ]
    if start_chapter is not None and end_chapter is not None:
        lines.append(f"📄 章节: {start_chapter}-{end_chapter}")
    if processed > 0 or status in ("进度", "完成"):
        lines.append(f"✔ 成功: {processed} 章")
    if failed > 0:
        lines.append(f"❌ 失败: {failed} 章")
    if error:
        lines.append(f"错误: {str(error)[:300]}")
    lines.append(_SEPARATOR)
    return "\n".join(lines)


def push_stage_event(
    *,
    config: dict,
    title: str,
    stage: str,
    status: str,
    start_chapter: int | None = None,
    end_chapter: int | None = None,
    processed: int = 0,
    failed: int = 0,
    error: str = "",
) -> bool:
    """推送阶段开始、进度、完成或异常事件。"""
    msg = build_stage_message(
        title=title,
        stage=stage,
        status=status,
        start_chapter=start_chapter,
        end_chapter=end_chapter,
        processed=processed,
        failed=failed,
        error=error,
    )
    return _send(_get_webhook(config), msg)


def push_task_complete(
    *,
    config: dict,
    title: str,
    total_chapters: int,
    total_words: int,
    draft: int | None = None,
    review: int | None = None,
    final: int | None = None,
    avg_score: float = 0.0,
    pass_rate: float | None = None,
    rewrite_count: int = 0,
) -> bool:
    """推送整个小说生成任务完成报告。

    avg_score 语义为"中位评分"（调用方传中位数）；pass_rate 为过审率（0-1）。
    """
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    # 默认值：调用方没传则假设都完成（向后兼容）
    d = total_chapters if draft is None else draft
    r = total_chapters if review is None else review
    f = total_chapters if final is None else final
    score_line = f"⭐ 中位评分: {avg_score:.2f}"
    if pass_rate is not None:
        score_line += f"（过审率 {pass_rate * 100:.1f}%）"
    lines = [
        f"🎉 《{title}》生成任务全部完成! ({ts})",
        _SEPARATOR,
        f"📋 大纲: {total_chapters}/{total_chapters} 章",
        f"✍ 初稿: {d}/{total_chapters} 章",
        f"📝 字数: {total_words:,}",
        f"🔍 审查: {r}/{total_chapters} 章",
        f"📤 终稿: {f}/{total_chapters} 章",
        score_line,
    ]
    if rewrite_count > 0:
        lines.append(f"🔄 重写优化: {rewrite_count} 章")
    lines.append(_SEPARATOR)
    return _send(_get_webhook(config), "\n".join(lines))


def push_interrupted(
    *,
    config: dict,
    title: str,
    reason: str = "用户中断",
) -> bool:
    """推送任务中断通知。"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    msg = (
        f"⚠️ 《{title}》生成任务中断 ({ts})\n"
        f"{_SEPARATOR}\n"
        f"原因: {reason}\n"
        f"进度已保存，可断点续传\n"
        f"{_SEPARATOR}"
    )
    return _send(_get_webhook(config), msg)


def push_error(
    *,
    config: dict,
    title: str,
    error: str,
) -> bool:
    """推送异常错误通知。"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    msg = (
        f"❌ 《{title}》生成任务异常 ({ts})\n"
        f"{_SEPARATOR}\n"
        f"错误: {str(error)[:300]}\n"
        f"{_SEPARATOR}"
    )
    return _send(_get_webhook(config), msg)
