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

configure_stdio()


LOCK_NAMES = (
    "wechat_pusher.lock",
    "outline_gate_watchdog.lock",
    "draft_gate_watchdog.lock",
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

    for rel in ("chapters/outline", "chapters/draft", "chapters/review", "chapters/final", "logs", "reports"):
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
