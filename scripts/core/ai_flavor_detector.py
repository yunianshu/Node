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
            except Exception as exc:
                pass
    if DEFAULT_LEXICON.exists():
        try:
            return json.loads(DEFAULT_LEXICON.read_text(encoding="utf-8"))
        except Exception as exc:
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


def _detect_short_sentence_fragmentation(text: str) -> list[dict]:
    """高频1-3字断句：如“快。滚。”“走。了。结。”。"""
    sents = _split_sentences(text)
    if len(sents) < 20:
        return []
    short_flags = [
        1 if 1 <= sum(1 for c in sent if "\u4e00" <= c <= "\u9fff") <= 3 else 0
        for sent in sents
    ]
    short_count = sum(short_flags)
    max_run = 0
    current = 0
    for flag in short_flags:
        if flag:
            current += 1
            max_run = max(max_run, current)
        else:
            current = 0
    density = short_count / max(1, len(sents))
    forced = re.search(r"(?:[\u4e00-\u9fff]{1,3}[。！？]){3,}", text)
    if max_run >= 4 or (forced and density >= 0.18) or density >= 0.28:
        evidence = forced.group(0)[:60] if forced else f"短句占比 {density:.0%}，最长连续 {max_run} 句"
        return [{
            "type": "short_sentence_fragmentation",
            "severity": "major",
            "evidence": evidence,
            "suggestion": "禁止用句号硬拆主谓宾制造沉重感；改用动作、对白潜台词和段落节奏承载情绪",
        }]
    return []


def _detect_symmetric_anchor_ending(text: str) -> list[dict]:
    """机械化“身后困境/身前方向/一步又一步”式章末安全锁。"""
    tail = text[-500:] if len(text) > 500 else text
    patterns = [
        r"身后[^。！？]{0,45}[。！？]\s*身前",
        r"身前[^。！？]{0,45}[。！？]\s*身后",
        r"一步[，,、 ]*又一步",
        r"朝着[^。！？]{1,30}[，,]?\s*迈开[了的]?步",
        r"未知的路",
    ]
    hits = []
    for pat in patterns:
        match = re.search(pat, tail)
        if match:
            hits.append(match.group(0))
    if len(hits) >= 2:
        return [{
            "type": "symmetric_anchor_ending",
            "severity": "major",
            "evidence": "；".join(hits[:3])[:100],
            "suggestion": "章末不要机械打包“身后困境+身前方向+迈步决心”；改成未解决的现场动作、关系反应或具体反转",
        }]
    return []


