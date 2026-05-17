#!/usr/bin/env python3
"""
Coordinator - 小说生成主控协调器
协调 Planner、Writer、Reviewer 三个 Agent 的工作
支持断点续传、配额监控、进度追踪
"""
import concurrent.futures
import json
import os
import subprocess
import threading
import sys
import time
from pathlib import Path

NOVELS_DIR = Path("D:/AiProject/Node/novels2")

# mmx CLI 路径（Windows 需通过 node 直接运行）
MMX_CLI_PATH = "C:/Users/Administrator/AppData/Roaming/npm/node_modules/mmx-cli/dist/mmx.mjs"
CHAPTERS_DIR = NOVELS_DIR / "chapters" / "draft"
REVIEWS_DIR = NOVELS_DIR / "reviews"
LOGS_DIR = NOVELS_DIR / "logs"
WORLD_FILE = NOVELS_DIR / "world.json"
OUTLINE_FILE = NOVELS_DIR / "outline.json"
CHARACTERS_FILE = NOVELS_DIR / "characters.json"
PROGRESS_FILE = NOVELS_DIR / "progress.json"
LOG_FILE = LOGS_DIR / "coordinator.log"

SCRIPTS_DIR = Path(__file__).parent


def log(msg: str):
    """记录日志"""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_script(script_name: str, *args) -> int:
    """运行子Agent脚本"""
    script_path = SCRIPTS_DIR / script_name
    cmd = [sys.executable, str(script_path)] + list(args)
    log(f"[Coordinator] 执行: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=False, text=True, encoding="utf-8")
    return result.returncode


def check_all_quotas():
    """检查所有MiniMax API配额，返回各类型剩余量"""
    result = subprocess.run(
        ["node", MMX_CLI_PATH, "quota", "show", "--quiet", "--output", "json"],
        capture_output=True, text=True, encoding="utf-8"
    )
    quotas = {}
    if result.returncode == 0:
        try:
            quota_list = json.loads(result.stdout)
            if isinstance(quota_list, list):
                for item in quota_list:
                    if isinstance(item, dict) and "model_name" in item:
                        name = item.get("model_name", "unknown")
                        used = item.get("current_interval_usage_count", 0)
                        limit = item.get("current_interval_total_count", 0)
                        remaining = limit - used
                        quotas[name] = {"used": used, "limit": limit, "remaining": remaining}
                        log(f"[Coordinator] 配额 {name}: {used}/{limit}, 剩余 {remaining}")
            elif isinstance(quota_list, dict) and "model_remains" in quota_list:
                for item in quota_list["model_remains"]:
                    name = item.get("model_name", "unknown")
                    used = item.get("current_interval_usage_count", 0)
                    limit = item.get("current_interval_total_count", 0)
                    remaining = limit - used
                    quotas[name] = {"used": used, "limit": limit, "remaining": remaining}
                    log(f"[Coordinator] 配额 {name}: {used}/{limit}, 剩余 {remaining}")
        except Exception as e:
            log(f"[Coordinator] 配额解析失败: {e}")
    if not quotas:
        log("[Coordinator] 无法获取配额信息，默认继续")
        quotas["MiniMax-M*"] = {"used": 0, "limit": 4500, "remaining": 4500}
    return quotas


def check_quota():
    """检查文本生成配额（兼容旧接口）"""
    quotas = check_all_quotas()
    for name, info in quotas.items():
        if "MiniMax-M" in name or "M*" in name:
            return info["remaining"]
    return quotas.get("MiniMax-M*", {}).get("remaining", 1000)


def load_progress():
    """加载进度"""
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "planner_done": False,
        "last_generated_chapter": 0,
        "last_reviewed_chapter": 0,
        "failed_chapters": [],
        "rewrite_queue": []
    }


def save_progress(progress):
    """保存进度"""
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def get_completed_chapters():
    """获取已完成的章节列表"""
    completed = []
    if CHAPTERS_DIR.exists():
        for f in CHAPTERS_DIR.glob("chapter_*.txt"):
            try:
                num = int(f.stem.split("_")[1])
                if f.stat().st_size > 1000:
                    completed.append(num)
            except (ValueError, IndexError):
                pass
    return sorted(completed)


def check_outline_complete():
    """检查大纲是否完整（2000章）"""
    if not OUTLINE_FILE.exists():
        return False
    try:
        with open(OUTLINE_FILE, "r", encoding="utf-8") as f:
            outline = json.load(f)
        count = len(outline.get("chapters", []))
        log(f"[Coordinator] 当前大纲: {count}/2000 章")
        return count >= 2000
    except Exception:
        return False


