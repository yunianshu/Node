import json
import os
import tempfile
import unittest
from pathlib import Path

import sys

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from novel_config import get_webhook_url, load_config
from mmx_client import compute_wait_seconds, extract_content
from reviewer import analyze_chapter_text


class ConfigAndMmxTest(unittest.TestCase):
    def test_load_config_merges_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "config.json").write_text(
                json.dumps({"api_qps": 3, "writer": {"max_retries": 5}}, ensure_ascii=False),
                encoding="utf-8",
            )

            cfg = load_config(base)

            self.assertEqual(cfg["api_qps"], 3)
            self.assertEqual(cfg["writer"]["max_retries"], 5)
            self.assertIn("reviewer", cfg)

    def test_get_webhook_url_prefers_env(self):
        old = os.environ.get("NOVEL_WEBHOOK_URL")
        os.environ["NOVEL_WEBHOOK_URL"] = "https://example.test/webhook"
        try:
            self.assertEqual(
                get_webhook_url({"webhook_url": "https://config.test/webhook"}),
                "https://example.test/webhook",
            )
        finally:
            if old is None:
                os.environ.pop("NOVEL_WEBHOOK_URL", None)
            else:
                os.environ["NOVEL_WEBHOOK_URL"] = old

    def test_extract_content_handles_response_prefix(self):
        raw = 'log\nResponse: {"content": "正文"}'

        self.assertEqual(extract_content(raw), "正文")

    def test_compute_wait_seconds_respects_qps_interval(self):
        wait = compute_wait_seconds(last_call_time=10.0, now=10.2, qps=2.0)

        self.assertAlmostEqual(wait, 0.3, places=2)

    def test_reviewer_local_analysis_flags_short_text(self):
        analysis = analyze_chapter_text("短文")

        self.assertFalse(analysis["word_count_ok"])
        self.assertIn("字数低于", analysis["issues"][0])


if __name__ == "__main__":
    unittest.main()
