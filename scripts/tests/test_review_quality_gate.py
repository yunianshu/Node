#!/usr/bin/env python3
"""正文审查质量门禁的隔离回归测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from core.review_quality import (
    DEFAULT_CRITICAL_DIMENSION_MIN_SCORES,
    evaluate_review_quality_gate,
)
from core.workflow_state import load_review_status
from pipeline.reviewer import _build_repair_tasks, _normalize_review_tasks
from pipeline.writer import _load_feedback_as_review


def build_review(*, overall_score: float = 8.5, **score_overrides: float) -> dict:
    scores = dict(DEFAULT_CRITICAL_DIMENSION_MIN_SCORES)
    scores.update(score_overrides)
    return {
        "chapter_number": 1,
        "status": "completed",
        "overall_score": overall_score,
        "verdict": "通过",
        "scores": scores,
        "summary": "隔离测试报告",
        "strengths": [], "weaknesses": [], "suggestions": [],
        "continuity_issues": [], "edits": [],
    }


class ReviewQualityGateTest(unittest.TestCase):
    def test_advisory_scene_mismatch_does_not_block(self) -> None:
        result = evaluate_review_quality_gate(
            build_review(),
            {"scene_realization_detection": {"required": True, "rate": 0, "advisory_only": True}},
            {},
        )
        self.assertTrue(result["passed"])

    def test_scene_realization_half_is_rejected(self) -> None:
        review = build_review()
        result = evaluate_review_quality_gate(
            review,
            {"scene_realization_detection": {"required": True, "rate": 0.5}},
            {},
        )

        self.assertFalse(result["passed"])
        self.assertIn("本地场景兑现率未达到0.75（当前0.5）", result["reasons"])

    def test_critical_dimension_below_threshold_is_rejected(self) -> None:
        review = build_review(character_voice=7.5)
        result = evaluate_review_quality_gate(review, {}, {})

        self.assertFalse(result["passed"])
        self.assertIn("关键维度人物辨识度未达到8分（当前7.5分）", result["reasons"])

    def test_qualified_review_passes_gate(self) -> None:
        review = build_review()
        result = evaluate_review_quality_gate(
            review,
            {"scene_realization_detection": {"required": True, "rate": 0.75}},
            {},
        )

        self.assertTrue(result["passed"])
        self.assertEqual([], result["reasons"])

    def test_workflow_rejects_report_without_passing_gate(self) -> None:
        review = build_review()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "review.json"
            path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
            _, status, _, passed = load_review_status(path, 8.5)
            self.assertEqual("quality_gate_failed", status)
            self.assertFalse(passed)

            review["quality_gate"] = {"passed": True, "reasons": []}
            path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
            _, status, _, passed = load_review_status(path, 8.5)
            self.assertEqual("completed", status)
            self.assertTrue(passed)

    def test_reviewer_keeps_a_bounded_repair_task_set(self) -> None:
        review = build_review()
        review.update({
            "strengths": ["优点一", "优点二", "优点三"],
            "weaknesses": ["问题一", "问题二", "问题三", "问题四", "问题五"],
            "suggestions": ["建议一", "建议二", "建议三", "建议四", "建议五"],
            "continuity_issues": ["连续性一", "连续性二", "连续性三", "连续性四", "连续性五"],
            "edits": [{"type": "replace"}] * 5,
        })

        _normalize_review_tasks(review, 4)
        tasks = _build_repair_tasks(review, ["门禁问题一", "门禁问题二"], 4)

        self.assertEqual(2, len(review["strengths"]))
        self.assertEqual(4, len(review["weaknesses"]))
        self.assertEqual(4, len(review["suggestions"]))
        self.assertEqual(4, len(review["edits"]))
        self.assertEqual(4, len(tasks))

    def test_writer_receives_all_bounded_repair_tasks(self) -> None:
        payload = {
            "1": {
                "failure_analysis": {
                    "targeted_repairs": ["硬修复一", "硬修复二"],
                    "adjustments": ["后备建议"],
                },
                "reviews": [{
                    "status": "completed",
                    "overall_score": 8.0,
                    "repair_tasks": ["审稿任务一", "审稿任务二"],
                    "suggestions": ["审稿建议"],
                    "weaknesses": ["审稿问题"],
                    "edits": [],
                }],
            }
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "feedback.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            review = _load_feedback_as_review(path, 1)

        self.assertEqual(
            ["硬修复一", "硬修复二", "审稿任务一", "审稿任务二"],
            review["repair_tasks"],
        )
        self.assertEqual(review["repair_tasks"], review["suggestions"])


if __name__ == "__main__":
    unittest.main()
