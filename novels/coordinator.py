#!/usr/bin/env python3
"""
Novel Coordinator - 小说生成统一主控（框架版）
支持：配置驱动、统一限流、自动补全、断点续传、配额监控、进度推送

用法:
    python -m novels.coordinator --config projects/novels5/config.json
    python -m novels.coordinator --config projects/novels5/config.json --skip-planner
"""
import argparse
import json
import queue
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 将 novels/ 加入路径以 import 框架模块
sys.path.insert(0, str(Path(__file__).parent.parent))

from novels.core.config import NovelConfig
from novels.core.llm_client import LLMClient
from novels.core.logger import get_logger
from novels.core.notifier import WeChatNotifier
from novels.core.quota import QuotaMonitor
from novels.agents.base import BaseAgent, AgentResult
from novels.multimedia.image_generator import ImageGenerator
from novels.multimedia.video_generator import VideoGenerator
from novels.multimedia.speech_generator import SpeechGenerator
from novels.multimedia.music_generator import MusicGenerator


class NovelCoordinator:
    """小说生成主控协调器"""

    def __init__(self, config: NovelConfig):
        self.config = config
        self.logger = get_logger("Coordinator", config.logs_dir)
        self.llm = LLMClient(
            mmx_path=config.mmx_path,
            model=config.model,
            qps=config.api_qps,
            log_dir=str(config.logs_dir),
        )
        self.quota = QuotaMonitor(config.mmx_path)
        self.notifier = WeChatNotifier(
            webhook_url=config.coordinator.wechat_webhook,
            min_interval=config.coordinator.push_interval_seconds,
        )

        # 创建必要目录
        for d in [config.draft_dir, config.final_dir, config.reviews_dir, config.logs_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # 进度状态
        self.progress = self._load_progress()

        # 运行标志
        self._running = True
        self._push_thread: threading.Thread | None = None

        # 多媒体生成器
        self._media_gens = self._init_media_generators()
        self._media_queue: queue.Queue = queue.Queue()
        self._media_thread: threading.Thread | None = None

    def _init_media_generators(self) -> dict:
        """初始化多媒体生成器"""
        if not self.config.multimedia.enabled:
            return {}
        gens = {}
        cfg = self.config.multimedia
        if cfg.images.enabled:
            gens["image"] = ImageGenerator(self.config)
        if cfg.videos.enabled:
            gens["video"] = VideoGenerator(self.config)
        if cfg.audio.enabled:
            gens["audio"] = SpeechGenerator(self.config)
        if cfg.music.enabled:
            gens["music"] = MusicGenerator(self.config)
        if gens:
            self.logger.info(f"多媒体生成已启用: {list(gens.keys())}")
        return gens

    def _start_media_worker(self):
        """启动后台多媒体生成线程"""
        if not self._media_gens or (self._media_thread and self._media_thread.is_alive()):
            return

        def worker():
            while self._running:
                try:
                    task = self._media_queue.get(timeout=5)
                except queue.Empty:
                    continue
                if task is None:
                    break
                ch, content, outline = task["ch"], task.get("content", ""), task.get("outline", {})
                for name, gen in self._media_gens.items():
                    try:
                        gen.generate(ch, content, outline)
                    except Exception as e:
                        self.logger.error(f"多媒体[{name}] 第{ch}章异常: {e}")
                self._media_queue.task_done()

        self._media_thread = threading.Thread(target=worker, daemon=True)
        self._media_thread.start()
        self.logger.info("多媒体生成线程已启动")

    def _enqueue_media(self, chapter_number: int):
        """将章节加入多媒体生成队列"""
        if not self._media_gens:
            return
        content = ""
        draft_path = self.config.draft_dir / f"chapter_{chapter_number:04d}.txt"
        if draft_path.exists():
            content = draft_path.read_text(encoding="utf-8")
        outline = self._read_outline_chapter(chapter_number)
        self._media_queue.put({"ch": chapter_number, "content": content, "outline": outline})

    def _read_outline_chapter(self, chapter_number: int) -> dict:
        """读取单章大纲信息"""
        if not self.config.outline_file.exists():
            return {}
        try:
            with open(self.config.outline_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for ch in data.get("chapters", []):
                if ch.get("chapter_number") == chapter_number:
                    return ch
        except Exception:
            pass
        return {}

    def _generate_cover_and_trailer(self):
        """生成封面和预告片（一次性任务）"""
        if not self._media_gens:
            return
        world = {}
        if self.config.world_file.exists():
            try:
                with open(self.config.world_file, "r", encoding="utf-8") as f:
                    world = json.load(f)
            except Exception:
                pass
        if "image" in self._media_gens:
            self.logger.info("生成小说封面...")
            self._media_gens["image"].generate_cover(world)
        if "music" in self._media_gens:
            self.logger.info("生成主题曲...")
            self._media_gens["music"].generate_theme(world)
        if "video" in self._media_gens:
            self.logger.info("生成预告片...")
            self._media_gens["video"].generate_trailer(world)

    # ------------------------------------------------------------------
    # 进度管理
    # ------------------------------------------------------------------
    def _load_progress(self) -> dict:
        if self.config.progress_file.exists():
            with open(self.config.progress_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {
            "planner_done": False,
            "last_generated_chapter": 0,
            "last_reviewed_chapter": 0,
            "failed_chapters": [],
            "rewrite_queue": [],
        }

    def _save_progress(self):
        with open(self.config.progress_file, "w", encoding="utf-8") as f:
            json.dump(self.progress, f, ensure_ascii=False, indent=2)

    def _get_completed_drafts(self) -> list[int]:
        if not self.config.draft_dir.exists():
            return []
        completed = []
        for f in self.config.draft_dir.glob("chapter_*.txt"):
            try:
                num = int(f.stem.split("_")[1])
                if f.stat().st_size > 1000:
                    completed.append(num)
            except (ValueError, IndexError):
                pass
        return sorted(completed)

    def _get_completed_reviews(self) -> list[int]:
        if not self.config.reviews_dir.exists():
            return []
        completed = []
        for f in self.config.reviews_dir.glob("chapter_*_review.json"):
            try:
                num = int(f.stem.split("_")[1])
                completed.append(num)
            except (ValueError, IndexError):
                pass
        return sorted(completed)

    def _count_final(self) -> int:
        if not self.config.final_dir.exists():
            return 0
        return len([f for f in self.config.final_dir.glob("chapter_*.txt") if f.stat().st_size > 1000])

    # ------------------------------------------------------------------
    # 通知
    # ------------------------------------------------------------------
    def _start_progress_pusher(self):
        """启动后台进度推送线程"""
        if self._push_thread and self._push_thread.is_alive():
            return

        def pusher():
            while self._running:
                time.sleep(self.config.coordinator.push_interval_seconds)
                if not self._running:
                    break
                drafts = len(self._get_completed_drafts())
                reviews = len(self._get_completed_reviews())
                self.notifier.push_progress(
                    self.config.title, drafts, reviews, self.config.total_chapters
                )

        self._push_thread = threading.Thread(target=pusher, daemon=True)
        self._push_thread.start()
        self.logger.info("进度推送线程已启动")

    # ------------------------------------------------------------------
    # 配额检查
    # ------------------------------------------------------------------
    def _check_quota_before_batch(self, batch_size: int) -> bool:
        """批次前配额检查，不足时等待或降速"""
        calls_needed = batch_size * 2  # writer + reviewer
        if self.quota.is_sufficient(calls_needed):
            return True

        self.logger.warning(f"配额不足（需 {calls_needed} 次调用），尝试等待...")
        self.notifier.push(f"[{self.config.title}] 配额不足，等待重置...")

        # 等待最多 6 分钟
        for _ in range(12):
            time.sleep(30)
            if self.quota.is_sufficient(calls_needed):
                self.logger.info("配额已恢复")
                return True

        # 仍然不足 → 动态降速
        self.logger.warning("配额仍未恢复，降低并发速率")
        self.llm.adjust_qps(0.5)
        return False

    # ------------------------------------------------------------------
    # 核心：工作队列模式
    # ------------------------------------------------------------------
    def _run_work_queue(
        self,
        task_name: str,
        chapter_numbers: list[int],
        worker_fn,
        num_workers: int | None = None,
        max_retries: int = 3,
    ) -> tuple[list[int], list[int]]:
        """
        通用工作队列：多线程消费章节任务。
        worker_fn: callable(chapter_number: int) -> bool
        返回: (成功列表, 失败列表)
        """
        if not chapter_numbers:
            return [], []

        num_workers = num_workers or self.config.coordinator.num_workers
        q = queue.Queue()
        for ch in chapter_numbers:
            q.put((ch, 0))  # (chapter, retry_count)

        completed: list[int] = []
        failed: list[int] = []
        lock = threading.Lock()

        self.logger.info(f"工作队列: {len(chapter_numbers)} 个任务, {num_workers} workers [{task_name}]")

        def worker():
            while True:
                try:
                    ch, retry = q.get(timeout=2)
                except queue.Empty:
                    return
                try:
                    success = worker_fn(ch)
                    if success:
                        with lock:
                            completed.append(ch)
                        self.logger.debug(f"{task_name} 第{ch}章完成")
                    else:
                        if retry < max_retries:
                            self.logger.warning(f"{task_name} 第{ch}章失败，重试({retry + 1}/{max_retries})")
                            q.put((ch, retry + 1))
                        else:
                            with lock:
                                failed.append(ch)
                            self.logger.error(f"{task_name} 第{ch}章彻底失败")
                except Exception as e:
                    self.logger.error(f"{task_name} 第{ch}章异常: {e}")
                    if retry < max_retries:
                        q.put((ch, retry + 1))
                    else:
                        with lock:
                            failed.append(ch)
                finally:
                    q.task_done()

        threads = [threading.Thread(target=worker) for _ in range(num_workers)]
        for t in threads:
            t.start()

        q.join()
        for t in threads:
            t.join(timeout=5)

        self.logger.info(f"{task_name} 批次完成: 成功 {len(completed)} 章, 失败 {len(failed)} 章")
        return completed, failed

    # ------------------------------------------------------------------
    # 各阶段封装（由子类或外部 Agent 实现）
    # ------------------------------------------------------------------
    def run_planner(self) -> bool:
        """运行大纲规划。若项目有 planner.py 则调用之，否则用框架默认。"""
        planner_script = self.config.path / "scripts" / "planner.py"
        if planner_script.exists():
            import subprocess
            rc = subprocess.run([sys.executable, str(planner_script)], capture_output=False).returncode
            return rc == 0

        # TODO: 框架内置 planner
        self.logger.error("未找到 planner.py，且框架内置 Planner 尚未实现")
        return False

    def run_writer(self, chapter_number: int) -> bool:
        """生成单章。优先调用项目 scripts/writer.py，否则框架内置。"""
        writer_script = self.config.path / "scripts" / "writer.py"
        if writer_script.exists():
            import subprocess
            rc = subprocess.run(
                [sys.executable, str(writer_script), "--chapter", str(chapter_number)],
                capture_output=False,
            ).returncode
            return rc == 0

        self.logger.error("未找到 writer.py，且框架内置 Writer 尚未实现")
        return False

    def run_reviewer(self, chapter_number: int) -> bool:
        """审查单章。优先调用项目 scripts/reviewer.py，否则框架内置。"""
        reviewer_script = self.config.path / "scripts" / "reviewer.py"
        if reviewer_script.exists():
            import subprocess
            rc = subprocess.run(
                [sys.executable, str(reviewer_script), "--chapter", str(chapter_number)],
                capture_output=False,
            ).returncode
            return rc == 0

        self.logger.error("未找到 reviewer.py，且框架内置 Reviewer 尚未实现")
        return False

    def run_rewrite(self, chapter_numbers: list[int]) -> bool:
        """重写指定章节。"""
        rewrite_script = self.config.path / "scripts" / "rewrite_agent.py"
        if rewrite_script.exists():
            import subprocess
            rc = subprocess.run([sys.executable, str(rewrite_script)], capture_output=False).returncode
            return rc == 0

        self.logger.error("未找到 rewrite_agent.py，且框架内置 RewriteAgent 尚未实现")
        return False

    # ------------------------------------------------------------------
    # 自动补全与重写
    # ------------------------------------------------------------------
    def _auto_fill_missing(self, start: int, end: int):
        """检测并补全缺失的章节（草稿 + 审查）"""
        # 1. 补全缺失草稿
        drafts = set(self._get_completed_drafts())
        missing_drafts = [ch for ch in range(start, end + 1) if ch not in drafts]
        if missing_drafts:
            self.logger.info(f"检测到 {len(missing_drafts)} 章草稿缺失，开始补全...")
            self.notifier.push(f"[{self.config.title}] 补全 {len(missing_drafts)} 章缺失草稿")
            # 补全用更低并发，避免再次触发限流
            old_qps = self.llm.bucket.qps
            self.llm.adjust_qps(min(old_qps, 1.0))
            self._run_work_queue("FillDraft", missing_drafts, self.run_writer, num_workers=3)
            self.llm.adjust_qps(old_qps)

        # 2. 补全缺失审查
        reviews = set(self._get_completed_reviews())
        missing_reviews = [ch for ch in range(start, end + 1) if ch not in reviews]
        if missing_reviews:
            self.logger.info(f"检测到 {len(missing_reviews)} 章审查缺失，开始补全...")
            old_qps = self.llm.bucket.qps
            self.llm.adjust_qps(min(old_qps, 1.0))
            self._run_work_queue("FillReview", missing_reviews, self.run_reviewer, num_workers=3)
            self.llm.adjust_qps(old_qps)

    def _auto_rewrite(self):
        """处理重写队列"""
        queue = self.progress.get("rewrite_queue", [])
        if not queue:
            return
        self.logger.info(f"处理重写队列: {len(queue)} 章")
        self.notifier.push(f"[{self.config.title}] 开始重写 {len(queue)} 章")
        self.run_rewrite(queue)
        self.progress["rewrite_queue"] = []
        self._save_progress()

    def _collect_rewrites(self, start: int, end: int) -> list[int]:
        """收集本批次需要重写的章节"""
        rewrites = []
        for ch in range(start, end + 1):
            review_file = self.config.reviews_dir / f"chapter_{ch:04d}_review.json"
            if not review_file.exists():
                continue
            try:
                with open(review_file, "r", encoding="utf-8") as f:
                    review = json.load(f)
                score = review.get("overall_score", 10)
                verdict = review.get("verdict", "")
                threshold = self.config.reviewer.rewrite_threshold
                if verdict == "需重写" or score < threshold:
                    rewrites.append(ch)
                    self.logger.info(f"第{ch}章评分{score}，标记为需重写")
            except Exception:
                pass
        return rewrites

    # ------------------------------------------------------------------
    # 报告
    # ------------------------------------------------------------------
    def generate_summary(self) -> dict:
        drafts = self._get_completed_drafts()
        reviews = self._get_completed_reviews()
        total_words = 0
        scores = []
        rewrite_count = 0

        for ch in drafts:
            # 优先 final，其次 draft
            final_file = self.config.final_dir / f"chapter_{ch:04d}.txt"
            draft_file = self.config.draft_dir / f"chapter_{ch:04d}.txt"
            src = final_file if final_file.exists() else draft_file
            if src.exists():
                text = src.read_text(encoding="utf-8")
                total_words += len(text)

            review_file = self.config.reviews_dir / f"chapter_{ch:04d}_review.json"
            if review_file.exists():
                try:
                    with open(review_file, "r", encoding="utf-8") as f:
                        review = json.load(f)
                    score = review.get("overall_score", 0)
                    scores.append(score)
                    if score < self.config.reviewer.rewrite_threshold or review.get("verdict") == "需重写":
                        rewrite_count += 1
                except Exception:
                    pass

        report = {
            "title": self.config.title,
            "total_chapters": len(drafts),
            "target_chapters": self.config.total_chapters,
            "total_words": total_words,
            "average_words_per_chapter": total_words // len(drafts) if drafts else 0,
            "average_score": sum(scores) / len(scores) if scores else 0,
            "rewrite_count": rewrite_count,
            "reviewed_count": len(reviews),
            "final_count": self._count_final(),
        }

        with open(self.config.summary_file, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        self.logger.info(f"总结报告: {report['total_chapters']}/{self.config.total_chapters} 章, "
                         f"{report['total_words']:,} 字, 平均评分 {report['average_score']:.2f}, "
                         f"需重写 {rewrite_count} 章")
        return report

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def run(self, skip_planner: bool = False, skip_review: bool = False, rewrite_only: bool = False):
        self.logger.info("=" * 60)
        self.logger.info(f"  小说生成协调器启动: {self.config.title}")
        self.logger.info("=" * 60)

        self._start_progress_pusher()
        self.logger.info(self.quota.format_report())

        # 阶段1: Planner
        if not skip_planner and not self.progress.get("planner_done"):
            if not self.config.world_file.exists() or not self.config.outline_file.exists():
                self.logger.info("检测到必要文件缺失，启动 Planner...")
                if self.run_planner():
                    self.progress["planner_done"] = True
                    self._save_progress()
                else:
                    self.logger.error("Planner 执行失败")
                    return
        else:
            self.progress["planner_done"] = True
            self._save_progress()

        # 多媒体：生成封面、主题曲、预告片
        if self._media_gens:
            self._start_media_worker()
            self._generate_cover_and_trailer()

        if rewrite_only:
            self._auto_rewrite()
            self.generate_summary()
            return

        # 解析大纲实际章节数
        actual_outline = self.config.total_chapters
        if self.config.outline_file.exists():
            try:
                with open(self.config.outline_file, "r", encoding="utf-8") as f:
                    outline = json.load(f)
                actual_outline = max(ch.get("chapter_number", 0) for ch in outline.get("chapters", [])) if outline.get("chapters") else self.config.total_chapters
            except Exception:
                pass

        end_chapter = min(self.config.total_chapters, actual_outline)
        batch_size = self.config.coordinator.batch_size
        start_chapter = max(1, self.progress.get("last_generated_chapter", 0) + 1)

        self.logger.info(f"生成范围: 第 {start_chapter}-{end_chapter} 章, 批次大小: {batch_size}")

        # 阶段2: Writer + Reviewer 循环
        for batch_start in range(start_chapter, end_chapter + 1, batch_size):
            batch_end = min(batch_start + batch_size - 1, end_chapter)
            self.logger.info(f"===== 开始第 {batch_start}-{batch_end} 章 =====")

            # 配额预检查
            self._check_quota_before_batch(batch_end - batch_start + 1)

            # Writer
            chapters = list(range(batch_start, batch_end + 1))
            num_w = self.config.coordinator.num_workers
            _, writer_failed = self._run_work_queue("Writer", chapters, self.run_writer, num_workers=num_w)
            if writer_failed:
                self.logger.warning(f"Writer 失败: {writer_failed}")

            # 更新进度
            drafts = self._get_completed_drafts()
            if drafts:
                self.progress["last_generated_chapter"] = max(drafts)
            self._save_progress()

            # Reviewer
            if not skip_review:
                _, reviewer_failed = self._run_work_queue("Reviewer", chapters, self.run_reviewer, num_workers=num_w)
                if reviewer_failed:
                    self.logger.warning(f"Reviewer 失败: {reviewer_failed}")

                rewrites = self._collect_rewrites(batch_start, batch_end)
                self.progress["rewrite_queue"].extend(rewrites)
                self.progress["last_reviewed_chapter"] = batch_end
                self._save_progress()

            # 自动补全（如果开启）
            if self.config.coordinator.auto_fill_missing:
                self._auto_fill_missing(batch_start, batch_end)

            # 多媒体生成（后台异步，不阻塞主流程）
            if self._media_gens:
                for ch in range(batch_start, batch_end + 1):
                    self._enqueue_media(ch)
                self.logger.info(f"已加入多媒体队列: 第{batch_start}-{batch_end}章 (队列长度 {self._media_queue.qsize()})")

            # 定期总结
            if batch_start % (batch_size * 10) == 1 or batch_end == end_chapter:
                self.generate_summary()

            time.sleep(self.config.coordinator.pause_between_batches)

        # 阶段3: 重写
        if self.config.coordinator.auto_rewrite:
            self._auto_rewrite()

        # 等待多媒体队列处理完毕
        if self._media_gens and self._media_queue.qsize() > 0:
            self.logger.info(f"等待多媒体队列清空（剩余 {self._media_queue.qsize()} 项）...")
            self._media_queue.join()
            self.logger.info("多媒体队列已清空")

        # 最终总结
        report = self.generate_summary()
        self.logger.info("=" * 60)
        self.logger.info("全部任务完成")
        self.logger.info(f"总进度: {report['total_chapters']}/{self.config.total_chapters} 章")
        self.logger.info(f"总字数: {report['total_words']:,} 字")
        self.logger.info(f"平均评分: {report['average_score']:.2f}")
        self.logger.info("=" * 60)

        self.notifier.push(f"[{self.config.title}] 全部完成! {report['total_chapters']}/{self.config.total_chapters} 章, "
                           f"{report['total_words']:,} 字, 评分 {report['average_score']:.2f}")
        self._running = False

    # ------------------------------------------------------------------
    # CLI 入口
    # ------------------------------------------------------------------
    @classmethod
    def from_cli(cls):
        parser = argparse.ArgumentParser(description="小说生成统一主控")
        parser.add_argument("--config", required=True, help="项目配置文件路径 (config.json)")
        parser.add_argument("--skip-planner", action="store_true", help="跳过 Planner")
        parser.add_argument("--skip-review", action="store_true", help="跳过 Review")
        parser.add_argument("--rewrite-only", action="store_true", help="只运行重写")
        args = parser.parse_args()

        config = NovelConfig.load(args.config)
        coordinator = cls(config)
        try:
            coordinator.run(
                skip_planner=args.skip_planner,
                skip_review=args.skip_review,
                rewrite_only=args.rewrite_only,
            )
        except KeyboardInterrupt:
            coordinator.logger.info("用户中断，保存进度...")
            coordinator._save_progress()
            coordinator.logger.info("进度已保存，可断点续传")
        except Exception as e:
            coordinator.logger.error(f"发生异常: {e}")
            coordinator.logger.error(traceback.format_exc())
            coordinator._save_progress()


if __name__ == "__main__":
    NovelCoordinator.from_cli()
