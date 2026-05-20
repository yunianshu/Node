import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path

import sys

TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from core.push_notifier import build_progress_message, build_stage_message


class PushNotifierTest(unittest.TestCase):
    def test_progress_message_uses_standard_book_format(self):
        msg = build_progress_message(
            title="回档2011",
            outline=2000,
            draft=300,
            reviewed=0,
            final=0,
            total_words=2_547_051,
            total_chapters=2000,
            active_writers=49,
        )

        self.assertRegex(msg.splitlines()[0], r"^📖 《回档2011》生成进度 \(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\)$")
        self.assertIn("📋 大纲: 2000/2000 章", msg)
        self.assertIn("✍ 初稿: 300/2000 章", msg)
        self.assertIn("📝 字数: 2,547,051", msg)
        self.assertIn("🔍 审查: 0/2000 章", msg)
        self.assertIn("📤 终稿: 0/2000 章", msg)
        self.assertIn("🤖 活跃进程: 49", msg)
        self.assertNotIn("进度更新", msg)
        self.assertNotIn("Coordinator已启动", msg)

    def test_global_wechat_single_message_reuses_standard_format(self):
        module_path = TOOLS_DIR / "wechat_notify.py"
        spec = importlib.util.spec_from_file_location("global_wechat_notify", module_path)
        wechat_notify = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wechat_notify)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "novels-test"
            draft_dir = project / "chapters" / "draft"
            outline_dir = project / "chapters" / "outline"
            draft_dir.mkdir(parents=True)
            outline_dir.mkdir(parents=True)
            (outline_dir / "chapter_0001.json").write_text(
                json.dumps({"chapter_number": 1, "title": "第一章"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (project / "world.json").write_text(
                json.dumps({"title": "回档2011"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (draft_dir / "chapter_0001.txt").write_text("字" * 1001, encoding="utf-8")

            msg = wechat_notify.build_single_message({"path": str(project)})

        self.assertRegex(msg.splitlines()[0], r"^📖 《回档2011》生成进度 ")
        self.assertIn("📋 大纲: 1/1 章", msg)
        self.assertIn("✍ 初稿: 1/1 章", msg)
        self.assertNotIn("【回档2011】生成进度", msg)

    def test_stage_message_covers_start_progress_complete_and_error(self):
        started = build_stage_message(
            title="回档2011",
            stage="初稿",
            status="开始",
            start_chapter=1,
            end_chapter=20,
        )
        progress = build_stage_message(
            title="回档2011",
            stage="初稿",
            status="进度",
            start_chapter=1,
            end_chapter=20,
            processed=8,
            failed=1,
        )
        completed = build_stage_message(
            title="回档2011",
            stage="初稿",
            status="完成",
            start_chapter=1,
            end_chapter=20,
            processed=19,
            failed=1,
        )
        failed = build_stage_message(
            title="回档2011",
            stage="初稿",
            status="异常",
            start_chapter=1,
            end_chapter=20,
            error="模型调用失败",
        )

        self.assertIn("🚀 《回档2011》初稿开始", started)
        self.assertIn("📄 章节: 1-20", started)
        self.assertIn("⏳ 《回档2011》初稿进度", progress)
        self.assertIn("✔ 成功: 8 章", progress)
        self.assertIn("❌ 失败: 1 章", progress)
        self.assertIn("✅ 《回档2011》初稿完成", completed)
        self.assertIn("❌ 《回档2011》初稿异常", failed)
        self.assertIn("错误: 模型调用失败", failed)


if __name__ == "__main__":
    unittest.main()
