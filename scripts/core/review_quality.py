#!/usr/bin/env python3
"""正文审查门禁的共享配置与判定。"""
from __future__ import annotations

from typing import Any
import hashlib
import math
from pathlib import Path


def read_manuscript(path: Path) -> str:
    """保留换行，报告摘要与发布正文使用同一文本表示。"""
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def review_matches_text(review: dict, text: str) -> bool:
    return bool(review.get("content_sha256")) and review["content_sha256"] == content_sha256(text)


DEFAULT_REVIEW_MIN_SCORE = 8.5
DEFAULT_SCENE_REALIZATION_MIN_RATE = 0.75
DEFAULT_MAX_REPAIR_TASKS = 4
DEFAULT_CRITICAL_DIMENSION_MIN_SCORES = {
    "character_voice": 8.0,
    "causal_logic": 8.0,
    "pacing": 8.0,
    "emotional_impact": 8.0,
    "information_freshness": 8.0,
    "payoff_intensity": 8.0,
    "read_desire": 8.0,
}
DIMENSION_LABELS = {
    "character_voice": "人物辨识度",
    "causal_logic": "因果逻辑",
    "pacing": "节奏把控",
    "emotional_impact": "情感冲击",
    "information_freshness": "信息新鲜度",
    "payoff_intensity": "爽点与反差",
    "read_desire": "读下去的欲望",
}


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def review_quality_settings(config: dict | None) -> dict:
    """读取兼容旧项目的正文质量门禁配置。"""
    source = config if isinstance(config, dict) else {}
    quality = source.get("quality") if isinstance(source.get("quality"), dict) else {}
    reviewer = source.get("reviewer") if isinstance(source.get("reviewer"), dict) else {}

    # 旧项目未声明 quality.review_min_score 时仍使用高质量默认值，而不继承过低的旧 reviewer.min_score。
    review_min_score = _bounded_float(
        quality.get("review_min_score", DEFAULT_REVIEW_MIN_SCORE),
        DEFAULT_REVIEW_MIN_SCORE,
        0.0,
        10.0,
    )
    golden_min_score = _bounded_float(
        reviewer.get("golden_chapter_min_score", 9.0),
        9.0,
        review_min_score,
        10.0,
    )
    scene_realization_min_rate = _bounded_float(
        quality.get("scene_realization_min_rate", DEFAULT_SCENE_REALIZATION_MIN_RATE),
        DEFAULT_SCENE_REALIZATION_MIN_RATE,
        0.0,
        1.0,
    )
    max_repair_tasks = _bounded_int(
        quality.get("max_repair_tasks", DEFAULT_MAX_REPAIR_TASKS),
        DEFAULT_MAX_REPAIR_TASKS,
        1,
        4,
    )

    configured_min_scores = quality.get("critical_dimension_min_scores")
    configured_min_scores = configured_min_scores if isinstance(configured_min_scores, dict) else {}
    critical_dimension_min_scores = {
        dimension: _bounded_float(
            configured_min_scores.get(dimension, default), default, 0.0, 10.0
        )
        for dimension, default in DEFAULT_CRITICAL_DIMENSION_MIN_SCORES.items()
    }
    return {
        "review_min_score": review_min_score,
        "golden_chapter_min_score": golden_min_score,
        "scene_realization_min_rate": scene_realization_min_rate,
        "max_repair_tasks": max_repair_tasks,
        "critical_dimension_min_scores": critical_dimension_min_scores,
    }


def evaluate_review_quality_gate(
    review_data: dict,
    local_analysis: dict | None,
    config: dict | None,
    *,
    base_reasons: list[str] | None = None,
) -> dict:
    """根据审稿分数和本地检查结果生成可持久化的质量门禁结论。"""
    settings = review_quality_settings(config)
    reasons = [str(item).strip() for item in (base_reasons or []) if str(item).strip()]
    score = review_data.get("overall_score") if isinstance(review_data, dict) else None
    if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 10:
        reasons.append("审稿总分缺失或非法")
    elif float(score) < settings["review_min_score"]:
        reasons.append(
            f"审稿总分未达到{settings['review_min_score']:g}分（当前{float(score):g}分）"
        )

    scores = review_data.get("scores") if isinstance(review_data, dict) else {}
    scores = scores if isinstance(scores, dict) else {}
    critical_scores: dict[str, float | None] = {}
    for dimension, minimum in settings["critical_dimension_min_scores"].items():
        label = DIMENSION_LABELS.get(dimension, dimension)
        value = scores.get(dimension)
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 10:
            critical_scores[dimension] = None
            reasons.append(f"关键维度{label}缺失")
            continue
        score_value = float(value)
        critical_scores[dimension] = score_value
        if score_value < minimum:
            reasons.append(
                f"关键维度{label}未达到{minimum:g}分（当前{score_value:g}分）"
            )

    analysis = local_analysis if isinstance(local_analysis, dict) else {}
    scene = analysis.get("scene_realization_detection")
    if isinstance(scene, dict) and scene.get("required") is True and not scene.get("advisory_only"):
        rate = scene.get("rate")
        if not isinstance(rate, (int, float)) or float(rate) < settings["scene_realization_min_rate"]:
            shown_rate = "未知" if not isinstance(rate, (int, float)) else f"{float(rate):g}"
            reasons.append(
                "本地场景兑现率未达到"
                f"{settings['scene_realization_min_rate']:g}（当前{shown_rate}）"
            )

    unique_reasons = list(dict.fromkeys(reasons))
    return {
        "passed": not unique_reasons,
        "reasons": unique_reasons,
        "review_min_score": settings["review_min_score"],
        "scene_realization_min_rate": settings["scene_realization_min_rate"],
        "critical_dimension_min_scores": settings["critical_dimension_min_scores"],
        "critical_scores": critical_scores,
    }
