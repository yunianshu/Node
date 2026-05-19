#!/usr/bin/env python3
"""小说生成工作流统一入口。

用法示例:
    python novel_workflow.py status --project D:/AiProject/Node/projects/novels6
    python novel_workflow.py generate --project D:/AiProject/Node/projects/novels6 --start 1 --end 100
    python novel_workflow.py review --project D:/AiProject/Node/projects/novels6
    python novel_workflow.py repair --project D:/AiProject/Node/projects/novels6 --mode draft --limit 50
    python novel_workflow.py notify --project D:/AiProject/Node/projects/novels6
    python novel_workflow.py resume --project D:/AiProject/Node/projects/novels6

也可通过环境变量设置默认项目:
    set NOVEL_PROJECT_DIR=D:/AiProject/Node/projects/novels6
    python novel_workflow.py status
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from novel_config import configure_stdio, load_config
from workflow_state import scan_chapter_status

configure_stdio()

SCRIPTS_DIR = Path(__file__).parent


def run(script: str, project: str, args: list[str]) -> int:
    cmd = [sys.executable, str(SCRIPTS_DIR / script), "--project", project, *args]
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, check=False, text=True, encoding="utf-8").returncode


def cmd_status(args):
    project = Path(args.project).resolve()
    config = load_config(project)
    total = config["total_chapters"]
    start, end = normalize_range(args, total)
    scan_total = end - start + 1
    statuses = scan_chapter_status(project, start, end)

    draft_ok = sum(1 for s in statuses.values() if s.draft_ok)
    review_ok = sum(1 for s in statuses.values() if s.review_ok)
    final_ok = sum(1 for s in statuses.values() if s.final_ok)
    total_words = sum(s.final_words for s in statuses.values() if s.final_ok)
    scores = [s.review_score for s in statuses.values() if s.review_score is not None]
    avg_score = sum(scores) / len(scores) if scores else 0

    hard_fail = sum(1 for s in statuses.values() if s.draft_grade == "hard_fail")
    warn = sum(1 for s in statuses.values() if s.draft_grade == "warn")

    print("=" * 50)
    print(f"项目: {project}")
    print("=" * 50)
    print(f"总章节数  : {total}")
    print(f"扫描范围  : {start}-{end}")
    print(f"初稿合格  : {draft_ok}/{scan_total}  (hard_fail={hard_fail}, warn={warn})")
    print(f"审查合格  : {review_ok}/{scan_total}")
    print(f"终稿合格  : {final_ok}/{scan_total}")
    print(f"总字数    : {total_words:,}")
    print(f"平均评分  : {avg_score:.2f}")
    print("=" * 50)

    step, _ = next_required_step(project, total)
    if step == "done":
        print("全部完成!")
    elif step == "draft":
        print("下一步: novel_workflow.py repair --mode draft")
    elif step == "review":
        print("下一步: novel_workflow.py review")
    else:
        print("下一步: novel_workflow.py repair --mode final")

    return 0


def cmd_generate(args):
    extra = []
    if args.start:
        extra += ["--start", str(args.start)]
    if args.end:
        extra += ["--end", str(args.end)]
    if args.skip_planner:
        extra.append("--skip-planner")
    if args.skip_review:
        extra.append("--skip-review")
    if args.batch_size:
        extra += ["--batch-size", str(args.batch_size)]
    return run("coordinator.py", args.project, extra)


def cmd_review(args):
    return run("fill_reviews_parallel.py", args.project, [])


def cmd_repair(args):
    extra = ["--mode", args.mode]
    if args.limit:
        extra += ["--limit", str(args.limit)]
    if args.dry_run:
        extra.append("--dry-run")
    if args.ignore_state:
        extra.append("--ignore-state")
    return run("repair_quality.py", args.project, extra)


def normalize_range(args, total: int) -> tuple[int, int]:
    start = max(1, int(getattr(args, "start", 0) or 1))
    end = int(getattr(args, "end", 0) or total)
    end = min(total, max(start, end))
    return start, end


def _quality_counts(project: Path, total: int, start: int = 1, end: int | None = None):
    end = total if end is None else end
    statuses = scan_chapter_status(project, start, end)
    return statuses, {
        "draft": sum(1 for s in statuses.values() if s.draft_ok),
        "review": sum(1 for s in statuses.values() if s.review_ok),
        "final": sum(1 for s in statuses.values() if s.final_ok),
    }


def next_required_step(project: Path, total: int) -> tuple[str, dict]:
    _, counts = _quality_counts(project, total)
    if counts["draft"] < total:
        return "draft", counts
    if counts["review"] < total:
        return "review", counts
    if counts["final"] < total:
        return "final", counts
    return "done", counts


def cmd_repair_all(args):
    project = Path(args.project).resolve()
    config = load_config(project)
    total = config["total_chapters"]
    rounds = 0
    while True:
        if args.max_rounds > 0 and rounds >= args.max_rounds:
            print(f"已达到最大轮次 {args.max_rounds}，停止 repair-all")
            return 0
        step, counts = next_required_step(project, total)
        print(f"当前质量门: draft={counts['draft']} review={counts['review']} final={counts['final']} / {total}")
        if step == "done":
            print("全部质量门已通过")
            return 0
        extra = ["--mode", step]
        if args.limit:
            extra += ["--limit", str(args.limit)]
        if args.dry_run:
            extra.append("--dry-run")
        rc = run("repair_quality.py", args.project, extra)
        rounds += 1
        if rc != 0 or args.dry_run:
            return rc


def cmd_notify(args):
    return run("wechat_notify.py", args.project, [])


def cmd_resume(args):
    project = Path(args.project).resolve()
    config = load_config(project)
    total = config["total_chapters"]

    progress_file = project / "progress.json"
    repair_file = project / "repair_state.json"

    statuses = scan_chapter_status(project, 1, total)
    draft_ok = sum(1 for s in statuses.values() if s.draft_ok)
    review_ok = sum(1 for s in statuses.values() if s.review_ok)
    final_ok = sum(1 for s in statuses.values() if s.final_ok)

    print("=" * 50)
    print(f"续跑诊断 - {project}")
    print("=" * 50)

    if progress_file.exists():
        try:
            progress = json.loads(progress_file.read_text(encoding="utf-8"))
            last_gen = progress.get("last_generated_chapter", 0)
            last_rev = progress.get("last_reviewed_chapter", 0)
            rewrite_queue = progress.get("rewrite_queue", [])
            print(f"progress.json:")
            print(f"  last_generated_chapter  : {last_gen}")
            print(f"  last_reviewed_chapter   : {last_rev}")
            print(f"  rewrite_queue 长度      : {len(rewrite_queue)}")
        except Exception as e:
            print(f"progress.json 读取失败: {e}")
    else:
        print("progress.json 不存在")

    if repair_file.exists():
        try:
            state = json.loads(repair_file.read_text(encoding="utf-8"))
            chapters = state.get("chapters", {})
            success = sum(1 for v in chapters.values() if v.get("status") == "success")
            failed = sum(1 for v in chapters.values() if v.get("status") == "failed")
            print(f"repair_state.json:")
            print(f"  记录章节数 : {len(chapters)}")
            print(f"  成功       : {success}")
            print(f"  失败待重试 : {failed}")
        except Exception as e:
            print(f"repair_state.json 读取失败: {e}")
    else:
        print("repair_state.json 不存在")

    print("-" * 50)
    print(f"当前质量门: draft={draft_ok} review={review_ok} final={final_ok} / {total}")

    step, _ = next_required_step(project, total)
    if step == "draft":
        print("下一步: 先修复初稿质量门")
        print(f"  novel_workflow.py repair --project {args.project} --mode draft")
    elif step == "review":
        print("下一步: 初稿已达标，补齐审查")
        print(f"  novel_workflow.py review --project {args.project}")
    elif step == "final":
        print("下一步: 审查已达标，修复终稿")
        print(f"  novel_workflow.py repair --project {args.project} --mode final")
    else:
        print("全部完成，无需续跑。")

    return 0


def build_summary(project: Path, start: int = 1, end: int | None = None) -> dict:
    config = load_config(project)
    total = config["total_chapters"]
    end = total if end is None else end
    statuses, counts = _quality_counts(project, total, start, end)
    scan_total = end - start + 1
    issue_top = {
        "draft": [ch for ch, status in statuses.items() if not status.draft_ok][:20],
        "review": [ch for ch, status in statuses.items() if not status.review_ok][:20],
        "final": [ch for ch, status in statuses.items() if not status.final_ok][:20],
    }
    scores = [s.review_score for s in statuses.values() if s.review_score is not None]
    return {
        "project": str(project),
        "total": total,
        "scan_range": {"start": start, "end": end, "total": scan_total},
        "counts": counts,
        "avg_score": round(sum(scores) / len(scores), 2) if scores else 0,
        "next_step": next_required_step_for_counts(counts, scan_total),
        "issue_top": issue_top,
    }


def next_required_step_for_counts(counts: dict, total: int) -> str:
    if counts["draft"] < total:
        return "draft"
    if counts["review"] < total:
        return "review"
    if counts["final"] < total:
        return "final"
    return "done"


def cmd_plan(args):
    project = Path(args.project).resolve()
    config = load_config(project)
    start, end = normalize_range(args, config["total_chapters"])
    summary = build_summary(project, start, end)
    print("=" * 50)
    print(f"执行计划 - {project}")
    print("=" * 50)
    print(f"扫描范围: {start}-{end}")
    print(f"质量门: draft={summary['counts']['draft']} review={summary['counts']['review']} final={summary['counts']['final']} / {summary['scan_range']['total']}")
    step = summary["next_step"]
    if step == "done":
        print("无需执行，全部质量门已通过")
        return 0
    command = {
        "draft": f"novel_workflow.py repair --project {args.project} --mode draft",
        "review": f"novel_workflow.py review --project {args.project}",
        "final": f"novel_workflow.py repair --project {args.project} --mode final",
    }[step]
    print(f"下一阶段: {step}")
    print(f"建议命令: {command}")
    print(f"问题章节预览: {summary['issue_top'][step]}")
    return 0


def cmd_report(args):
    project = Path(args.project).resolve()
    config = load_config(project)
    start, end = normalize_range(args, config["total_chapters"])
    summary = build_summary(project, start, end)
    output = project / "summary_report.json"
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入报告: {output}")
    print(f"下一阶段: {summary['next_step']}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="小说生成工作流统一入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
子命令说明:
  status    扫描并展示当前章节状态
  generate  调用 coordinator 生成初稿/终稿
  review    调用并行 reviewer 补全审查
  repair    按质量门修复历史产物
  notify    推送当前进度到企业微信
  resume    读取进度文件，给出续跑建议
  plan      输出下一步执行计划
  report    写入 summary_report.json

项目目录可通过 --project 指定，或设置 NOVEL_PROJECT_DIR 环境变量。
        """,
    )
    parser.add_argument("--project", "-p", type=str,
                        default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录（默认从环境变量 NOVEL_PROJECT_DIR 读取）")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="扫描并展示当前章节状态")
    status.add_argument("--start", type=int, default=0, help="起始章节")
    status.add_argument("--end", type=int, default=0, help="结束章节")

    gen = sub.add_parser("generate", help="调用 coordinator 生成初稿/终稿")
    gen.add_argument("--start", type=int, default=0, help="起始章节")
    gen.add_argument("--end", type=int, default=0, help="结束章节")
    gen.add_argument("--skip-planner", action="store_true", help="跳过 Planner")
    gen.add_argument("--skip-review", action="store_true", help="跳过 Review")
    gen.add_argument("--batch-size", type=int, default=0, help="批次大小")

    sub.add_parser("review", help="调用并行 reviewer 补全审查")

    rep = sub.add_parser("repair", help="按质量门修复历史产物")
    rep.add_argument("--mode", choices=["draft", "review", "final"], default="final")
    rep.add_argument("--limit", type=int, default=0, help="最多处理多少章")
    rep.add_argument("--dry-run", action="store_true", help="只列命令不执行")
    rep.add_argument("--ignore-state", action="store_true", help="忽略断点状态")

    rep_all = sub.add_parser("repair-all", help="按 draft -> review -> final 顺序修复")
    rep_all.add_argument("--limit", type=int, default=0, help="每轮最多处理多少章")
    rep_all.add_argument("--dry-run", action="store_true", help="只执行下一步计划")
    rep_all.add_argument("--max-rounds", type=int, default=1, help="最多执行多少轮，0表示不限")

    sub.add_parser("notify", help="推送当前进度到企业微信")
    sub.add_parser("resume", help="读取进度文件，给出续跑建议")
    plan = sub.add_parser("plan", help="输出下一步执行计划")
    plan.add_argument("--start", type=int, default=0, help="起始章节")
    plan.add_argument("--end", type=int, default=0, help="结束章节")
    report = sub.add_parser("report", help="写入 summary_report.json")
    report.add_argument("--start", type=int, default=0, help="起始章节")
    report.add_argument("--end", type=int, default=0, help="结束章节")

    args = parser.parse_args()

    if not args.project:
        print("错误: 必须指定 --project 或设置 NOVEL_PROJECT_DIR 环境变量")
        sys.exit(1)

    handlers = {
        "status": cmd_status,
        "generate": cmd_generate,
        "review": cmd_review,
        "repair": cmd_repair,
        "repair-all": cmd_repair_all,
        "notify": cmd_notify,
        "resume": cmd_resume,
        "plan": cmd_plan,
        "report": cmd_report,
    }

    rc = handlers[args.command](args)
    if rc != 0:
        sys.exit(rc)


if __name__ == "__main__":
    main()
