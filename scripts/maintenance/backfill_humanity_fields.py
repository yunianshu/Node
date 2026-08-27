#!/usr/bin/env python3
"""Backfill humanity-oriented fields for existing novel projects.

The script is deterministic and conservative: by default it only reports what
would change. Use --apply to write characters.json and chapter outline files.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from core.novel_config import configure_stdio, load_config, resolve_project_dir
from core.workflow_state import atomic_write_json

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
    return json.loads(path.read_text(encoding="utf-8"))


def _first_text(*values: Any, default: str = "") -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return default


def _safe_name(role: dict) -> str:
    return _first_text(role.get("name"), role.get("identity"), default="这个人")


def _default_life_profile(role: dict, protagonist_name: str = "") -> dict:
    name = _safe_name(role)
    identity = _first_text(role.get("identity"), role.get("role"), role.get("tier"), default="当前身份")
    motivation = _first_text(role.get("motivation"), role.get("description"), role.get("arc"), default="想保住自己在乎的人和事")
    relation = _first_text(role.get("relationship_with_protagonist"), default=f"与{protagonist_name or '主角'}存在未说透的牵连")
    return {
        "family_ties": f"{name}仍被一段亲近关系牵动：{relation}，这段关系会影响其关键选择。",
        "livelihood_pressure": f"{name}的现实压力来自{identity}带来的生计、体面或身份成本，不能只靠口号解决。",
        "old_debts": f"{name}心里有一笔旧账：围绕“{motivation[:40]}”形成的人情、亏欠或旧恩怨。",
        "soft_spot": f"{name}最容易被触动的是某个具体的人、旧物或日常场景，写作时要落到动作和物件。",
        "daily_habits": ["说话前会先停一下", "遇到压力时会下意识整理随身小物"],
        "relationship_taboo": f"{name}不愿承认自己其实害怕辜负某个人，常用沉默或硬话遮掩。",
    }


def _profile_complete(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for key in LIFE_PROFILE_KEYS:
        if key == "daily_habits":
            habits = value.get(key)
            if not isinstance(habits, list) or not any(str(item).strip() for item in habits):
                return False
            continue
        if not str(value.get(key, "")).strip():
            return False
    return True


def _iter_role_dicts(value: Any, path: str = ""):
    if isinstance(value, dict):
        if any(key in value for key in ("name", "identity", "motivation", "description", "arc")):
            yield path or "root", value
        for key, item in value.items():
            if key == "life_profile":
                continue
            child_path = f"{path}.{key}" if path else str(key)
            yield from _iter_role_dicts(item, child_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_role_dicts(item, f"{path}[{index}]")


def backfill_characters(project: Path, *, apply: bool, backup: bool) -> dict:
    path = project / "characters.json"
    data = load_json(path)
    if not isinstance(data, dict):
        return {"file": str(path), "status": "missing_or_invalid", "updated": 0}
    protagonist = data.get("protagonist") if isinstance(data.get("protagonist"), dict) else {}
    protagonist_name = str(protagonist.get("name", "")).strip()
    updated = []
    for role_path, role in _iter_role_dicts(data):
        if _profile_complete(role.get("life_profile")):
            continue
        existing = role.get("life_profile") if isinstance(role.get("life_profile"), dict) else {}
        default = _default_life_profile(role, protagonist_name)
        merged = {**default, **{key: value for key, value in existing.items() if value}}
        if not isinstance(merged.get("daily_habits"), list) or not merged["daily_habits"]:
            merged["daily_habits"] = default["daily_habits"]
        role["life_profile"] = merged
        updated.append({"path": role_path, "name": _safe_name(role)})
    if apply and updated:
        if backup:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        atomic_write_json(path, data)
    return {"file": str(path), "status": "ok", "updated": len(updated), "roles": updated[:30]}


def _clean_list(value: Any, limit: int = 3) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = str(item).strip()
        if text:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _build_human_anchor(outline: dict) -> str:
    title = _first_text(outline.get("title"), default="本章")
    location = _first_text(outline.get("location"), default="现场")
    mood = _first_text(outline.get("mood"), default="压抑")
    summary = _first_text(outline.get("summary"), outline.get("chapter_goal"), default="本章事件推进")
    characters = _clean_list(outline.get("characters_involved"), limit=3)
    people = "、".join(characters) if characters else "主角和身边人"
    events = _clean_list(outline.get("key_events"), limit=2)
    event_text = "；".join(events) if events else summary[:80]
    return (
        f"{title}的人情味锚点：在{location}的{mood}气氛里，{people}要面对一件现实压力"
        f"（{summary[:60]}），并通过{event_text[:90]}显出亏欠、牵挂或不愿说出口的真话；"
        "正文需落到一个可触摸生活物件或一句带潜台词的对白。"
    )


def _valid_human_anchor(value: Any) -> bool:
    text = str(value or "").strip()
    if len(text) < 25:
        return False
    placeholders = ("生活压力", "关系牵挂", "潜台词或生活物件", "本章烟火气锚点")
    return not any(text == item or text.endswith(item) for item in placeholders)


def _outline_review_file(project: Path, chapter: int) -> Path:
    return project / "chapters" / "outline_review" / f"chapter_{chapter:04d}_review.json"


def backfill_outlines(
    project: Path,
    *,
    start: int,
    end: int,
    apply: bool,
    backup: bool,
    invalidate_reviews: bool,
) -> dict:
    outline_dir = project / "chapters" / "outline"
    files = sorted(outline_dir.glob("chapter_*.json"))
    updated: list[dict] = []
    invalidated_reviews: list[dict] = []
    scanned = 0
    for path in files:
        try:
            chapter = int(path.stem.rsplit("_", 1)[1])
        except (IndexError, ValueError):
            continue
        if start and chapter < start:
            continue
        if end and chapter > end:
            continue
        data = load_json(path)
        if not isinstance(data, dict):
            continue
        scanned += 1
        if _valid_human_anchor(data.get("human_anchor")):
            continue
        data["human_anchor"] = _build_human_anchor(data)
        updated.append({"chapter": chapter, "file": str(path)})
        if apply:
            if backup:
                shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
            atomic_write_json(path, data)
            review_file = _outline_review_file(project, chapter)
            if invalidate_reviews and review_file.exists():
                if backup:
                    shutil.copy2(review_file, review_file.with_suffix(review_file.suffix + ".bak"))
                review_file.unlink()
                invalidated_reviews.append({"chapter": chapter, "file": str(review_file)})
        elif invalidate_reviews:
            review_file = _outline_review_file(project, chapter)
            if review_file.exists():
                invalidated_reviews.append({"chapter": chapter, "file": str(review_file), "would_delete": True})
    return {
        "outline_dir": str(outline_dir),
        "scanned": scanned,
        "updated": len(updated),
        "chapters": updated[:80],
        "updated_chapters": [item["chapter"] for item in updated],
        "invalidated_outline_reviews": invalidated_reviews[:80],
    }


def _changed_outline_range(outlines_result: dict) -> tuple[int, int] | None:
    chapters = [
        int(chapter)
        for chapter in outlines_result.get("updated_chapters", [])
        if isinstance(chapter, int)
    ]
    if not chapters:
        return None
    return min(chapters), max(chapters)


def _suggested_commands(project: Path, outlines_result: dict) -> list[str]:
    chapter_range = _changed_outline_range(outlines_result)
    if chapter_range is None:
        return []
    start, end = chapter_range
    project_arg = str(project)
    return [
        f'python "scripts/pipeline/coordinator.py" --project "{project_arg}" --start {start} --end {end}',
        f'python "scripts/pipeline/outline_reviewer.py" --project "{project_arg}" --start {start} --end {end}',
    ]


def _artifact_presence(project: Path, chapters: list[int]) -> dict:
    specs = {
        "outline_review": ("outline_review", "_review.json"),
        "draft": ("draft", ".txt"),
        "review": ("review", "_review.json"),
        "final": ("final", ".txt"),
    }
    presence: dict[str, dict] = {}
    for key, (folder, suffix) in specs.items():
        found = []
        for chapter in chapters:
            path = project / "chapters" / folder / f"chapter_{chapter:04d}{suffix}"
            if path.exists():
                found.append(chapter)
        presence[key] = {"count": len(found), "chapters": found[:30]}
    return presence


def _recommend_action(config: dict, presence: dict) -> tuple[str, str]:
    text_artifacts = sum(presence[key]["count"] for key in ("draft", "review", "final"))
    backfill_cfg = config.get("backfill") if isinstance(config.get("backfill"), dict) else {}
    outline_first = bool(config.get("outline_first"))

    if text_artifacts:
        return (
            "rerun_quality_gate",
            "目标章节已有 draft/review/final，单独重审大纲无法保证正文兑现新的 human_anchor。",
        )
    if backfill_cfg.get("prefer_rerun_quality_gate_without_text_artifacts", False):
        return (
            "rerun_quality_gate",
            "目标章节暂无 draft/review/final，完整质量门不会覆盖既有正文，可直接让大纲与正文按新 human_anchor 收敛。",
        )
    if outline_first:
        return (
            "review_outline_only",
            "项目启用了 outline-first 且目标章节暂无正文产物，先重审大纲能以较低风险验证 human_anchor。",
        )
    return (
        "review_outline_only",
        "目标章节暂无正文产物，先重审大纲可验证补齐字段；需要生成正文时再运行完整质量门。",
    )


def _suggested_actions(project: Path, outlines_result: dict, config: dict) -> list[dict]:
    commands = _suggested_commands(project, outlines_result)
    if not commands:
        return []
    chapters = [
        int(chapter)
        for chapter in outlines_result.get("updated_chapters", [])
        if isinstance(chapter, int)
    ]
    presence = _artifact_presence(project, chapters)
    recommended_id, recommendation_reason = _recommend_action(config, presence)
    def mark(action: dict) -> dict:
        action["recommended"] = action["id"] == recommended_id
        if action["recommended"]:
            action["recommendation_reason"] = recommendation_reason
        return action
    return [
        mark({
            "id": "rerun_quality_gate",
            "label": "重跑完整章节质量门",
            "command": commands[0],
            "when_to_use": "迁移后希望让大纲、正文、审查和终稿按新 human_anchor 全链路重新收敛时使用。",
            "risk": "会触发目标章节大纲门和正文质量门；若已有 draft/review/final 被判过期或被清理，可能产生正文重写。",
            "affected_artifacts": [
                "chapters/outline/chapter_XXXX.json",
                "chapters/outline_review/chapter_XXXX_review.json",
                "chapters/draft/chapter_XXXX.txt",
                "chapters/review/chapter_XXXX_review.json",
                "chapters/final/chapter_XXXX.txt",
                "chapters/character_states/chapter_XXXX.json",
                "chapters/arc_states/chapter_XXXX.json",
                "chapters/relationship_states/chapter_XXXX.json",
                "reports/progress.json",
            ],
            "expected_outputs": [
                "目标章节大纲重新通过 human_warmth 设计门",
                "目标章节正文按新的 human_anchor 重新收敛",
                "通过后刷新 final 与章节状态",
            ],
            "observed_artifacts": presence,
        }),
        mark({
            "id": "review_outline_only",
            "label": "只重审单章大纲",
            "command": commands[1],
            "when_to_use": "只想验证补齐后的 human_anchor 是否满足 Outline Reviewer 设计门时使用。",
            "risk": "只更新 outline_review，不会重写正文；若正文已经存在，仍可能没有兑现新的 human_anchor。",
            "affected_artifacts": [
                "chapters/outline_review/chapter_XXXX_review.json",
                "reports/progress.json",
            ],
            "expected_outputs": [
                "目标章节大纲重新接受 Outline Reviewer 检查",
                "确认 human_anchor 是否满足 human_warmth 设计门",
            ],
            "observed_artifacts": presence,
        }),
    ]


def _recommended_action(actions: list[dict]) -> dict | None:
    for action in actions:
        if isinstance(action, dict) and action.get("recommended"):
            return action
    return None


def _action_argv(project: Path, outlines_result: dict, action_id: str) -> list[str] | None:
    chapter_range = _changed_outline_range(outlines_result)
    if chapter_range is None:
        return None
    start, end = chapter_range
    if action_id == "rerun_quality_gate":
        script = TOOLS_ROOT / "pipeline" / "coordinator.py"
    elif action_id == "review_outline_only":
        script = TOOLS_ROOT / "pipeline" / "outline_reviewer.py"
    else:
        return None
    return [
        sys.executable,
        str(script),
        "--project",
        str(project),
        "--start",
        str(start),
        "--end",
        str(end),
    ]


def _run_recommended_action(
    project: Path,
    outlines_result: dict,
    actions: list[dict],
    *,
    max_run_chapters: int,
    allowed_action_ids: set[str],
) -> dict:
    action = _recommended_action(actions)
    if not action:
        return {"status": "skipped", "reason": "no_recommended_action"}
    action_id = str(action.get("id", "")).strip()
    if allowed_action_ids and action_id not in allowed_action_ids:
        return {
            "status": "blocked",
            "reason": "recommended_action_not_allowed",
            "action_id": action_id,
            "allowed_action_ids": sorted(allowed_action_ids),
            "suggestion": f"如确认要执行该动作，请追加 --allow-run-action {action_id}",
        }
    chapter_range = _changed_outline_range(outlines_result)
    if chapter_range is None:
        return {"status": "skipped", "reason": "no_changed_outline_range", "action_id": action_id}
    start, end = chapter_range
    chapter_count = end - start + 1
    if max_run_chapters > 0 and chapter_count > max_run_chapters:
        return {
            "status": "blocked",
            "reason": "recommended_action_range_too_large",
            "action_id": action_id,
            "start": start,
            "end": end,
            "chapter_count": chapter_count,
            "max_run_chapters": max_run_chapters,
            "suggestion": "缩小 --start/--end 范围，或显式提高 --max-run-chapters 后再执行。",
        }
    argv = _action_argv(project, outlines_result, action_id)
    if not argv:
        return {"status": "skipped", "reason": "no_changed_outline_range", "action_id": action_id}
    completed = subprocess.run(argv, cwd=str(TOOLS_ROOT.parent))
    return {
        "status": "completed" if completed.returncode == 0 else "failed",
        "action_id": action_id,
        "command_argv": argv,
        "returncode": completed.returncode,
    }


def _has_text_artifacts(presence: dict | None) -> bool:
    if not isinstance(presence, dict):
        return True
    return any(int((presence.get(key) or {}).get("count", 0) or 0) > 0 for key in ("draft", "review", "final"))


def _allowed_run_actions(config: dict, cli_actions: list[str], presence: dict | None = None) -> set[str]:
    allowed = {"review_outline_only"}
    backfill_cfg = config.get("backfill")
    if isinstance(backfill_cfg, dict):
        configured = backfill_cfg.get("run_allowed_actions")
        if isinstance(configured, str):
            configured = [configured]
        if isinstance(configured, list):
            for item in configured:
                action_id = str(item).strip()
                if not action_id:
                    continue
                if (
                    action_id == "rerun_quality_gate"
                    and _has_text_artifacts(presence)
                    and not backfill_cfg.get("allow_rerun_quality_gate_with_text_artifacts", False)
                ):
                    continue
                allowed.add(action_id)
        if (
            backfill_cfg.get("allow_rerun_quality_gate_without_text_artifacts", False)
            and not _has_text_artifacts(presence)
        ):
            allowed.add("rerun_quality_gate")
    allowed.update(str(item).strip() for item in cli_actions if str(item).strip())
    return allowed


def main() -> int:
    parser = argparse.ArgumentParser(description="为旧项目补齐 life_profile 与 human_anchor")
    parser.add_argument("--project", "-p", default=os.getenv("NOVEL_PROJECT_DIR", ""))
    parser.add_argument("--start", type=int, default=0, help="只处理起始章节（默认全部）")
    parser.add_argument("--end", type=int, default=0, help="只处理结束章节（默认全部）")
    parser.add_argument("--apply", action="store_true", help="实际写入文件；默认只预览")
    parser.add_argument("--backup", action="store_true", help="写入前为变更文件生成 .bak")
    parser.add_argument(
        "--invalidate-outline-reviews",
        action="store_true",
        help="补 human_anchor 后删除对应 outline_review，强制后续重新过审；默认仅在 dry-run 输出 would_delete",
    )
    parser.add_argument(
        "--run-recommended-action",
        action="store_true",
        help="显式确认执行 suggested_actions 中 recommended=true 的动作；必须与 --apply 同时使用。",
    )
    parser.add_argument(
        "--max-run-chapters",
        type=int,
        default=50,
        help="--run-recommended-action 允许自动执行的最大章节数；设为0表示不限制。",
    )
    parser.add_argument(
        "--allow-run-action",
        action="append",
        default=[],
        help=(
            "允许 --run-recommended-action 自动执行的 action id；可重复传入。"
            "默认只允许 review_outline_only，也可在 config.json 的 backfill.run_allowed_actions 中配置；"
            "配置级 rerun_quality_gate 会在目标章已有 draft/review/final 时被拦截，CLI 显式传入可覆盖。"
        ),
    )
    args = parser.parse_args()

    project = resolve_project_dir(args.project)
    config = load_config(project)
    outlines_result = backfill_outlines(
        project,
        start=args.start,
        end=args.end,
        apply=args.apply,
        backup=args.backup,
        invalidate_reviews=args.invalidate_outline_reviews,
    )
    suggested_actions = _suggested_actions(project, outlines_result, config)
    recommended = _recommended_action(suggested_actions)
    action_presence = recommended.get("observed_artifacts") if isinstance(recommended, dict) else None
    allowed_run_actions = _allowed_run_actions(config, args.allow_run_action, action_presence)
    backfill_cfg = config.get("backfill") if isinstance(config.get("backfill"), dict) else {}
    result = {
        "project": str(project),
        "mode": "apply" if args.apply else "dry_run",
        "characters": backfill_characters(project, apply=args.apply, backup=args.backup),
        "outlines": outlines_result,
        "suggested_commands": _suggested_commands(project, outlines_result),
        "recommendation_context": {
            "outline_first": bool(config.get("outline_first")),
            "allowed_run_actions": sorted(allowed_run_actions),
            "conditional_rerun_quality_gate_allowed": (
                "rerun_quality_gate" in allowed_run_actions
                and bool(backfill_cfg.get("allow_rerun_quality_gate_without_text_artifacts", False))
                and not _has_text_artifacts(action_presence)
            ),
        },
        "suggested_actions": suggested_actions,
    }
    exit_code = 0
    if args.run_recommended_action:
        if not args.apply:
            result["executed_action"] = {
                "status": "blocked",
                "reason": "run_recommended_action_requires_apply",
            }
            exit_code = 2
        else:
            executed = _run_recommended_action(
                project,
                outlines_result,
                suggested_actions,
                max_run_chapters=args.max_run_chapters,
                allowed_action_ids=allowed_run_actions,
            )
            result["executed_action"] = executed
            if executed.get("status") == "failed":
                exit_code = int(executed.get("returncode") or 0)
            elif executed.get("status") == "blocked":
                exit_code = 3
            else:
                exit_code = 0
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not args.apply:
        print("Dry run only. Re-run with --apply to write changes.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
