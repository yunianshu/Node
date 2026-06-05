#!/usr/bin/env python3
"""
Reviewer Agent - 审查Agent
负责审查章节质量并输出评分报告
"""

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))


import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from core.mmx_client import MmxError, call_mmx as call_mmx_client
from core.novel_config import configure_stdio, load_config, load_origin_materials
# 微信推送已禁用，改由 coordinator 统一推送进度
# from core.push_notifier import push_stage_complete
from core.workflow_state import (
    load_outline_chapter, load_review_status, outline_index_path, review_dir,
    scan_chapter_status, write_status_file, highest_contiguous, report_path,
)

configure_stdio()

NOVELS_DIR = None
CHAPTERS_DIR = None
OUTLINE_FILE = None
CHARACTERS_FILE = None
WORLD_FILE = None
REVIEWS_DIR = None
LOG_FILE = None
CONFIG = None
ORIGIN_MATERIALS = ""


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, CHAPTERS_DIR, OUTLINE_FILE, CHARACTERS_FILE, WORLD_FILE, REVIEWS_DIR, LOG_FILE, CONFIG, ORIGIN_MATERIALS
    NOVELS_DIR = Path(project_dir).resolve()
    CHAPTERS_DIR = NOVELS_DIR / "chapters" / "draft"
    CHARACTERS_FILE = NOVELS_DIR / "characters.json"
    WORLD_FILE = NOVELS_DIR / "world.json"
    OUTLINE_FILE = outline_index_path(NOVELS_DIR)
    REVIEWS_DIR = review_dir(NOVELS_DIR)
    LOG_FILE = NOVELS_DIR / "logs" / "reviewer.log"
    CONFIG = load_config(NOVELS_DIR)
    review_cfg = CONFIG.get("reviewer", {})
    ORIGIN_MATERIALS = load_origin_materials(
        NOVELS_DIR,
        max_chars=int(review_cfg.get("origin_max_chars", 4000) or 4000),
    )


