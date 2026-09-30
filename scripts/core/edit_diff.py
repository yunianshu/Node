#!/usr/bin/env python3
"""文本/JSON 增量编辑工具：Reviewer 输出 edit ops，Writer/Outliner 负责 apply。"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any


class EditApplyError(RuntimeError):
    """编辑操作无法安全 apply 时抛出。"""


def apply_reviewed_edits(
    text: str,
    edits: list[dict],
    approved_edits: list[dict],
    *,
    max_changed_ratio: float = 0.15,
    min_words: int = 0,
    max_words: int | None = None,
) -> str:
    """Apply exact, unambiguous edits only inside reviewer-nominated regions.

    Resolve every span against the original text before changing anything. Unlike
    the legacy fuzzy editor, preserve all bytes outside those spans, including
    whitespace. Bound inserted text as well as removed text; length similarity
    alone would permit an equal-length rewrite of the entire chapter.
    """
    if not 0 < max_changed_ratio <= 1:
        raise EditApplyError("max_changed_ratio 必须在 (0, 1] 内")
    if not edits or not isinstance(edits, list) or not approved_edits:
        raise EditApplyError("缺少编辑操作或审查定位，保留原稿")

    def locate(edit: dict) -> tuple[int, int]:
        if not isinstance(edit, dict) or edit.get("type") not in {"replace", "delete", "insert"}:
            raise EditApplyError("无效编辑类型")
        anchor = edit.get("after") if edit["type"] == "insert" else edit.get("old")
        if not isinstance(anchor, str) or not anchor.strip():
            raise EditApplyError("编辑锚点不能为空")
        start = text.find(anchor)
        if start < 0 or text.find(anchor, start + 1) >= 0:
            raise EditApplyError("编辑锚点缺失或不唯一")
        return start, start + len(anchor)

    approved = [(item, locate(item)) for item in approved_edits]
    changes = []
    cost = 0
    for edit in edits:
        start, end = locate(edit)
        kind = edit["type"]
        if not any(
            item["type"] == kind and lo <= start and end <= hi
            and (kind != "insert" or end == hi)
            for item, (lo, hi) in approved
        ):
            raise EditApplyError("编辑超出审查指出的范围")
        replacement = "" if kind == "delete" else edit.get("text" if kind == "insert" else "new")
        if not isinstance(replacement, str) or (kind != "delete" and not replacement.strip()):
            raise EditApplyError("修改后文本缺失")
        if kind == "insert":
            start = end
        cost += max(end - start, len(replacement))
        changes.append((start, end, replacement))
    changes.sort(key=lambda item: (item[0], item[1]))
    for previous, current in zip(changes, changes[1:]):
        if current[0] < previous[1] or current[0] == previous[0]:
            raise EditApplyError("编辑范围重叠")
    if cost > len(text) * max_changed_ratio:
        raise EditApplyError("修改量超过配置上限，保留原稿")
    result = text
    for start, end, replacement in reversed(changes):
        result = result[:start] + replacement + result[end:]
    if len(result) < min_words or (max_words is not None and len(result) > max_words):
        raise EditApplyError("补丁后字数不合格；禁止截断原稿")
    if result == text:
        raise EditApplyError("补丁没有产生有效修改")
    return result


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _find_exact_or_fuzzy(haystack: str, needle: str, threshold: float = 0.85) -> tuple[int, int] | None:
    """在 haystack 中查找 needle，先精确子串，再模糊匹配。
    返回 (start, end) 或 None。
    """
    needle = _normalize(needle)
    haystack = _normalize(haystack)
    if not needle.strip():
        return None

    # 1. 精确子串
    idx = haystack.find(needle)
    if idx != -1:
        return idx, idx + len(needle)

    # 2. 去除首尾空白后再试
    stripped = needle.strip()
    if stripped and stripped != needle:
        idx = haystack.find(stripped)
        if idx != -1:
            return idx, idx + len(stripped)

    # 3. 按段落匹配：needle 可能跨段落被截断，逐段找最佳匹配段
    needle_paras = [p.strip() for p in stripped.split("\n\n") if p.strip()]
    if len(needle_paras) == 1:
        # 模糊匹配单行/单段
        candidates = [p for p in haystack.split("\n\n") if p.strip()]
        matches = difflib.get_close_matches(stripped, candidates, n=1, cutoff=threshold)
        if matches:
            match = matches[0]
            idx = haystack.find(match)
            if idx != -1:
                return idx, idx + len(match)
        # 尝试按句子匹配
        sentences = re.split(r"(?<=[。！？；.!?])", haystack)
        best = difflib.get_close_matches(stripped, sentences, n=1, cutoff=threshold)
        if best:
            idx = haystack.find(best[0])
            if idx != -1:
                return idx, idx + len(best[0])
    elif len(needle_paras) > 1:
        # 多段模糊匹配：找与 needle 首段+末段最近的位置
        first = needle_paras[0]
        last = needle_paras[-1]
        first_idx = haystack.find(first)
        if first_idx == -1:
            candidates = [p for p in haystack.split("\n\n") if p.strip()]
            matches = difflib.get_close_matches(first, candidates, n=1, cutoff=threshold)
            if matches:
                first_idx = haystack.find(matches[0])
        if first_idx != -1:
            search_start = first_idx + len(first)
            last_idx = haystack.find(last, search_start)
            if last_idx == -1:
                candidates = [p for p in haystack[search_start:].split("\n\n") if p.strip()]
                matches = difflib.get_close_matches(last, candidates, n=1, cutoff=threshold)
                if matches:
                    last_idx = haystack.find(matches[0], search_start)
            if last_idx != -1:
                return first_idx, last_idx + len(last)
    return None


def apply_text_edits(text: str, edits: list[dict]) -> tuple[str, list[dict]]:
    """对 text 顺序 apply edits。

    edits 支持：
    - {"type": "replace", "old": str, "new": str}
    - {"type": "insert", "after": str, "text": str}
    - {"type": "delete", "old": str}

    返回 (new_text, applied_log)。
    任何 edit 匹配失败都会 raise EditApplyError。
    """
    text = _normalize(text)
    applied: list[dict] = []

    # 为了安全，从后往前 apply，避免前面改动影响后面位置
    # 但 replace/insert/delete 都依赖查找，所以位置相对稳定
    for edit in edits:
        op_type = edit.get("type", "replace")
        if op_type == "replace":
            old = str(edit.get("old", ""))
            new = str(edit.get("new", ""))
            span = _find_exact_or_fuzzy(text, old)
            if span is None:
                raise EditApplyError(f"replace 未找到匹配文本：{old[:80]}...")
            start, end = span
            text = text[:start] + new + text[end:]
            applied.append({"type": "replace", "matched": text[start:start + len(new)], "new": new})
        elif op_type == "delete":
            old = str(edit.get("old", ""))
            span = _find_exact_or_fuzzy(text, old)
            if span is None:
                raise EditApplyError(f"delete 未找到匹配文本：{old[:80]}...")
            start, end = span
            text = text[:start] + text[end:]
            applied.append({"type": "delete", "matched": old})
        elif op_type == "insert":
            after = str(edit.get("after", ""))
            insert_text = str(edit.get("text", ""))
            span = _find_exact_or_fuzzy(text, after)
            if span is None:
                raise EditApplyError(f"insert 未找到锚点文本：{after[:80]}...")
            end = span[1]
            text = text[:end] + insert_text + text[end:]
            applied.append({"type": "insert", "after": after, "text": insert_text})
        else:
            raise EditApplyError(f"未知 edit 类型：{op_type}")

    return text, applied


def similarity(a: str, b: str) -> float:
    """两段文本的相似度，0-1。"""
    a = _normalize(a)
    b = _normalize(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def check_preserved_ratio(old_text: str, new_text: str, edits: list[dict], min_preserved_ratio: float = 0.55) -> float:
    """检查未被修改的部分保留了多少。
    简单策略：如果 edits 为空，返回 1.0；否则根据 edit 数量和文本相似度估算。
    """
    if not edits:
        return 1.0
    return similarity(old_text, new_text)


def build_edit_prompt(old_text: str, review_data: dict, chapter_number: int, min_words: int, max_words: int) -> tuple[str, str]:
    """构造让模型输出结构化 edit ops 的 system/user prompt。"""
    suggestions = review_data.get("suggestions", [])
    weaknesses = review_data.get("weaknesses", [])
    continuity_issues = review_data.get("continuity_issues", [])

    system = f"""你是一位冷酷的资深小说编辑，专门负责基于审查意见对章节做**定点增量修改**。
