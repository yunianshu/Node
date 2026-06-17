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
from core.novel_config import configure_stdio, load_config, load_origin_materials, resolve_project_dir
# 微信推送已禁用，改由 coordinator 统一推送进度
# from core.push_notifier import push_stage_complete
from core.workflow_state import (
    load_outline_chapter, load_review_status, review_dir,
    scan_chapter_status, write_status_file, highest_contiguous, report_path,
)

configure_stdio()

NOVELS_DIR = None
CHAPTERS_DIR = None
CHARACTERS_FILE = None
WORLD_FILE = None
REVIEWS_DIR = None
LOG_FILE = None
CONFIG = None
ORIGIN_MATERIALS = ""


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, CHAPTERS_DIR, CHARACTERS_FILE, WORLD_FILE, REVIEWS_DIR, LOG_FILE, CONFIG, ORIGIN_MATERIALS
    NOVELS_DIR = Path(project_dir).resolve()
    CHAPTERS_DIR = NOVELS_DIR / "chapters" / "draft"
    CHARACTERS_FILE = NOVELS_DIR / "characters.json"
    WORLD_FILE = NOVELS_DIR / "world.json"
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


def review_chapter(
    chapter_number: int,
    chapter_file_override: str | Path | None = None,
    review_file_override: str | Path | None = None,
) -> dict:
    chapter_file = Path(chapter_file_override) if chapter_file_override else CHAPTERS_DIR / f"chapter_{chapter_number:04d}.txt"
    review_file = Path(review_file_override) if review_file_override else REVIEWS_DIR / f"chapter_{chapter_number:04d}_review.json"

    if not chapter_file.exists():
        log(f"[Reviewer] 第{chapter_number}章文件不存在")
        return {"status": "no_file"}

    if review_file.exists():
        configured_min_score = float(CONFIG.get("reviewer", {}).get("min_score", 8.5))
        _, status, _, ok = load_review_status(review_file, configured_min_score)
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
    review_min_score = float(CONFIG.get("reviewer", {}).get("min_score", 8.5))
    min_words = int(quality.get("min_chapter_words", 5000))
    max_words = int(quality.get("max_chapter_words", 12000))
    warn_min = int(quality.get("warn_min_chapter_words", min_words - 200))
    warn_max = int(quality.get("warn_max_chapter_words", max_words + 3000))

    system = f"""你是一位拥有20年经验的资深网络小说编辑，同时也是一位苛刻的"神作猎手"。
你的任务不是找"合格"的章节，而是找出"为什么这章还没达到9分"的每一个原因。
本书是《{book_title}》。
{genre_text}

## 【9分神作评分标准】
- 10分：传世级神作。每一句都在推动剧情或揭示人物，章末钩子让人失眠，读完之后心跳加速，无法停止思考。
- 9-9.9分：优秀神作。悬念密集，情感冲击强烈，信息新鲜，读完立刻想打开下一章。只存在极小的可改进空间。
- 8-8.9分：良好但不够惊艳。有可读性，但存在套路化倾向、悬念不足、情感平淡或信息重复等问题。低于{review_min_score}分，必须重写。
- 7-7.9分：平庸。有明显水文、套路、OOC或逻辑问题，读者很可能中途弃书。
- 低于7分：不合格，存在严重质量问题。

【你的审查哲学】
- 不要给"辛苦分"。写得多、写得顺不等于写得好。
- 不要放过"还行"——"还行"就是失败的委婉说法。
- 重点关注：读者读完这章后，会不会立刻想读下一章？如果不会，问题出在哪？
- 如果你给不出9分以上，必须在 weaknesses 中明确说明"距离9分的具体差距"。

输出必须是合法的紧凑JSON，不要使用Markdown代码块，不要输出JSON之外的任何文字。
审查意见要短而具体，整份JSON尽量控制在1500个中文字符以内。"""

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
  "overall_score": "请给出0-10的客观评分。9分意味着'非常想读下一章'，10分意味着'震撼到说不出话'。不要给辛苦分",
  "verdict": "通过/需修改/需重写。注意：如果 overall_score >= {review_min_score}，verdict 必须写'通过'；只有低于{review_min_score}分才写'需重写'或'需修改'",
  "scores": {{
    "writing_quality": "文笔流畅度（0-10）",
    "plot_coherence": "剧情连贯性（0-10）",
    "character_consistency": "人物一致性（0-10）",
    "scene_description": "场景氛围感（0-10。不是画面多清晰，而是氛围多压迫/诡异/震撼）",
    "dialogue_quality": "对话质量（0-10。是否有潜台词？是否推动情节？是否避免了解说员式对话？）",
    "outline_adherence": "大纲遵循度（0-10）",
    "pacing": "节奏把控（0-10。是否存在平铺直叙超过1500字？中段是否有小高潮？）",
    "emotional_impact": "情感冲击力（0-10。是否触及角色核心恐惧/欲望？是否有刺点？）",
    "hook_strength": "章末钩子强度（0-10。最后200字是否让人心跳加速、必须读下一章？）",
    "suspense_density": "悬念密度（0-10。每800-1200字是否有新信息/冲突升级/意外转折？）",
    "information_freshness": "信息新鲜度（0-10。是否带来至少一个此前从未出现过的新元素？有无重复已知信息？）",
    "anti_cliche": "反套路程度（0-10。是否存在标准升级流/打怪流/解谜流模板？是否有意外和不可预测性？）",
    "read_desire": "读下去的欲望（0-10。假设你是第一次读的读者，读完这章后有多想立刻打开下一章？）"
  }},
  "word_count_check": {{
    "actual": {len(chapter_content)},
    "target": {min_words},
    "status": "达标/偏短/偏长"
  }},
  "strengths": ["优点1，80字以内"],
  "weaknesses": ["不足1，80字以内。如果给分低于9分，必须在这里明确写出'距离9分的具体差距'"],
  "suggestions": ["具体修改建议1，80字以内"],
  "continuity_issues": ["与前文不一致之处，80字以内"],
  "summary": "总体评价，80字以内。如果评分低于9分，用一句话回答：'本章最致命的短板是什么？'",
  "edits": [
    {{
      "type": "replace",
      "old": "正文中需要被替换的原文片段（30-200字，必须精确可定位）",
      "new": "替换后的文本"
    }},
    {{
      "type": "insert",
      "after": "原文锚点片段（插入位置）",
      "text": "要插入的新内容"
    }},
    {{
      "type": "delete",
      "old": "要删除的原文片段"
    }}
  ]
}}