def check_all_files_exist():
    """检查所有必要文件是否存在"""
    return WORLD_FILE.exists() and OUTLINE_FILE.exists() and CHARACTERS_FILE.exists()


def run_planner():
    """运行Planner Agent"""
    log("=" * 60)
    log("[Coordinator] 启动 Planner Agent")
    log("=" * 60)
    return run_script("planner.py")


def run_writer_batch(start: int, end: int) -> list:
    """运行Writer Agent生成一批章节"""
    log("=" * 60)
    log(f"[Coordinator] 启动 Writer Agent: 第{start}-{end}章")
    log("=" * 60)
    return run_script("writer.py", "--start", str(start), "--end", str(end))


def run_reviewer_batch(start: int, end: int) -> list:
    """运行Reviewer Agent审查一批章节"""
    log("=" * 60)
    log(f"[Coordinator] 启动 Reviewer Agent: 第{start}-{end}章")
    log("=" * 60)
    return run_script("reviewer.py", "--start", str(start), "--end", str(end))


def run_parallel_agents_work_queue(agent_name: str, start: int, end: int, num_workers: int = 5, max_retries: int = 3) -> list:
    """
    工作队列模式：每个Agent完成后立即领取下一个任务。
    避免Agent空闲等待，最大化并行效率。
    """
    import queue

    total = end - start + 1
    if total <= 0:
        return []

    # 构建任务队列（每个章节一个任务）
    task_queue = queue.Queue()
    for ch in range(start, end + 1):
        task_queue.put((ch, 0))  # (chapter_number, retry_count)

    log(f"[Coordinator] 工作队列模式: {total} 个任务, {num_workers} 个 {agent_name} Agent")

    completed = []
    failed = []
    lock = threading.Lock()

    def worker():
        while True:
            try:
                chapter, retry = task_queue.get(timeout=1)
            except queue.Empty:
                return

            try:
                if agent_name == "writer.py":
                    rc = run_script(agent_name, "--chapter", str(chapter))
                elif agent_name == "reviewer.py":
                    rc = run_script(agent_name, "--chapter", str(chapter))
                elif agent_name == "rewrite_agent.py":
                    rc = run_script(agent_name, "--chapter", str(chapter))
                else:
                    rc = run_script(agent_name, "--start", str(chapter), "--end", str(chapter))

                if rc == 0:
                    with lock:
                        completed.append(chapter)
                    log(f"[Coordinator] {agent_name} 第{chapter}章完成 (rc={rc})")
                else:
                    if retry < max_retries:
                        log(f"[Coordinator] {agent_name} 第{chapter}章失败(rc={rc})，重新入队({retry+1}/{max_retries})")
                        task_queue.put((chapter, retry + 1))
                    else:
                        with lock:
                            failed.append(chapter)
                        log(f"[Coordinator] {agent_name} 第{chapter}章彻底失败，已放弃")
            except Exception as exc:
                log(f"[Coordinator] {agent_name} 第{chapter}章异常: {exc}")
                if retry < max_retries:
                    task_queue.put((chapter, retry + 1))
                else:
                    with lock:
                        failed.append(chapter)
            finally:
                task_queue.task_done()

    threads = []
    for _ in range(num_workers):
        t = threading.Thread(target=worker)
        t.start()
        threads.append(t)

    task_queue.join()

    for t in threads:
        t.join(timeout=5)

    log(f"[Coordinator] {agent_name} 批次完成: 成功 {len(completed)} 章, 失败 {len(failed)} 章")
    return completed, failed


def check_rewrites(start: int, end: int) -> list:
    """检查需要重写的章节"""
    rewrite_list = []
    for ch in range(start, end + 1):
        review_file = REVIEWS_DIR / f"chapter_{ch:04d}_review.json"
        if review_file.exists():
            with open(review_file, "r", encoding="utf-8") as f:
                review = json.load(f)
            verdict = review.get("verdict", "")
            score = review.get("overall_score", 10)
            if verdict == "需重写" or score < 7:
                rewrite_list.append(ch)
                log(f"[Coordinator] 第{ch}章评分{score}， verdict: {verdict}，标记为需重写")
    return rewrite_list


