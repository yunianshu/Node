import json
import tempfile
import unittest
from pathlib import Path

import sys

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from workflow_state import ChapterStatus, scan_chapter_status


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


if __name__ == "__main__":
    unittest.main()
