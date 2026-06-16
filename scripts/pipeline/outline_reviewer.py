#!/usr/bin/env python3
"""
Outline Reviewer Agent - 单章大纲审查Agent
负责审查单章大纲质量，确保达到初稿生成标准。
"""

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

import argparse
import json
import os
import time
from pathlib import Path

from core.mmx_client import MmxError, call_mmx as call_mmx_client
from core.novel_config import configure_stdio, load_config, load_origin_materials, resolve_project_dir
from core.workflow_state import (
    load_outline_chapter,
    load_outline_review_status,
    outline_dir,
    outline_review_dir,
)

configure_stdio()

NOVELS_DIR = None
OUTLINE_REVIEW_DIR = None
LOG_FILE = None
CONFIG = None
ORIGIN_MATERIALS = ""


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, OUTLINE_REVIEW_DIR, LOG_FILE, CONFIG, ORIGIN_MATERIALS
    NOVELS_DIR = Path(project_dir).resolve()
    OUTLINE_REVIEW_DIR = outline_review_dir(NOVELS_DIR)
    LOG_FILE = NOVELS_DIR / "logs" / "outline_reviewer.log"
    CONFIG = load_config(NOVELS_DIR)
    review_cfg = CONFIG.get("outline_reviewer", {})
    ORIGIN_MATERIALS = load_origin_materials(
        NOVELS_DIR,
        max_chars=int(review_cfg.get("origin_max_chars", 4000) or 4000),
    )


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def call_mmx(system_prompt: str, user_prompt: str, max_tokens: int = 4096, temperature: float = 0.3) -> str:
    try:
        cfg = CONFIG.get("outline_reviewer", {})
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
            raw_name="outline_reviewer",
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


