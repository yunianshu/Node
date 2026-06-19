from __future__ import annotations

import re
import statistics
from typing import Any


REQUIRED_DESIGN_GATES = (
    "core_desire",
    "irreversible_choice",
    "midpoint_reversal",
    "strong_hook",
)


def cast_name_set(characters: Any) -> set[str]:
    """从 characters.json 提取全部已知角色名（规范名 + 已登记别名），用于闭环角色校验。"""
    names: set[str] = set()
    if isinstance(characters, dict):
        char_list = characters.get("characters", [])
    else:
        char_list = characters
    if isinstance(char_list, list):
        for c in char_list:
            if not isinstance(c, dict):
                continue
            n = str(c.get("name", "")).strip()
            if n:
                names.add(n)
            aliases = c.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            for a in aliases:
                a = str(a).strip()
                if a:
                    names.add(a)
    return names


def clean_char_name(entry: Any) -> str:
    """从 characters_involved 条目提取裸规范名：
    去掉括号注释（'苏雯（记者）'→'苏雯'）和 '类别：' 前缀（'反派：高建军'→'高建军'）。
    整句描述/群体（'受害者家属'）原样保留，随后由 unregistered 判定。"""
    s = str(entry).strip()
    s = re.sub(r"[（(][^）)]*[）)]", "", s).strip()
    if re.search(r"[：:]", s):
        s = re.split(r"[：:]", s)[-1].strip()
    return s


def detect_cast_violations(
    characters_involved: Any, cast_names: set[str]
) -> dict[str, list[str]]:
    """闭环角色校验（人物漂移根因）：characters_involved 必须 ⊂ 已登记角色名册。

    返回 {polluted:[...], unregistered:[...]}：
    - polluted: 带括号注释/'类别：'前缀的非规范写法（如 '苏雯（记者）'）；
    - unregistered: 清洗后仍不在名册的名字（临时生造角色、群体描述、未登记别名）。
    任一非空即说明出场角色未在前期锁定，是 world_consistency 漂移的直接来源。
    """
    polluted: list[str] = []
    unregistered: list[str] = []
    if isinstance(characters_involved, str):
        characters_involved = [characters_involved]
    if not isinstance(characters_involved, list):
        return {"polluted": [], "unregistered": []}
    for entry in characters_involved:
        raw = str(entry).strip()
        if not raw:
            continue
        cleaned = clean_char_name(entry)
        if cleaned != raw:
            polluted.append(raw)
        if cleaned and cleaned not in cast_names:
            unregistered.append(cleaned)
    return {
        "polluted": list(dict.fromkeys(polluted)),
        "unregistered": list(dict.fromkeys(unregistered)),
    }


def normalize_beat(beat: Any) -> str:
    """归一化 story_beat 标签，便于跨章比较。"""
    return str(beat or "").strip().lower().replace("-", "_").replace(" ", "_")


def detect_beat_runs(
    beats: list[tuple[int, str]], *, max_consecutive: int = 2
) -> list[dict[str, Any]]:
    """检测连续相同 story_beat 的平推段——注水腰的根因。

    AI 单章审查只能判断 beat 是否"名实相符"，结构上看不到连续多章同 beat
    造成的节奏塌陷。本函数做跨章确定性检查。

    beats: [(chapter_number, story_beat), ...]，按章序升序，缺 beat 的章已剔除。
    连续（章号相邻且 beat 相同）数量 > max_consecutive 即报一条 issue。
    返回 [{type, chapters, beat, run_length, evidence, suggestion}]。
    """
    issues: list[dict[str, Any]] = []
    norm = [(ch, normalize_beat(b)) for ch, b in beats if normalize_beat(b)]
    i = 0
    while i < len(norm):
        j = i
        while (
            j + 1 < len(norm)
            and norm[j + 1][0] == norm[j][0] + 1
            and norm[j + 1][1] == norm[i][1]
        ):
            j += 1
        run_len = j - i + 1
        if run_len > max_consecutive:
            chs = [norm[k][0] for k in range(i, j + 1)]
            beat = norm[i][1]
            issues.append({
                "type": "flat_beat_run",
                "chapters": chs,
                "beat": beat,
                "run_length": run_len,
                "evidence": f"第{chs[0]}-{chs[-1]}章连续{run_len}章同为'{beat}'，无转折，构成注水腰",
                "suggestion": (
                    f"打破连续'{beat}'：把其中一章改为不同转折 beat（all_is_lost 谷底 / "
                    f"midpoint 赌注升级 / catalyst 新催化 / dark_moment），让每章推进不同结构功能"
                ),
            })
        i = j + 1
    return issues



