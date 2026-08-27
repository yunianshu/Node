#!/usr/bin/env python3
"""全书伏笔闭环审计：writer 前的硬门禁。

流程：重建台账 → 检出 dangling → 生成回收补丁（LLM）→ 回写大纲 → 门禁判定。
退出码：0=通过（无 critical dangling），1=dangling 超阈值。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from core.llm_client import LLMError, call_llm
from core.novel_config import load_config, resolve_project_dir
from core.workflow_state import atomic_write_json, outline_chapter_path
from core.json_repair import fix_inner_quotes, fix_truncated_json
from core.foreshadowing_ledger import (
    dangling_threads, ledger_path, rebuild_from_outlines, resolve_thread, save_ledger,
)


def log(project: Path, msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [ForeshadowingAudit] {msg}"
    print(line, flush=True)
    p = project / "logs" / "foreshadowing_audit.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(line + "\n")


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
    return {"status": "parse_error", "raw_response": raw[:3000]}


def ai_call(project: Path, config: dict, prompt: str, raw_name: str) -> dict:
    cfg = config.get("foreshadowing_audit", {})
    try:
        raw = call_llm(
            config,
            project,
            "foreshadowing_audit",
            "你是长篇小说伏笔回收专家。只依据输入证据，输出合法紧凑JSON，不使用Markdown。",
            prompt,
            max_tokens=int(cfg.get("max_tokens", 2048)),
            temperature=float(cfg.get("temperature", 0.3)),
            retries=int(cfg.get("retries", 2)),
            retry_delay=float(cfg.get("retry_delay", 5.0)),
            timeout=int(cfg.get("timeout_seconds", 180)),
            raw_name=raw_name,
        )
    except LLMError as exc:
        return {"status": "failed", "error": str(exc)}
    result = _parse_json(raw)
    result.setdefault("status", "completed")
    return result


def generate_resolution_patch(project: Path, config: dict, thread: dict,
                              total_chapters: int) -> dict:
    """对单条 dangling 线程生成回收补丁。"""
    target = min(int(thread.get("must_resolve_by", total_chapters) or total_chapters),
                 total_chapters)
    setup = thread.get("setup", "")
    prompt = f"""以下伏笔在大纲中埋下但未回收，请在指定章节大纲中插入回收情节。
【不得改动已有主线】，只在指定章节的 key_events（追加1-2条）或 foreshadowing 字段增量补内容。

待回收伏笔：
- id: {thread.get('id')}
- 埋设于第{thread.get('planted_at')}章：{setup}
- 类别: {thread.get('category')}，优先级: {thread.get('priority')}

指定回收章：第{target}章

要求：
1. 回收必须自然融入该章已有剧情，不能突兀
2. 回收要制造情感或信息回报，禁止"他终于明白了"式一笔带过
3. foreshadowing_update 必须使用 [收]FXXX 说明 格式

