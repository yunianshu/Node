#!/usr/bin/env python3
"""
Rewrite Agent - 重写Agent
根据Reviewer的审核意见，对初稿进行重写
初稿来源: chapters/draft/
重写输出: chapters/final/
"""

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))


import argparse
import concurrent.futures
import json
import os
import sys
import time
from pathlib import Path

from core.mmx_client import MmxError, call_mmx as call_mmx_client
from core.novel_config import configure_stdio, load_config
from core.push_notifier import push_stage_complete
from core.workflow_state import load_outline_chapter, outline_index_path, review_dir
from core.workflow_state import analyze_chapter_text, is_valid_chapter_text, load_review_status, read_text_length
from core.workflow_state import scan_chapter_status, write_status_file, highest_contiguous, report_path

configure_stdio()

NOVELS_DIR = None
DRAFT_DIR = None
FINAL_DIR = None
REVIEWS_DIR = None
WORLD_FILE = None
OUTLINE_FILE = None
CHARACTERS_FILE = None
LOG_FILE = None
CONFIG = None
MAX_REWRITE_ATTEMPTS = 5


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, DRAFT_DIR, FINAL_DIR, REVIEWS_DIR, WORLD_FILE, OUTLINE_FILE, CHARACTERS_FILE, LOG_FILE, CONFIG
    NOVELS_DIR = Path(project_dir).resolve()
    DRAFT_DIR = NOVELS_DIR / "chapters" / "draft"
    FINAL_DIR = NOVELS_DIR / "chapters" / "final"
    REVIEWS_DIR = review_dir(NOVELS_DIR)
    WORLD_FILE = NOVELS_DIR / "world.json"
    OUTLINE_FILE = outline_index_path(NOVELS_DIR)
    CHARACTERS_FILE = NOVELS_DIR / "characters.json"
    LOG_FILE = NOVELS_DIR / "logs" / "rewrite_agent.log"
    CONFIG = load_config(NOVELS_DIR)


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def call_mmx(system_prompt: str, user_prompt: str, max_tokens: int = 8192, temperature: float = 0.7) -> str:
    try:
        return call_mmx_client(
            system_prompt,
            user_prompt,
            model=CONFIG["model"],
            mmx_path=CONFIG["mmx_path"],
            max_tokens=max_tokens,
            temperature=temperature,
            retries=CONFIG["writer"]["max_retries"],
            retry_delay=CONFIG["writer"]["retry_delay"],
            log_dir=NOVELS_DIR / "logs" / "raw_responses",
            raw_name="rewrite",
            qps=CONFIG["api_qps"],
            rate_state_dir=NOVELS_DIR / "logs" / "rate_limit",
        )
    except MmxError as e:
        log(f"[ERROR] mmx调用失败: {e}")
        return ""


def load_json(filepath: Path) -> dict:
    if not filepath.exists():
        return {}
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def rewrite_attempt_dir(chapter_number: int) -> Path:
    return NOVELS_DIR / "chapters" / "rewrite" / f"chapter_{chapter_number:04d}"


def score_rewrite_attempt(content: str) -> dict:
    word_count, grade, passed, issues = analyze_chapter_text(content)
    score = 10.0 if passed else 0.0
    if not passed:
        if word_count >= 4500:
            score += 5.0
        elif word_count > 0:
            score += min(4.0, word_count / 4500 * 4.0)
        if grade == "warn":
            score += 2.0
        score -= min(3.0, len(issues) * 0.5)
    return {
        "word_count": word_count,
        "grade": grade,
        "passed": passed,
        "issues": issues,
        "score": round(max(0.0, min(10.0, score)), 2),
    }


def save_rewrite_attempt(chapter_number: int, attempt: int, content: str, review: dict) -> dict:
    target_dir = rewrite_attempt_dir(chapter_number)
    target_dir.mkdir(parents=True, exist_ok=True)
    text_file = target_dir / f"attempt_{attempt:02d}.txt"
    review_file = target_dir / f"attempt_{attempt:02d}_review.json"
    text_file.write_text(content, encoding="utf-8")
    review_file.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "attempt": attempt,
        "content_file": str(text_file),
        "review_file": str(review_file),
        **review,
    }


def select_best_attempt(attempts: list[dict]) -> dict | None:
    if not attempts:
        return None
    passed = [item for item in attempts if item.get("passed")]
    candidates = passed or attempts
    return max(candidates, key=lambda item: (item.get("score", 0), item.get("word_count", 0)))


