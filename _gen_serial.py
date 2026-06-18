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
    """运行子进程，超时强杀整个进程树（含 mmx/node 子进程），返回 (returncode, stdout)。

    subprocess.run 的 timeout 在 Windows 上不会杀死子进程的子进程（mmx 调 node），
    导致 API 挂起时 node 进程残留、脚本永久阻塞。改用 Popen + taskkill /T 强杀进程树。
    """
    import subprocess as sp
    proc = sp.Popen(cmd_args, stdout=sp.PIPE, stderr=sp.PIPE,
                    text=True, encoding="utf-8", errors="replace", cwd=str(ROOT))
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, (stdout or "") + (stderr or "")
    except sp.TimeoutExpired:
        # 强杀整个进程树（/T 杀子进程 /F 强制）
        try:
            sp.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                   capture_output=True, timeout=15)
        except Exception:
            pass
        # 额外杀所有 node.exe（mmx 调用的 node 可能不在 writer 进程树内）
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


def ensure_prerequisites(project, py_exe):
    """确保 writer/reviewer 所需产物就绪：world.json、characters.json、大纲、媒体资产。

    顺序：planner（世界观+角色+媒体）→ outliner（大纲）→ outline_reviewer（大纲审查）。
    - 缺 world.json/characters.json → 跑 planner（会顺带生成媒体）
    - 缺大纲（outline 章数 < total）→ 跑 outliner + outline_reviewer
    - 有 world/characters 但缺媒体 → 单独跑 media_generator
    全部就绪 → 直接返回。
    """
    cfg = json.loads((project / "config.json").read_text(encoding="utf-8"))
    total = int(cfg.get("total_chapters", 500))
    world_file = project / "world.json"
    chars_file = project / "characters.json"
    media_cfg = cfg.get("media", {})
    media_enabled = media_cfg.get("enabled", True)

    # 1. 世界观与角色（planner 会顺带生成媒体）
    if not world_file.exists() or not chars_file.exists():
        print("[预检查] world.json/characters.json 缺失，启动 planner...")
        rc, out = run([py_exe, "scripts/pipeline/planner.py", "--project", str(project)],
                      timeout=900)
        if rc != 0:
            print(f"[预检查][警告] planner 返回 rc={rc}：{out[:300]}")

    # 2. 大纲（_gen_serial 本身不生成大纲，必须先跑 outliner）
    outline_dir = project / "chapters" / "outline"
    outline_count = len(list(outline_dir.glob("chapter_*.json"))) if outline_dir.exists() else 0
    if outline_count < total:
        missing = total - outline_count
        print(f"[预检查] 大纲不足（{outline_count}/{total}，缺 {missing} 章），启动 outliner...")
        rc, out = run([py_exe, "scripts/pipeline/outliner.py", "--project", str(project),
                       "--start", "1", "--end", str(total)], timeout=480)
        if rc != 0:
            print(f"[预检查][警告] outliner 返回 rc={rc}：{out[:300]}")
        # 大纲审查
        print("[预检查] 启动 outline_reviewer...")
        rc, out = run([py_exe, "scripts/pipeline/outline_reviewer.py", "--project", str(project),
                       "--start", "1", "--end", str(total)], timeout=480)
        if rc != 0:
            print(f"[预检查][警告] outline_reviewer 返回 rc={rc}：{out[:300]}")

    # 3. 媒体（若启用）
    if not media_enabled:
        return
    if media_assets_ready(project):
        return
    print("[预检查] 媒体资产缺失，启动 media_generator...")
    rc, out = run([py_exe, "scripts/pipeline/media_generator.py", "--project", str(project)],
                  timeout=3600)
    if rc != 0:
        print(f"[预检查][警告] media_generator 返回 rc={rc}（视频生成较慢，可能超时）：{out[:300]}")