def design_gate_passed(review: dict[str, Any], gate_name: str) -> bool:
    gates = review.get("design_gates")
    if not isinstance(gates, dict):
        return False
    gate = gates.get(gate_name)
    return (
        isinstance(gate, dict)
        and gate.get("passed") is True
        and isinstance(gate.get("evidence"), str)
        and bool(gate["evidence"].strip())
    )


def aggregate_outline_reviews(
    chapter: int,
    reviews: list[dict[str, Any]],
    *,
    min_score: float,
    required_rounds: int,
    required_votes: int,
    max_score_spread: float,
) -> dict[str, Any]:
    scores = [
        float(review["overall_score"])
        for review in reviews
        if isinstance(review.get("overall_score"), (int, float))
    ]
    gate_votes = {
        gate: sum(design_gate_passed(review, gate) for review in reviews)
        for gate in REQUIRED_DESIGN_GATES
    }
    complete = len(reviews) == required_rounds and len(scores) == required_rounds
    median_score = float(statistics.median(scores)) if scores else 0.0
    score_pass_votes = sum(score >= min_score for score in scores)
    score_spread = max(scores) - min(scores) if len(scores) >= 2 else None
    score_stable = score_spread is not None and score_spread <= max_score_spread
    all_gates_pass = all(votes >= required_votes for votes in gate_votes.values())
    passed = (
        complete
        and median_score >= min_score
        and score_pass_votes >= required_votes
        and all_gates_pass
    )

    aggregate_gates: dict[str, Any] = {}
    for gate_name in REQUIRED_DESIGN_GATES:
        evidence = [
            review.get("design_gates", {}).get(gate_name, {}).get("evidence", "")
            for review in reviews
            if design_gate_passed(review, gate_name)
        ]
        aggregate_gates[gate_name] = {
            "passed": gate_votes[gate_name] >= required_votes,
            "votes": gate_votes[gate_name],
            "required_votes": required_votes,
            "evidence": next((item for item in evidence if item), ""),
        }

    weaknesses: list[str] = []
    suggestions: list[str] = []
    continuity_issues: list[str] = []
    for review in reviews:
        weaknesses.extend(str(item) for item in review.get("weaknesses", [])[:3])
        suggestions.extend(str(item) for item in review.get("suggestions", [])[:3])
        continuity_issues.extend(str(item) for item in review.get("continuity_issues", [])[:2])

    return {
        "chapter_number": chapter,
        "status": "completed" if complete else "incomplete_rounds",
        "overall_score": round(median_score, 3),
        "verdict": "通过" if passed else "需修改",
        "scores": {
            "round_scores": scores,
            "median": round(median_score, 3),
            "pass_votes": score_pass_votes,
            "required_votes": required_votes,
            "spread": round(score_spread, 3) if score_spread is not None else None,
        },
        "design_gates": aggregate_gates,
        "design_gate_passed": all_gates_pass,
        "quality_gate": {
            "passed": passed,
            "required_rounds": required_rounds,
            "completed_rounds": len(reviews),
            "min_score": min_score,
            "score_pass_votes": score_pass_votes,
            "required_votes": required_votes,
            "score_stable": score_stable,
            "max_score_spread": max_score_spread,
        },
        "review_rounds": reviews,
        "strengths": list(dict.fromkeys(
            str(item)
            for review in reviews
            for item in review.get("strengths", [])[:2]
        ))[:3],
        "weaknesses": list(dict.fromkeys(weaknesses))[:6],
        "suggestions": list(dict.fromkeys(suggestions))[:6],
        "continuity_issues": list(dict.fromkeys(continuity_issues))[:4],
        "summary": (
            f"{required_rounds}轮审查中位分{median_score:g}，"
            f"评分通过{score_pass_votes}/{required_votes}票，"
            f"设计门票数{gate_votes}。"
        ),
    }
