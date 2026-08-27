#!/usr/bin/env python3
"""Deterministic audit for story-flow and humanity evidence artifacts."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from core.novel_config import configure_stdio, load_config, resolve_project_dir
from core.workflow_state import atomic_write_json, report_path

configure_stdio()


LIFE_PROFILE_KEYS = (
    "family_ties",
    "livelihood_pressure",
    "old_debts",
    "soft_spot",
    "daily_habits",
    "relationship_taboo",
)


def load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None


def _iter_role_dicts(value: Any, path: str = ""):
    if isinstance(value, dict):
        if any(key in value for key in ("name", "identity", "motivation", "description", "arc")):
            yield path or "root", value
        for key, child in value.items():
            if key == "life_profile":
                continue
            child_path = f"{path}.{key}" if path else str(key)
            yield from _iter_role_dicts(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_role_dicts(child, f"{path}[{index}]")


def _life_profile_complete(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for key in LIFE_PROFILE_KEYS:
        item = value.get(key)
        if key == "daily_habits":
            if not isinstance(item, list) or not any(str(v).strip() for v in item):
                return False
            continue
        if not str(item or "").strip():
            return False
    return True


def _chapter_range(config: dict, start: int, end: int) -> tuple[int, int]:
    total = int(config.get("total_chapters", 0) or 0)
    if total <= 0:
        total = 1
    first = max(1, int(start or 1))
    last = int(end or total)
    last = min(max(first, last), total)
    return first, last


def _count_chapter_files(project: Path, folder: str, suffix: str, start: int, end: int) -> dict:
    chapters = []
    base = project / "chapters" / folder
    for chapter in range(start, end + 1):
        path = base / f"chapter_{chapter:04d}{suffix}"
        if path.exists():
            chapters.append(chapter)
    return {"count": len(chapters), "chapters_sample": chapters[:30]}


def audit_characters(project: Path) -> dict:
    data = load_json(project / "characters.json")
    if not isinstance(data, dict):
        return {"status": "missing_or_invalid", "role_count": 0, "complete_count": 0, "missing": []}
    roles = []
    missing = []
    for path, role in _iter_role_dicts(data):
        name = str(role.get("name") or role.get("identity") or path).strip()
        complete = _life_profile_complete(role.get("life_profile"))
        roles.append({"path": path, "name": name, "life_profile_complete": complete})
        if not complete:
            missing.append({"path": path, "name": name})
    return {
        "status": "ok",
        "role_count": len(roles),
        "complete_count": sum(1 for item in roles if item["life_profile_complete"]),
        "missing": missing[:30],
    }


def audit_outlines(project: Path, start: int, end: int) -> dict:
    rows = []
    missing_human_anchor = []
    for chapter in range(start, end + 1):
        path = project / "chapters" / "outline" / f"chapter_{chapter:04d}.json"
        data = load_json(path)
        if not isinstance(data, dict):
            rows.append({"chapter": chapter, "exists": False})
            continue
        anchor = str(data.get("human_anchor", "") or "").strip()
        ok = len(anchor) >= 25
        rows.append({"chapter": chapter, "exists": True, "human_anchor_ok": ok})
        if not ok:
            missing_human_anchor.append(chapter)
    return {
        "outline_count": sum(1 for row in rows if row.get("exists")),
        "human_anchor_ok_count": sum(1 for row in rows if row.get("human_anchor_ok")),
        "missing_human_anchor_chapters": missing_human_anchor[:50],
    }


def audit_outline_reviews(project: Path, start: int, end: int) -> dict:
    missing = []
    failed_human_warmth = []
    passed = 0
    for chapter in range(start, end + 1):
        path = project / "chapters" / "outline_review" / f"chapter_{chapter:04d}_review.json"
        data = load_json(path)
        if not isinstance(data, dict):
            missing.append(chapter)
            continue
        gates = data.get("design_gates") if isinstance(data.get("design_gates"), dict) else {}
        human = gates.get("human_warmth")
        if human is False:
            failed_human_warmth.append(chapter)
        verdict = str(data.get("verdict", "")).strip()
        if verdict in {"通过", "pass", "passed"} or data.get("passed") is True:
            passed += 1
    return {
        "review_count": (end - start + 1) - len(missing),
        "passed_count": passed,
        "missing_review_chapters": missing[:50],
        "failed_human_warmth_chapters": failed_human_warmth[:50],
    }


def audit_draft_reviews(project: Path, start: int, end: int) -> dict:
    missing = []
    failed_human = []
    failed_relationship = []
    origin_attention = []
    passed = 0
    for chapter in range(start, end + 1):
        path = project / "chapters" / "review" / f"chapter_{chapter:04d}_review.json"
        data = load_json(path)
        if not isinstance(data, dict):
            missing.append(chapter)
            continue
        if str(data.get("verdict", "")).strip() in {"通过", "pass", "passed"}:
            passed += 1
        local = data.get("local_analysis") if isinstance(data.get("local_analysis"), dict) else {}
        human = local.get("human_warmth_detection")
        if isinstance(human, dict) and human.get("passed") is False:
            failed_human.append(chapter)
        relationship = local.get("relationship_obligation_detection")
        if isinstance(relationship, dict) and relationship.get("required") is True and relationship.get("passed") is False:
            failed_relationship.append(chapter)
        origin = local.get("origin_fact_reference_detection")
        if isinstance(origin, dict) and origin.get("needs_attention") is True:
            origin_attention.append(chapter)
    return {
        "review_count": (end - start + 1) - len(missing),
        "passed_count": passed,
        "missing_review_chapters": missing[:50],
        "failed_human_warmth_chapters": failed_human[:50],
        "failed_relationship_obligation_chapters": failed_relationship[:50],
        "origin_fact_attention_chapters": origin_attention[:50],
    }


def audit_book_review(project: Path) -> dict:
    base = project / "reports" / "book_review"
    final = load_json(base / "final_book_review.json")
    manifest = load_json(base / "repair_manifest.json")
    local_scan = load_json(base / "local_full_scan.json")
    result = {
        "final_book_review_exists": isinstance(final, dict),
        "local_full_scan_exists": isinstance(local_scan, dict),
        "repair_manifest_exists": isinstance(manifest, dict),
    }
    if isinstance(final, dict):
        result.update({
            "score": final.get("score"),
            "verdict": final.get("verdict"),
            "relationship_repair_target_count": len(final.get("relationship_repair_targets", []))
            if isinstance(final.get("relationship_repair_targets"), list) else 0,
        })
    if isinstance(local_scan, dict):
        issues = local_scan.get("issues") if isinstance(local_scan.get("issues"), list) else []
        result["human_warmth_streak_count"] = sum(
            1 for item in issues if isinstance(item, dict) and item.get("category") == "human_warmth_streak"
        )
    if isinstance(manifest, dict):
        result["repair_manifest_status"] = manifest.get("status")
        result["repair_target_chapters"] = manifest.get("all_repair_chapters", [])
        result["verification_status"] = (manifest.get("verification") or {}).get("verified") if isinstance(manifest.get("verification"), dict) else None
    return result


def _status(level: str, label: str, evidence: dict, action: str = "") -> dict:
    return {
        "status": level,
        "label": label,
        "evidence": evidence,
        "suggested_action": action,
    }


def _stage_statuses(
    project: Path,
    *,
    start: int,
    end: int,
    artifacts: dict,
    characters: dict,
    outlines: dict,
    outline_reviews: dict,
    draft_reviews: dict,
    book_review: dict,
) -> dict:
    span = end - start + 1
    stages: dict[str, dict] = {}
    role_count = int(characters.get("role_count", 0) or 0)
    complete_count = int(characters.get("complete_count", 0) or 0)
    if characters.get("status") != "ok" or role_count == 0:
        stages["planner_characters"] = _status(
            "fail",
            "角色档案缺失或不可用",
            {"role_count": role_count, "complete_count": complete_count},
            f'python "scripts/pipeline/planner.py" --project "{project}"',
        )
    elif complete_count < role_count:
        stages["planner_characters"] = _status(
            "warn",
            "部分角色缺 life_profile",
            {"role_count": role_count, "complete_count": complete_count, "missing": characters.get("missing", [])[:10]},
            f'python "scripts/maintenance/backfill_humanity_fields.py" --project "{project}" --start {start} --end {end}',
        )
    else:
        stages["planner_characters"] = _status("ok", "角色 life_profile 证据完整", {"role_count": role_count})

    outline_count = int(outlines.get("outline_count", 0) or 0)
    anchor_ok = int(outlines.get("human_anchor_ok_count", 0) or 0)
    if outline_count < span:
        stages["outlines"] = _status(
            "fail",
            "单章大纲不完整",
            {"outline_count": outline_count, "expected": span},
            f'python "scripts/pipeline/outliner.py" --project "{project}" --start {start} --end {end}',
        )
    elif anchor_ok < outline_count:
        stages["outlines"] = _status(
            "warn",
            "部分大纲缺 human_anchor",
            {"outline_count": outline_count, "human_anchor_ok_count": anchor_ok, "missing": outlines.get("missing_human_anchor_chapters", [])[:20]},
            f'python "scripts/maintenance/backfill_humanity_fields.py" --project "{project}" --start {start} --end {end}',
        )
    else:
        stages["outlines"] = _status("ok", "单章大纲和 human_anchor 完整", {"outline_count": outline_count})

    review_count = int(outline_reviews.get("review_count", 0) or 0)
    failed_design = outline_reviews.get("failed_human_warmth_chapters", [])
    if review_count < span:
        stages["outline_reviews"] = _status(
            "warn",
            "部分大纲尚未审查",
            {"review_count": review_count, "expected": span, "missing": outline_reviews.get("missing_review_chapters", [])[:20]},
            f'python "scripts/pipeline/outline_reviewer.py" --project "{project}" --start {start} --end {end}',
        )
    elif failed_design:
        stages["outline_reviews"] = _status(
            "fail",
            "大纲 human_warmth 设计门存在失败章节",
            {"failed_human_warmth_chapters": failed_design[:20]},
            f'python "scripts/pipeline/coordinator.py" --project "{project}" --start {start} --end {end}',
        )
    else:
        stages["outline_reviews"] = _status("ok", "大纲审查人情味设计门无确定性失败", {"review_count": review_count})

    final_count = int((artifacts.get("final") or {}).get("count", 0) or 0)
    draft_review_count = int(draft_reviews.get("review_count", 0) or 0)
    draft_failures = (
        draft_reviews.get("failed_human_warmth_chapters", [])
        + draft_reviews.get("failed_relationship_obligation_chapters", [])
        + draft_reviews.get("origin_fact_attention_chapters", [])
    )
    if final_count == 0 and draft_review_count == 0:
        stages["draft_reviews"] = _status("warn", "正文阶段尚无审查证据", {"review_count": 0, "final_count": 0})
    elif draft_failures:
        stages["draft_reviews"] = _status(
            "fail",
            "正文审查存在人情味/关系/origin facts 缺口",
            {
                "failed_human_warmth_chapters": draft_reviews.get("failed_human_warmth_chapters", [])[:20],
                "failed_relationship_obligation_chapters": draft_reviews.get("failed_relationship_obligation_chapters", [])[:20],
                "origin_fact_attention_chapters": draft_reviews.get("origin_fact_attention_chapters", [])[:20],
            },
            f'python "scripts/pipeline/coordinator.py" --project "{project}" --start {start} --end {end}',
        )
    else:
        stages["draft_reviews"] = _status("ok", "正文审查未发现确定性人情味缺口", {"review_count": draft_review_count, "final_count": final_count})

    relationship_count = int((artifacts.get("relationship_states") or {}).get("count", 0) or 0)
    if final_count and relationship_count < final_count:
        stages["relationship_states"] = _status(
            "warn",
            "关系状态少于 final，后续关系注入证据不完整",
            {"relationship_state_count": relationship_count, "final_count": final_count},
            f'python "scripts/pipeline/coordinator.py" --project "{project}" --start {start} --end {end}',
        )
    else:
        stages["relationship_states"] = _status(
            "ok",
            "关系状态证据数量与正文进度匹配",
            {"relationship_state_count": relationship_count, "final_count": final_count},
        )

    if not book_review.get("final_book_review_exists"):
        stages["book_review"] = _status("warn", "整本终审尚未生成", {"final_book_review_exists": False})
    elif book_review.get("repair_manifest_exists") and book_review.get("repair_manifest_status") not in {"verified", None}:
        stages["book_review"] = _status(
            "fail",
            "整本终审存在待修复或待验证清单",
            {
                "score": book_review.get("score"),
                "verdict": book_review.get("verdict"),
                "repair_manifest_status": book_review.get("repair_manifest_status"),
                "repair_target_chapters": book_review.get("repair_target_chapters", []),
            },
            f'python "scripts/pipeline/coordinator.py" --project "{project}" --start {start} --end {end}',
        )
    else:
        stages["book_review"] = _status(
            "ok",
            "整本终审无待处理修复清单",
            {"score": book_review.get("score"), "verdict": book_review.get("verdict")},
        )
    return stages


def _overall_status(stage_statuses: dict) -> str:
    values = [str(item.get("status", "")) for item in stage_statuses.values() if isinstance(item, dict)]
    if "fail" in values:
        return "fail"
    if "warn" in values:
        return "warn"
    return "ok"


def build_audit(project: Path, *, start: int = 0, end: int = 0) -> dict:
    config = load_config(project)
    first, last = _chapter_range(config, start, end)
    span = last - first + 1
    artifacts = {
        "outline": _count_chapter_files(project, "outline", ".json", first, last),
        "outline_review": _count_chapter_files(project, "outline_review", "_review.json", first, last),
        "draft": _count_chapter_files(project, "draft", ".txt", first, last),
        "review": _count_chapter_files(project, "review", "_review.json", first, last),
        "final": _count_chapter_files(project, "final", ".txt", first, last),
        "relationship_states": _count_chapter_files(project, "relationship_states", ".json", first, last),
    }
    characters = audit_characters(project)
    outlines = audit_outlines(project, first, last)
    outline_reviews = audit_outline_reviews(project, first, last)
    draft_reviews = audit_draft_reviews(project, first, last)
    book_review = audit_book_review(project)
    gaps = []
    if characters.get("missing"):
        gaps.append("characters.json 中仍有角色缺 life_profile")
    if outlines.get("missing_human_anchor_chapters"):
        gaps.append("部分单章大纲缺 human_anchor")
    if outline_reviews.get("failed_human_warmth_chapters"):
        gaps.append("部分大纲 human_warmth 设计门未通过")
    if draft_reviews.get("failed_human_warmth_chapters"):
        gaps.append("部分正文未通过 human_warmth_detection")
    if draft_reviews.get("failed_relationship_obligation_chapters"):
        gaps.append("部分正文未兑现 relationship_obligation")
    if draft_reviews.get("origin_fact_attention_chapters"):
        gaps.append("部分正文需要补 origin/facts 事实素材兑现")
    if artifacts["relationship_states"]["count"] < artifacts["final"]["count"]:
        gaps.append("relationship_states 数量少于 final，后续关系注入证据不完整")
    stage_statuses = _stage_statuses(
        project,
        start=first,
        end=last,
        artifacts=artifacts,
        characters=characters,
        outlines=outlines,
        outline_reviews=outline_reviews,
        draft_reviews=draft_reviews,
        book_review=book_review,
    )
    for stage in stage_statuses.values():
        if not isinstance(stage, dict) or stage.get("status") == "ok":
            continue
        label = str(stage.get("label", "")).strip()
        if label and label not in gaps:
            gaps.append(label)
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "project": str(project),
        "range": {"start": first, "end": last, "chapter_count": span},
        "overall_status": _overall_status(stage_statuses),
        "stage_statuses": stage_statuses,
        "artifacts": artifacts,
        "characters": characters,
        "outlines": outlines,
        "outline_reviews": outline_reviews,
        "draft_reviews": draft_reviews,
        "book_review": book_review,
        "gaps": gaps,
        "suggested_next_actions": _suggest_actions(project, first, last, gaps),
    }


def _suggest_actions(project: Path, start: int, end: int, gaps: list[str]) -> list[str]:
    actions = []
    if any("life_profile" in gap or "human_anchor" in gap for gap in gaps):
        actions.append(
            f'python "scripts/maintenance/backfill_humanity_fields.py" --project "{project}" --start {start} --end {end}'
        )
    if any("大纲" in gap or "human_anchor" in gap for gap in gaps):
        actions.append(
            f'python "scripts/pipeline/outline_reviewer.py" --project "{project}" --start {start} --end {end}'
        )
    if any("正文" in gap or "relationship" in gap or "origin/facts" in gap for gap in gaps):
        actions.append(
            f'python "scripts/pipeline/coordinator.py" --project "{project}" --start {start} --end {end}'
        )
    if not actions:
        actions.append("当前范围的人情味流程证据未发现确定性缺口；可继续跑整本终审或人工抽样复核。")
    return actions


def main() -> int:
    parser = argparse.ArgumentParser(description="扫描小说项目的人情味流程证据与缺口")
    parser.add_argument("--project", "-p", default=os.getenv("NOVEL_PROJECT_DIR", ""))
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int, default=0)
    parser.add_argument("--output", type=str, default="")
    args = parser.parse_args()

    project = resolve_project_dir(args.project)
    report = build_audit(project, start=args.start, end=args.end)
    output = Path(args.output) if args.output else report_path(project, "story_flow_audit.json")
    atomic_write_json(output, report)
    print(json.dumps({
        "project": str(project),
        "output": str(output),
        "range": report["range"],
        "overall_status": report["overall_status"],
        "gap_count": len(report["gaps"]),
        "gaps": report["gaps"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