def media_assets_ready(project):
    """检查三类媒体是否都已生成（与 coordinator.media_assets_ready 口径一致）。"""
    image_exts = {".png", ".jpg", ".jpeg", ".webp"}
    cover = any(p.is_file() and p.suffix.lower() in image_exts
                for p in (project / "media" / "images").glob("cover*"))
    video = (project / "media" / "videos" / "world_video.mp4").exists()
    song = any((project / "media" / "music").glob("theme_song.*"))
    return cover and video and song


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
    ap.add_argument("--workers", type=int, default=4, help="并发章节数(默认4)")
    ap.add_argument("--skip-planner", action="store_true",
                    help="跳过 planner 预检查（默认会自动补齐 world/characters/大纲/媒体）")
    args = ap.parse_args()

    project = (ROOT / args.project).resolve()
    cfg = json.loads((project / "config.json").read_text(encoding="utf-8"))
    min_score = float(cfg.get("reviewer", {}).get("min_score", 8.5))
    total_chapters = int(cfg.get("total_chapters", 500))
    py_exe = sys.executable

    # 启动前确保 planner 产物就绪：world.json / characters.json / 媒体资产。
    # _gen_serial 本身只跑 writer→reviewer，不生成世界观与媒体；
    # 若缺产物则自动调用 planner（planner 末尾会生成媒体）。
    if not args.skip_planner:
        ensure_prerequisites(project, py_exe)

    # 启动时清理残留的 node 进程（mmx 调用卡死后会残留，累积会拖慢系统）
    try:
        subprocess.run(["taskkill", "/F", "/IM", "node.exe"],
                       capture_output=True, timeout=15)
        print("[启动] 已清理残留 node 进程")
    except Exception:
        pass

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

    # 并发生成：用线程池同时处理多章（MiniMax qps=15 支持并发）
    from concurrent.futures import ThreadPoolExecutor, as_completed
    max_workers = args.workers if hasattr(args, 'workers') and args.workers > 0 else 4

    def process_one_chapter(chapter):
        """处理单章：writer→reviewer→promote final。线程安全（不同章节不冲突）。"""
        ch_tag = f"ch{chapter:04d}"
        draft = project / "chapters" / "draft" / f"chapter_{chapter:04d}.txt"
        rf = project / "chapters" / "review" / f"chapter_{chapter:04d}_review.json"
        final = project / "chapters" / "final" / f"chapter_{chapter:04d}.txt"
        final.parent.mkdir(parents=True, exist_ok=True)

        # 跳过已有 final
        if final.exists():
            return chapter, "SKIP", -1

        best_score = -1
        best_text = None

        # 检查低分 draft 强制重生
        if draft.exists() and rf.exists():
            try:
                old_score = json.loads(rf.read_text(encoding="utf-8")).get("overall_score")
                if isinstance(old_score, (int, float)) and old_score < args.force_regen_below:
                    draft.unlink()
                    rf.unlink()
            except Exception:
                pass

        for round_n in range(1, args.max_rounds + 1):
            if rf.exists() and round_n > 1:
                rf.unlink()
            # writer
            if not draft.exists() or round_n > 1:
                rc, out = run([py_exe, "scripts/pipeline/writer.py",
                               "--project", str(project), "--chapter", str(chapter)], timeout=600)
                if rc != 0 or not draft.exists():
                    continue
            # reviewer
            rc, out = run([py_exe, "scripts/pipeline/reviewer.py",
                           "--project", str(project), "--chapter", str(chapter)], timeout=150)
            score, verdict, _ = get_review_score(project, chapter)
            if score is not None and score > best_score:
                best_score = score
                best_text = draft.read_text(encoding="utf-8") if draft.exists() else best_text
            if score is not None and score >= min_score and verdict not in ("需重写", "需修改"):
                break

        # promote
        adopted = False
        if best_score >= min_score:
            if best_text:
                final.write_text(best_text, encoding="utf-8")
            elif draft.exists():
                shutil.copy2(draft, final)
            adopted = True
            status = "PASS"
        elif best_score >= min_score - 0.3:
            if best_text:
                final.write_text(best_text, encoding="utf-8")
            elif draft.exists():
                shutil.copy2(draft, final)
            adopted = True
            status = f"PASS_LENIENT({best_score})"
        else:
            status = f"FAIL({best_score})"

        print(f"[{ch_tag}] {status}", flush=True)
        return chapter, status, best_score

    # 分批并发：每批 max_workers 章
    pending = list(range(args.start, args.end + 1))
    batch_size = max_workers
    total_done = 0

    while pending:
        elapsed = time.time() - start_time
        if elapsed > args.time_limit:
            print(f"\n[时间预算耗尽] {elapsed:.0f}s > {args.time_limit}s, 已完成{total_done}章", flush=True)
            break

        batch = pending[:batch_size]
        pending = pending[batch_size:]
        print(f"\n--- 并发批次: ch{batch[0]:04d}-ch{batch[-1]:04d} ({len(batch)}章并发) ---", flush=True)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(process_one_chapter, ch): ch for ch in batch}
            for future in as_completed(futures):
                ch = futures[future]
                try:
                    chapter, status, score = future.result()
                    total_done += 1
                    flag = "✅" if "PASS" in status else "❌"
                    print(f"  {flag} ch{chapter:04d}: {status}", flush=True)
                except Exception as exc:
                    print(f"  ❌ ch{ch:04d}: 异常 {exc}", flush=True)

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