你的任务不是重写整章，而是只修改被明确指出的问题，**未提及的段落必须逐字保留**。

## 输出格式
你必须只输出合法 JSON，不要 Markdown 代码块，不要解释：
{{
  "replacements": [
    {{"old": "要替换的原文片段（必须能在正文中找到）", "new": "替换后的文本"}}
  ],
  "insertions": [
    {{"after": "原文锚点片段", "text": "要插入的内容"}}
  ],
  "deletions": [
    {{"old": "要删除的原文片段"}}
  ]
}}

## 规则
1. **只改问题区域**：只能在下方审查指定的 old/after 范围内修改，操作类型也必须一致；不要改动其他位置。
2. **old/after 必须精确**：尽量引用原文中连续 30-200 字的完整段落，不要只给几个字，避免匹配失败。
3. **禁止伪原创**：未要求修改的句子必须原样保留，禁止为了"润色"改掉原本合格的文字。
4. **保持总字数在 {min_words}-{max_words} 字之间**；如果需要大幅删减，优先用 deletions；如果需要补充，用 insertions。
5. **所有 edit 必须解决 reviewer 指出的 weaknesses/suggestions/continuity_issues**。
6. 如果问题无法局部修复，输出 {{"edits": []}}。系统将保留原稿并报告失败，不会自动全文重写。
7. 不以提升分数为目标；不自动补潜台词、微表情、物象或强钩子。保持作者原有表达。"""

    prompt = f"""请对第{chapter_number}章进行定点增量修改。

