import time
import unittest
from pathlib import Path

import sys

TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from maintenance.repair_quality import record_repair_result, should_skip_by_state


class RepairQualityTest(unittest.TestCase):
    def test_failed_chapter_waits_until_next_retry_time(self):
        state = {"chapters": {}}
        record_repair_result(state, "final", 3, 1)

        self.assertTrue(should_skip_by_state(state, "final", 3))

    def test_success_chapter_is_skipped(self):
        state = {"chapters": {}}
        record_repair_result(state, "draft", 5, 0)

        self.assertTrue(should_skip_by_state(state, "draft", 5))

    def test_quality_failure_with_zero_rc_is_failed(self):
        state = {"chapters": {}}
        record_repair_result(state, "final", 7, 0, "final_missing")

        item = state["chapters"]["0007"]
        self.assertEqual(item["status"], "failed")
        self.assertEqual(item["failure_reason"], "final_missing")


if __name__ == "__main__":
    unittest.main()