def rewrite_chapter(chapter_number: int, retry: int = 0) -> str:
    draft_file = DRAFT_DIR / f"chapter_{chapter_number:04d}.txt"
    final_file = FINAL_DIR / f"chapter_{chapter_number:04d}.txt"
    review_file = REVIEWS_DIR / f"chapter_{chapter_number:04d}_review.json"

    final_exists, final_words, final_ok = read_text_length(final_file)
    _, _, _, review_ok = load_review_status(review_file)
    if final_exists and final_ok and review_ok:
        log(f"[Rewrite] 第{chapter_number}章终稿已存在且质量门通过（{final_words}字），跳过")
        return "exists"

    if not draft_file.exists():
        log(f"[Rewrite] 第{chapter_number}章初稿不存在，无法重写")
        return "no_draft"

    if not review_file.exists():
        log(f"[Rewrite] 第{chapter_number}章无审查报告，跳过")
        return "no_review"

    review_data = load_json(review_file)
    verdict = review_data.get("verdict", "")
    score = review_data.get("overall_score", 10)

    if verdict != "需重写" and score >= 7:
        _, draft_words, draft_ok = read_text_length(draft_file)
        if not draft_ok:
            log(f"[Rewrite] 第{chapter_number}章评分{score}但初稿字数不合格（{draft_words}字），进入重写")
        else:
            log(f"[Rewrite] 第{chapter_number}章评分{score}无需重写，直接复制初稿到终稿")
            content = draft_file.read_text(encoding="utf-8")
            FINAL_DIR.mkdir(parents=True, exist_ok=True)
            with open(final_file, "w", encoding="utf-8") as f:
                f.write(content)
            return "copied"

    world = load_json(WORLD_FILE)
    characters = load_json(CHARACTERS_FILE)

    chapter_outline = load_outline_chapter(NOVELS_DIR, chapter_number)

    if not chapter_outline:
        log(f"[Rewrite] 第{chapter_number}章大纲不存在")
        return "no_outline"

    prev_summary = load_outline_chapter(NOVELS_DIR, chapter_number - 1).get("summary", "")
    next_summary = load_outline_chapter(NOVELS_DIR, chapter_number + 1).get("summary", "")

    prev_ending = ""
    if chapter_number > 1:
        prev_final = FINAL_DIR / f"chapter_{chapter_number-1:04d}.txt"
        prev_draft = DRAFT_DIR / f"chapter_{chapter_number-1:04d}.txt"
        prev_file = prev_final if prev_final.exists() else prev_draft
        if prev_file.exists():
            with open(prev_file, "r", encoding="utf-8") as f:
                content = f.read()
            prev_ending = content[-500:] if len(content) > 500 else content

    draft_content = draft_file.read_text(encoding="utf-8")

    suggestions = review_data.get("suggestions", [])
    continuity_issues = review_data.get("continuity_issues", [])
    strengths = review_data.get("strengths", [])
    weaknesses = review_data.get("weaknesses", [])
    scores = review_data.get("scores", {})

    review_section = "\n\n## 编辑审查反馈（请严格参考以下建议重写）\n"

    if strengths:
        review_section += "\n### 原文优点（请务必保留）\n"
        for s in strengths:
            review_section += f"- {s}\n"

    if weaknesses:
        review_section += "\n### 原文不足（必须改进）\n"
        for w in weaknesses:
            review_section += f"- {w}\n"

    if suggestions:
        review_section += "\n### 具体修改建议（必须落实）\n"
        for s in suggestions:
            review_section += f"- {s}\n"

    if continuity_issues and continuity_issues[0] != "与前文不一致之处（如有）":
        review_section += "\n### 连续性问题（必须修正）\n"
        for c in continuity_issues:
            if c and c != "与前文不一致之处（如有）":
                review_section += f"- {c}\n"

    if scores:
        review_section += "\n### 各维度评分\n"
        for dim, score_val in scores.items():
            review_section += f"- {dim}: {score_val}分\n"

    review_section += f"\n### 原文参考（前1000字，了解原有风格）\n{draft_content[:1000]}\n...\n"
    review_section += f"\n### 原文参考（结尾500字，保持结尾走向）\n{draft_content[-500:] if len(draft_content) > 500 else draft_content}\n"

    system = """你是一位顶尖的中文网络小说作家，同时也是资深编辑。
你现在需要对一篇初稿进行**重写升级**，而不是从零创作。
重写原则：
1. 保留原文的优点和核心情节框架，不要完全推倒重来
2. 严格落实验编给出的具体修改建议，每一条都要落实
3. 修正连续性问题和逻辑漏洞
4. 弥补文笔、节奏、对话等方面的不足
5. 每章必须4500-5500字，这是硬性要求
6. 保持角色性格一致性，前后情节衔接自然
7. 文笔要比原文更加流畅、细腻、有张力
8. 重写后的内容应当是原文的全面提升版"""

    prompt = f"""请根据以下信息，重写第{chapter_number}章《{chapter_outline.get('title', '未命名')}》。

## 世界观背景
{json.dumps(world, ensure_ascii=False, indent=2)[:1500]}

## 角色信息
{json.dumps(characters, ensure_ascii=False, indent=2)[:1500]}

## 本章大纲（必须严格遵循）
{json.dumps(chapter_outline, ensure_ascii=False, indent=2)}

## 前一章摘要（用于衔接）
{prev_summary}

## 前一章结尾（用于衔接）
{prev_ending[:300]}

## 后一章摘要（为后续铺垫）
{next_summary}{review_section}

## 写作要求
1. 本章字数必须在4500-5500字之间，绝对不可低于4500字
2. 严格按照大纲核心事件展开，不要遗漏任何情节点
3. 开头要自然衔接前一章，结尾要留悬念或引出下一章
4. 对话要符合角色性格，推动情节发展，对话要详细具体
5. 场景描写要生动细致，让读者有画面感，不要一笔带过
6. 战斗/冲突场面要紧张刺激，有层次感
7. 心理描写要细腻，展现主角内心变化
8. 不要流水账，要有起伏和转折
9. 不要输出章节标题，直接从正文开始
10. 不要输出任何元信息，只输出正文

请开始重写："""

    attempts = []
    for attempt in range(1, MAX_REWRITE_ATTEMPTS + 1):
        log(f"[Rewrite] 正在重写第{chapter_number}章（第{attempt}/{MAX_REWRITE_ATTEMPTS}次，初稿{len(draft_content)}字，评分{score}）...")
        content = call_mmx(system, prompt, max_tokens=12000, temperature=0.75)

        if not content:
            log(f"[Rewrite] 第{chapter_number}章第{attempt}次重写失败，无返回内容")
            time.sleep(CONFIG["writer"]["retry_delay"])
            continue

        content = content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            content = "\n".join(lines).strip()

        word_count = len(content)

        if word_count < 4500:
            log(f"[Rewrite] 第{chapter_number}章第{attempt}次字数不足({word_count}字)，尝试补充...")
            supplement_prompt = f"""以下是一章小说的内容，但字数只有{word_count}字，需要扩展到至少4500字。
请在保持原有情节和风格的基础上，通过以下方式扩充：
1. 增加环境描写的细节
2. 扩展对话内容，让对话更完整
3. 增加角色的心理活动和内心独白
4. 增加场景转换的过渡描写
5. 增加侧面描写和氛围渲染

请直接在原文基础上扩充，输出完整的4500字以上版本：

{content}"""
            supplement = call_mmx(system, supplement_prompt, max_tokens=12000, temperature=0.7)
            if supplement and len(supplement) > word_count:
                content = supplement.strip()
                word_count = len(content)
                log(f"[Rewrite] 第{chapter_number}章第{attempt}次扩充后{word_count}字")

        attempt_review = score_rewrite_attempt(content)
        attempts.append(save_rewrite_attempt(chapter_number, attempt, content, attempt_review))
        log(
            f"[Rewrite] 第{chapter_number}章第{attempt}次评分{attempt_review['score']}，"
            f"字数{attempt_review['word_count']}，通过={attempt_review['passed']}"
        )
        if attempt_review["passed"]:
            break
        time.sleep(CONFIG["writer"]["retry_delay"])

    best = select_best_attempt(attempts)
    if not best:
        log(f"[Rewrite] 第{chapter_number}章重写失败，{MAX_REWRITE_ATTEMPTS}次均无有效内容")
        return "failed"

    selected_content = Path(best["content_file"]).read_text(encoding="utf-8")
    FINAL_DIR.mkdir(parents=True, exist_ok=True)
    final_file.write_text(selected_content, encoding="utf-8")

    meta = {
        "chapter": chapter_number,
        "max_attempts": MAX_REWRITE_ATTEMPTS,
        "selected_attempt": best["attempt"],
        "selected_reason": "quality_passed" if best.get("passed") else "best_score_fallback",
        "attempts": attempts,
    }
    meta_file = rewrite_attempt_dir(chapter_number) / "rewrite_meta.json"
    meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    log(
        f"[Rewrite] 第{chapter_number}章终稿已保存（选用第{best['attempt']}次，"
        f"评分{best.get('score')}，{best.get('word_count')}字） -> {final_file}"
    )
    return "success" if best.get("passed") else "best_effort"