def review_outline(
    chapter_number: int,
    outline_file_override: Path | None = None,
    review_file_override: Path | None = None,
    context_outline_dir: Path | None = None,
) -> dict:
    outline_file = outline_file_override or outline_dir(NOVELS_DIR) / f"chapter_{chapter_number:04d}.json"
    review_file = review_file_override or OUTLINE_REVIEW_DIR / f"chapter_{chapter_number:04d}_review.json"

    if not outline_file.exists():
        log(f"[OutlineReviewer] 第{chapter_number}章大纲文件不存在")
        return {"status": "no_file"}

    if review_file.exists():
        try:
            existing = json.loads(review_file.read_text(encoding="utf-8"))
            min_score = float(CONFIG.get("outline_reviewer", {}).get("min_score", 8.5))
            _, status, score, ok = load_outline_review_status(review_file, min_score)
            if ok:
                log(f"[OutlineReviewer] 第{chapter_number}章大纲已有达标审查报告（{score}分），跳过")
                return existing
            log(
                f"[OutlineReviewer] 第{chapter_number}章现有审查未达门槛"
                f"（status={status}, score={score}, min={min_score:g}），重新审查"
            )
        except Exception:
            pass

    outline = load_json(outline_file)

    # 加载前后章节作为上下文
    def load_context_outline(number: int) -> dict:
        if number <= 0:
            return {}
        if context_outline_dir is None:
            return load_outline_chapter(NOVELS_DIR, number)
        return load_json(context_outline_dir / f"chapter_{number:04d}.json")

    prev_outline = load_context_outline(chapter_number - 1)
    next_outline = load_context_outline(chapter_number + 1)

    world = load_json(NOVELS_DIR / "world.json")
    characters = load_json(NOVELS_DIR / "characters.json")

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

    min_score = float(CONFIG.get("outline_reviewer", {}).get("min_score", 8.5))

    system = f"""你是一位拥有20年经验的资深网络小说总编，同时也是一位苛刻的"神作猎手"。
你的任务是判断单章大纲能否稳定支撑高质量正文，并找出阻止它达到优秀水平的关键原因。
本书是《{book_title}》。
{genre_text}

## 【高质量单章大纲评分标准】
- 10分：大纲足以支撑传世级神作。悬念密集，情感冲击强烈，信息新鲜，章末钩子让人失眠，Writer据此必能写出让人欲罢不能的章节。
- 9-9.9分：优秀大纲。悬念设计到位，情绪曲线清晰，反套路，有新鲜感，足以支撑9分正文。
- 8-8.9分：良好到优秀。结构完整、有明确冲突和钩子；达到{min_score}分且四项设计门通过即可进入后续全书审查。
- 7-7.9分：平庸大纲。有明显套路、重复、动机牵强或缺乏钩子的问题。
- 低于7分：不合格，存在严重设计缺陷。

【你的审查哲学】
- 不要给"辛苦分"。字段全不等于设计好。
- 不要给字段完整性辛苦分，但也不要把每章都强行要求成卷终高潮。
- 重点关注：这个大纲能否让 Writer 写出一章让人"读完立刻想打开下一章"的内容？
- 如果你给不出9分以上，必须在 weaknesses 中明确说明"距离9分的具体差距"。

输出必须是合法的紧凑JSON，不要使用Markdown代码块，不要输出JSON之外的任何文字。
审查意见要短而具体，整份JSON尽量控制在1500个中文字符以内。"""

    context_parts = []
    if prev_outline:
        context_parts.append(f"""## 前一章大纲（第{chapter_number - 1}章）
标题：{prev_outline.get('title', 'N/A')}
摘要：{prev_outline.get('summary', 'N/A')[:200]}
关键事件：{prev_outline.get('key_events', [])}""")
    if next_outline:
        context_parts.append(f"""## 后一章大纲（第{chapter_number + 1}章）
标题：{next_outline.get('title', 'N/A')}
摘要：{next_outline.get('summary', 'N/A')[:200]}
关键事件：{next_outline.get('key_events', [])}""")
    context_text = "\n\n".join(context_parts)

    prompt = f"""请审查以下第{chapter_number}章的单章大纲。

## 世界观与角色设定
{json.dumps(characters, ensure_ascii=False, indent=2)[:800]}

## origin/ 原始参考素材
{ORIGIN_MATERIALS or "（无）"}

{context_text}

## 待审查大纲（第{chapter_number}章）
{json.dumps(outline, ensure_ascii=False, indent=2)}

请只输出以下JSON格式的审查报告，数组最多3条，每条不超过80字：
{{
  "chapter_number": {chapter_number},
  "overall_score": "请给出0-10的客观评分。9分意味着Writer据此必能写出让人欲罢不能的章节。不要给辛苦分",
  "verdict": "通过/需修改/需重写。注意：如果 overall_score >= {min_score}，verdict 必须写'通过'；只有低于{min_score}分才写'需重写'或'需修改'",
  "scores": {{
    "plot_attraction": "剧情吸引力（0-10）",
    "pacing": "节奏把控（0-10。中段是否有小高潮？是否存在超过1500字无转折的平铺直叙？）",
    "character_motivation": "人物动机合理性（0-10）",
    "satisfaction_design": "爽点设计（0-10。爽点是否触及核心恐惧/欲望？是否反套路？）",
    "foreshadowing": "伏笔与呼应（0-10）",
    "scene_diversity": "场景多样性（0-10）",
    "power_consistency": "力量体系一致性（0-10）",
    "hook_strength": "章末钩子强度（0-10。chapter_hook是否明确、强力、让人心跳加速？）",
    "emotional_arc": "情绪曲线设计（0-10。emotional_arc是否有起伏？是否全程单一情绪？）",
    "suspense_density": "悬念密度（0-10。tension_points是否有至少3个有效张力节点？分布是否合理？）",
    "information_freshness": "信息新鲜度（0-10。是否有至少一个此前从未出现的新元素？有无重复已知信息？）",
    "anti_cliche": "反套路程度（0-10。是否存在标准战斗/解谜模板？是否有意外和不可预测性？）",
    "writeability": "整体可写性（0-10。Writer能否据此写出9分神作级正文？）"
  }},
  "design_gates": {{
    "core_desire": {{"passed": true, "evidence": "主角本章具体想得到或保护什么，60字以内"}},
    "irreversible_choice": {{"passed": true, "evidence": "本章不可撤销的选择、损失或暴露，60字以内"}},
    "midpoint_reversal": {{"passed": true, "evidence": "中段如何改变原行动方案，60字以内"}},
    "strong_hook": {{"passed": true, "evidence": "章末正在发生的具体危机或反转，60字以内"}}
  }},
  "strengths": ["优点1，80字以内"],
  "weaknesses": ["不足1，80字以内。如果给分低于9分，必须在这里明确写出距离9分的具体差距"],
  "suggestions": ["具体修改建议1，80字以内"],
  "continuity_issues": ["与前后章衔接问题，80字以内"],
  "summary": "总体评价，80字以内。如果评分低于9分，用一句话回答：本章大纲最致命的短板是什么？"
}}

【9分神作大纲审查清单——逐条自检】
请在给出评分前逐条检查。四项 design_gates 是硬门槛；其余项目用于综合评分，不得因为单个非致命维度不足就机械封顶：
1. chapter_hook字段是否明确写出了一个让人心跳加速的强力钩子（危机升级/信息反转/情感爆点）？
2. chapter_hook是否禁止了平静收尾、总结现状、铺垫过渡？
3. emotional_arc是否描述了清晰的情绪起伏（如压抑→紧张→希望→绝望），而非全程单一情绪？
4. tension_points是否至少包含3个有效的让人无法停止阅读的关键时刻？
5. 本章是否带来有效的新进展（新信息、新关系变化、新风险或旧伏笔回收），不要求每章强行新增人物或地点？
6. 是否存在套路化设计（标准战斗流程、标准解谜流程、配角当解说员）？
7. 本章回报是否触及角色核心欲望、恐惧或明确阶段目标，而非只有表层事件堆叠？
8. 如果Writer严格按这个大纲写，能否产出一章让人读完立刻想打开下一章的内容？

要求：
1. 评分要客观可复现。9分代表单章设计突出；达到{min_score}分且四项设计门通过，代表足以进入全书层级审查。
2. 重点审查：悬念密度、钩子强度、情绪曲线、信息新鲜度、反套路程度。这五个维度比字段完整性更重要。
3. 剧情是否有真正的冲突和转折，而非流水账
4. 爽点设计是否到位：是否有期待感、压制、反转、碾压等要素，且是否触及角色内核
5. 人物动机是否合理，是否与角色设定一致
6. 与前后章的衔接是否自然，伏笔是否呼应
7. 场景使用是否有效；单章允许集中在一个地点，但不能重复做同样的事或缺少状态变化
8. 力量体系是否自洽，实力成长是否有合理铺垫
9. 信息是否足够详细，Writer能否据此写出{outline.get('word_count_target', 5000)}字高质量正文
10. 如果origin/中存在素材，必须检查大纲是否参考并遵守原始素材；与素材冲突需列入weaknesses或continuity_issues
11. 如低于{min_score}分必须标记为需重写
12. 必须输出合法JSON，不要Markdown，不要长篇解释"""

    log(f"[OutlineReviewer] 正在审查第{chapter_number}章大纲...")
    content = call_mmx(system, prompt, max_tokens=4096, temperature=0.3)

    if not content:
        log(f"[OutlineReviewer] 第{chapter_number}章大纲审查失败")
        review_data = {
            "chapter_number": chapter_number,
            "status": "failed",
        }
        review_file.parent.mkdir(parents=True, exist_ok=True)
        with open(review_file, "w", encoding="utf-8") as f:
            json.dump(review_data, f, ensure_ascii=False, indent=2)
        return review_data

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

    def _extract_first_json_object(text: str) -> dict:
        cleaned = text.strip()
        if "```json" in cleaned:
            cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in cleaned:
            cleaned = cleaned.split("```", 1)[1].split("```", 1)[0].strip()
        start = cleaned.find("{")
        if start < 0:
            raise ValueError("response contains no JSON object")
        data, _ = json.JSONDecoder().raw_decode(cleaned[start:])
        if not isinstance(data, dict):
            raise ValueError("review response is not a JSON object")
        return data

    try:
        review_data = _extract_first_json_object(content)
        review_data["status"] = "completed"
        # 将 overall_score 统一转为 float
        review_data["overall_score"] = _parse_score(review_data.get("overall_score"))
        # 将 scores 子项也转为 float
        scores = review_data.get("scores")
        if isinstance(scores, dict):
            for k, v in list(scores.items()):
                scores[k] = _parse_score(v)
        design_gates = review_data.get("design_gates")
        required_gates = (
            "core_desire",
            "irreversible_choice",
            "midpoint_reversal",
            "strong_hook",
        )
        gate_results = []
        if isinstance(design_gates, dict):
            normalized_gates = {
                str(key).replace(" ", "").replace("-", "_"): value
                for key, value in design_gates.items()
            }
            for gate_name in required_gates:
                gate = normalized_gates.get(gate_name)
                passed = isinstance(gate, dict) and gate.get("passed") is True
                evidence = gate.get("evidence", "") if isinstance(gate, dict) else ""
                gate_results.append(passed and isinstance(evidence, str) and bool(evidence.strip()))
            review_data["design_gates"] = normalized_gates
        design_gate_passed = len(gate_results) == len(required_gates) and all(gate_results)
        review_data["design_gate_passed"] = design_gate_passed
        if not design_gate_passed:
            score = review_data.get("overall_score")
            if isinstance(score, (int, float)) and score >= 9.0:
                review_data["overall_score"] = 8.8
            review_data["verdict"] = "需修改"
    except Exception as e:
        log(f"[OutlineReviewer] JSON解析失败: {e}")
        review_data = {
            "chapter_number": chapter_number,
            "status": "parse_error",
            "raw_response": content,
        }

    review_file.parent.mkdir(parents=True, exist_ok=True)
    with open(review_file, "w", encoding="utf-8") as f:
        json.dump(review_data, f, ensure_ascii=False, indent=2)

    overall = review_data.get("overall_score", "N/A")
    verdict = review_data.get("verdict", "N/A")
    log(f"[OutlineReviewer] 第{chapter_number}章大纲审查完成，评分: {overall}，verdict: {verdict}")
    return review_data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-p", type=str,
                        default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录")
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=10, help="结束章节")
    parser.add_argument("--chapter", type=int, default=0, help="只审查某一章")
    parser.add_argument("--outline-file", type=str, default="", help="候选大纲文件；启用后审查该文件而非正式大纲")
    parser.add_argument("--review-file", type=str, default="", help="候选审查输出文件")
    parser.add_argument("--outline-dir", type=str, default="", help="候选大纲目录；批量审查时同时作为前后章上下文")
    parser.add_argument("--review-dir", type=str, default="", help="候选审查报告输出目录")
    args = parser.parse_args()

    try:
        project = resolve_project_dir(args.project)
    except ValueError as exc:
        print(f"错误: {exc}")
        sys.exit(1)

    init_project(project)

    print("=" * 60)
    print("Outline Reviewer Agent 启动")
    print(f"项目: {NOVELS_DIR}")
    print("=" * 60)

    OUTLINE_REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    total = 1 if args.chapter > 0 else (args.end - args.start + 1)
    failed = 0
    candidate_outline_dir = Path(args.outline_dir).resolve() if args.outline_dir else None
    candidate_review_dir = Path(args.review_dir).resolve() if args.review_dir else None
    if args.chapter > 0:
        outline_override = Path(args.outline_file) if args.outline_file else None
        if outline_override is None and candidate_outline_dir is not None:
            outline_override = candidate_outline_dir / f"chapter_{args.chapter:04d}.json"
        review_override = Path(args.review_file) if args.review_file else None
        if review_override is None and candidate_review_dir is not None:
            review_override = candidate_review_dir / f"chapter_{args.chapter:04d}_review.json"
        result = review_outline(
            args.chapter,
            outline_override,
            review_override,
            candidate_outline_dir,
        )
        if result.get("status") in ("failed", "no_file", "parse_error"):
            failed += 1
    else:
        for ch in range(args.start, args.end + 1):
            outline_override = (
                candidate_outline_dir / f"chapter_{ch:04d}.json"
                if candidate_outline_dir is not None
                else None
            )
            review_override = (
                candidate_review_dir / f"chapter_{ch:04d}_review.json"
                if candidate_review_dir is not None
                else None
            )
            result = review_outline(
                ch,
                outline_override,
                review_override,
                candidate_outline_dir,
            )
            if result.get("status") in ("failed", "no_file", "parse_error"):
                failed += 1
            time.sleep(1)

    log(f"[OutlineReviewer] 完成 {total - failed} 章，失败 {failed} 章")
    log("[OutlineReviewer] 全部完成")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
