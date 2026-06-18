# -*- coding: utf-8 -*-
"""串行章节生成（章间串行 + 单章内大纲/正文候选竞速）。

架构（满足"章与章之间前面的先生成"）：
- 章节严格串行：第 N 章全部完成（大纲+正文+终稿）后才开始第 N+1 章
- 单章内竞速：大纲并发生成 N 个候选→审查→首个达 min_score 即采纳（stop_on_first_pass）；
              正文同理。未达标的候选继续跑，全部未达标则取最高分重写一轮。

每章流程：
1. outline race：并发 candidates 个 outliner 候选 → outline_reviewer 审查 → 选首个通过
2. draft race：并发 candidates 个 writer 候选 → reviewer 审查 → 选首个通过
3. promote：通过的 draft → final
4. 未通过则取最高分候选，基于审查反馈重写（最多 max_rounds 轮）

用法: py _gen_serial.py --project projects/novels14
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))


def run(cmd_args, timeout=400):
    """运行子进程，超时强杀整个进程树（含 mmx/node 子进程），返回 (returncode, stdout)。"""
    import subprocess as sp
    proc = sp.Popen(cmd_args, stdout=sp.PIPE, stderr=sp.PIPE,
                    text=True, encoding="utf-8", errors="replace", cwd=str(ROOT))
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, (stdout or "") + (stderr or "")
    except sp.TimeoutExpired:
        try:
            sp.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                   capture_output=True, timeout=15)
        except Exception:
            pass
        try:
            sp.run(["taskkill", "/F", "/IM", "node.exe"],
                   capture_output=True, timeout=15)
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
        return -1, "TIMEOUT_KILLED"


def media_assets_ready(project):
    image_exts = {".png", ".jpg", ".jpeg", ".webp"}
    cover = any(p.is_file() and p.suffix.lower() in image_exts
                for p in (project / "media" / "images").glob("cover*"))
    video = (project / "media" / "videos" / "world_video.mp4").exists()
    song = any((project / "media" / "music").glob("theme_song.*"))
    return cover and video and song


def ensure_prerequisites(project, py_exe):
    """确保产物就绪：planner（世界观+角色+媒体）→ outliner 全量 → outline_reviewer 全量。

    注意：这里只生成【第一批】大纲供正文衔接；真正的高质量大纲竞速在 process_chapter 内逐章做。
    但为避免正文依赖空大纲，此处先用单次 outliner 把全书大纲铺一遍（后续逐章竞速会覆盖）。
    实际上竞速模式下，ensure_prerequisites 只补 world/characters/媒体，大纲由逐章竞速生成。
    """
    cfg = json.loads((project / "config.json").read_text(encoding="utf-8"))
    total = int(cfg.get("total_chapters", 500))
    world_file = project / "world.json"
    chars_file = project / "characters.json"
    media_cfg = cfg.get("media", {})
    media_enabled = media_cfg.get("enabled", True)

    if not world_file.exists() or not chars_file.exists():
        print("[预检查] world.json/characters.json 缺失，启动 planner...")
        rc, out = run([py_exe, "scripts/pipeline/planner.py", "--project", str(project)],
                      timeout=900)
        if rc != 0:
            print(f"[预检查][警告] planner 返回 rc={rc}：{out[:300]}")

    # 竞速模式下大纲逐章生成，此处不预铺。仅检查媒体。
    if not media_enabled:
        return
    if media_assets_ready(project):
        return
    print("[预检查] 媒体资产缺失，启动 media_generator...")
    rc, out = run([py_exe, "scripts/pipeline/media_generator.py", "--project", str(project)],
                  timeout=3600)
    if rc != 0:
        print(f"[预检查][警告] media_generator 返回 rc={rc}（视频生成较慢，可能超时）：{out[:300]}")


def read_review_score(review_file):
    if not review_file.exists():
        return None, None, None
    try:
        d = json.loads(review_file.read_text(encoding="utf-8"))
        return d.get("overall_score"), d.get("verdict"), d
    except Exception:
        return None, None, None


def _outline_race_paths(project, chapter, attempt):
    """候选大纲/审查的文件路径。"""
    root = project / "logs" / "outline_candidates" / f"ch{chapter:04d}" / f"attempt{attempt}"
    return root


def race_outline(project, py_exe, chapter, candidates, workers, min_score, timeout):
    """并发生成 candidates 个单章大纲候选 → 审查 → 返回首个通过的 (outline_dict, score)。

    stop_on_first_pass：任一候选审查通过（score>=min_score 且 verdict 通过）即停止其余。
    全部未通过：返回最高分候选。
    """
    cand_root = _outline_race_paths(project, chapter, attempt=1)
    cand_root.mkdir(parents=True, exist_ok=True)

    def gen_one(cand_no):
        cand_file = cand_root / f"cand_{cand_no:02d}.json"
        cand_file.parent.mkdir(parents=True, exist_ok=True)
        if cand_file.exists():
            cand_file.unlink()
        rc, out = run([py_exe, "scripts/pipeline/outliner.py",
                       "--project", str(project), "--chapter", str(chapter),
                       "--candidate-file", str(cand_file)], timeout=timeout)
        if rc != 0 or not cand_file.exists():
            return cand_no, None, None, f"outliner rc={rc}"
        outline = json.loads(cand_file.read_text(encoding="utf-8"))
        # 审查候选大纲
        rev_file = cand_root / f"cand_{cand_no:02d}_review.json"
        rc2, out2 = run([py_exe, "scripts/pipeline/outline_reviewer.py",
                         "--project", str(project), "--chapter", str(chapter),
                         "--outline-file", str(cand_file),
                         "--review-file", str(rev_file)], timeout=timeout)
        score, verdict, _ = read_review_score(rev_file)
        return cand_no, outline, score, verdict

    best = None  # (outline, score, cand_no)
    with ThreadPoolExecutor(max_workers=min(workers, candidates)) as ex:
        futures = {ex.submit(gen_one, n): n for n in range(1, candidates + 1)}
        for fut in as_completed(futures):
            cand_no, outline, score, verdict = fut.result()
            if outline is None:
                print(f"  [大纲竞速] 候选{cand_no} 生成失败", flush=True)
                continue
            print(f"  [大纲竞速] 候选{cand_no} score={score} verdict={verdict}", flush=True)
            if score is not None and (best is None or score > (best[1] or -1)):
                best = (outline, score, cand_no)
            # stop_on_first_pass
            if score is not None and score >= min_score and verdict not in ("需重写", "需修改"):
                print(f"  [大纲竞速] 候选{cand_no} 达标({score}>={min_score})，停止其余候选", flush=True)
                # 取消未完成的
                for f in futures:
                    f.cancel()
                return best
    return best


def race_draft(project, py_exe, chapter, candidates, workers, min_score, timeout):
    """并发生成 candidates 个正文候选 → 审查 → 返回首个通过的 (draft_path, score)。

    writer 用 --output-file 写候选文件（候选模式，不写正式 draft 目录），
    确保读得到前一章正式 final 的结尾（章间串行保证）。
    """
    cand_root = project / "logs" / "draft_candidates" / f"ch{chapter:04d}"
    cand_root.mkdir(parents=True, exist_ok=True)

    def gen_one(cand_no):
        draft_file = cand_root / f"cand_{cand_no:02d}.txt"
        rev_file = cand_root / f"cand_{cand_no:02d}_review.json"
        if draft_file.exists():
            draft_file.unlink()
        rc, out = run([py_exe, "scripts/pipeline/writer.py",
                       "--project", str(project), "--chapter", str(chapter),
                       "--output-file", str(draft_file)], timeout=timeout)
        if rc != 0 or not draft_file.exists():
            return cand_no, None, None, None, f"writer rc={rc}"
        rc2, out2 = run([py_exe, "scripts/pipeline/reviewer.py",
                         "--project", str(project), "--chapter", str(chapter),
                         "--chapter-file", str(draft_file),
                         "--review-file", str(rev_file)], timeout=timeout)
        score, verdict, _ = read_review_score(rev_file)
        return cand_no, draft_file, score, verdict

    best = None  # (draft_file, score, cand_no)
    with ThreadPoolExecutor(max_workers=min(workers, candidates)) as ex:
        futures = {ex.submit(gen_one, n): n for n in range(1, candidates + 1)}
        for fut in as_completed(futures):
            cand_no, draft_file, score, verdict = fut.result()
            if draft_file is None:
                print(f"  [正文竞速] 候选{cand_no} 生成失败", flush=True)
                continue
            print(f"  [正文竞速] 候选{cand_no} score={score} verdict={verdict}", flush=True)
            if score is not None and (best is None or score > (best[1] or -1)):
                best = (draft_file, score, cand_no)
            if score is not None and score >= min_score and verdict not in ("需重写", "需修改"):
                print(f"  [正文竞速] 候选{cand_no} 达标({score}>={min_score})，停止其余候选", flush=True)
                for f in futures:
                    f.cancel()
                return best
    return best


def process_chapter(project, py_exe, chapter, cfg, candidates, workers, max_rounds, timeout):
    """处理单章：大纲竞速 → 写入正式大纲 → 正文竞速 → promote final。返回 (status, score)。"""
    min_score = float(cfg.get("reviewer", {}).get("min_score", 8.5))
    outline_min = float(cfg.get("outline_reviewer", {}).get("min_score", 8.0))
    ch_tag = f"ch{chapter:04d}"
    outline_dir = project / "chapters" / "outline"
    draft_dir = project / "chapters" / "draft"
    review_dir = project / "chapters" / "review"
    final_dir = project / "chapters" / "final"
    for d in (outline_dir, draft_dir, review_dir, final_dir):
        d.mkdir(parents=True, exist_ok=True)

    outline_file = outline_dir / f"chapter_{chapter:04d}.json"
    draft_file = draft_dir / f"chapter_{chapter:04d}.txt"
    final_file = final_dir / f"chapter_{chapter:04d}.txt"

    if final_file.exists():
        return "SKIP", -1

    # === 阶段1：大纲竞速 ===
    if not outline_file.exists():
        print(f"\n[{ch_tag}] === 大纲竞速（{candidates}候选）===", flush=True)
        best = race_outline(project, py_exe, chapter, candidates, workers, outline_min, timeout)
        if best is None or best[0] is None:
            print(f"[{ch_tag}] 大纲竞速全部失败", flush=True)
            return "OUTLINE_FAIL", -1
        outline, score, cand_no = best
        outline_file.write_text(json.dumps(outline, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[{ch_tag}] 大纲采纳候选{cand_no}（score={score}）", flush=True)

    # === 阶段2：正文竞速（含重写轮次）===
    best_score = -1
    best_text = None
    for round_n in range(1, max_rounds + 1):
        print(f"\n[{ch_tag}] === 正文竞速 轮{round_n}/{max_rounds}（{candidates}候选）===", flush=True)
        best = race_draft(project, py_exe, chapter, candidates, workers, min_score, timeout)
        if best is None or best[0] is None:
            print(f"[{ch_tag}] 轮{round_n} 正文竞速全部失败", flush=True)
            continue
        draft_cand, score, cand_no = best
        if score is not None and score > best_score:
            best_score = score
            best_text = draft_cand.read_text(encoding="utf-8")
        if score is not None and score >= min_score:
            break
        # 未达标：把最高分候选写入正式 draft，供下一轮 writer --review-feedback 重写参考
        if best_text:
            draft_file.write_text(best_text, encoding="utf-8")
        print(f"[{ch_tag}] 轮{round_n} 最高分 {best_score} < {min_score}，继续重写", flush=True)

    # === 阶段3：promote final ===
    if best_score >= min_score:
        if best_text:
            final_file.write_text(best_text, encoding="utf-8")
        elif draft_file.exists():
            shutil.copy2(draft_file, final_file)
        # 同步正式 draft/review（供后续章节衔接与终审）
        if best_text:
            draft_file.write_text(best_text, encoding="utf-8")
        extract_state_if_needed(project, cfg, chapter, best_text or "")
        print(f"[{ch_tag}] PASS ({best_score})", flush=True)
        return "PASS", best_score
    if best_score >= min_score - 0.3:
        if best_text:
            final_file.write_text(best_text, encoding="utf-8")
        elif draft_file.exists():
            shutil.copy2(draft_file, final_file)
        if best_text:
            draft_file.write_text(best_text, encoding="utf-8")
        extract_state_if_needed(project, cfg, chapter, best_text or "")
        print(f"[{ch_tag}] PASS_LENIENT ({best_score})", flush=True)
        return "PASS_LENIENT", best_score
    print(f"[{ch_tag}] FAIL ({best_score})", flush=True)
    return "FAIL", best_score


def extract_state_if_needed(project, cfg, chapter, text):
    """promote final 后抽取角色状态快照，供下一章 writer 注入（跨章一致性保障）。"""
    if not text.strip():
        return
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from core.character_state import extract_character_states, state_file
        if state_file(project, chapter).exists():
            return  # 已抽取过
        chars_meta = {}
        cf = project / "characters.json"
        if cf.exists():
            chars_meta = json.loads(cf.read_text(encoding="utf-8"))
        result = extract_character_states(project, cfg, chapter, text, chars_meta)
        n = len(result.get("characters", {}))
        print(f"  [ch{chapter:04d}] 角色状态抽取: {n} 个角色", flush=True)
    except Exception as exc:
        print(f"  [ch{chapter:04d}] 角色状态抽取失败（不影响生成）: {exc}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="串行章节生成（章间串行+单章候选竞速）")
    ap.add_argument("--project", "-p", default="projects/novels12")
    ap.add_argument("--start", type=int, default=0, help="起始章(0=自动从最后final+1)")
    ap.add_argument("--end", type=int, default=0, help="结束章(0=到config.total_chapters)")
    ap.add_argument("--candidates", type=int, default=4, help="单章候选竞速数(默认4)")
    ap.add_argument("--workers", type=int, default=4, help="候选并发数(默认4，<=candidates)")
    ap.add_argument("--max-rounds", type=int, default=4, help="正文未达标时重写轮数上限")
    ap.add_argument("--time-limit", type=int, default=0, help="本次运行最大秒数(0=不限)")
    ap.add_argument("--timeout", type=int, default=600, help="单个 outliner/writer 调用超时秒数")
    ap.add_argument("--skip-planner", action="store_true", help="跳过 planner 预检查")
    args = ap.parse_args()

    project = (ROOT / args.project).resolve()
    cfg = json.loads((project / "config.json").read_text(encoding="utf-8"))
    total_chapters = int(cfg.get("total_chapters", 500))
    py_exe = sys.executable

    if not args.skip_planner:
        ensure_prerequisites(project, py_exe)

    # 清理残留 node
    try:
        subprocess.run(["taskkill", "/F", "/IM", "node.exe"], capture_output=True, timeout=15)
    except Exception:
        pass

    # 自动起点
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
    workers = max(1, min(args.workers, args.candidates))

    print(f"\n{'='*60}")
    print(f"串行生成: 第{args.start}-{args.end}章 | 候选{args.candidates} 并发{workers} | 重写轮{args.max_rounds}")
    print(f"{'='*60}")

    scores = []
    passed = 0
    for chapter in range(args.start, args.end + 1):
        if args.time_limit and (time.time() - start_time) > args.time_limit:
            print(f"\n[时间预算耗尽] 已完成 {chapter - args.start} 章", flush=True)
            break
        status, score = process_chapter(
            project, py_exe, chapter, cfg,
            args.candidates, workers, args.max_rounds, args.timeout,
        )
        if "PASS" in status:
            passed += 1
            if isinstance(score, (int, float)) and score > 0:
                scores.append(score)

    # 汇总
    print(f"\n{'='*60}")
    print(f"汇总: {passed}/{args.end - args.start + 1} 章通过")
    if scores:
        print(f"通过章均分: {sum(scores)/len(scores):.3f}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
