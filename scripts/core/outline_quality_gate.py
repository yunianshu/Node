from __future__ import annotations

import statistics
from typing import Any


REQUIRED_DESIGN_GATES = (
    "core_desire",
    "irreversible_choice",
    "midpoint_reversal",
    "strong_hook",
)


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
    median_score = float(statistics.median(scores)) if complete else 0.0
    score_pass_votes = sum(score >= min_score for score in scores)
    score_spread = max(scores) - min(scores) if complete else None
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
