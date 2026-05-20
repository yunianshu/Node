#!/usr/bin/env python3
"""补全按章大纲索引中缺失的章节大纲。"""

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from core.mmx_client import call_mmx, MmxError
from core.novel_config import load_config
from core.workflow_state import list_outline_chapters, write_outline_chapters


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")


def main():
    project_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(os.getenv("NOVEL_PROJECT_DIR", ""))
    if not project_dir:
        log("错误: 需要指定项目目录或设置 NOVEL_PROJECT_DIR")
        sys.exit(1)

    novels_dir = Path(project_dir).resolve()
    config = load_config(novels_dir)
    total = config["total_chapters"]
    existing = {c.get("chapter_number", 0): c for c in list_outline_chapters(novels_dir)}
    missing = [i for i in range(1, total + 1) if i not in existing]

    if not missing:
        log("大纲已完整，无需补全")
        return

    # 按连续区间分组
    intervals = []
    start = missing[0]
    prev = missing[0]
    for m in missing[1:]:
        if m == prev + 1:
            prev = m
        else:
            intervals.append((start, prev))
            start = m
            prev = m
    intervals.append((start, prev))

    log(f"缺失 {len(missing)} 章，共 {len(intervals)} 个区间")

    # 加载世界观和前提
    world_file = novels_dir / "world.json"
    world = json.load(open(world_file, "r", encoding="utf-8")) if world_file.exists() else {}
    premise_file = novels_dir / "premise.txt"
    premise = premise_file.read_text(encoding="utf-8") if premise_file.exists() else ""

    world_json = json.dumps(world, ensure_ascii=False, indent=2)

    batch_size = 15
    total_generated = 0

    for interval_start, interval_end in intervals:
        for batch_start in range(interval_start, interval_end + 1, batch_size):
            batch_end = min(batch_start + batch_size - 1, interval_end)
            log(f"正在生成第 {batch_start}-{batch_end} 章大纲...")

            # 找前后上下文
            prev_context = ""
            before_ch = existing.get(batch_start - 1)
            if before_ch:
                prev_context = f"\n前一章（第{batch_start-1}章）摘要：{before_ch.get('summary', '')[:200]}"

            after_context = ""
            after_ch = existing.get(batch_end + 1)
            if after_ch:
                after_context = f"\n后一章（第{batch_end+1}章）摘要：{after_ch.get('summary', '')[:200]}"

            system = """你是一位顶级东方玄幻/仙侠小说大纲设计师。

你需要设计详细的大纲，每章包含标题、核心事件、涉及角色、场景、情感基调。
严格按照故事前提和世界观来设计大纲。
输出必须是合法的JSON格式，不要包含任何markdown代码块标记。"""

            prompt = f"""请根据以下信息，生成第{batch_start}章到第{batch_end}章的详细大纲。

故事前提：
{premise[:2000]}

世界观背景：
{world_json[:2000]}

{prev_context}
{after_context}

请输出以下JSON结构：
{{
  "chapters": [
    {{
      "chapter_number": {batch_start},
      "title": "章节标题",
      "summary": "核心事件摘要（150-250字）",
      "characters_involved": ["角色名1"],
      "location": "场景地点",
      "mood": "情感基调",
      "key_events": ["事件1", "事件2"],
      "foreshadowing": "埋下的伏笔",
      "power_progression": "实力变化说明",
      "word_count_target": 5000
    }},
    ...
  ]
}}

要求：
1. 每章必须有独特的核心事件，不能流水账
2. 情节要有起伏，有高潮有低谷
3. 主角的实力和技能要逐步成长，保持升级爽感
4. 伏笔要前后呼应，与前后章节自然衔接
5. 必须输出合法JSON，总共{batch_end - batch_start + 1}个章节对象"""

            try:
                content = call_mmx(
                    system,
                    prompt,
                    model=config["model"],
                    mmx_path=config["mmx_path"],
                    max_tokens=8192,
                    temperature=0.5,
                    retries=config["writer"]["max_retries"],
                    retry_delay=config["writer"]["retry_delay"],
                    qps=config["api_qps"],
                )
            except MmxError as e:
                log(f"API 调用失败: {e}")
                continue

            if not content:
                log(f"第 {batch_start}-{batch_end} 章大纲生成失败（返回空）")
                continue

            # 解析 JSON
            try:
                if "```json" in content:
                    content = content.split("```json")[1].split("```")[0].strip()
                elif "```" in content:
                    content = content.split("```")[1].split("```")[0].strip()
                batch = json.loads(content)
                new_chapters = batch.get("chapters", [])
                for ch in new_chapters:
                    existing[ch.get("chapter_number", 0)] = ch
                total_generated += len(new_chapters)
                log(f"第 {batch_start}-{batch_end} 章已生成 {len(new_chapters)} 章")
            except Exception as e2:
                log(f"第 {batch_start}-{batch_end} 章解析失败: {e2}")
                # 保存原始内容供调试
                raw_file = novels_dir / "logs" / f"outline_gap_{batch_start:04d}.raw"
                with open(raw_file, "w", encoding="utf-8") as f:
                    f.write(content)

            # 每批保存一次，防止中断丢失；只写单章大纲文件。
            write_outline_chapters(novels_dir, {"chapters": existing.values()})

    log(f"补全完成，共生成 {total_generated} 章，大纲总计 {len(existing)} 章")


if __name__ == "__main__":
    main()
