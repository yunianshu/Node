import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from pipeline import rewrite_agent


class RewriteAttemptsTest(unittest.TestCase):
    def test_select_best_attempt_prefers_passed_attempt(self):
        attempts = [
            {"attempt": 1, "score": 9.5, "word_count": 4500, "passed": False},
            {"attempt": 2, "score": 8.0, "word_count": 5100, "passed": True},
        ]

        best = rewrite_agent.select_best_attempt(attempts)

        self.assertEqual(best["attempt"], 2)

    def test_select_best_attempt_falls_back_to_highest_score(self):
        attempts = [
            {"attempt": 1, "score": 4.5, "word_count": 4300, "passed": False},
            {"attempt": 2, "score": 6.0, "word_count": 4900, "passed": False},
        ]

        best = rewrite_agent.select_best_attempt(attempts)

        self.assertEqual(best["attempt"], 2)

    def test_save_rewrite_attempt_records_text_and_review(self):
        with TemporaryDirectory() as tmp:
            rewrite_agent.NOVELS_DIR = Path(tmp)
            review = {"score": 6.5, "passed": False, "word_count": 4800, "issues": ["length_too_short"]}

            record = rewrite_agent.save_rewrite_attempt(1, 1, "正文", review)

            self.assertTrue(Path(record["content_file"]).exists())
            self.assertTrue(Path(record["review_file"]).exists())
            saved = json.loads(Path(record["review_file"]).read_text(encoding="utf-8"))
            self.assertEqual(saved["score"], 6.5)


if __name__ == "__main__":
    unittest.main()