def analyze_chapter_text(chapter_content: str) -> dict:
    text = chapter_content.strip()
    length = len(text)
    paragraph_count = len([p for p in text.splitlines() if p.strip()])
    dialogue_count = text.count("\u201c") + text.count('"')
    issues = []

    quality = CONFIG.get("quality", {}) if CONFIG else {}
    min_words = int(quality.get("min_chapter_words", 5000))
    max_words = int(quality.get("max_chapter_words", 12000))
    hard_fail_min = int(quality.get("hard_fail_min_chapter_words", 3000))
    warn_min = int(quality.get("warn_min_chapter_words", min_words - 200))
    warn_max = int(quality.get("warn_max_chapter_words", max_words + 3000))

    if length < warn_min:
        issues.append(f"字数低于{warn_min}字，当前{length}字")
    if length > warn_max:
        issues.append(f"字数超过{warn_max}字，当前{length}字")
    if paragraph_count < 20:
        issues.append(f"段落数量偏少，当前{paragraph_count}段")
    if dialogue_count < 4:
        issues.append("对话标记偏少，可能缺少角色互动")
    return {
        "word_count": length,
        "word_count_ok": min_words <= length <= max_words,
        "paragraph_count": paragraph_count,
        "dialogue_marker_count": dialogue_count,
        "issues": issues,
        "local_ok": not issues,
    }


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def call_mmx(system_prompt: str, user_prompt: str, max_tokens: int = 4096, temperature: float = 0.3) -> str:
    try:
        cfg = CONFIG.get("reviewer", {})
        return call_mmx_client(
            system_prompt,
            user_prompt,
            model=CONFIG["model"],
            mmx_path=CONFIG["mmx_path"],
            max_tokens=cfg.get("max_tokens", max_tokens),
            temperature=cfg.get("temperature", temperature),
            retries=CONFIG["writer"]["max_retries"],
            retry_delay=CONFIG["writer"]["retry_delay"],
            log_dir=NOVELS_DIR / "logs" / "raw_responses",
            raw_name="reviewer",
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


def _parse_score(val):
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        val = val.strip()
        if "/" in val:
            num = val.split("/")[0].strip()
            try:
                return float(num)
            except ValueError:
                pass
        try:
            return float(val)
        except ValueError:
            pass
    return val


def _extract_json_text(content: str) -> str:
    if "```json" in content:
        return content.split("```json", 1)[1].split("```", 1)[0].strip()
    if "```" in content:
        return content.split("```", 1)[1].split("```", 1)[0].strip()
    return content.strip()


def _extract_string_field(content: str, field: str) -> str:
    match = re.search(rf'"{re.escape(field)}"\s*:\s*"([^"]*)"', content)
    return match.group(1).strip() if match else ""


def _extract_score_field(content: str, field: str):
    match = re.search(rf'"{re.escape(field)}"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?', content)
    return _parse_score(match.group(1)) if match else None


def _extract_array_items(content: str, field: str, limit: int = 3) -> list[str]:
    match = re.search(rf'"{re.escape(field)}"\s*:\s*\[(.*?)\]', content, re.S)
    if not match:
        return []
    items = re.findall(r'"([^"]+)"', match.group(1))
    return [item[:120] for item in items[:limit]]


def _partial_review_from_raw(chapter_number: int, content: str, local_analysis: dict) -> dict:
    score = _extract_score_field(content, "overall_score")
    verdict = _extract_string_field(content, "verdict") or "需修改"
    if score is None:
        return {
            "chapter_number": chapter_number,
            "status": "parse_error",
            "raw_response": content,
            "local_analysis": local_analysis,
        }
    return {
        "chapter_number": chapter_number,
        "status": "completed",
        "overall_score": score,
        "verdict": verdict,
        "scores": {},
        "strengths": _extract_array_items(content, "strengths"),
        "weaknesses": _extract_array_items(content, "weaknesses"),
        "suggestions": _extract_array_items(content, "suggestions"),
        "continuity_issues": _extract_array_items(content, "continuity_issues"),
        "summary": _extract_string_field(content, "summary") or "审查JSON被截断，已提取核心评分与意见。",
        "raw_response": content[:3000],
        "local_analysis": local_analysis,
    }


def review_chapter(chapter_number: int) -> dict:
    chapter_file = CHAPTERS_DIR / f"chapter_{chapter_number:04d}.txt"
    review_file = REVIEWS_DIR / f"chapter_{chapter_number:04d}_review.json"

    if not chapter_file.exists():
        log(f"[Reviewer] 第{chapter_number}章文件不存在")
        return {"status": "no_file"}

    if review_file.exists():
        _, status, _, ok = load_review_status(review_file)
        if ok:
            log(f"[Reviewer] 第{chapter_number}章已有有效审查报告，跳过")
            with open(review_file, "r", encoding="utf-8") as f:
                return json.load(f)
        log(f"[Reviewer] 第{chapter_number}章审查报告无效（{status}），重新审查")

    with open(chapter_file, "r", encoding="utf-8") as f:
        chapter_content = f.read()
    local_analysis = analyze_chapter_text(chapter_content)

    chapter_outline = load_outline_chapter(NOVELS_DIR, chapter_number)

    characters = load_json(CHARACTERS_FILE)

    content_sample = chapter_content[:1500]
    mid_start = max(0, len(chapter_content) // 2 - 500)
    content_sample += "\n\n[中间部分...]\n\n" + chapter_content[mid_start:mid_start + 800]
    content_sample += "\n\n[结尾部分...]\n\n" + chapter_content[-800:]

    world = load_json(WORLD_FILE)
    book_title = world.get("title", "本小说")
    world_desc = world.get("world_description", "")[:300]
    themes = world.get("themes", [])
    power_system = world.get("power_system", {})
    power_name = power_system.get("name", "")
    power_desc = power_system.get("description", "")[:200]

    genre_hints = []
    if themes:
        genre_hints.append(f"核心主题：{'; '.join(themes[:3])}")
    if power_name:
        genre_hints.append(f"力量体系：{power_name}（{power_desc}）")
    if world_desc:
        genre_hints.append(f"世界观：{world_desc}")
    genre_text = "\n".join(genre_hints) if genre_hints else "请根据世界观和角色设定判断题材类型。"

    quality = CONFIG.get("quality", {})
    review_min_score = float(CONFIG.get("reviewer", {}).get("min_score", 7.0))
    min_words = int(quality.get("min_chapter_words", 5000))
    max_words = int(quality.get("max_chapter_words", 12000))
    warn_min = int(quality.get("warn_min_chapter_words", min_words - 200))
    warn_max = int(quality.get("warn_max_chapter_words", max_words + 3000))

    system = f"""你是一位资深网络小说编辑，拥有20年审稿经验。
你需要从多个维度审查章节质量，并给出具体的修改建议。
本书是《{book_title}》。
{genre_text}
评分标准：9-10分优秀，8-9分良好，达到{review_min_score}分为通过，低于{review_min_score}分需重写。
优秀章节完全可以给出9分以上，请根据实际质量客观评分，不要人为压低分数。
输出必须是合法的紧凑JSON，不要使用Markdown代码块，不要输出JSON之外的任何文字。
审查意见要短而具体，整份JSON尽量控制在1200个中文字符以内。"""

    prompt = f"""请审查以下第{chapter_number}章的内容。

## 章节大纲
{json.dumps(chapter_outline, ensure_ascii=False, indent=2) if chapter_outline else '未找到大纲'}

## 角色设定
{json.dumps(characters, ensure_ascii=False, indent=2)[:1000]}

## origin/ 原始参考素材
{ORIGIN_MATERIALS or "（无）"}

## 章节内容（节选）
{content_sample}

## 章节字数
{len(chapter_content)}字

## 本地全文检查
{json.dumps(local_analysis, ensure_ascii=False, indent=2)}

请只输出以下JSON格式的审查报告，数组最多3条，每条不超过80字：
{{
  "chapter_number": {chapter_number},
  "overall_score": "请给出0-10的客观评分，质量优秀的章节可给9分以上",
  "verdict": "通过/需修改/需重写",
  "scores": {{
    "writing_quality": "文笔流畅度（0-10）",
    "plot_coherence": "剧情连贯性（0-10）",
    "character_consistency": "人物一致性（0-10）",
    "scene_description": "场景描写（0-10）",
    "dialogue_quality": "对话质量（0-10）",
    "outline_adherence": "大纲遵循度（0-10）",
    "pacing": "节奏把控（0-10）",
    "emotional_impact": "情感冲击力（0-10）"
  }},
  "word_count_check": {{
    "actual": {len(chapter_content)},
    "target": {min_words},
    "status": "达标/偏短/偏长"
  }},
  "strengths": ["优点1，80字以内"],
  "weaknesses": ["不足1，80字以内"],
  "suggestions": ["具体修改建议1，80字以内"],
  "continuity_issues": ["与前文不一致之处，80字以内"],
  "summary": "总体评价，80字以内"
}}

要求：
1. 评分要客观公正，质量优秀的章节完全可以给出9分以上，不要人为压低分数
2. 重点审查内容是否符合本书的世界观设定和角色性格
3. 剧情推进是否自然，有无逻辑漏洞或突兀转折
4. 对话是否符合角色身份和时代背景
5. 必须给出具体的修改建议，不能泛泛而谈，但每类最多3条
6. 如果 origin/ 中存在素材，必须检查正文是否参考并遵守原始素材；与素材冲突需列入 weaknesses 或 continuity_issues
7. 如低于{review_min_score}分必须标记为"需重写"
8. 字数不足{warn_min}或超过{warn_max}要标记字数问题
9. 必须输出合法JSON，不要Markdown，不要长篇解释"""

    log(f"[Reviewer] 正在审查第{chapter_number}章...")
    content = call_mmx(system, prompt, max_tokens=4096, temperature=0.3)

    if not content:
        log(f"[Reviewer] 第{chapter_number}章审查失败")
        review_data = {
            "chapter_number": chapter_number,
            "status": "failed",
            "local_analysis": local_analysis,
        }
        REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
        with open(review_file, "w", encoding="utf-8") as f:
            json.dump(review_data, f, ensure_ascii=False, indent=2)
        return review_data

    try:
        content = _extract_json_text(content)
        review_data = json.loads(content)
        review_data["status"] = "completed"
        review_data["local_analysis"] = local_analysis

        # 将 overall_score 统一转为 float，避免字符串类型导致 schema 校验失败
        review_data["overall_score"] = _parse_score(review_data.get("overall_score"))
        # 同时将 scores 子项也转为 float
        scores = review_data.get("scores")
        if isinstance(scores, dict):
            for k, v in list(scores.items()):
                scores[k] = _parse_score(v)
    except Exception as e:
        log(f"[Reviewer] JSON解析失败: {e}")
        review_data = _partial_review_from_raw(chapter_number, content, local_analysis)
        if review_data.get("status") == "completed":
            log(f"[Reviewer] 已从截断JSON中提取评分: {review_data.get('overall_score')}，verdict: {review_data.get('verdict')}")

    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    with open(review_file, "w", encoding="utf-8") as f:
        json.dump(review_data, f, ensure_ascii=False, indent=2)

    overall = review_data.get("overall_score", "N/A")
    verdict = review_data.get("verdict", "N/A")
    log(f"[Reviewer] 第{chapter_number}章审查完成，评分: {overall}， verdict: {verdict}")
    return review_data


def _refresh_status(start: int, end: int) -> None:
    """扫描处理过的章节，增量更新 chapter_status.json 和 progress.json"""
    try:
        log(f"[Reviewer] 刷新状态文件 ({start}-{end})...")
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
        progress_file.parent.mkdir(parents=True, exist_ok=True)
        with open(progress_file, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)

        log(f"[Reviewer] 状态刷新完成，last_reviewed_chapter={progress['last_reviewed_chapter']}")
    except Exception as e:
        log(f"[Reviewer] 状态刷新失败: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-p", type=str,
                        default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录（默认从环境变量 NOVEL_PROJECT_DIR 读取）")
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=10, help="结束章节")
    parser.add_argument("--chapter", type=int, default=0, help="只审查某一章")
    parser.add_argument("--final", action="store_true", help="审查终稿（final 目录）而非草稿")
    args = parser.parse_args()

    if not args.project:
        print("错误: 必须指定 --project 或设置 NOVEL_PROJECT_DIR 环境变量")
        sys.exit(1)

    init_project(args.project)

    global CHAPTERS_DIR, REVIEWS_DIR
    if args.final:
        CHAPTERS_DIR = NOVELS_DIR / "chapters" / "final"
        REVIEWS_DIR = NOVELS_DIR / "chapters" / "review_final"

    print("=" * 60)
    print("Reviewer Agent 启动")
    print(f"项目: {NOVELS_DIR}")
    if args.final:
        print("模式: 审查终稿")
    print("=" * 60)

    CHAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    total = 1 if args.chapter > 0 else (args.end - args.start + 1)
    failed = 0
    if args.chapter > 0:
        result = review_chapter(args.chapter)
        if result.get("status") in ("failed", "no_file", "parse_error"):
            failed += 1
        _refresh_status(args.chapter, args.chapter)
    else:
        for ch in range(args.start, args.end + 1):
            result = review_chapter(ch)
            if result.get("status") in ("failed", "no_file", "parse_error"):
                failed += 1
            time.sleep(1)
        _refresh_status(args.start, args.end)

    # 获取书名并发送推送
    title = "本小说"
    world_file = NOVELS_DIR / "world.json"
    if world_file.exists():
        try:
            with open(world_file, "r", encoding="utf-8") as f:
                title = json.load(f).get("title", title)
        except Exception:
            pass

    start_ch = args.chapter if args.chapter > 0 else args.start
    end_ch = args.chapter if args.chapter > 0 else args.end
    # 微信推送已禁用，改由 coordinator 统一推送进度
    # push_stage_complete(
    #     config=CONFIG,
    #     title=title,
    #     stage="审查",
    #     start_chapter=start_ch,
    #     end_chapter=end_ch,
    #     processed=total - failed,
    #     failed=failed,
    # )
    log(f"[Reviewer] 完成 {total - failed} 章，失败 {failed} 章")

    log("[Reviewer] 全部完成")


if __name__ == "__main__":
    main()
