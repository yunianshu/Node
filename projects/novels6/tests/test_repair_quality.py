import time
import unittest
from pathlib import Path

import sys

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from repair_quality import record_repair_result, should_skip_by_state


class RepairQualityTest(unittest.TestCase):
    def test_failed_chapter_waits_until_next_retry_time(self):
        state = {"chapters": {}}
        record_repair_result(state, "final", 3, 1)

        self.assertTrue(should_skip_by_state(state, "final", 3))

    def test_success_chapter_is_skipped(self):
        state = {"chapters": {}}
        record_repair_result(state, "draft", 5, 0)

        self.assertTrue(should_skip_by_state(state, "draft", 5))


if __name__ == "__main__":
    unittest.main()
