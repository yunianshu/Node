#!/usr/bin/env python3
"""Portability preflight for a novel project.

This script is intentionally read-mostly. It validates that a moved project can
be found and that local dependencies are reachable without printing secrets.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from core.mmx_client import _mmx_base_cmd
from core.novel_config import configure_stdio, get_webhook_url, load_config, resolve_project_dir
from core.push_notifier import story_flow_audit_state

configure_stdio()


LOCK_NAMES = (
    "wechat_pusher.lock",
    "outline_gate_watchdog.lock",
    "draft_gate_watchdog.lock",
)

LIFE_PROFILE_KEYS = (
    "family_ties",
    "livelihood_pressure",
    "old_debts",
    "soft_spot",
    "daily_habits",
    "relationship_taboo",
)


def _process_exists(pid_text: str) -> bool:
    try:
        pid = int(str(pid_text).strip())
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return result.returncode == 0 and str(pid) in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _check_writeable(path: Path) -> tuple[bool, str]:
    path.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path, delete=False) as f:
            f.write("ok")
            temp = Path(f.name)
        temp.unlink(missing_ok=True)
        return True, "可写"
    except Exception as exc:
        return False, f"不可写: {exc}"


def _mmx_status(mmx_path: str) -> tuple[bool, str]:
    try:
        cmd = _mmx_base_cmd(mmx_path)
    except Exception as exc:
        return False, str(exc)
    exe = cmd[0]
    if exe == "node":
        node = shutil.which("node")
        if not node:
            return False, "mmx 为 JS 入口，但 node 不在 PATH"
        script = Path(cmd[1])
        if not script.exists():
            return False, f"mmx JS 入口不存在: {script}"
        return True, f"node={node}; mmx={script}"
    found = shutil.which(exe) or exe
    if not found:
        return False, f"mmx 命令不可达: {exe}"
    return True, f"mmx={found}"


def _float_config(config: dict, section: str, key: str, default: float) -> float:
    value = config.get(section, {}).get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _iter_role_dicts(value):
    if isinstance(value, dict):
        if any(key in value for key in ("name", "identity", "motivation", "description", "arc")):
            yield value
        for key, item in value.items():
            if key == "life_profile":
                continue
            yield from _iter_role_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_role_dicts(item)


def _life_profile_complete(value) -> bool:
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


def _valid_human_anchor(value) -> bool:
    text = str(value or "").strip()
    if len(text) < 25:
        return False
    placeholders = ("生活压力", "关系牵挂", "潜台词或生活物件", "本章烟火气锚点")
    return not any(text == item or text.endswith(item) for item in placeholders)


def _humanity_field_status(project: Path) -> dict:
    characters = _load_json(project / "characters.json")
    role_count = 0
    missing_life_profile = 0
    if isinstance(characters, dict):
        for role in _iter_role_dicts(characters):
            role_count += 1
            if not _life_profile_complete(role.get("life_profile")):
                missing_life_profile += 1

    outline_dir = project / "chapters" / "outline"
    outline_count = 0
    missing_human_anchor: list[int] = []
    for path in sorted(outline_dir.glob("chapter_*.json")):
        try:
            chapter = int(path.stem.rsplit("_", 1)[1])
        except (IndexError, ValueError):
            continue
        data = _load_json(path)
        if not isinstance(data, dict):
            continue
        outline_count += 1
        if not _valid_human_anchor(data.get("human_anchor")):
            missing_human_anchor.append(chapter)
    return {
        "role_count": role_count,
        "missing_life_profile": missing_life_profile,
        "outline_count": outline_count,
        "missing_human_anchor": missing_human_anchor,
    }


def _legacy_outline_artifacts(project: Path) -> list[Path]:
    """Return old aggregate outline files that current workflow must ignore."""
    candidates = [
        project / "outline.json",
        project / "chapters" / "outline" / "index.json",
    ]
    return [path for path in candidates if path.exists()]


def _story_flow_audit_warning(project: Path) -> tuple[str, bool]:
    """Summarize the latest read-only story-flow audit without generating it."""
    script = Path(__file__).with_name("story_flow_audit.py")
    command = f'python "scripts/maintenance/story_flow_audit.py" --project "{project}"'
    if not script.exists():
        return "story_flow_audit.py 缺失；无法生成流程证据审计报告", True

    state = story_flow_audit_state(project)
    if not state.get("exists"):
        return f"尚未生成 story_flow_audit.json；建议运行: {command}", False
    if not state.get("valid"):
        return f"story_flow_audit.json 不可解析；建议重新运行: {command}", False
    if state.get("stale"):
        latest = str(state.get("latest_input_path", "") or "").strip()
        detail = f"；最新输入产物: {latest}" if latest else ""
        return f"story_flow_audit.json 已早于关键输入产物{detail}。建议重新运行: {command}", False

    status = str(state.get("status", "") or "unknown").strip()
    generated_at = str(state.get("generated_at", "") or "").strip()
    if status == "ok":
        suffix = f" ({generated_at})" if generated_at else ""
        print(f"[OK] story_flow_audit.json overall_status=ok{suffix}")
        return "", False

    gaps = state.get("gaps") if isinstance(state.get("gaps"), list) else []
    gap_text = "；".join(str(item) for item in gaps[:3] if str(item).strip())
    if len(gaps) > 3:
        gap_text += f"；另有 {len(gaps) - 3} 项"
    detail = f"，主要缺口: {gap_text}" if gap_text else ""
    return f"story_flow_audit.json overall_status={status}{detail}。建议运行: {command}", False


def main() -> int:
    parser = argparse.ArgumentParser(description="检查小说项目迁移后是否可运行")
    parser.add_argument("--project", "-p", default=os.getenv("NOVEL_PROJECT_DIR", ""))
    args = parser.parse_args()

    issues: list[str] = []
    warnings: list[str] = []

    try:
        project = resolve_project_dir(args.project)
    except ValueError as exc:
        print(f"[FAIL] 项目目录解析失败: {exc}")
        return 1

    config = load_config(project)
    print(f"[OK] project={project}")

    config_file = project / "config.json"
    if config_file.exists():
        try:
            json.loads(config_file.read_text(encoding="utf-8"))
            print("[OK] config.json 可解析")
        except Exception as exc:
            issues.append(f"config.json 解析失败: {exc}")
    else:
        warnings.append("config.json 不存在，将使用默认配置")

    outline_min_score = _float_config(config, "outline_reviewer", "min_score", 8.5)
    draft_min_score = _float_config(config, "reviewer", "min_score", 8.5)
    if draft_min_score < 8.5:
        warnings.append(f"reviewer.min_score={draft_min_score:g} 低于当前推荐门槛 8.5")
    if draft_min_score < outline_min_score:
        warnings.append(
            f"reviewer.min_score={draft_min_score:g} 低于 outline_reviewer.min_score={outline_min_score:g}，正文门槛可能偏松"
        )

    for rel in (
        "chapters/outline",
        "chapters/outline_review",
        "chapters/draft",
        "chapters/review",
        "chapters/final",
        "chapters/character_states",
        "chapters/arc_states",
        "chapters/relationship_states",
        "logs",
        "reports",
    ):
        ok, detail = _check_writeable(project / rel)
        (print if ok else issues.append)(f"[OK] {rel} {detail}" if ok else f"{rel} {detail}")

    for name in ("premise.txt", "world.json", "characters.json"):
        path = project / name
        if path.exists():
            print(f"[OK] {name} 存在")
        else:
            warnings.append(f"{name} 缺失；对应阶段会自动补齐或需要先运行 Planner")

    mmx_ok, mmx_detail = _mmx_status(str(config.get("mmx_path", "")))
    if mmx_ok:
        print(f"[OK] MiniMax CLI 可定位: {mmx_detail}")
    else:
        issues.append(f"MiniMax CLI 不可用: {mmx_detail}")

    webhook = get_webhook_url(config)
    if webhook:
        print("[OK] 企业微信 webhook 已配置（已隐藏）")
    else:
        warnings.append("企业微信 webhook 未配置；推送会跳过")

    humanity = _humanity_field_status(project)
    missing_profiles = humanity["missing_life_profile"]
    missing_anchors = humanity["missing_human_anchor"]
    if humanity["role_count"] and not missing_profiles:
        print("[OK] characters.json life_profile 字段完整")
    if humanity["outline_count"] and not missing_anchors:
        print("[OK] 单章大纲 human_anchor 字段完整")
    if missing_profiles or missing_anchors:
        start_end = ""
        if missing_anchors:
            start_end = f" --start {min(missing_anchors)} --end {max(missing_anchors)}"
        warnings.append(
            "旧项目人情味字段未补齐："
            f"{missing_profiles} 个角色缺 life_profile，"
            f"{len(missing_anchors)} 章缺 human_anchor。"
            f"建议运行: python \"scripts/maintenance/backfill_humanity_fields.py\" --project \"{project}\"{start_end}"
        )

    legacy_outlines = _legacy_outline_artifacts(project)
    if legacy_outlines:
        warnings.append(
            "发现旧版聚合大纲文件，当前流程不会读取这些文件："
            + "；".join(str(path.relative_to(project)) for path in legacy_outlines)
            + "。请确认单章大纲位于 chapters/outline/chapter_XXXX.json；如旧文件内容仍有价值，需人工迁移到单章大纲后再重审。"
        )

    audit_message, audit_blocking = _story_flow_audit_warning(project)
    if audit_message:
        (issues if audit_blocking else warnings).append(audit_message)

    for lock_name in LOCK_NAMES:
        lock = project / "logs" / lock_name
        if not lock.exists():
            continue
        pid = lock.read_text(encoding="utf-8", errors="ignore").strip()
        if _process_exists(pid):
            print(f"[OK] {lock_name} 对应进程仍在运行 PID={pid}")
        else:
            warnings.append(f"{lock_name} 是过期锁；相关 lane 启动时会自动清理或可手工删除")

    if sys.version_info < (3, 12):
        warnings.append(f"Python 版本为 {sys.version.split()[0]}；建议使用 3.12")
    else:
        print(f"[OK] Python {sys.version.split()[0]}")

    for item in warnings:
        print(f"[WARN] {item}")
    for item in issues:
        print(f"[FAIL] {item}")

    if issues:
        print(f"[FAIL] 迁移自检未通过：{len(issues)} 个阻断问题")
        return 1
    print("[OK] 迁移自检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
