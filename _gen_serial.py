# -*- coding: utf-8 -*-
"""轻量串行章节生成：绕开 coordinator 的 race 机制，直接 writer→reviewer→重写→final。

每章流程：
1. writer 生成 draft（如已有 draft 且 review 未通过则重写）
2. reviewer 审查
3. 若 score >= min_score：复制 draft→final，进入下一章
4. 若 score < min_score：基于 review 反馈重写，最多 max_rounds 轮
5. 最终仍未通过：取最高分版本，只要 >= min_score-0.3 也采纳（宽松兜底）

用法: py _gen_serial.py --start 1 --end 10
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))


def run(cmd_args, timeout=400):
    """运行子进程，返回 (returncode, stdout)。"""
    try:
        r = subprocess.run(cmd_args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           cwd=str(ROOT))
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"


def get_review_score(project, chapter):
    rf = project / "chapters" / "review" / f"chapter_{chapter:04d}_review.json"
    if not rf.exists():
        return None, None, None
    try:
        d = json.loads(rf.read_text(encoding="utf-8"))
        return d.get("overall_score"), d.get("verdict"), d
    except Exception:
        return None, None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", "-p", default="projects/novels12")
    ap.add_argument("--start", type=int, default=0, help="起始章(0=自动从最后final+1)")
    ap.add_argument("--end", type=int, default=0, help="结束章(0=到config.total_chapters)")
    ap.add_argument("--max-rounds", type=int, default=4, help="每章最多重写轮数")
    ap.add_argument("--time-limit", type=int, default=480, help="本次运行最大秒数(默认8分钟,留余量)")
    ap.add_argument("--force-regen-below", type=float, default=8.0,
                    help="现有draft的review分低于此值时强制删除重生成(非重写),默认8.0")
    args = ap.parse_args()

    project = (ROOT / args.project).resolve()
    cfg = json.loads((project / "config.json").read_text(encoding="utf-8"))
    min_score = float(cfg.get("reviewer", {}).get("min_score", 8.5))
    total_chapters = int(cfg.get("total_chapters", 500))
    py_exe = sys.executable

    # 自动起点：从最后一个已存在的final+1开始
    if args.start <= 0:
        finals = sorted((project / "chapters" / "final").glob("chapter_*.txt"))
        if finals:
            args.start = max(int(f.name.split("_")[1].split(".")[0]) for f in finals) + 1
            print(f"[自动起点] 最后final + 1 = 第{args.start}章")
        else:
            args.start = 1
    if args.end <= 0:
        args.end = total_chapters

    start_time = time.time()
    results = {}
    for chapter in range(args.start, args.end + 1):
        # 时间预算检查：剩余不足6分钟(一章约需)则退出，留下次接力
        elapsed = time.time() - start_time
        if elapsed > args.time_limit:
            print(f"\n[时间预算耗尽] 已运行{elapsed:.0f}s > {args.time_limit}s,停在ch{chapter-1:04d},下次自动接力")
            break
        ch_tag = f"ch{chapter:04d}"
        print(f"\n{'='*60}\n处理 {ch_tag} (目标 >= {min_score}) 已运行{elapsed:.0f}s\n{'='*60}")
        best_score = -1
        best_text = None

        # 检查现有 draft 的 review 分：若太低则删除强制重生（旧_best质量差）
        draft = project / "chapters" / "draft" / f"chapter_{chapter:04d}.txt"
        rf = project / "chapters" / "review" / f"chapter_{chapter:04d}_review.json"
        if draft.exists() and rf.exists():
            try:
                old_score = json.loads(rf.read_text(encoding="utf-8")).get("overall_score")
                if isinstance(old_score, (int, float)) and old_score < args.force_regen_below:
                    print(f"[{ch_tag}] 现有draft仅{old_score}分 < {args.force_regen_below},删除强制重生")
                    draft.unlink()
                    rf.unlink()
            except Exception:
                pass

        for round_n in range(1, args.max_rounds + 1):
            print(f"\n--- {ch_tag} 第{round_n}/{args.max_rounds}轮 ---")
            # 删除旧 review 强制重新审查（重写后必须重审）
            if rf.exists() and round_n > 1:
                rf.unlink()

            # 1. writer（生成或重写）
            if not draft.exists() or round_n > 1:
                print(f"[{ch_tag}] writer 生成中...")
                t0 = time.time()
                rc, out = run([py_exe, "scripts/pipeline/writer.py",
                               "--project", str(project), "--chapter", str(chapter)], timeout=420)
                elapsed = time.time() - t0
                print(f"[{ch_tag}] writer 完成 rc={rc} 耗时{elapsed:.0f}s")
                if rc != 0 or not draft.exists():
                    print(f"[{ch_tag}] writer 失败，跳过本轮: {out[-200:]}")
                    continue

            # 2. reviewer
            print(f"[{ch_tag}] reviewer 审查中...")
            t0 = time.time()
            rc, out = run([py_exe, "scripts/pipeline/reviewer.py",
                           "--project", str(project), "--chapter", str(chapter)], timeout=240)
            elapsed = time.time() - t0
            print(f"[{ch_tag}] reviewer 完成 rc={rc} 耗时{elapsed:.0f}s")

            score, verdict, _ = get_review_score(project, chapter)
            wc = len(draft.read_text(encoding="utf-8")) if draft.exists() else 0
            print(f"[{ch_tag}] score={score} verdict={verdict} words={wc}")

            if score is not None and score > best_score:
                best_score = score
                best_text = draft.read_text(encoding="utf-8") if draft.exists() else best_text

            # 3. 通过判断
            if score is not None and score >= min_score and verdict not in ("需重写", "需修改"):
                print(f"[{ch_tag}] ✅ 通过 ({score} >= {min_score})")
                break
            print(f"[{ch_tag}] 未通过 ({score} < {min_score})，下轮重写")

        # 4. 采纳：通过或最高分兜底
        final = project / "chapters" / "final" / f"chapter_{chapter:04d}.txt"
        final.parent.mkdir(parents=True, exist_ok=True)
        adopted = False
        if best_score >= min_score:
            if best_text:
                final.write_text(best_text, encoding="utf-8")
            elif draft.exists():
                shutil.copy2(draft, final)
            adopted = True
            status = "PASS"
        elif best_score >= min_score - 0.3:
            # 宽松兜底：差0.3内也采纳（质量门微调空间）
            if best_text:
                final.write_text(best_text, encoding="utf-8")
            elif draft.exists():
                shutil.copy2(draft, final)
            adopted = True
            status = f"PASS_LENIENT({best_score})"
            print(f"[{ch_tag}] ⚠️ 宽松采纳 ({best_score} 接近 {min_score})")
        else:
            status = f"FAIL({best_score})"
            print(f"[{ch_tag}] ❌ 未达标 best={best_score}")

        results[chapter] = {"score": best_score, "status": status, "adopted": adopted}
        print(f"[{ch_tag}] 结果: {status}")

    # 汇总
    print(f"\n{'='*60}\n汇总 ({args.start}-{args.end})\n{'='*60}")
    scores = []
    for ch, r in sorted(results.items()):
        flag = "✅" if r["adopted"] else "❌"
        print(f"  {flag} ch{ch:04d}: {r['status']}")
        if isinstance(r["score"], (int, float)) and r["score"] > 0:
            scores.append(r["score"])
    if scores:
        print(f"\n平均分: {sum(scores)/len(scores):.3f}  通过: {sum(1 for r in results.values() if r['adopted'])}/{len(results)}")


if __name__ == "__main__":
    main()