def generate_summary_report():
    """生成生成总结报告"""
    log("=" * 60)
    log("[Coordinator] 生成总结报告")
    log("=" * 60)

    completed = get_completed_chapters()
    total_words = 0
    review_scores = []
    rewrite_count = 0

    for ch in completed:
        # 优先读取 final/，其次 draft/
        final_file = NOVELS_DIR / "chapters" / "final" / f"chapter_{ch:04d}.txt"
        draft_file = CHAPTERS_DIR / f"chapter_{ch:04d}.txt"
        chapter_file = final_file if final_file.exists() else draft_file
        with open(chapter_file, "r", encoding="utf-8") as f:
            content = f.read()
        total_words += len(content)

        review_file = REVIEWS_DIR / f"chapter_{ch:04d}_review.json"
        if review_file.exists():
            with open(review_file, "r", encoding="utf-8") as f:
                review = json.load(f)
            score = review.get("overall_score", 0)
            review_scores.append(score)
            if score < 7 or review.get("verdict") == "需重写":
                rewrite_count += 1

    report = {
        "total_chapters": len(completed),
        "target_chapters": 2000,
        "total_words": total_words,
        "average_words_per_chapter": total_words // len(completed) if completed else 0,
        "average_score": sum(review_scores) / len(review_scores) if review_scores else 0,
        "rewrite_count": rewrite_count,
        "completed_chapters": completed
    }

    report_file = NOVELS_DIR / "summary_report.json"
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    log(f"[Coordinator] 总结报告: 已完成 {report['total_chapters']}/2000 章")
    log(f"[Coordinator] 总字数: {report['total_words']:,} 字")
    log(f"[Coordinator] 平均评分: {report['average_score']:.2f}")
    log(f"[Coordinator] 需重写: {rewrite_count} 章")
    return report


