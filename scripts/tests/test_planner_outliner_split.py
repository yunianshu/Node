import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from pipeline import outliner, planner


class PlannerOutlinerSplitTest(unittest.TestCase):
    def test_planner_main_does_not_generate_outline(self):
        with TemporaryDirectory() as tmp:
            args = ["planner.py", "--project", tmp, "--start", "1", "--end", "1"]
            with patch.object(sys, "argv", args), \
                    patch.object(planner, "generate_world") as generate_world, \
                    patch.object(planner, "generate_characters") as generate_characters, \
                    patch.object(planner, "generate_outline_range") as generate_outline:
                planner.main()

            generate_world.assert_called_once()
            generate_characters.assert_called_once()
            generate_outline.assert_not_called()

    def test_outliner_custom_outline_file_writes_chapter_files(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            outliner.init_project(project)
            segment_file = project / "outline_part_0001_0001.json"
            content = json.dumps({
                "chapters": [{
                    "chapter_number": 1,
                    "title": "第一章",
                    "summary": "测试摘要",
                    "characters_involved": ["主角"],
                    "location": "测试地点",
                    "mood": "推进",
                    "key_events": ["事件"],
                    "foreshadowing": "伏笔",
                    "power_progression": "无",
                    "word_count_target": 5000,
                }]
            }, ensure_ascii=False)

            with patch.object(outliner, "call_mmx", return_value=content):
                outliner.generate_outline_range(1, 1, segment_file)

            self.assertTrue(segment_file.exists())
            chapter_file = project / "chapters" / "outline" / "chapter_0001.json"
            self.assertTrue(chapter_file.exists())
            self.assertEqual(json.loads(chapter_file.read_text(encoding="utf-8"))["chapter_number"], 1)

    def test_outliner_without_outline_file_does_not_create_index_file(self):
        with TemporaryDirectory() as tmp:
            project = Path(tmp)
            outliner.init_project(project)
            content = json.dumps({
                "chapters": [{
                    "chapter_number": 1,
                    "title": "第一章",
                    "summary": "测试摘要",
                    "characters_involved": ["主角"],
                    "location": "测试地点",
                    "mood": "推进",
                    "key_events": ["事件"],
                    "foreshadowing": "伏笔",
                    "power_progression": "无",
                    "word_count_target": 5000,
                }]
            }, ensure_ascii=False)

            with patch.object(outliner, "call_mmx", return_value=content):
                outliner.generate_outline_range(1, 1)

            self.assertTrue((project / "chapters" / "outline" / "chapter_0001.json").exists())
            self.assertFalse((project / "chapters" / "outline" / "index.json").exists())
            self.assertFalse((project / "outline.json").exists())


if __name__ == "__main__":
    unittest.main()
