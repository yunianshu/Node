#!/usr/bin/env python3
"""
批量将 novels1 终稿打磨到 8.5+
策略：基于 review_final 评分，按优先级分批 polish
"""
import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.novel_config import configure_stdio
from core.push_notifier import _get_webhook, _send

configure_stdio()


def push_wechat(msg: str, config: dict = None):
    url = _get_webhook(config) if config else None
    if not url:
        return
    _send(url, msg)


def load_json(f: Path) -> dict:
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_candidates(project_dir: str, max_score: float = 8.5) -> list:
    """返回所有评分 < max_score 的章节，按分数升序排列（低分优先）"""
    review_dir = Path(project_dir) / "chapters" / "review_final"
    if not review_dir.exists():
        return []

    candidates = []
    for f in sorted(review_dir.glob("chapter_*_review.json")):
        try:
            ch = int(f.stem.split("_")[1])
            data = load_json(f)
            score = data.get("overall_score", data.get("score", 10))
            score = float(score)
            if score < max_score:
                candidates.append((ch, score))
        except Exception:
            continue

    candidates.sort(key=lambda x: (x[1], x[0]))
    return candidates


def already_polished(project_dir: str, ch: int) -> bool:
    """检查章节是否已在 polish 记录中标记为完成"""
    meta_dir = Path(project_dir) / "chapters" / "rewrite" / f"chapter_{ch:04d}"
    meta_file = meta_dir / "rewrite_meta.json"
    if not meta_file.exists():
        return False
    try:
        meta = load_json(meta_file)
        # polish 模式下会 force 重写，所以如果 meta 存在且 selected_reason 不是 best_effort，认为已处理
        return meta.get("selected_attempt", 0) > 0
    except Exception:
        return False


def run_polish(project_dir: str, ch: int) -> tuple:
    """调用 rewrite_agent.py --final --polish --force --chapter"""
    cmd = [
        sys.executable, "-u",
        "scripts/pipeline/rewrite_agent.py",
        "--project", project_dir,
        "--final", "--polish", "--force", "--chapter", str(ch),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(Path.cwd()),
            encoding="utf-8",
            errors="replace",
        )
        ok = result.returncode == 0 and ("终稿已保存" in result.stdout or "success" in result.stdout.lower())
        return ch, ok, result.stdout[-500:] if len(result.stdout) > 500 else result.stdout
    except subprocess.TimeoutExpired:
        return ch, False, "timeout"
    except Exception as e:
        return ch, False, str(e)


def main():
    parser = argparse.ArgumentParser(description="批量打磨终稿到8.5+")
    parser.add_argument("--project", "-p", default="projects/novels1", help="项目目录")
    parser.add_argument("--max-score", type=float, default=9.0, help="打磨阈值（默认9.0）")
    parser.add_argument("--workers", type=int, default=10, help="并行数")
    parser.add_argument("--limit", type=int, default=0, help="最多处理N章（0=全部）")
    parser.add_argument("--dry-run", action="store_true", help="只列出候选，不执行")
    parser.add_argument("--resume", action="store_true", help="断点续传：跳过已有polish记录的章节")
    parser.add_argument("--wechat", action="store_true", help="启用企业微信进度推送")
    args = parser.parse_args()

    project_dir = str(Path(args.project).resolve())
    candidates = get_candidates(project_dir, args.max_score)

    if args.resume:
        candidates = [(ch, s) for ch, s in candidates if not already_polished(project_dir, ch)]

    if args.limit > 0:
        candidates = candidates[:args.limit]

    total = len(candidates)
    if total == 0:
        print("✅ 没有需要打磨的章节")
        return

    print(f"📊 《{Path(project_dir).name}》需要打磨的章节: {total} 个")
    print(f"   最低分: {candidates[0][1]:.1f}, 最高分: {candidates[-1][1]:.1f}")
    print()

    # 分组统计
    low = [(c, s) for c, s in candidates if s < 7.5]
    mid = [(c, s) for c, s in candidates if 7.5 <= s < 8.0]
    high = [(c, s) for c, s in candidates if 8.0 <= s < 8.5]
    print(f"   🔴 <7.5 分: {len(low)} 章")
    print(f"   🟡 7.5-8.0 分: {len(mid)} 章")
    print(f"   🟢 8.0-8.4 分: {len(high)} 章")
    print()

    if args.dry_run:
        print("前20个候选章节:")
        for ch, s in candidates[:20]:
            print(f"  ch{ch}: {s}")
        return

    # 获取书名
    title = "本小说"
    world_file = Path(project_dir) / "world.json"
    if world_file.exists():
        title = load_json(world_file).get("title", title)

    completed = 0
    failed = []
    start_time = time.time()

    def _push(msg: str):
        if args.wechat:
            push_wechat(msg)
        print(msg)

    _push(f"🚀 《{title}》终稿打磨启动\n目标: {total} 章 → 8.5+\n并行: {args.workers} workers")

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(run_polish, project_dir, ch): (ch, s) for ch, s in candidates}
        for future in as_completed(futures):
            ch, score = futures[future]
            try:
                ch_ret, ok, output = future.result()
            except Exception as e:
                ok, output = False, str(e)

            if ok:
                completed += 1
            else:
                failed.append((ch, score, output))

            if (completed + len(failed)) % 10 == 0:
                elapsed = time.time() - start_time
                rate = (completed + len(failed)) / elapsed if elapsed > 0 else 0
                remain = (total - completed - len(failed)) / rate if rate > 0 else 0
                _push(
                    f"📖 《{title}》打磨进度\n"
                    f"完成: {completed}/{total}\n"
                    f"失败: {len(failed)}\n"
                    f"速率: {rate:.2f} 章/min\n"
                    f"ETA: {remain/60:.1f}min"
                )

    elapsed = time.time() - start_time
    _push(
        f"✅ 《{title}》打磨完成\n"
        f"成功: {completed}/{total}\n"
        f"失败: {len(failed)}\n"
        f"耗时: {elapsed/60:.1f}min"
    )

    if failed:
        print("\n失败章节:")
        for ch, s, err in failed[:20]:
            print(f"  ch{ch} (score={s}): {err[:100]}")


if __name__ == "__main__":
    main()
