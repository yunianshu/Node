import json
import tempfile
import unittest
from pathlib import Path

import sys

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from workflow_state import ChapterStatus, analyze_chapter_text, scan_chapter_status


def chapter_text(words: int = 5000, title: str = "第一章 测试章节") -> str:
    paragraphs = [title]
    remaining = max(0, words - len(title) - 1)
    for index in range(1, 31):
        prefix = f"第{index}段，"
        paragraph_words = max(1, remaining // 30 - len(prefix))
        paragraphs.append(prefix + "字" * paragraph_words)
    return "\n".join(paragraphs) + "。"


class WorkflowStateTest(unittest.TestCase):
    def test_parse_error_review_is_not_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            draft_dir = base / "chapters" / "draft"
            final_dir = base / "chapters" / "final"
            reviews_dir = base / "reviews"
            draft_dir.mkdir(parents=True)
            final_dir.mkdir(parents=True)
            reviews_dir.mkdir(parents=True)

            (draft_dir / "chapter_0001.txt").write_text(chapter_text(), encoding="utf-8")
            (final_dir / "chapter_0001.txt").write_text(chapter_text(), encoding="utf-8")
            (reviews_dir / "chapter_0001_review.json").write_text(
                json.dumps({"status": "parse_error", "raw_response": "bad"}),
                encoding="utf-8",
            )

            status = scan_chapter_status(base, 1, 1)[1]

            self.assertEqual(status.review_status, "parse_error")
            self.assertFalse(status.review_ok)
            self.assertFalse(status.final_ok)

    def test_completed_review_requires_schema_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            reviews_dir = base / "reviews"
            reviews_dir.mkdir(parents=True)
            (reviews_dir / "chapter_0001_review.json").write_text(
                json.dumps({"status": "completed", "overall_score": 8, "verdict": "通过"}),
                encoding="utf-8",
            )

            status = scan_chapter_status(base, 1, 1)[1]

            self.assertEqual(status.review_status, "schema_missing_scores")
            self.assertFalse(status.review_ok)

    def test_short_draft_is_not_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            draft_dir = base / "chapters" / "draft"
            draft_dir.mkdir(parents=True)
            (draft_dir / "chapter_0002.txt").write_text("字" * 1200, encoding="utf-8")

            status = scan_chapter_status(base, 2, 2)[2]

            self.assertIsInstance(status, ChapterStatus)
            self.assertEqual(status.draft_words, 1200)
            self.assertFalse(status.draft_ok)
            self.assertEqual(status.draft_grade, "hard_fail")

    def test_near_range_chapter_is_warn_not_hard_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            draft_dir = base / "chapters" / "draft"
            draft_dir.mkdir(parents=True)
            (draft_dir / "chapter_0003.txt").write_text(chapter_text(4400), encoding="utf-8")

            status = scan_chapter_status(base, 3, 3)[3]

            self.assertFalse(status.draft_ok)
            self.assertEqual(status.draft_grade, "warn")

    def test_missing_title_does_not_block_quality_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            draft_dir = base / "chapters" / "draft"
            draft_dir.mkdir(parents=True)
            (draft_dir / "chapter_0004.txt").write_text("字" * 5000 + "。", encoding="utf-8")

            status = scan_chapter_status(base, 4, 4)[4]

            self.assertTrue(status.draft_ok)
            self.assertEqual(status.draft_grade, "ok")

    def test_title_check_is_configurable(self):
        text = "字" * 5000 + "。"

        _, _, ok, issues = analyze_chapter_text(text, rules={"title_required": True})

        self.assertTrue(ok)
        self.assertIn("missing_title", issues)

    def test_status_cache_reuses_existing_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            draft_dir = base / "chapters" / "draft"
            draft_dir.mkdir(parents=True)
            draft_file = draft_dir / "chapter_0006.txt"
            draft_file.write_text(chapter_text(), encoding="utf-8")

            first = scan_chapter_status(base, 6, 6, use_cache=True)[6]
            second = scan_chapter_status(base, 6, 6, use_cache=True)[6]

            self.assertTrue((base / ".workflow_status_cache.json").exists())
            self.assertEqual(first.draft_words, second.draft_words)

    def test_similar_paragraphs_are_hard_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            draft_dir = base / "chapters" / "draft"
            draft_dir.mkdir(parents=True)
            base_paragraph = "青石长街尽头，苏长空缓缓收起纸伞，目光越过雨幕，看见远处灯火在风中摇晃。"
            paragraphs = ["第一章 相似段落"] + [
                base_paragraph + f"第{index}次细节稍有变化，衣袂仍被夜风卷起。"
                for index in range(30)
            ]
            (draft_dir / "chapter_0005.txt").write_text("\n".join(paragraphs) + "。", encoding="utf-8")

            status = scan_chapter_status(base, 5, 5)[5]

            self.assertFalse(status.draft_ok)
            self.assertEqual(status.draft_grade, "hard_fail")
            self.assertIn("similar_paragraphs", status.failed_reason)


if __name__ == "__main__":
    unittest.main()
