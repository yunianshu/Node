#!/usr/bin/env python3
"""人物成长弧线追踪：六阶段跨章进度管理。

每章正文生成后，抽取主角的弧线当前阶段（origin→trigger→inner_conflict→
turning_point→awakening→destination），存为 arc_states/chapter_XXXX.json。
writer 生成下一章前注入此进度，确保角色成长曲线有方向、不倒退。

弧线六阶段：
  origin          起点状态（初始信念/弱点/缺失）
  trigger         触发事件（打破原有世界观的契机）
  inner_conflict  内在冲突（旧信念与新认知的拉扯）
  turning_point   转折点（被迫做出关键选择）
  awakening       觉醒（完成核心转变）
  destination     终点新状态（以全新人格面对世界）

状态文件位置：projects/<book>/chapters/arc_states/chapter_XXXX.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS_ROOT = Path(__file__).resolve().parent.parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from core.json_repair import fix_inner_quotes, fix_truncated_json
from core.mmx_client import MmxError, call_mmx
from core.workflow_state import atomic_write_json

# 六阶段定义（顺序即成长方向）
ARC_STAGES = [
    "origin",
    "trigger",
    "inner_conflict",
    "turning_point",
    "awakening",
    "destination",
]
STAGE_LABELS = {
    "origin": "起点（初始信念/弱点/缺失）",
    "trigger": "触发（打破原有世界观的契机）",
    "inner_conflict": "内在冲突（旧信念与新认知的拉扯）",
    "turning_point": "转折（被迫做出关键选择）",
    "awakening": "觉醒（完成核心转变）",
    "destination": "终点（以全新人格面对世界）",
}


def arc_dir(project: Path) -> Path:
    return project / "chapters" / "arc_states"


def arc_file(project: Path, chapter: int) -> Path:
    return arc_dir(project) / f"chapter_{chapter:04d}.json"


def empty_arc() -> dict:
    return {"protagonist": "", "current_stage": "", "stage_evidence": "", "chapter": 0}


def load_arc(project: Path, chapter: int) -> dict:
    f = arc_file(project, chapter)
    if not f.exists():
        return empty_arc()
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and "current_stage" in data else empty_arc()
    except Exception as exc:
        return empty_arc()


def latest_arc_before(project: Path, chapter: int) -> dict:
    """返回第 chapter 章之前最近的弧线快照（用于注入 writer）。"""
    for ch in range(chapter - 1, 0, -1):
        arc = load_arc(project, ch)
        if arc.get("current_stage"):
            return arc
    return empty_arc()


def format_arc_for_prompt(arc: dict) -> str:
    """把弧线快照格式化为可注入 writer prompt 的文本。无数据返回空串。"""
    stage = arc.get("current_stage", "")
    if not stage:
        return ""
    label = STAGE_LABELS.get(stage, stage)
    name = arc.get("protagonist", "主角")
    evidence = arc.get("stage_evidence", "")
    lines = [
        "## 【主角成长弧线进度】（截至上一章，本章不得让角色成长倒退）",
        f"- 主角：{name}",
        f"- 当前弧线阶段：{label}",
    ]
    if evidence:
        lines.append(f"- 阶段依据：{evidence}")
    lines.append(
        "本章应根据剧情推进维持或推进主角的弧线阶段，不得让角色行为与当前成长阶段矛盾。"
    )
    return "\n".join(lines)


def extract_arc_progress(
    project: Path, config: dict, chapter: int, text: str,
    characters_meta: dict | None = None,
) -> dict:
    """从一章正文抽取主角成长弧线当前阶段。用 LLM，返回弧线快照 dict。"""
    chars_meta = characters_meta or {}
    protagonist_name = ""
    growth_path: list[str] = []
    if isinstance(chars_meta.get("protagonist"), dict):
        protagonist_name = chars_meta["protagonist"].get("name", "")
        growth_path = chars_meta["protagonist"].get("growth_path", [])

    # 取上一章弧线作为参照（不允许倒退）
    prev_arc = latest_arc_before(project, chapter)
    prev_stage = prev_arc.get("current_stage", "")
    prev_label = STAGE_LABELS.get(prev_stage, prev_stage)

    stages_hint = "\n".join(f"  - {s}：{STAGE_LABELS[s]}" for s in ARC_STAGES)
    growth_hint = "；".join(growth_path) if growth_path else "（自动判断）"

    prompt = f"""分析以下小说第{chapter}章正文，判断主角【{protagonist_name or "（自动识别）"}】
在本章结束时处于哪个成长弧线阶段。

弧线六阶段（按成长方向排列，后面的阶段表示更深层的成长）：
{stages_hint}

主角预设成长路径（参考）：{growth_hint}
上一章弧线阶段：{prev_label or "（无，可能是开篇）"}

规则：
1. 只能维持或推进阶段，不得倒退（除非有合理的剧情理由，需在 evidence 中说明）
2. stage_evidence 用一句话引用本章具体情节作为判断依据
3. 只输出紧凑JSON，不要Markdown

{{"protagonist": "{protagonist_name or "主角"}", "current_stage": "origin/trigger/inner_conflict/turning_point/awakening/destination", "stage_evidence": "一句话说明判断依据（引用本章具体情节）"}}

第{chapter}章正文：
{text[:6000]}"""
    cfg = config.get("arc_state", {})
    try:
        raw = call_mmx(
            "你是小说人物成长分析专家。精准判断角色弧线阶段，只基于正文事实。",
            prompt,
            model=config["model"],
            mmx_path=config["mmx_path"],
            max_tokens=int(cfg.get("max_tokens", 1024)),
            temperature=float(cfg.get("temperature", 0.1)),
            retries=int(cfg.get("retries", 2)),
            retry_delay=float(cfg.get("retry_delay", 5.0)),
            timeout=int(cfg.get("timeout_seconds", 120)),
            log_dir=project / "logs" / "raw_responses",
            raw_name=f"arc_state_ch{chapter:04d}",
            qps=float(config.get("api_qps", 5.0)),
            rate_state_dir=project / "logs" / "rate_limit",
        )
    except MmxError as exc:
        return {"protagonist": protagonist_name, "current_stage": prev_stage or "",
                "stage_evidence": "", "chapter": chapter, "error": str(exc)}

    parsed = _parse_json(raw)
    stage = parsed.get("current_stage", prev_stage or "")
    # 校验 stage 合法性
    if stage not in ARC_STAGES:
        stage = prev_stage or ARC_STAGES[0]
    result = {
        "protagonist": parsed.get("protagonist", protagonist_name),
        "current_stage": stage,
        "stage_evidence": parsed.get("stage_evidence", ""),
        "chapter": chapter,
    }
    arc_dir(project).mkdir(parents=True, exist_ok=True)
    atomic_write_json(arc_file(project, chapter), result)
    return result


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()
    for cand in (text, fix_inner_quotes(text), fix_truncated_json(text)):
        try:
            data = json.loads(cand)
            if isinstance(data, dict):
                return data
        except Exception as exc:
            continue
    return {}


def _selftest() -> None:
    arc = {
        "protagonist": "林深",
        "current_stage": "turning_point",
        "stage_evidence": "林深在废弃仓库中选择向警方交出U盘",
        "chapter": 10,
    }
    text = format_arc_for_prompt(arc)
    assert "主角成长弧线进度" in text
    assert "转折" in text
    assert "林深" in text
    assert format_arc_for_prompt(empty_arc()) == ""
    assert latest_arc_before(Path("nonexistent_xyz"), 5).get("current_stage") == ""
    print("arc_state selftest OK")


if __name__ == "__main__":
    _selftest()
