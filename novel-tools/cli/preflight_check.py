#!/usr/bin/env python3
"""小说工作流提交前检查。"""

from __future__ import annotations

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

import argparse
import subprocess
import json

from core.novel_config import configure_stdio
from core.workflow_state import analyze_chapter_text, load_quality_rules, report_path, scan_chapter_status
from tool_paths import script_path

configure_stdio()


ROOT = Path(__file__).resolve().parents[2]
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


def check_content_risks(project: Path, strict: bool = False, fast: bool = False, use_cache: bool = False) -> bool:
    print("\n[检查] 内容产物风险")
    config_file = project / "config.json"
    total = 0
    if config_file.exists():
        try:
            total = int(json.loads(config_file.read_text(encoding="utf-8")).get("total_chapters", 0))
        except Exception:
            total = 0
    if total <= 0:
        print("[提示] 未找到 total_chapters，跳过内容产物风险检查")
        return True

    scan_total = min(total, 200) if fast else total
    rules = load_quality_rules(project)
    statuses = scan_chapter_status(project, 1, scan_total, use_cache=use_cache)
    risks = {
        "blocker": {
            "draft": [ch for ch, status in statuses.items() if not status.draft_ok],
            "review": [ch for ch, status in statuses.items() if not status.review_ok],
            "final": [ch for ch, status in statuses.items() if not status.final_ok],
        },
        "warn": {
            "title": [],
            "paragraph": [],
        },
        "info": {
            "summary_report": [] if report_path(project, "summary_report.json").exists() else ["missing"],
        },
    }
    title_missing = []
    paragraph_sparse = []
    for chapter in range(1, scan_total + 1):
        draft_file = project / "chapters" / "draft" / f"chapter_{chapter:04d}.txt"
        if not draft_file.exists():
            continue
        text = draft_file.read_text(encoding="utf-8", errors="ignore")
        _, _, _, issues = analyze_chapter_text(text, rules=rules)
        if "missing_title" in issues:
            title_missing.append(chapter)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) < 20:
            paragraph_sparse.append(chapter)
    risks["warn"]["title"] = title_missing
    risks["warn"]["paragraph"] = paragraph_sparse

    draft_bad = risks["blocker"]["draft"]
    review_bad = risks["blocker"]["review"]
    final_bad = risks["blocker"]["final"]
    blocker_count = sum(len(items) for items in risks["blocker"].values())
    warn_count = sum(len(items) for items in risks["warn"].values())
    info_count = sum(len(items) for items in risks["info"].values())

    print(f"风险分级: blocker={blocker_count}, warn={warn_count}, info={info_count}")
    print(f"扫描范围: 1-{scan_total}{'（快速模式）' if fast else ''}")
    print(f"初稿未达标: {len(draft_bad)}/{scan_total}")
    print(f"审查未达标: {len(review_bad)}/{scan_total}")
    print(f"终稿未达标: {len(final_bad)}/{scan_total}")
    print(f"初稿缺标题: {len(title_missing)}/{scan_total}")
    print(f"初稿段落偏少: {len(paragraph_sparse)}/{scan_total}")
    for name, items in (("draft", draft_bad), ("review", review_bad), ("final", final_bad)):
        if items:
            preview = ", ".join(str(ch) for ch in items[:10])
            print(f"  {name} 前10个问题章节: {preview}")
    for name, items in (("title", title_missing), ("paragraph", paragraph_sparse)):
        if items:
            preview = ", ".join(str(ch) for ch in items[:10])
            print(f"  {name} 前10个风险章节: {preview}")
    for name, items in risks["info"].items():
        if items:
            print(f"  info/{name}: {', '.join(items)}")

    if strict and (draft_bad or review_bad or final_bad):
        print("[失败] 严格内容检查未通过")
        return False
    print("[通过] 内容风险已报告")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="小说工作流提交前检查")
    parser.add_argument(
        "--project",
        "-p",
        type=Path,
        required=True,
        help="小说项目目录",
    )
    parser.add_argument("--strict-content", action="store_true", help="内容产物不完整时返回失败")
    parser.add_argument("--fast", action="store_true", help="快速模式，只扫描前200章内容风险")
    parser.add_argument("--cache", action="store_true", help="内容风险扫描启用状态缓存")
    args = parser.parse_args()
    project = args.project.resolve()
    repair_dry_run_cmd = [
        sys.executable,
        str(script_path("novel_workflow.py")),
        "--project",
        str(project),
        "repair-all",
        "--limit",
        "2",
        "--dry-run",
    ]
    if args.fast:
        repair_dry_run_cmd = [
            sys.executable,
            str(script_path("novel_workflow.py")),
            "--project",
            str(project),
            "repair",
            "--mode",
            "draft",
            "--start",
            "1",
            "--end",
            "200",
            "--limit",
            "2",
            "--dry-run",
        ]

    checks = [
        run_step(
            "单元测试",
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "novel-tools/tests",
            ],
        ),
        check_python_syntax([ROOT / "novel-tools"]),
        scan_secrets([ROOT / "novel-tools", project, ROOT / ".gitignore"]),
        run_step(
            "质量修复 dry-run",
            repair_dry_run_cmd,
        ),
        check_content_risks(project, strict=args.strict_content, fast=args.fast, use_cache=args.cache),
        show_git_status(),
    ]

    if all(checks):
        print("\n[通过] preflight 检查完成")
        return 0
    print("\n[失败] preflight 检查未通过")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
