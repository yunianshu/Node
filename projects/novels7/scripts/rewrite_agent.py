#!/usr/bin/env python3
"""
Rewrite Agent - 重写Agent (novels7版)
根据Reviewer的审核意见，对初稿进行重写
初稿来源: output/chapters/draft/
重写输出: output/chapters/final/
"""
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from novels.core.config import NovelConfig
from novels.core.llm_client import LLMClient
from novels.core.logger import get_logger


PROJECT_DIR = Path("D:/AiProject/Node/projects/novels7")
DRAFT_DIR = PROJECT_DIR / "output/chapters/draft"
FINAL_DIR = PROJECT_DIR / "output/chapters/final"
REVIEWS_DIR = PROJECT_DIR / "output/reviews"
WORLD_FILE = PROJECT_DIR / "output/world.json"
OUTLINE_FILE = PROJECT_DIR / "output/outline.json"
CHARACTERS_FILE = PROJECT_DIR / "output/characters.json"
LOG_FILE = PROJECT_DIR / "logs/rewrite_agent.log"


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_json(filepath: Path) -> dict:
    if not filepath.exists():
        return {}
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def rewrite_chapter(chapter_number: int, client: LLMClient, config: NovelConfig,
                    world_data: dict, outline: dict, characters: dict,
                    retry: int = 0) -> str:
    """根据Reviewer意见重写单个章节"""
    draft_file = DRAFT_DIR / f"chapter_{chapter_number:04d}.txt"
    final_file = FINAL_DIR / f"chapter_{chapter_number:04d}.txt"
    review_file = REVIEWS_DIR / f"chapter_{chapter_number:04d}_review.json"

    if final_file.exists() and final_file.stat().st_size > 1000:
        log(f"[Rewrite] 第{chapter_number}章终稿已存在，跳过")
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

    # 只重写评分低于7分或标记为"需重写"的章节
    if verdict != "需重写" and score >= 7:
        log(f"[Rewrite] 第{chapter_number}章评分{score}无需重写，直接复制初稿到终稿")
        content = draft_file.read_text(encoding="utf-8")
        FINAL_DIR.mkdir(parents=True, exist_ok=True)
        with open(final_file, "w", encoding="utf-8") as f:
            f.write(content)
        return "copied"

    chapter_outline = None
    for ch in outline.get("chapters", []):
        if ch.get("chapter_number") == chapter_number:
            chapter_outline = ch
            break

    if not chapter_outline:
        log(f"[Rewrite] 第{chapter_number}章大纲不存在")
        return "no_outline"

    prev_summary = ""
    next_summary = ""
    for ch in outline.get("chapters", []):
        if ch.get("chapter_number") == chapter_number - 1:
            prev_summary = ch.get("summary", "")
        if ch.get("chapter_number") == chapter_number + 1:
            next_summary = ch.get("summary", "")

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

    user = f"""请根据以下信息，重写第{chapter_number}章《{chapter_outline.get('title', '未命名')}》。

## 世界观背景
{json.dumps(world_data, ensure_ascii=False, indent=2)[:1500]}

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

    log(f"[Rewrite] 正在重写第{chapter_number}章（初稿{len(draft_content)}字，评分{score}）...")
    content = client.call(system_prompt=system, user_prompt=user,
                          max_tokens=12000, temperature=0.75)

    if not content:
        if retry < 3:
            log(f"[Rewrite] 第{chapter_number}章重写失败，重试({retry+1}/3)...")
            time.sleep(5)
            return rewrite_chapter(chapter_number, client, config, world_data, outline, characters, retry + 1)
        log(f"[Rewrite] 第{chapter_number}章重写失败，已达最大重试次数")
        return "failed"

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
        log(f"[Rewrite] 第{chapter_number}章字数不足({word_count}字)，尝试补充...")
        supplement_prompt = f"""以下是一章小说的内容，但字数只有{word_count}字，需要扩展到至少4500字。
