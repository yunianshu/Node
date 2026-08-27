"""JSON 状态机式引号修复工具。"""
from __future__ import annotations

import json as _json
from typing import Optional


def _cjk_count(text: str) -> int:
    return sum(1 for ch in text if "一" <= ch <= "鿿")


def repair_latin1_gbk_mojibake(text: str) -> str:
    """如果文本里几乎没有 CJK 字符，怀疑是 latin1 误读 GBK 字节，尝试反转。"""
    if not text or _cjk_count(text) > 0:
        return text
    try:
        repaired = text.encode("latin1").decode("gbk")
    except UnicodeError:
        return text
    if _cjk_count(repaired) > _cjk_count(text):
        return repaired
    return text


def fix_inner_quotes(text: str) -> str:
    """状态机：识别 JSON 字符串边界，将字符串内未转义的 " 替换为单引号。"""
    out = []
    i = 0
    in_string = False
    n = len(text)
    while i < n:
        ch = text[i]
        if not in_string:
            if ch == '"':
                in_string = True
                out.append(ch)
            else:
                out.append(ch)
        else:
            if ch == "\\":
                out.append(ch)
                if i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
            elif ch == '"':
                # 跳过空白看下一个非空字符
                j = i + 1
                while j < n and text[j] in " \t\n\r":
                    j += 1
                next_ch = text[j] if j < n else ""
                if next_ch in (":", ",", "}", "]", ""):
                    in_string = False
                    out.append(ch)
                else:
                    out.append("'")
            else:
                out.append(ch)
        i += 1
    return "".join(out)


def fix_truncated_json(text: str) -> str:
    """修复被截断的 JSON：找到最后一个完整的 } 或 ], 截断到那里并补全开括号。"""
    # 状态机跟踪未闭合的 [ 和 {
    stack = []
    in_string = False
    escape = False
    last_valid_end = 0  # 最后一个安全可截断位置
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == '{':
                stack.append('}')
                last_valid_end = i + 1
            elif ch == '[':
                stack.append(']')
                last_valid_end = i + 1
            elif ch in ('}', ']'):
                if stack and stack[-1] == ch:
                    stack.pop()
                    last_valid_end = i + 1
                # 多了闭合符号（说明这之后是垃圾），截断
                else:
                    return text[:i]
    # 截到 last_valid_end 并补全未闭合的括号
    if stack:
        return text[:last_valid_end] + ''.join(reversed(stack))
    if last_valid_end > 0:
        return text[:last_valid_end]
    return text


def strip_json_markdown(content: str) -> str:
    """剥离 ```json ... ``` 或 ``` ... ``` 包裹。"""
    if not content:
        return content
    if "```json" in content:
        return content.split("```json", 1)[1].split("```", 1)[0].strip()
    if "```" in content:
        return content.split("```", 1)[1].split("```", 1)[0].strip()
    return content.strip()


def parse_score(val):
    """将评分统一转为 float；无法转换时原样返回。"""
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

