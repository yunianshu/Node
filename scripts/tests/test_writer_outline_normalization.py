import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from pipeline.writer import normalize_outline_text
from pipeline import outliner


class WriterOutlineNormalizationTest(unittest.TestCase):
    def test_normalize_outline_text_repairs_latin1_gbk_mojibake(self):
        outline = {
            "title": "Ê÷´óÕÐ·ç",
            "characters_involved": ["Àî·²"],
            "summary": "ÕâÊÇÒ»¶ÎÂÒÂë",
        }

        normalized = normalize_outline_text(outline)

        self.assertEqual(normalized["title"], "树大招风")
        self.assertEqual(normalized["characters_involved"][0], "李凡")
        self.assertIn("这是", normalized["summary"])

    def test_normalize_outline_text_keeps_valid_chinese(self):
        outline = {"title": "树大招风", "characters_involved": ["李凡"]}

        normalized = normalize_outline_text(outline)

        self.assertEqual(normalized, outline)

    def test_outliner_custom_outline_file_writes_chapter_files(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            outliner.init_project(project)
            segment_file = project / "outline_part_0001_0001.json"
            content = (
                '{"chapters":[{"chapter_number":1,"title":"第一章",'
                '"summary":"测试摘要","characters_involved":["主角"],'
                '"location":"测试地点","mood":"推进","key_events":["事件"],'
                '"foreshadowing":"伏笔","power_progression":"无",'
                '"word_count_target":5000}]}'
            )

            with patch.object(outliner, "call_mmx", return_value=content):
                outliner.generate_outline_range(1, 1, segment_file)

            self.assertTrue(segment_file.exists())
            self.assertTrue((project / "chapters" / "outline" / "chapter_0001.json").exists())


if __name__ == "__main__":
    unittest.main()
