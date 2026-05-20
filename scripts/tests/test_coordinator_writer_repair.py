import unittest
from unittest.mock import patch

import sys
import tempfile
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from pipeline.coordinator import (
    all_reviews_finished,
    collect_failed_writer_chapters,
    run_streaming_process,
    summarize_agent_results,
)


class CoordinatorWriterRepairTest(unittest.TestCase):
    def test_collect_failed_writer_chapters_expands_failed_ranges_and_invalid_statuses(self):
        writer_results = [(204, 204, 0), (232, 233, 1)]
        draft_statuses = {
            230: type("Status", (), {"draft_ok": True})(),
            231: type("Status", (), {"draft_ok": True})(),
            232: type("Status", (), {"draft_ok": False})(),
            233: type("Status", (), {"draft_ok": True})(),
            234: type("Status", (), {"draft_ok": False})(),
        }

        failed = collect_failed_writer_chapters(writer_results, draft_statuses, 230, 234)

        self.assertEqual(failed, [232, 233, 234])

    def test_all_reviews_finished_requires_completed_review_status(self):
        statuses = {
            1: type("Status", (), {"review_exists": True, "review_status": "completed"})(),
            2: type("Status", (), {"review_exists": True, "review_status": "parse_error"})(),
        }

        self.assertFalse(all_reviews_finished(statuses, 1, 2))

        statuses[2] = type("Status", (), {"review_exists": True, "review_status": "completed"})()

        self.assertTrue(all_reviews_finished(statuses, 1, 2))

    def test_run_streaming_process_tees_child_output_to_log_file_and_logger(self):
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "child.log"
            cmd = [
                sys.executable,
                "-c",
                "print('子进程第一行'); print('子进程第二行')",
            ]

            with patch("pipeline.coordinator.log", side_effect=seen.append):
                rc = run_streaming_process(cmd, log_file)

            self.assertEqual(rc, 0)
            self.assertIn("[Child] 子进程第一行", seen)
            self.assertIn("[Child] 子进程第二行", seen)
            text = log_file.read_text(encoding="utf-8")
            self.assertIn("子进程第一行", text)
            self.assertIn("子进程第二行", text)

    def test_summarize_agent_results_counts_chapters_not_processes(self):
        results = [(44, 45, 0), (46, 50, 0), (51, 52, 1)]

        processed, failed = summarize_agent_results(results)

        self.assertEqual(processed, 7)
        self.assertEqual(failed, 2)


if __name__ == "__main__":
    unittest.main()
