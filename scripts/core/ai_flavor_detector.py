#!/usr/bin/env python3
"""正文 AI 味确定性检测。不耗 API。

基于 StoryScope 30特征精神 + 中文网文通病：排比抒情、总结收尾、形容词堆砌、
低段落方差（过于工整）、元叙述、告知而非展示。

返回 ai_flavor_score（0-10，越高越自然）+ 定位到段落的问题清单。
词表可被 projects/<book>/ai_flavor_lexicon.json 覆盖。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

DEFAULT_LEXICON = Path(__file__).resolve().parent / "ai_flavor_lexicon.json"


def load_lexicon(project: Path | None = None) -> dict:
    if project is not None:
        override = project / "ai_flavor_lexicon.json"
        if override.exists():
            try:
                return json.loads(override.read_text(encoding="utf-8"))
            except Exception:
                pass
    if DEFAULT_LEXICON.exists():
        try:
            return json.loads(DEFAULT_LEXICON.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _count_per_kanji(text: str, hits: int) -> float:
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    return hits / (cjk / 1000) if cjk else 0.0


def _detect_parallel(text: str, lex: dict) -> list[dict]:
    issues = []
    for pat in lex.get("parallel_patterns", []):
        try:
            for m in re.finditer(pat, text):
                issues.append({
                    "type": "parallel_sentiment", "severity": "major",
                    "evidence": m.group(0)[:60],
                    "suggestion": "打破排比工整，改为长短句交替 + 具体动作",
                })
        except re.error:
            continue
    return issues


def _detect_summary_ending(text: str, lex: dict) -> list[dict]:
    tail = text[-300:] if len(text) > 300 else text
    issues = []
    for phrase in lex.get("summary_ending_phrases", []):
        if phrase in tail:
            issues.append({
                "type": "summary_ending", "severity": "major",
                "evidence": phrase,
                "suggestion": "章末禁止总结收尾，改为未决的威胁或未说出口的话",
            })
    return issues


def _detect_adjective_pileup(text: str, lex: dict) -> list[dict]:
    hits = sum(text.count(w) for w in lex.get("adjective_pileup", []))
    density = _count_per_kanji(text, hits)
    if density >= 2.0:
        return [{
            "type": "adjective_pileup", "severity": "minor",
            "evidence": f"高频AI形容词密度 {density:.1f}/千字",
            "suggestion": "删除空泛形容词，替换为可感感官细节",
        }]
    return []


def _detect_meta(text: str, lex: dict) -> list[dict]:
    issues = []
    for phrase in lex.get("meta_narration", []):
        if phrase in text:
            issues.append({
                "type": "meta_narration", "severity": "major",
                "evidence": phrase,
                "suggestion": "删除生成痕迹/元叙述",
            })
    return issues


def _detect_telling(text: str, lex: dict) -> list[dict]:
    hits = sum(text.count(v) for v in lex.get("telling_verbs", []))
    density = _count_per_kanji(text, hits)
    if density >= 1.0:
        return [{
            "type": "telling_not_showing", "severity": "minor",
            "evidence": f"'感到/意识到'类告知密度 {density:.1f}/千字",
            "suggestion": "用动作和物象替代直接告知情绪",
        }]
    return []


def _detect_low_variance(text: str) -> list[dict]:
    paras = _split_paragraphs(text)
    if len(paras) < 6:
        return []
    lengths = [sum(1 for c in p if "\u4e00" <= c <= "\u9fff") for p in paras]
    mean = sum(lengths) / len(lengths) if lengths else 0
    if mean <= 0:
        return []
    var = sum((l - mean) ** 2 for l in lengths) / len(lengths)
    cv = (var ** 0.5) / mean  # 变异系数
    if cv < 0.4:
        return [{
            "type": "low_variance", "severity": "minor",
            "evidence": f"段落长度变异系数 {cv:.2f}（过于工整）",
            "suggestion": "穿插1-2句短段落制造停顿，再用长段落铺陈",
        }]
    return []


def _locate_paragraph(paras: list[str], evidence: str) -> str:
    if not evidence or not paras:
        return "未定位"
    for i, p in enumerate(paras, 1):
        if evidence[:20] in p:
            return f"第{i}段"
    return "未定位"


def detect_ai_flavor(text: str, project: Path | None = None) -> dict:
    """检测 AI 味。返回 {ai_flavor_score, issues}。"""
    text = str(text or "")
    lex = load_lexicon(project)
    issues = (
        _detect_parallel(text, lex)
        + _detect_summary_ending(text, lex)
        + _detect_adjective_pileup(text, lex)
        + _detect_meta(text, lex)
        + _detect_telling(text, lex)
        + _detect_low_variance(text)
    )
    # 加权扣分
    penalty = 0.0
    for it in issues:
        sev = it.get("severity")
        penalty += {"major": 1.5, "minor": 0.6}.get(sev, 0.5)
    score = max(0.0, min(10.0, 10.0 - penalty))
    # 给每个 issue 定位到段落
    paras = _split_paragraphs(text)
    for it in issues:
        it["paragraph"] = _locate_paragraph(paras, it.get("evidence", ""))
    return {"ai_flavor_score": round(score, 2), "issues": issues}


def _selftest() -> None:
    natural = "他站起身。窗外有风。\n\n杯子很凉。他没说话，把信折好放进抽屉。\n\n门外的脚步声停了。"
    r1 = detect_ai_flavor(natural)
    assert r1["ai_flavor_score"] >= 8.5, r1
    ai_heavy = (
        "苏长空不仅感受到了磅礴的力量，而且看到了璀璨的光芒，更明白了恐怖的真相。\n\n"
        "他心中涌起一股暖流。他意识到，命运亘古不变。\n\n"
        "故事才刚刚开始。"
    )
    r2 = detect_ai_flavor(ai_heavy)
    assert r2["ai_flavor_score"] < 7.0, r2
    types = {i["type"] for i in r2["issues"]}
    assert "parallel_sentiment" in types, types
    assert "summary_ending" in types, types
    assert "telling_not_showing" in types, types
    print("ai_flavor_detector selftest OK")


if __name__ == "__main__":
    _selftest()