def _refresh_status(start: int, end: int) -> None:
    """扫描处理过的章节，增量更新 chapter_status.json 和 progress.json"""
    try:
        log(f"[Rewrite] 刷新状态文件 ({start}-{end})...")
        statuses = scan_chapter_status(NOVELS_DIR, start, end, use_cache=False)
        write_status_file(NOVELS_DIR, statuses.values())

        progress_file = report_path(NOVELS_DIR, "progress.json")
        if progress_file.exists():
            with open(progress_file, "r", encoding="utf-8") as f:
                progress = json.load(f)
        else:
            progress = {
                "planner_done": True,
                "last_generated_chapter": 0,
                "last_reviewed_chapter": 0,
                "failed_chapters": [],
                "rewrite_queue": [],
            }

        all_statuses = scan_chapter_status(NOVELS_DIR, 1, CONFIG["total_chapters"], use_cache=False)
        progress["last_reviewed_chapter"] = highest_contiguous(all_statuses, 1, "review_ok")

        # 清理 rewrite_queue：移除已有终稿的章节
        queue = set(progress.get("rewrite_queue", []))
        for ch in range(start, end + 1):
            key = f"{ch:04d}"
            s = all_statuses.get(ch)
            if s and s.final_ok and ch in queue:
                queue.discard(ch)
        progress["rewrite_queue"] = sorted(queue)

        progress_file.parent.mkdir(parents=True, exist_ok=True)
        with open(progress_file, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)

        log(f"[Rewrite] 状态刷新完成，last_reviewed_chapter={progress['last_reviewed_chapter']}, rewrite_queue={len(progress['rewrite_queue'])}")
    except Exception as e:
        log(f"[Rewrite] 状态刷新失败: {e}")