def main():
    print("=" * 70)
    print("  2000章玄幻小说Agent系统 - Coordinator")
    print("=" * 70)

    # 创建目录
    NOVELS_DIR.mkdir(parents=True, exist_ok=True)
    CHAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # 加载进度
    progress = load_progress()
    log(f"[Coordinator] 当前进度: 已生成 {progress['last_generated_chapter']} 章，已审查 {progress['last_reviewed_chapter']} 章")

    # 解析命令行参数
    import argparse
    parser = argparse.ArgumentParser(description="小说生成协调器")
    parser.add_argument("--batch-size", type=int, default=40, help="每批生成的章节数")
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=2000, help="结束章节")
    parser.add_argument("--skip-planner", action="store_true", help="跳过Planner阶段")
    parser.add_argument("--skip-review", action="store_true", help="跳过Review阶段")
    parser.add_argument("--rewrite-only", action="store_true", help="只运行重写")
    args = parser.parse_args()

    # 阶段1: Planner
    need_planner = not args.skip_planner and not check_all_files_exist()
    if need_planner:
        log("[Coordinator] 检测到必要文件缺失，启动Planner...")
        if run_planner() != 0:
            log("[ERROR] Planner执行失败，请检查日志")
            return
        progress["planner_done"] = True
        save_progress(progress)
    elif check_all_files_exist():
        outline_count = 0
        if OUTLINE_FILE.exists():
            try:
                with open(OUTLINE_FILE, "r", encoding="utf-8") as f:
                    o = json.load(f)
                outline_count = len(o.get("chapters", []))
            except Exception:
                pass
        log(f"[Coordinator] 世界观、大纲({outline_count}章)、角色档案已存在")
        progress["planner_done"] = True
        save_progress(progress)

    if not progress["planner_done"]:
        log("[ERROR] Planner未完成且跳过标志未设置")
        return

    # 获取实际大纲章节数
    actual_outline_count = 0
    if OUTLINE_FILE.exists():
        try:
            with open(OUTLINE_FILE, "r", encoding="utf-8") as f:
                o = json.load(f)
            actual_outline_count = max(ch.get("chapter_number", 0) for ch in o.get("chapters", [])) if o.get("chapters") else 0
        except Exception:
            pass

    # 阶段2: Writer + Reviewer 循环
    batch_size = args.batch_size
    start_chapter = max(args.start, progress["last_generated_chapter"] + 1)
    end_chapter = min(args.end, actual_outline_count) if actual_outline_count > 0 else args.end

    log(f"[Coordinator] 生成范围: 第{start_chapter}-{end_chapter}章（大纲共{actual_outline_count}章），批次大小: {batch_size}")

    for batch_start in range(start_chapter, end_chapter + 1, batch_size):
        batch_end = min(batch_start + batch_size - 1, end_chapter)

        # 检查所有配额
        all_quotas = check_all_quotas()
        text_remaining = all_quotas.get("MiniMax-M*", {}).get("remaining", 0)

        # 计算本批需要的文本调用数：写 + 审查（审查每章1次，但Reviewer脚本内部会调用）
        calls_needed = batch_size * 2  # writer + reviewer 各一次

        if text_remaining < calls_needed:
            log(f"[Coordinator] 文本配额不足（剩余{text_remaining}，需要{calls_needed}）")
            # 检查其他配额状态
            other_quotas = []
            for name, info in all_quotas.items():
                if name != "MiniMax-M*" and info["remaining"] > 0:
                    other_quotas.append(f"{name}: {info['remaining']}/{info['limit']}")
            if other_quotas:
                log(f"[Coordinator] 其他可用配额: {', '.join(other_quotas)}")
                log(f"[Coordinator] 注意：其他API（语音/视频/音乐等）无法生成文本内容，需等待文本配额重置")
            log(f"[Coordinator] 等待配额重置...")
            # 等待约5分钟后重试
            wait_minutes = 5

            # 利用等待期间生成章节图片（后台运行，不阻塞）
            img_script = SCRIPTS_DIR / "generate_images.py"
            if img_script.exists():
                log("[Coordinator] 配额等待期间，启动图片生成...")
                try:
                    subprocess.Popen(
                        [sys.executable, str(img_script)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0
                    )
                except Exception as e:
                    log(f"[Coordinator] 图片生成启动失败: {e}")

            log(f"[Coordinator] 等待 {wait_minutes} 分钟...")
            time.sleep(wait_minutes * 60)
            # 重新检查
            all_quotas = check_all_quotas()
            text_remaining = all_quotas.get("MiniMax-M*", {}).get("remaining", 0)
            if text_remaining < calls_needed:
                log(f"[Coordinator] 配额仍然不足，本次批次暂停，下次启动将从第{batch_start}章继续")
                break
            remaining_quota = check_quota()
            if remaining_quota < batch_size * 3:
                log("[Coordinator] 配额仍然不足，生成暂停")
                break

        # 写入本批章节（并行10个Writer）
        log(f"[Coordinator] ===== 开始第 {batch_start}-{batch_end} 章 =====")

        # 写入本批章节（并行20个Writer）
        log(f"[Coordinator] ===== 开始第 {batch_start}-{batch_end} 章 =====")

        writer_completed, writer_failed = run_parallel_agents_work_queue("writer.py", batch_start, batch_end, num_workers=60)
        if writer_failed:
            log(f"[WARNING] Writer Agent 失败章节: {writer_failed}")

        # 更新进度
        completed = get_completed_chapters()
        if completed:
            progress["last_generated_chapter"] = max(completed)
        save_progress(progress)

        # 审查本批章节（并行Reviewer）
        if not args.skip_review:
            reviewer_completed, reviewer_failed = run_parallel_agents_work_queue("reviewer.py", batch_start, batch_end, num_workers=60)
            if reviewer_failed:
                log(f"[WARNING] Reviewer Agent 失败章节: {reviewer_failed}")

            # 检查需要重写的章节
            rewrites = check_rewrites(batch_start, batch_end)
            progress["rewrite_queue"].extend(rewrites)
            progress["last_reviewed_chapter"] = batch_end
            save_progress(progress)

        # 每10批生成一次总结
        if batch_start % (batch_size * 10) == 1 or batch_end == end_chapter:
            generate_summary_report()

        # 批次间暂停
        log(f"[Coordinator] 第 {batch_start}-{batch_end} 章完成，暂停3秒...")
        time.sleep(3)

    # 阶段3: 处理重写队列（调用独立的 Rewrite Agent）
    if progress["rewrite_queue"] and not args.rewrite_only:
        log("=" * 60)
        log(f"[Coordinator] 处理重写队列: {len(progress['rewrite_queue'])} 章")
        log("=" * 60)

        # 调用 rewrite_agent.py 处理需要重写的章节
        rc = run_script("rewrite_agent.py")
        log(f"[Coordinator] Rewrite Agent 完成 (rc={rc})")

        progress["rewrite_queue"] = []
        save_progress(progress)

    # 最终总结
    report = generate_summary_report()
    log("=" * 60)
    log("[Coordinator] 全部任务完成")
    log(f"[Coordinator] 总进度: {report['total_chapters']}/2000 章")
    log(f"[Coordinator] 总字数: {report['total_words']:,} 字")
    log("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("[Coordinator] 用户中断，保存进度...")
        progress = load_progress()
        save_progress(progress)
        log("[Coordinator] 进度已保存，可断点续传")
    except Exception as e:
        log(f"[ERROR] 发生异常: {e}")
        import traceback
        log(traceback.format_exc())
