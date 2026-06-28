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


def _phrase_density(text: str, phrases: list[str]) -> tuple[int, float]:
    """返回 (命中次数, 每千字密度)。空词表返回 (0, 0)。"""
    if not phrases:
        return 0, 0.0
    hits = sum(text.count(w) for w in phrases)
    return hits, _count_per_kanji(text, hits)


def _split_sentences(text: str) -> list[str]:
    """按中英文句末标点切句，过滤空串。"""
    parts = re.split(r"[。！？!?；;\n]+", text)
    return [s.strip() for s in parts if s.strip()]


def _detect_simile(text: str, lex: dict) -> list[dict]:
    """比喻标记（仿佛/宛如/如同…）过密。比喻本身合法，只在密度过高时报。"""
    words = lex.get("simile_markers", [])
    hits, density = _phrase_density(text, words)
    if density >= 3.0:
        return [{
            "type": "simile_overuse", "severity": "minor",
            "evidence": f"比喻标记密度 {density:.1f}/千字（{hits}处）",
            "suggestion": "精简比喻，优先用直接动作和具象细节，一章核心比喻不超过2-3个",
        }]
    return []


def _detect_micro_expressions(text: str, lex: dict) -> list[dict]:
    """AI 最爱的伪微表情/内心独白套路（眼中闪过一丝/嘴角微微上扬/心中暗道…）。"""
    words = lex.get("micro_expression_cliches", [])
    hits, density = _phrase_density(text, words)
    if density >= 0.8:
        return [{
            "type": "micro_expression_cliche", "severity": "minor",
            "evidence": f"套路化微表情/内心独白密度 {density:.1f}/千字（{hits}处）",
            "suggestion": "删除'眼中闪过一丝/嘴角微微上扬/心中暗道'，用具体动作、对白或环境细节外化情绪",
        }]
    return []


def _detect_soft_adverbs(text: str, lex: dict) -> list[dict]:
    """软调副词（缓缓/微微/默默/淡淡…）过密，AI 常用营造画面感。"""
    words = lex.get("soft_adverbs", [])
    hits, density = _phrase_density(text, words)
    if density >= 3.5:
        return [{
            "type": "soft_adverb_overuse", "severity": "minor",
            "evidence": f"软调副词密度 {density:.1f}/千字（{hits}处）",
            "suggestion": "删去冗余'缓缓/微微/默默'，让动词本身承担动作质感",
        }]
    return []


def _detect_reflexive(text: str, lex: dict) -> list[dict]:
    """反射性套路（不由得/忍不住/情不自禁…）。"""
    words = lex.get("reflexive_cliches", [])
    hits, density = _phrase_density(text, words)
    if density >= 1.2:
        return [{
            "type": "reflexive_cliche", "severity": "minor",
            "evidence": f"反射性套路密度 {density:.1f}/千字（{hits}处）",
            "suggestion": "减少'不由得/忍不住'，直接写动作或省略心理过渡",
        }]
    return []


def _detect_essay_connectives(text: str, lex: dict) -> list[dict]:
    """议论文连接词泄漏进小说（毫无疑问/众所周知/总而言之…），小说正文几乎不该出现。"""
    words = lex.get("essay_connectives", [])
    if not words:
        return []
    hits = sum(text.count(w) for w in words)
    if hits <= 0:
        return []
    sev = "major" if hits >= 3 else "minor"
    return [{
        "type": "essay_connective", "severity": sev,
        "evidence": f"议论文连接词 {hits} 处（小说正文应近乎为零）",
        "suggestion": "删除'毫无疑问/众所周知/总而言之/不得不说'等连接词，让叙事本身推进",
    }]


def _detect_temporal_fillers(text: str, lex: dict) -> list[dict]:
    """时间填充词（顿时/霎时间/刹那间…）过密。"""
    words = lex.get("temporal_fillers", [])
    hits, density = _phrase_density(text, words)
    if density >= 1.8:
        return [{
            "type": "temporal_filler", "severity": "minor",
            "evidence": f"时间填充词密度 {density:.1f}/千字（{hits}处）",
            "suggestion": "减少'顿时/霎时间'，用动作节奏本身制造紧迫感",
        }]
    return []


