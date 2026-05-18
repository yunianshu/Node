import json
import tempfile
import unittest
from pathlib import Path

import sys

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from workflow_state import ChapterStatus, scan_chapter_status


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

            (draft_dir / "chapter_0001.txt").write_text("字" * 5000, encoding="utf-8")
            (final_dir / "chapter_0001.txt").write_text("字" * 5000, encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