def _detect_abstract_concept_pileup(text: str) -> list[dict]:
    """抽象修炼/概念名词替代落地描写。"""
    concept_terms = [
        "道意之核", "共鸣圈", "踏天之境", "正邪合一", "本源", "法则", "道意",
        "天命", "命格", "气运", "神魂", "真意", "因果", "轮回", "大道",
        "灵气", "经脉", "丹田", "识海", "境界", "共鸣",
    ]
    hits = sum(text.count(term) for term in concept_terms)
    suffix_hits = len(re.findall(r"[\u4e00-\u9fff]{1,6}之(?:核|境|力|门|心|道|意|源)", text))
    total_hits = hits + suffix_hits
    density = _count_per_kanji(text, total_hits)
    concrete_terms = [
        "饭", "碗", "门", "鞋", "袖口", "账", "药", "汗", "血", "泥", "雨棚",
        "灯", "窗", "桌", "椅", "手机", "票", "墙", "井", "炉", "摊", "街",
    ]
    concrete_hits = sum(text.count(term) for term in concrete_terms)
    if density >= 4.0 and concrete_hits < max(3, total_hits // 3):
        return [{
            "type": "abstract_concept_pileup",
            "severity": "major",
            "evidence": f"抽象修炼/概念词密度 {density:.1f}/千字（约{total_hits}处），具象生活物件偏少",
            "suggestion": "把概念变化落到可见代价、身体反应、器物变化、旁人误解或环境后果上",
        }]
    return []


def _normalize_refrain_sentence(sentence: str) -> str:
    sentence = re.sub(r"[，,；;：:\s]+", "", sentence)
    sentence = re.sub(r"[\u4e00-\u9fff]{1,3}", lambda m: m.group(0), sentence)
    return sentence[:42]


def _detect_formal_refrain_stagnation(text: str) -> list[dict]:
    """Detect prose-poem style repeated paragraph frames that freeze narrative time."""
    paras = _split_paragraphs(text)
    if len(paras) < 8:
        return []
    issues: list[dict] = []

    frame_hits = 0
    frame_examples: list[str] = []
    for para in paras:
        head = para[:80]
        if re.search(r"^[^。！？]{1,24}(?:边|里|外|下|旁|处|前|后)?[，,][^。！？]{1,24}被[^。！？]{1,30}衬得格外扎眼", head):
            frame_hits += 1
            if len(frame_examples) < 2:
                frame_examples.append(head[:60])
    if frame_hits >= 5 or frame_hits / len(paras) >= 0.35:
        issues.append({
            "type": "formal_refrain_stagnation",
            "severity": "major",
            "evidence": f"重复段式 {frame_hits}/{len(paras)} 段：" + " / ".join(frame_examples),
            "suggestion": "打散散文诗式段落模板；每段改为新的行动、反应、阻碍或信息变化，而不是同一聚焦句式变奏",
        })

    sentence_counts: dict[str, tuple[int, str]] = {}
    for sent in re.split(r"[。！？!?]", text):
        sent = sent.strip()
        cjk_len = sum(1 for c in sent if "\u4e00" <= c <= "\u9fff")
        if cjk_len < 18:
            continue
        key = _normalize_refrain_sentence(sent)
        if len(key) < 18:
            continue
        count, sample = sentence_counts.get(key, (0, sent[:80]))
        sentence_counts[key] = (count + 1, sample)
    repeated = [(count, sample) for count, sample in sentence_counts.values() if count >= 4]
    if repeated:
        count, sample = max(repeated, key=lambda item: item[0])
        issues.append({
            "type": "repeated_authorial_judgment",
            "severity": "major",
            "evidence": f"判断句重复 {count} 次：{sample}",
            "suggestion": "删除反复替读者下判断的锚点句；用一次不可逆动作或一句潜台词让读者自己感到重量",
        })

    action_verbs = (
        "推", "拉", "递", "藏", "抢", "追", "逃", "打", "砸", "撕", "签", "跪",
        "喊", "问", "答", "拦", "挡", "掀", "翻", "塞", "合", "摔", "拔", "照",
        "抓", "放", "取", "交", "烧", "封", "开", "关", "走", "跑",
    )
    static_markers = ("扎眼", "格外", "像", "仿佛", "沉默", "雨", "衬得")
    static_like = 0
    low_action = 0
    for para in paras:
        action_count = sum(para.count(v) for v in action_verbs)
        marker_count = sum(para.count(v) for v in static_markers)
        if marker_count >= 2 and action_count <= 2:
            static_like += 1
        if action_count <= 1:
            low_action += 1
    if len(paras) >= 12 and static_like / len(paras) >= 0.45 and low_action / len(paras) >= 0.35:
        issues.append({
            "type": "static_lyrical_scene",
            "severity": "major",
            "evidence": f"静态意象段偏多 {static_like}/{len(paras)}，低行动段 {low_action}/{len(paras)}",
            "suggestion": "把静态意象改成历时事件：目标受阻、人物行动、关系反应、信息变化和不可逆后果",
        })
    return issues


def _detect_authorial_aside(text: str) -> list[dict]:
    """Detect high-confidence author commentary that explains the chapter's meaning."""
    patterns = [
        r"这一章[^。！？]{0,24}(?:往前挪|真正|意味着|象征|核心|关键)",
        r"这就是(?:小说|故事|本章)[^。！？]{0,24}(?:应该|要做|想要)",
        r"读者(?:终于|能|会|应该|可以)[^。！？]{0,30}(?:看见|明白|知道|感到|意识到)",
        r"权力最怕的不是[^。！？]{4,80}是[^。！？]{4,80}",
        r"真正让[^。！？]{1,30}(?:立住|成立|改变|推进)的[^。！？]{0,50}是",
        r"他终于知道自己[^。！？]{0,30}往前挪[^。！？]{0,30}是什么",
    ]
    hits: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            sample = match.group(0).strip()
            if sample and sample not in hits:
                hits.append(sample[:90])
            if len(hits) >= 3:
                break
        if len(hits) >= 3:
            break
    if not hits:
        return []
    return [{
        "type": "authorial_aside",
        "severity": "major",
        "evidence": " / ".join(hits),
        "suggestion": "删除作者旁注式总结；把判断交给动作、物件、对白和他人反应，让读者自行得出结论",
    }]


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
            "suggestion": "调整句长起伏，但不要高频使用1-3字断句；用动作、对白和段落结构制造节奏",
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
        + _detect_short_sentence_fragmentation(text)
        + _detect_symmetric_anchor_ending(text)
        + _detect_abstract_concept_pileup(text)
        + _detect_formal_refrain_stagnation(text)
        + _detect_authorial_aside(text)
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
    fractured = "他。没有，哭。走。了。结。快。滚。" * 4 + "门外有人敲门，杯子在桌沿晃了一下。"
    r4 = detect_ai_flavor(fractured)
    assert "short_sentence_fragmentation" in {i["type"] for i in r4["issues"]}, r4
    symmetric = "他把信塞进袖口，推门出去。" * 20 + "身后，追兵逼近。身前，是未知的路。他朝着东方，迈开了步。一步，又一步。"
    r5 = detect_ai_flavor(symmetric)
    assert "symmetric_anchor_ending" in {i["type"] for i in r5["issues"]}, r5
    concept = "道意之核在共鸣圈中震动，踏天之境的本源法则牵动神魂经脉。" * 20
    r6 = detect_ai_flavor(concept)
    assert "abstract_concept_pileup" in {i["type"] for i in r6["issues"]}, r6
    refrain = "\n\n".join(
        f"井亭边，旧物{i}被雨声衬得格外扎眼。沈砚没有把话说满，他只看了一眼苏雯。这里不是给他升级的秘境，是一条街的药钱和旧案压出来的窄路。"
        for i in range(12)
    )
    r7 = detect_ai_flavor(refrain)
    types7 = {i["type"] for i in r7["issues"]}
    assert "formal_refrain_stagnation" in types7, r7
    assert "repeated_authorial_judgment" in types7, r7
    aside = (
        "沈砚把墨印推到案中央。读者终于能看见他真正往前挪了一步。"
        "权力最怕的不是一个少年逞强，是一条街忽然都记起自己看见过什么。"
    )
    r8 = detect_ai_flavor(aside)
    assert "authorial_aside" in {i["type"] for i in r8["issues"]}, r8
    print("ai_flavor_detector selftest OK")


if __name__ == "__main__":
    _selftest()