只输出JSON：
{{"thread_id":"{thread.get('id')}","target_chapter":{target},
"key_events_addition":["新增关键事件，30-60字"],
"foreshadowing_update":"[收]{thread.get('id')} 回收说明（30-80字）",
"resolution_summary":"50字内回收说明"}}"""
    return ai_call(project, config, prompt, f"foreshadow_patch_{thread.get('id')}")


def apply_patch(project: Path, patch: dict) -> bool:
    """把补丁增量写回 target_chapter 的大纲 JSON。返回是否成功。"""
    target = int(patch.get("target_chapter", 0) or 0)
    if target <= 0:
        return False
    path = outline_chapter_path(project, target)
    if not path.exists():
        return False
    try:
        outline = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False
    additions = patch.get("key_events_addition", [])
    if isinstance(additions, list):
        events = outline.get("key_events", [])
        if not isinstance(events, list):
            events = []
        for ev in additions:
            if str(ev).strip() and str(ev).strip() not in events:
                events.append(str(ev).strip())
        outline["key_events"] = events
    update = str(patch.get("foreshadowing_update", "")).strip()
    if update:
        existing = str(outline.get("foreshadowing", "")).strip()
        outline["foreshadowing"] = (existing + "\n" + update).strip() if existing else update
    atomic_write_json(path, outline)
    return True


def patch_outline_from_thread(project: Path, config: dict, thread: dict,
                              total_chapters: int) -> bool:
    """生成并应用一条回收补丁。成功返回 True。"""
    patch = generate_resolution_patch(project, config, thread, total_chapters)
    if patch.get("status") != "completed":
        return False
    if not apply_patch(project, patch):
        return False
    # 标记台账已回收（resolved_in_outline=True）
    tid = thread.get("id")
    ledger_path_file = ledger_path(project)
    if ledger_path_file.exists():
        try:
            ledger = json.loads(ledger_path_file.read_text(encoding="utf-8"))
            resolve_thread(ledger, tid, resolved_at=int(patch.get("target_chapter", 0) or 0),
                           resolution=patch.get("resolution_summary", ""),
                           resolved_in_outline=True)
            save_ledger(project, ledger)
        except Exception as exc:
            pass
    return True


def run_audit(project: Path, config: dict, *, apply_patches: bool,
              max_rounds: int, block_threshold: int) -> dict:
    total = int(config.get("total_chapters", 0))
    ledger = rebuild_from_outlines(project, book_id=project.name, total_chapters=total)
    save_ledger(project, ledger)
    log(project, f"台账重建完成: {ledger['stats']}")

    dangling = dangling_threads(ledger)
    critical = [t for t in dangling if t.get("priority") == "critical"]
    log(project, f"dangling={len(dangling)} critical={len(critical)}")

    applied = 0
    if apply_patches and dangling:
        for rnd in range(1, max_rounds + 1):
            # 重新读取当前台账中的 dangling（每轮重建后刷新）
            ledger = rebuild_from_outlines(project, book_id=project.name, total_chapters=total)
            save_ledger(project, ledger)
            still = dangling_threads(ledger)
            if not still:
                break
            log(project, f"补丁轮 {rnd}/{max_rounds}，待处理 {len(still)} 条")
            for t in still:
                if patch_outline_from_thread(project, config, t, total):
                    applied += 1

    ledger = rebuild_from_outlines(project, book_id=project.name, total_chapters=total)
    save_ledger(project, ledger)
    final_critical = sum(1 for t in ledger["threads"]
                         if t.get("status") != "resolved" and t.get("priority") == "critical")
    passed = final_critical <= block_threshold
    result = {
        "status": "completed",
        "stats": ledger["stats"],
        "critical_dangling": final_critical,
        "patches_applied": applied,
        "block_threshold": block_threshold,
        "gate_passed": passed,
        "ledger_file": str(ledger_path(project)),
    }
    report = project / "reports" / "foreshadowing_audit_report.json"
    atomic_write_json(report, result)
    log(project, f"审计完成 gate_passed={passed} {ledger['stats']}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="全书伏笔闭环审计（writer 前门禁）")
    parser.add_argument("--project", "-p", default=os.getenv("NOVEL_PROJECT_DIR", ""))
    parser.add_argument("--force", action="store_true", help="忽略缓存，全量重建")
    parser.add_argument("--apply-patches", action="store_true",
                        help="自动生成并应用回收补丁（默认只报告）")
    parser.add_argument("--max-rounds", type=int, default=3, help="补丁重试上限")
    parser.add_argument("--block-threshold", type=int, default=0,
                        help="critical dangling 阻断阈值（默认0）")
    args = parser.parse_args()
    project = resolve_project_dir(args.project)
    config = load_config(project)
    result = run_audit(
        project, config,
        apply_patches=args.apply_patches,
        max_rounds=args.max_rounds,
        block_threshold=args.block_threshold,
    )
    return 0 if result["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