## 审查意见
允许修改的范围（old/after 必须来自原稿，禁止扩大范围）：
{json.dumps(review_data.get('edits', []), ensure_ascii=False)}

**不足**：
{chr(10).join(f"- {w}" for w in weaknesses) or "（无）"}

**修改建议**：
{chr(10).join(f"- {s}" for s in suggestions) or "（无）"}

**连续性问题**：
{chr(10).join(f"- {c}" for c in continuity_issues) or "（无）"}

## 当前正文（请只修改问题部分，其余保留）
{old_text}

## 要求
1. 先分析每条审查意见对应正文中的哪个位置。
2. 输出 JSON edit ops，精确 apply 这些修改。
3. 未涉及修改的段落必须保持原样。
4. 输出合法 JSON，不要 Markdown 代码块。"""

    return system, prompt


def parse_edit_ops(content: str) -> list[dict]:
    """解析模型输出的 edit ops（兼容 replacements/insertions/deletions 或 type 字段）。"""
    content = content.strip()
    if content.startswith("```"):
        # 去掉 markdown 代码块
        lines = content.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        content = "\n".join(lines).strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise EditApplyError(f"edit ops JSON 解析失败: {e}")

    if not isinstance(data, dict):
        raise EditApplyError("edit ops 必须是 JSON object")

    edits: list[dict] = []

    # 兼容统一 type 格式
    for item in data.get("edits", []):
        if isinstance(item, dict):
            edits.append(item)

    # 兼容 replacements/insertions/deletions 格式
    for rep in data.get("replacements", []):
        if isinstance(rep, dict) and "old" in rep:
            edits.append({"type": "replace", "old": rep["old"], "new": rep.get("new", "")})
    for ins in data.get("insertions", []):
        if isinstance(ins, dict) and "after" in ins:
            edits.append({"type": "insert", "after": ins["after"], "text": ins.get("text", "")})
    for delete in data.get("deletions", []):
        if isinstance(delete, dict) and "old" in delete:
            edits.append({"type": "delete", "old": delete["old"]})

    return edits


def apply_json_field_edit(obj: dict, edit: dict) -> None:
    """对 dict 做字段级修改。

    edit 格式：
    - {"field": "summary", "action": "replace", "value": "..."}
    - {"field": "key_events", "action": "append", "value": "..."}
    - {"field": "key_events", "action": "replace_index", "index": 2, "value": "..."}
    """
    field = edit.get("field")
    action = edit.get("action", "replace")
    value = edit.get("value")
    if field not in obj:
        raise EditApplyError(f"字段 {field} 不存在")
    if action == "replace":
        obj[field] = value
    elif action == "append":
        current = obj[field]
        if not isinstance(current, list):
            raise EditApplyError(f"字段 {field} 不是列表，无法 append")
        current.append(value)
    elif action == "replace_index":
        current = obj[field]
        idx = edit.get("index")
        if not isinstance(current, list):
            raise EditApplyError(f"字段 {field} 不是列表，无法 replace_index")
        if idx is None or idx < 0 or idx >= len(current):
            raise EditApplyError(f"索引 {idx} 超出 {field} 范围")
        current[idx] = value
    elif action == "delete_index":
        current = obj[field]
        idx = edit.get("index")
        if not isinstance(current, list):
            raise EditApplyError(f"字段 {field} 不是列表，无法 delete_index")
        if idx is None or idx < 0 or idx >= len(current):
            raise EditApplyError(f"索引 {idx} 超出 {field} 范围")
        del current[idx]
    else:
        raise EditApplyError(f"未知字段 edit action: {action}")