def _get_book_title() -> str:
    title = "书生武道通神"
    if WORLD_FILE.exists():
        try:
            with open(WORLD_FILE, "r", encoding="utf-8") as f:
                title = json.load(f).get("title", title)
        except Exception:
            pass
    return title


def get_rewrite_candidates() -> list:
    candidates = []
    if not REVIEWS_DIR.exists():
        return candidates

    for review_file in REVIEWS_DIR.glob("chapter_*_review.json"):
        try:
            num = int(review_file.stem.split("_")[1])
            review = load_json(review_file)
            verdict = review.get("verdict", "")
            score = review.get("overall_score", 10)
            if verdict == "需重写" or score < 7:
                candidates.append((num, score, verdict))
        except Exception:
            continue

    candidates.sort(key=lambda x: x[0])
    return candidates


def main():
    parser = argparse.ArgumentParser(description="根据Reviewer意见重写章节")
    parser.add_argument("--project", "-p", type=str,
                        default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录")
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=0, help="结束章节")
    parser.add_argument("--chapter", type=int, default=0, help="只重写某一章")
    parser.add_argument("--candidates", action="store_true", help="只列出需要重写的章节")
    parser.add_argument("--workers", type=int, default=0, help="并行重写Agent数量")
    parser.add_argument("--all", action="store_true", help="处理所有章节（含直接复制）")
    args = parser.parse_args()

    if not args.project:
        print("错误: 必须指定 --project 或设置 NOVEL_PROJECT_DIR 环境变量")
        sys.exit(1)

    init_project(args.project)
    end = args.end or CONFIG["total_chapters"]
    workers = args.workers or CONFIG["coordinator"]["num_workers"]

    print("=" * 60)
    print("Rewrite Agent 启动")
    print(f"项目: {NOVELS_DIR}")
    print("=" * 60)

    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    FINAL_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    if args.candidates:
        candidates = get_rewrite_candidates()
        print(f"需要重写的章节: {len(candidates)} 个")
        for num, score, verdict in candidates:
            print(f"  第{num}章: 评分{score} [{verdict}]")
        return

    title = _get_book_title()

    if args.chapter > 0:
        result = rewrite_chapter(args.chapter)
        _refresh_status(args.chapter, args.chapter)
        push_stage_complete(
            config=CONFIG, title=title, stage="终稿",
            start_chapter=args.chapter, end_chapter=args.chapter,
            processed=1 if result in ("success", "best_effort", "copied", "exists") else 0,
            failed=1 if result == "failed" else 0,
        )
        return

    if args.all:
        chapters_to_process = []
        for ch in range(args.start, end + 1):
            review_file = REVIEWS_DIR / f"chapter_{ch:04d}_review.json"
            if review_file.exists():
                review = load_json(review_file)
                score = review.get("overall_score", 10)
                verdict = review.get("verdict", "")
                if verdict == "需重写" or score < 7:
                    chapters_to_process.append((ch, "rewrite"))
                else:
                    chapters_to_process.append((ch, "copy"))
            else:
                draft_file = DRAFT_DIR / f"chapter_{ch:04d}.txt"
                if draft_file.exists():
                    chapters_to_process.append((ch, "copy"))

        print(f"共需处理 {len(chapters_to_process)} 章（重写 + 复制）")

        completed = 0
        failed = []

        def process_single(args_tuple):
            ch, action = args_tuple
            if action == "copy":
                draft_file = DRAFT_DIR / f"chapter_{ch:04d}.txt"
                final_file = FINAL_DIR / f"chapter_{ch:04d}.txt"
                if draft_file.exists() and (not final_file.exists() or final_file.stat().st_size < 1000):
                    content = draft_file.read_text(encoding="utf-8")
                    FINAL_DIR.mkdir(parents=True, exist_ok=True)
                    with open(final_file, "w", encoding="utf-8") as f:
                        f.write(content)
                    log(f"[Rewrite] 第{ch}章直接复制到终稿（{len(content)}字）")
                    return ch, "copied"
                return ch, "exists"
            else:
                result = rewrite_chapter(ch)
                return ch, result

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(process_single, item): item for item in chapters_to_process}
            for future in concurrent.futures.as_completed(futures):
                ch, result = future.result()
                if result in ("success", "best_effort", "copied"):
                    completed += 1
                elif result == "failed":
                    failed.append(ch)
                print(f"进度: {completed}/{len(chapters_to_process)} 完成, 失败: {len(failed)}")

        log(f"[Rewrite] 全部完成: 成功{completed}章, 失败{len(failed)}章")
        if failed:
            log(f"失败章节: {failed}")
        _refresh_status(args.start, end)
        push_stage_complete(
            config=CONFIG, title=title, stage="终稿",
            start_chapter=args.start, end_chapter=end,
            processed=completed, failed=len(failed),
        )
        return

    candidates = get_rewrite_candidates()
    print(f"需要重写的章节: {len(candidates)} 个")
    for num, score, verdict in candidates:
        print(f"  第{num}章: 评分{score} [{verdict}]")

    if not candidates:
        print("没有需要重写的章节")
        return

    completed = 0
    failed = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(rewrite_chapter, num): num for num, _, _ in candidates}
        for future in concurrent.futures.as_completed(futures):
            ch = futures[future]
            try:
                result = future.result()
                if result in ("success", "best_effort"):
                    completed += 1
                elif result == "failed":
                    failed.append(ch)
            except Exception as exc:
                log(f"[ERROR] 第{ch}章异常: {exc}")
                failed.append(ch)

    log(f"[Rewrite] 完成: 成功{completed}章, 失败{len(failed)}章")
    if failed:
        log(f"失败: {failed}")

    # 刷新状态：使用 candidates 的章节范围
    if candidates:
        ch_nums = [num for num, _, _ in candidates]
        _refresh_status(min(ch_nums), max(ch_nums))
        push_stage_complete(
            config=CONFIG, title=title, stage="终稿重写",
            start_chapter=min(ch_nums), end_chapter=max(ch_nums),
            processed=completed, failed=len(failed),
        )


if __name__ == "__main__":
    main()