【9分神作审查清单——逐条自检】
请你在给出评分前，先在心中逐条回答以下问题。如果有任何一条答案为"否"或"不够"，overall_score 不得超过8.5分：
1. 章末最后200字是否包含一个让人心跳加速的强力钩子（危机升级/信息反转/情感爆点）？
2. 本章是否有至少一个让读者心头一紧的"刺点"细节（反常动作、未说出口的话、突然沉默）？
3. 每800-1200字是否至少有一次有效推进（新信息、冲突升级、意外转折、人物关系质变）？
4. 本章是否给读者带来至少一个"此前从未出现过的新元素"？
5. 心理描写是否展现了真实的情感波动，而不是代码化/分析化的流水账？
6. 冲突是否触及角色核心恐惧或核心欲望，而非表层利害计算？
7. 是否存在套路化描写（标准战斗模板、标准解谜流程、配角当解说员）？
8. 如果我是第一次读这本书的读者，读完这章后会不会立刻想打开下一章？

要求：
1. 评分要冷酷客观。不要给辛苦分，不要给"还行"分。9分意味着"非常想读下一章"，8分意味着"看完了，还行"。
2. 重点审查：悬念密度、情感冲击、信息新鲜度、反套路程度、读下去的欲望。这五个维度比文笔更重要。
3. 剧情推进是否自然，有无逻辑漏洞或突兀转折
4. 对话是否有潜台词，是否符合角色身份，是否避免了解说员式长篇大论
5. 必须给出具体的修改建议，不能泛泛而谈，但每类最多3条
6. 如果 origin/ 中存在素材，必须检查正文是否参考并遵守原始素材；与素材冲突需列入 weaknesses 或 continuity_issues
7. 如低于{review_min_score}分必须标记为"需重写"
8. 字数不足{warn_min}或超过{warn_max}要标记字数问题
9. 必须输出合法JSON，不要Markdown，不要长篇解释
10. **必须输出 edits 数组**：如果 verdict 不是"通过"，必须给出至少一条具体可定位的 edit ops（replace/insert/delete），用于定点修改而不是全文重写。edit 的 old/after 字段必须引用原文真实片段，长度 30-200 字。"""

    log(f"[Reviewer] 正在审查第{chapter_number}章...")
    start_time = time.time()
    content = call_mmx(system, prompt, max_tokens=4096, temperature=0.3)
    elapsed = time.time() - start_time
    log(f"[Reviewer] 第{chapter_number}章审查 API 调用耗时 {elapsed:.1f}s")

    if not content:
        log(f"[Reviewer] 第{chapter_number}章审查失败")
        review_data = {
            "chapter_number": chapter_number,
            "status": "failed",
            "local_analysis": local_analysis,
        }
        review_file.parent.mkdir(parents=True, exist_ok=True)
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

    review_file.parent.mkdir(parents=True, exist_ok=True)
    with open(review_file, "w", encoding="utf-8") as f:
        json.dump(review_data, f, ensure_ascii=False, indent=2)

    overall = review_data.get("overall_score", "N/A")
    verdict = review_data.get("verdict", "N/A")
    edits = review_data.get("edits", [])
    edit_count = len(edits) if isinstance(edits, list) else 0
    log(f"[Reviewer] 第{chapter_number}章审查完成，评分: {overall}， verdict: {verdict}， edits: {edit_count}")
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
    parser.add_argument("--chapter-file", type=str, default="", help="候选模式：审查指定正文文件")
    parser.add_argument("--review-file", type=str, default="", help="候选模式：审查报告写入指定文件")
    args = parser.parse_args()

    try:
        project = resolve_project_dir(args.project)
    except ValueError as exc:
        print(f"错误: {exc}")
        sys.exit(1)

    init_project(project)

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
        result = review_chapter(
            args.chapter,
            chapter_file_override=args.chapter_file or None,
            review_file_override=args.review_file or None,
        )
        if result.get("status") in ("failed", "no_file", "parse_error"):
            failed += 1
        if not (args.chapter_file or args.review_file):
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