请在保持原有情节和风格的基础上，通过以下方式扩充：
1. 增加环境描写的细节
2. 扩展对话内容，让对话更完整
3. 增加角色的心理活动和内心独白
4. 增加场景转换的过渡描写
5. 增加侧面描写和氛围渲染

请直接在原文基础上扩充，输出完整的4500字以上版本：

{content}"""
        supplement = client.call(system_prompt=system, user_prompt=supplement_prompt,
                                 max_tokens=12000, temperature=0.7)
        if supplement and len(supplement) > word_count:
            content = supplement.strip()
            word_count = len(content)
            log(f"[Rewrite] 第{chapter_number}章扩充后{word_count}字")

    FINAL_DIR.mkdir(parents=True, exist_ok=True)
    with open(final_file, "w", encoding="utf-8") as f:
        f.write(content)

    log(f"[Rewrite] 第{chapter_number}章终稿已保存（{word_count}字）")
    return "success"


def get_rewrite_candidates() -> list:
    """获取需要重写的章节列表"""
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
    print("=" * 60)
    print("Rewrite Agent 启动")
    print("=" * 60)

    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    FINAL_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    import argparse
    parser = argparse.ArgumentParser(description="根据Reviewer意见重写章节")
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=2000, help="结束章节")
    parser.add_argument("--chapter", type=int, default=0, help="只重写某一章")
    parser.add_argument("--candidates", action="store_true", help="只列出需要重写的章节")
    parser.add_argument("--workers", type=int, default=20, help="并行重写Agent数量")
    parser.add_argument("--all", action="store_true", help="处理所有章节（含直接复制）")
    args = parser.parse_args()

    config_path = PROJECT_DIR / "config.json"
    config = NovelConfig.load(config_path)
    client = LLMClient(
        mmx_path=config.mmx_path,
        model=config.model,
        qps=config.api_qps,
        log_dir=str(config.logs_dir)
    )

    world_data = load_json(WORLD_FILE)
    outline = load_json(OUTLINE_FILE)
    characters = load_json(CHARACTERS_FILE)

    if args.candidates:
        candidates = get_rewrite_candidates()
        print(f"需要重写的章节: {len(candidates)} 个")
        for num, score, verdict in candidates:
            print(f"  第{num}章: 评分{score} [{verdict}]")
        return

    if args.chapter > 0:
        rewrite_chapter(args.chapter, client, config, world_data, outline, characters)
        return

    if args.all:
        chapters_to_process = []
        for ch in range(args.start, args.end + 1):
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
                result = rewrite_chapter(ch, client, config, world_data, outline, characters)
                return ch, result

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(process_single, item): item for item in chapters_to_process}
            for future in concurrent.futures.as_completed(futures):
                ch, result = future.result()
                if result in ("success", "copied"):
                    completed += 1
                elif result == "failed":
                    failed.append(ch)
                print(f"进度: {completed}/{len(chapters_to_process)} 完成, 失败: {len(failed)}")

        log(f"[Rewrite] 全部完成: 成功{completed}章, 失败{len(failed)}章")
        if failed:
            log(f"失败章节: {failed}")
        return

    # 默认模式：只重写评分低的章节
    candidates = get_rewrite_candidates()
    print(f"需要重写的章节: {len(candidates)} 个")
    for num, score, verdict in candidates:
        print(f"  第{num}章: 评分{score} [{verdict}]")

    if not candidates:
        print("没有需要重写的章节")
        return

    completed = 0
    failed = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(rewrite_chapter, num, client, config, world_data, outline, characters): num for num, _, _ in candidates}
        for future in concurrent.futures.as_completed(futures):
            ch = futures[future]
            try:
                result = future.result()
                if result == "success":
                    completed += 1
                elif result == "failed":
                    failed.append(ch)
            except Exception as exc:
                log(f"[ERROR] 第{ch}章异常: {exc}")
                failed.append(ch)

    log(f"[Rewrite] 完成: 成功{completed}章, 失败{len(failed)}章")
    if failed:
        log(f"失败: {failed}")


if __name__ == "__main__":
    main()