def _detect_dash_overuse(text: str, lex: dict) -> list[dict]:
    """破折号 —— 作为戏剧停顿的滥用。"""
    dash = lex.get("drama_dash", "——")
    hits = text.count(dash)
    density = _count_per_kanji(text, hits)
    if density >= 4.0:
        return [{
            "type": "dash_overuse", "severity": "minor",
            "evidence": f"破折号密度 {density:.1f}/千字（{hits}处）",
            "suggestion": "减少破折号停顿，改用短句或动作断句",
        }]
    return []


def _detect_sentence_uniformity(text: str) -> list[dict]:
    """句长方差过低（burstiness 不足）—— AI 句子长度过于均匀。需 >=8 句才可靠。"""
    sents = _split_sentences(text)
    if len(sents) < 8:
        return []
    lengths = [sum(1 for c in s if "一" <= c <= "鿿") for s in sents]
    lengths = [l for l in lengths if l > 0]
    if len(lengths) < 8:
        return []
    mean = sum(lengths) / len(lengths)
    if mean <= 0:
        return []
    var = sum((l - mean) ** 2 for l in lengths) / len(lengths)
    cv = (var ** 0.5) / mean
    if cv < 0.40:
        return [{
            "type": "sentence_uniformity", "severity": "minor",
            "evidence": f"句长变异系数 {cv:.2f}（句子长度过于均匀）",
            "suggestion": "长短句交替：用1-3字的短句打断，再接长句铺陈，制造节奏起伏",
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
    """检测 AI 味。返回 {ai_flavor_score, issues}。

    信号分两类：(1) 结构化句式/收尾/元叙述/告知——命中即报；
    (2) 词频密度（比喻/微表情套路/软调副词/反射套路/议论文连接词/时间填充/破折号）+ 节奏方差——
    只在密度超阈值时报，避免对正常网文误伤。
    """
    text = str(text or "")
    lex = load_lexicon(project)
    issues = (
        _detect_parallel(text, lex)
        + _detect_summary_ending(text, lex)
        + _detect_adjective_pileup(text, lex)
        + _detect_meta(text, lex)
        + _detect_telling(text, lex)
        + _detect_low_variance(text)
        + _detect_simile(text, lex)
        + _detect_micro_expressions(text, lex)
        + _detect_soft_adverbs(text, lex)
        + _detect_reflexive(text, lex)
        + _detect_essay_connectives(text, lex)
        + _detect_temporal_fillers(text, lex)
        + _detect_dash_overuse(text, lex)
        + _detect_sentence_uniformity(text)
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
        "他不仅感受到了磅礴的力量，而且看到了璀璨的光芒，更明白了恐怖的真相。\n\n"
        "他心中涌起一股暖流。他意识到，命运亘古不变。\n\n"
        "故事才刚刚开始。"
    )
    r2 = detect_ai_flavor(ai_heavy)
    assert r2["ai_flavor_score"] < 7.0, r2
    types = {i["type"] for i in r2["issues"]}
    assert "parallel_sentiment" in types, types
    assert "summary_ending" in types, types
    assert "telling_not_showing" in types, types
    assert "micro_expression_cliche" in types, types  # '心中涌起' 是套路化微表情/内心独白
    # 新增：正常长段落不应被句长方差误报（burstiness 检查需 >=8 句，此样本短句不应触发）
    r3 = detect_ai_flavor(
        "他没说话。\n\n风很大。\n\n她把杯子放下，转过身去看窗外，雨点正密密地砸在玻璃上，"
        "顺着纹路往下淌，像谁在窗户上写了一封没有收件人的信。\n\n"
        "他想说点什么，最终只是把外套披在她肩上。\n\n门外的脚步声停了。"
    )
    assert r3["ai_flavor_score"] >= 7.5, r3
    print("ai_flavor_detector selftest OK")


if __name__ == "__main__":
    _selftest()
