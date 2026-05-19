#!/usr/bin/env python3
"""小说工作流提交前检查。"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECT = ROOT / "projects" / "novels6"
SECRET_PATTERNS = (
    "qya" + "pi.weixin" + ".qq.com/cgi-bin/webhook/send?key=",
    "key=22" + "ea4574-b1f0-4c36-af2d-b13c6c2d471b",
)


def run_step(name: str, cmd: list[str], required: bool = True) -> bool:
    print(f"\n[检查] {name}")
    print("$ " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT, check=False, text=True, encoding="utf-8")
    if result.returncode == 0:
        print(f"[通过] {name}")
        return True
    level = "失败" if required else "提示"
    print(f"[{level}] {name}，退出码: {result.returncode}")
    return not required


def scan_secrets(paths: list[Path]) -> bool:
    print("\n[检查] 敏感 Webhook 扫描")
    hits: list[str] = []
    suffixes = {".py", ".json", ".md", ".txt", ".gitignore"}
    for base in paths:
        if not base.exists():
            continue
        files = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
        for file_path in files:
            if file_path.suffix not in suffixes and file_path.name != ".gitignore":
                continue
            try:
                text = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for pattern in SECRET_PATTERNS:
                if pattern in text:
                    hits.append(str(file_path.relative_to(ROOT)))
                    break
    if hits:
        print("[失败] 发现硬编码 Webhook:")
        for hit in hits:
            print(f"  {hit}")
        return False
    print("[通过] 未发现硬编码 Webhook")
    return True


def show_git_status() -> bool:
    print("\n[检查] Git 工作区状态")
    result = subprocess.run(
        ["git", "status", "--short"],
        cwd=ROOT,
        check=False,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    if result.stdout.strip():
        print(result.stdout.rstrip())
    else:
        print("工作区干净")
    return result.returncode == 0


def check_python_syntax(paths: list[Path]) -> bool:
    print("\n[检查] 核心脚本语法")
    failures: list[str] = []
    for base in paths:
        for file_path in base.rglob("*.py"):
            try:
                source = file_path.read_text(encoding="utf-8")
                compile(source, str(file_path), "exec")
            except Exception as exc:
                failures.append(f"{file_path.relative_to(ROOT)}: {exc}")
    if failures:
        print("[失败] Python 语法检查失败:")
        for item in failures:
            print(f"  {item}")
        return False
    print("[通过] Python 语法检查通过")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="小说工作流提交前检查")
    parser.add_argument(
        "--project",
        "-p",
        type=Path,
        default=DEFAULT_PROJECT,
        help="小说项目目录，默认检查 projects/novels6",
    )
    args = parser.parse_args()
    project = args.project.resolve()

    checks = [
        run_step(
            "单元测试",
            [
                sys.executable,
                "-m",
                "unittest",
                "projects/novels6/tests/test_config_and_mmx.py",
                "projects/novels6/tests/test_workflow_state.py",
                "projects/novels6/tests/test_repair_quality.py",
            ],
        ),
        check_python_syntax([ROOT / "novel-tools", ROOT / "projects" / "novels6" / "scripts"]),
        scan_secrets([ROOT / "novel-tools", project, ROOT / ".gitignore"]),
        run_step(
            "质量修复 dry-run",
            [
                sys.executable,
                str(ROOT / "novel-tools" / "repair_quality.py"),
                "--project",
                str(project),
                "--mode",
                "final",
                "--limit",
                "2",
                "--dry-run",
            ],
        ),
        show_git_status(),
    ]

    if all(checks):
        print("\n[通过] preflight 检查完成")
        return 0
    print("\n[失败] preflight 检查未通过")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
