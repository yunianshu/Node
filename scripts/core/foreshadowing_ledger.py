#!/usr/bin/env python3
"""全书级伏笔台账：登记、回收、dangling 查询。

台账文件位置：projects/<book>/reports/foreshadowing_ledger.json
foreshadowing 字段约定（机器可解析）：
  埋设：[埋]描述文字（F00X）
  回收：[收]F00X 回收说明
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from core.workflow_state import atomic_write_json, report_path

LEDGER_FILENAME = "foreshadowing_ledger.json"

# 伏笔类别 → 最迟回收偏移（占全书总章数比例）
CATEGORY_RESOLVE_RATIO = {
    "character_internal": 0.40,   # 角色内心线：埋设后 40% 章数内回收
    "plot_mystery": 0.50,          # 剧情谜团：中点前回收
    "world_rule": 0.95,            # 世界观规则：finale 前回收
    "power_system": 0.60,          # 力量体系：60% 内回收
    "relationship": 0.25,          # 关系线：快速回收
}
DEFAULT_CATEGORY = "plot_mystery"
DEFAULT_PRIORITY = "major"
DEFAULT_LAYER = "meso"  # G7: 默认中观悬念
VALID_PRIORITIES = {"critical", "major", "minor"}
VALID_LAYERS = {"macro", "meso", "micro"}  # G7: 宏观(贯穿全书)/中观(数十章)/微观(章末钩子)
VALID_STATUSES = {"planted", "planned_resolution", "claimed", "resolved", "dangling"}
# 匹配 foreshadowing 字段中的 [埋]/[收] 标记与伏笔 id
PLANT_RE = re.compile(r"\[埋\].*?\(?\s*(F\d{3,})\s*\)?", re.S)
RESOLVE_RE = re.compile(r"\[收\]\s*(F\d{3,})\s*(.*)", re.S)
THREAD_ID_RE = re.compile(r"^F(\d{3,})$")


def ledger_path(project: Path) -> Path:
    return report_path(project, LEDGER_FILENAME)


def empty_ledger(book_id: str = "", total_chapters: int = 0) -> dict:
    return {
        "version": 1,
        "book_id": book_id,
        "total_chapters": total_chapters,
        "threads": [],
        "stats": {"total": 0, "resolved": 0, "dangling": 0, "critical_dangling": 0},
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def load_ledger(project: Path) -> dict:
    path = ledger_path(project)
    if not path.exists():
        return empty_ledger()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and "threads" in data else empty_ledger()
    except Exception:
        return empty_ledger()


def save_ledger(project: Path, ledger: dict) -> None:
    recompute_stats(ledger)
    ledger["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    atomic_write_json(ledger_path(project), ledger)


def _next_thread_id(ledger: dict) -> str:
    max_num = 0
    for t in ledger.get("threads", []):
        m = THREAD_ID_RE.match(str(t.get("id", "")))
        if m:
            max_num = max(max_num, int(m.group(1)))
    return f"F{max_num + 1:03d}"


def compute_must_resolve_by(planted_at: int, total_chapters: int,
                            category: str = DEFAULT_CATEGORY) -> int:
    ratio = CATEGORY_RESOLVE_RATIO.get(category, CATEGORY_RESOLVE_RATIO[DEFAULT_CATEGORY])
    offset = max(1, round(total_chapters * ratio))
    return min(planted_at + offset, total_chapters)


def register_thread(ledger: dict, *, planted_at: int, setup: str,
                    category: str = DEFAULT_CATEGORY, priority: str = DEFAULT_PRIORITY,
                    total_chapters: int | None = None,
                    must_resolve_by: int | None = None,
                    thread_id: str | None = None,
                    layer: str = DEFAULT_LAYER,
                    audience_knows: bool = True,
                    character_knows: bool = False) -> str:
    if category not in CATEGORY_RESOLVE_RATIO:
        category = DEFAULT_CATEGORY
    if priority not in VALID_PRIORITIES:
        priority = DEFAULT_PRIORITY
    if layer not in VALID_LAYERS:
        layer = DEFAULT_LAYER
    setup = str(setup or "").strip()
    if not setup:
        raise ValueError("setup 不能为空")
    tc = total_chapters if total_chapters is not None else ledger.get("total_chapters", 0)
    if must_resolve_by is None:
        must_resolve_by = compute_must_resolve_by(planted_at, tc, category)
    if thread_id is None:
        thread_id = _next_thread_id(ledger)
    # 若 id 已存在则跳过（幂等）
    if any(t.get("id") == thread_id for t in ledger.get("threads", [])):
        return thread_id
    ledger.setdefault("threads", []).append({
        "id": thread_id,
        "planted_at": planted_at,
        "setup": setup,
        "category": category,
        "priority": priority,
        "layer": layer,  # G7: macro/meso/micro
        "audience_knows": audience_knows,  # G7: 读者是否已知此悬念存在
        "character_knows": character_knows,  # G7: 角色是否已知此悬念存在
        "status": "planted",
        "must_resolve_by": must_resolve_by,
        "resolved_at": None,
        "resolution_plan": "",
        "resolved_in_outline": False,
    })
    return thread_id


def resolve_thread(ledger: dict, thread_id: str, *, resolved_at: int,
                   resolution: str = "", resolved_in_outline: bool = True) -> bool:
    for t in ledger.get("threads", []):
        if t.get("id") == thread_id and t.get("status") != "resolved":
            t["status"] = "resolved"
            t["resolved_at"] = resolved_at
            t["resolution_plan"] = str(resolution or "").strip()
            t["resolved_in_outline"] = bool(resolved_in_outline)
            return True
    return False


def claim_thread(ledger: dict, thread_id: str, *, claimed_at: int,
                 resolution: str = "") -> bool:
    """标记伏笔为"大纲声称回收"（claimed）——等待正文 writer 兑现后才能转 resolved。

    区分 claimed 与 resolved 的目的：大纲写了 [收]FXXX 只代表"声称要回收"，
    正文是否真的兑现 payoff 需要在 writer 阶段回验。未回验的 claimed 在终审仍计入
    未完全回收，倒逼大纲层与正文层一致。
    """
    for t in ledger.get("threads", []):
        if t.get("id") == thread_id and t.get("status") not in {"resolved", "claimed"}:
            t["status"] = "claimed"
            t["claimed_at"] = claimed_at
            t["resolution_plan"] = str(resolution or "").strip()
            t["resolved_in_outline"] = True
            return True
    return False


def _text_mentions_setup(text: str, setup: str, *, min_overlap: int = 1) -> bool:
    """粗判正文是否提及了伏笔 setup 的关键内容。

    大纲回收声明 [收]F003 只代表"声称要回收"，正文是否兑现需要回验。本函数做轻量
    本地检查：从 setup 抽取 2-4 字连续 CJK 片段作为关键词候选，看正文是否包含。
    纯本地计算，不耗 LLM。

    宽松策略：setup 的核心名词实体（如"黑剑""守墓人""身世"）任一在正文出现即认为
    呼应了，允许语序改写、同义扩展。停用动词/方位词（"留在""之后"）不计。
    """
    import re as _re
    if not setup or not text:
        return False
    setup = str(setup)
    text = str(text)
    # 滑动窗口切分 setup 的所有 2-4 字 CJK 片段（含"黑剑"等子串），而非贪婪整段匹配
    import re as _re
    cjk_only = "".join(_re.findall(r"[\u4e00-\u9fff]", setup))
    chunks: list[str] = []
    for size in (2, 3, 4):
        for i in range(len(cjk_only) - size + 1):
            chunks.append(cjk_only[i:i + size])
    chunks = list(dict.fromkeys(chunks))  # 保序去重
    if not chunks:
        compact = _re.sub(r"[\W_]+", "", setup, flags=_re.UNICODE)
        chunks = [compact[i:i + 3] for i in range(len(compact) - 2)] if len(compact) >= 3 else [compact]
    # 停用片段：动词/方位/虚词，出现在正文也不代表呼应了核心实体
    stopword_like = {
        "主角", "故事", "情节", "内容", "事件", "情况", "问题", "之后", "随后", "突然",
        "留在", "在案", "案发", "发现", "现场", "终于", "原来", "这是", "就是", "不过",
    }
    keywords = [c for c in chunks if c not in stopword_like]
    if not keywords:
        keywords = chunks
    # 正文包含任一关键词即认为呼应（setup 的 CJK 片段都是具体名词）
    return any(kw in text for kw in keywords)


def verify_resolution_in_text(ledger: dict, chapter: int, text: str) -> dict:
    """回验第 chapter 章正文是否兑现了该章 claimed/resolved 伏笔。

    对每个在本章声称回收的伏笔（claimed_at==chapter 或 resolved_at==chapter），
    检查正文是否真提到 setup 关键内容。验证通过则升为 resolved，否则降回 planted
    并记录 unresolved_in_text=True，供终审识别"声称回收但正文没写"的假回收。

    返回 {verified: [...], failed: [...]}，不耗 LLM。
    """
    verified: list[str] = []
    failed: list[str] = []
    changed = False
    for t in ledger.get("threads", []):
        tid = t.get("id", "")
        is_this_chapter = (
            (t.get("status") == "claimed" and int(t.get("claimed_at", 0) or 0) == chapter)
            or (t.get("status") == "resolved" and int(t.get("resolved_at", 0) or 0) == chapter)
        )
        if not is_this_chapter:
            continue
        setup = str(t.get("setup", ""))
        if _text_mentions_setup(text, setup):
            t["status"] = "resolved"
            t["resolved_at"] = chapter
            t["resolved_in_outline"] = True
            t.pop("unresolved_in_text", None)
            verified.append(tid)
            changed = True
        else:
            # 正文没兑现：降回 planted 并打标，终审可据此扣分
            t["status"] = "planted"
            t["unresolved_in_text"] = True
            t["failed_verify_at"] = chapter
            t.pop("claimed_at", None)
            failed.append(tid)
            changed = True
    return {"verified": verified, "failed": failed, "changed": changed}


def recompute_stats(ledger: dict) -> dict:
    threads = ledger.get("threads", [])
    total = len(threads)
    resolved = sum(1 for t in threads if t.get("status") == "resolved")
    claimed = sum(1 for t in threads if t.get("status") == "claimed")
    failed_verify = sum(1 for t in threads if t.get("unresolved_in_text"))
    # dangling 包含所有未真正 resolved 的（planted/claimed/dangling）
    dangling = total - resolved
    critical_dangling = sum(
        1 for t in threads
        if t.get("status") != "resolved" and t.get("priority") == "critical"
    )
    ledger["stats"] = {
        "total": total, "resolved": resolved,
        "claimed": claimed,
        "claimed_unverified": claimed,
        "failed_verify": failed_verify,
        "dangling": dangling, "critical_dangling": critical_dangling,
    }
    return ledger["stats"]


def dangling_threads(ledger: dict, *, as_of_chapter: int | None = None) -> list[dict]:
    """返回所有未回收的线程；若给 as_of_chapter，进一步要求已过 must_resolve_by。"""
    out = []
    for t in ledger.get("threads", []):
        if t.get("status") == "resolved":
            continue
        if as_of_chapter is not None:
            mrb = int(t.get("must_resolve_by", 0) or 0)
            if mrb and as_of_chapter < mrb:
                continue
        out.append(t)
    return out


def parse_foreshadowing_field(text: str) -> dict:
    """解析一章 foreshadowing 字段，返回 {planted: [(id,setup)], resolved: [(id,note)]}。

    兼容无标记的纯文本（视为一条无 id 的埋设，id 留空）。
    """
    text = str(text or "")
    planted: list[tuple[str, str]] = []
    resolved: list[tuple[str, str]] = []
    for m in PLANT_RE.finditer(text):
        raw = m.group(0)
        tid = m.group(1)
        setup = re.sub(r"^\[埋\]", "", raw).strip()
        # 剥离尾部伏笔 id 及其包裹括号（兼容半角 () 与全角 （））
        setup = re.sub(r"[\(（]?\s*" + re.escape(tid) + r"\s*[\)）]?\s*$", "", setup).strip()
        planted.append((tid, setup or tid))
    for m in RESOLVE_RE.finditer(text):
        resolved.append((m.group(1), m.group(2).strip()))
    has_marker = bool(PLANT_RE.search(text) or RESOLVE_RE.search(text))
    if not has_marker and text.strip():
        planted.append(("", text.strip()[:200]))
    return {"planted": planted, "resolved": resolved, "has_marker": has_marker}


def rebuild_from_outlines(project: Path, *, book_id: str = "",
                          total_chapters: int | None = None) -> dict:
    """扫描全书大纲，按章节顺序重建台账。

    - 遍历 chapter_XXXX.json，解析 foreshadowing 字段
    - [埋] 标记（带 id）→ register_thread（已存在同 id 则跳过）
    - [收] 标记 → resolve_thread
    - 无 id 的纯文本埋设 → 用 planted_at+setup 生成新 id
    所有未回收线程按是否过 must_resolve_by 标 planted/dangling（由 recompute_stats 统计）
    """
    from core.workflow_state import list_outline_chapters
    chapters = list_outline_chapters(project)
    tc = total_chapters or (len(chapters))
    ledger = empty_ledger(book_id or project.name, total_chapters=tc)
    chapters.sort(key=lambda c: int(c.get("chapter_number", 0) or 0))
    for ch in chapters:
        num = int(ch.get("chapter_number", 0) or 0)
        if num <= 0:
            continue
        parsed = parse_foreshadowing_field(ch.get("foreshadowing", ""))
        for tid, setup in parsed["planted"]:
            existing = next((t for t in ledger["threads"] if t.get("id") == tid), None)
            if existing:
                continue
            # 带 id 的标记用原 id；无 id 的纯文本让 register 自动生成
            register_thread(
                ledger, planted_at=num, setup=setup or f"第{num}章伏笔",
                category=DEFAULT_CATEGORY, priority=DEFAULT_PRIORITY,
                total_chapters=tc,
                thread_id=tid or None,
            )
        for tid, note in parsed["resolved"]:
            if tid and any(t.get("id") == tid for t in ledger["threads"]):
                resolve_thread(ledger, tid, resolved_at=num, resolution=note)
    # 标记 dangling：所有未 resolved 的，若已过 must_resolve_by
    for t in ledger["threads"]:
        if t.get("status") != "resolved":
            mrb = int(t.get("must_resolve_by", 0) or 0)
            t["status"] = "dangling" if (mrb and tc >= mrb) else t.get("status", "planted")
    recompute_stats(ledger)
    return ledger


def _selftest() -> None:
    led = empty_ledger("test", total_chapters=60)
    f1 = register_thread(led, planted_at=7, setup="战无双杀气失控",
                         category="character_internal", priority="critical")
    assert f1 == "F001", f1
    assert led["threads"][0]["must_resolve_by"] == 7 + round(60 * 0.40)
    f2 = register_thread(led, planted_at=12, setup="第四元缺陷", category="plot_mystery")
    assert f2 == "F002"
    assert len(dangling_threads(led)) == 2
    assert dangling_threads(led, as_of_chapter=5) == []  # 都没过最迟章
    assert len(dangling_threads(led, as_of_chapter=60)) == 2
    ok = resolve_thread(led, "F001", resolved_at=26, resolution="失控被稳定")
    assert ok and led["threads"][0]["status"] == "resolved"
    recompute_stats(led)
    assert led["stats"] == {"total": 2, "resolved": 1, "dangling": 1, "critical_dangling": 0}
    parsed = parse_foreshadowing_field("[埋]战无双杀气失控（F007）\n[收]F003 身世揭晓——他是混沌投射")
    assert parsed["planted"] == [("F007", "战无双杀气失控")], parsed["planted"]
    assert parsed["resolved"] == [("F003", "身世揭晓——他是混沌投射")], parsed["resolved"]
    # rebuild 覆盖测试
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        odir = proj / "chapters" / "outline"
        odir.mkdir(parents=True)
        for num, foreshadow in [
            (7, "[埋]战无双杀气失控（F101）"),
            (12, "[埋]第四元缺陷（F102）"),
            (26, "[收]F101 杀气失控被姬雪潇稳定"),
        ]:
            (odir / f"chapter_{num:04d}.json").write_text(
                json.dumps({"chapter_number": num, "foreshadowing": foreshadow},
                           ensure_ascii=False), encoding="utf-8")
        led2 = rebuild_from_outlines(proj, total_chapters=60)
        ids = {t["id"]: t for t in led2["threads"]}
        assert ids["F101"]["status"] == "resolved", ids["F101"]
        assert ids["F102"]["status"] == "dangling", ids["F102"]
        assert led2["stats"]["dangling"] == 1, led2["stats"]
    print("foreshadowing_ledger selftest OK")


if __name__ == "__main__":
    _selftest()
