#!/usr/bin/env python3
"""
Planner Agent - 框架总领Agent
负责生成和维护小说世界观、章节大纲、角色档案
支持 --start/--end 参数，可并行生成指定范围的大纲
"""
import argparse
import json
import os
import sys
from pathlib import Path

from mmx_client import MmxError, call_mmx as call_mmx_client
from novel_config import load_config

NOVELS_DIR = None
WORLD_FILE = None
OUTLINE_FILE = None
CHARACTERS_FILE = None
CONFIG = None
NOVEL_PREMISE = ""


def init_project(project_dir: str | Path) -> None:
    global NOVELS_DIR, WORLD_FILE, OUTLINE_FILE, CHARACTERS_FILE, CONFIG, NOVEL_PREMISE
    NOVELS_DIR = Path(project_dir).resolve()
    WORLD_FILE = NOVELS_DIR / "world.json"
    OUTLINE_FILE = NOVELS_DIR / "outline.json"
    CHARACTERS_FILE = NOVELS_DIR / "characters.json"
    CONFIG = load_config(NOVELS_DIR)
    total = CONFIG["total_chapters"]

    premise_file = NOVELS_DIR / "premise.txt"
    if premise_file.exists():
        NOVEL_PREMISE = premise_file.read_text(encoding="utf-8").replace("{total_chapters}", str(total))
    else:
        NOVEL_PREMISE = f"""《长生武道：虚空万界行》是《长生武道：从五禽养生拳开始》的续作/后传。

前作结局回顾：
主角苏长空，从黑铁山庄一个孱弱少年起步，修炼五禽养生拳、龟息真定功、天蚕神功等长生武学，靠"寿命增长则天赋无限提升"的金手指一步步崛起。历经百年，他达到了前无古人的"魂界"境界——在识海中开辟天地、演化世界，独立于天地之外。他斩杀了祸乱天地的大反派天魔神，拯救了人族。此时他100岁，寿命10000年，潜能值1000点。
苏长空的同伴包括：华善（古圣，生命之道，治愈大师，鹤发童颜的老者）、姬雪潇（神凰转世，掌握虚无之道，冰晶火焰构成的神凰本体）、战无双等古圣强者。他们炼化了天道碎片，原本无法离开故土天地，但苏长空的魂界可以收容他们，带他们一起离开。

续写设定：
苏长空带着华善、姬雪潇等同伴，离开了故乡天地，进入了无尽虚空。无尽虚空是一片浩瀚的黑暗空间，其中漂浮着无数"天地"（世界），每个天地都有独立的天道法则和修炼体系。虚空中存在着各种各样的文明、种族和强者，还有危险的虚空生物、虚空风暴等。
苏长空的魂界是一个还在成长中的独立世界，他需要在探索中不断壮大魂界。魂界境之上还有更高境界：界主境（完全掌控一方天地）、虚空境（在虚空中自由穿行，不惧虚空风暴）、混沌境（超越虚空，触及宇宙本源）。
全书共{total}章，每章约5000字。风格延续前作的热血、升级、长生武道流，融合虚空万界、异界探索、文明碰撞等元素。"""


def call_mmx(system_prompt: str, user_prompt: str, max_tokens: int = 8192, temperature: float = 0.4) -> str:
    try:
        return call_mmx_client(
            system_prompt,
            user_prompt,
            model=CONFIG["model"],
            mmx_path=CONFIG["mmx_path"],
            max_tokens=max_tokens,
            temperature=temperature,
            retries=CONFIG["writer"]["max_retries"],
            retry_delay=CONFIG["writer"]["retry_delay"],
            log_dir=NOVELS_DIR / "logs" / "raw_responses",
            raw_name="planner",
            qps=CONFIG["api_qps"],
            rate_state_dir=NOVELS_DIR / "logs" / "rate_limit",
        )
    except MmxError as e:
        print(f"[ERROR] mmx调用失败: {e}", file=sys.stderr)
        return ""


def generate_world():
    if WORLD_FILE.exists():
        print(f"[Planner] world.json 已存在，跳过生成")
        return

    system = """你是一位顶级东方玄幻/武侠/修仙世界观架构师。
你需要根据用户提供的故事 premise，构建一个完整、详细、有深度的世界观。
输出必须是合法的JSON格式，不要包含任何markdown代码块标记。"""

    prompt = f"""请根据以下故事设定，构建完整的世界观JSON：

{NOVEL_PREMISE}

请输出以下JSON结构：
{{
  "title": "小说标题",
  "subtitle": "副标题",
  "world_name": "世界名称",
  "world_description": "世界整体描述（500字）",
  "power_system": {{
    "name": "修炼体系名称",
    "description": "修炼体系描述",
    "levels": [
      {{"name": "等级1", "description": "描述"}},
      ...
    ]
  }},
  "factions": [
    {{"name": "势力名称", "description": "描述", "alignment": "正/邪/中"}}
  ],
  "key_locations": [
    {{"name": "地点名称", "description": "描述", "significance": "重要性"}}
  ],
  "rules": ["世界规则1", "世界规则2"],
  "themes": ["主题1", "主题2", "主题3"],
  "overall_arc": "整体故事弧线描述（300字）",
  "three_act_structure": {{
    "act1": "第一幕描述",
    "act2": "第二幕描述",
    "act3": "第三幕描述"
  }}
}}

要求：
1. 修炼体系必须严格遵循 premise 中描述的体系，不要擅自添加或修改境界名称
2. 地点要有层次感和探索价值，从底层到高层逐步展开
3. 势力设计要符合主角的底层起步设定
4. 整体架构要支撑{CONFIG['total_chapters']}章的篇幅
5. 必须输出合法的JSON，不要任何注释或额外文本"""

    print("[Planner] 正在生成世界观...")
    content = call_mmx(system, prompt, max_tokens=8192, temperature=0.4)
    if not content:
        print("[Planner] 世界观生成失败")
        return

    try:
        world_data = json.loads(content)
        with open(WORLD_FILE, "w", encoding="utf-8") as f:
            json.dump(world_data, f, ensure_ascii=False, indent=2)
        print(f"[Planner] 世界观已保存到 {WORLD_FILE}")
    except json.JSONDecodeError as e:
        print(f"[Planner] JSON解析失败: {e}")
        try:
            start = content.index("{")
            end = content.rindex("}") + 1
            world_data = json.loads(content[start:end])
            with open(WORLD_FILE, "w", encoding="utf-8") as f:
                json.dump(world_data, f, ensure_ascii=False, indent=2)
            print(f"[Planner] 世界观已保存（经过修复）")
        except Exception as e2:
            print(f"[Planner] 修复失败: {e2}")
            with open(WORLD_FILE.with_suffix(".raw"), "w", encoding="utf-8") as f:
                f.write(content)


def generate_characters():
    if CHARACTERS_FILE.exists():
        print(f"[Planner] characters.json 已存在，跳过生成")
        return

    system = """你是一位顶级角色设计师，擅长设计有深度、有成长弧线的角色。
你需要根据故事 premise 设计主要角色。
输出必须是合法的JSON格式。"""

    prompt = f"""请根据以下故事设定，设计主要角色档案：

{NOVEL_PREMISE}

请输出包含 protagonist、companions、new_characters、antagonists 的JSON结构。

要求：
1. 主角设计要符合 premise 中的描述，有完整的成长路径设计
2. 同伴角色要有血有肉，与主角有真实的情感羁绊
3. 新角色至少设计8个重要角色，涵盖同伴、导师、对手等类型
4. 可以有红颜知己或暧昧角色，但不要太滥
5. 反派要有层次，设计至少3个层级的反派（小反派、中BOSS、最终BOSS）
6. 必须输出合法JSON"""

    print("[Planner] 正在生成角色档案...")
    content = call_mmx(system, prompt, max_tokens=8192, temperature=0.4)
    if not content:
        print("[Planner] 角色档案生成失败")
        return

    try:
        chars_data = json.loads(content)
        with open(CHARACTERS_FILE, "w", encoding="utf-8") as f:
            json.dump(chars_data, f, ensure_ascii=False, indent=2)
        print(f"[Planner] 角色档案已保存到 {CHARACTERS_FILE}")
    except json.JSONDecodeError as e:
        print(f"[Planner] JSON解析失败: {e}")
        try:
            start = content.index("{")
            end = content.rindex("}") + 1
            chars_data = json.loads(content[start:end])
            with open(CHARACTERS_FILE, "w", encoding="utf-8") as f:
                json.dump(chars_data, f, ensure_ascii=False, indent=2)
            print(f"[Planner] 角色档案已保存（经过修复）")
        except Exception as e2:
            print(f"[Planner] 修复失败: {e2}")


def generate_outline_range(start: int, end: int, outline_file: Path = None):
    batch_size = 15
    output_file = outline_file or OUTLINE_FILE

    world = {}
    characters = {}
    if WORLD_FILE.exists():
        with open(WORLD_FILE, "r", encoding="utf-8") as f:
            world = json.load(f)
    if CHARACTERS_FILE.exists():
        with open(CHARACTERS_FILE, "r", encoding="utf-8") as f:
            characters = json.load(f)

    world_json = json.dumps(world, ensure_ascii=False, indent=2)
    chars_json = json.dumps(characters, ensure_ascii=False, indent=2)

    outline = {"chapters": []}
    last_chapter = 0
    if output_file.exists():
        with open(output_file, "r", encoding="utf-8") as f:
            outline = json.load(f)
        if outline.get("chapters"):
            last_chapter = max(ch.get("chapter_number", 0) for ch in outline["chapters"])

    if last_chapter >= end:
        print(f"[Planner] 大纲已生成到第{last_chapter}章，范围{start}-{end}已覆盖，跳过")
        return

    actual_start = max(start, last_chapter + 1)

    system = """你是一位顶级东方玄幻/武侠/修仙小说大纲设计师。
你需要设计详细的大纲，每章包含标题、核心事件、涉及角色、场景、情感基调。
严格按照 premise 中描述的故事设定和主角设定来设计大纲。
输出必须是合法的JSON格式。"""

    for batch_start in range(actual_start, end + 1, batch_size):
        batch_end = min(batch_start + batch_size - 1, end)
        print(f"[Planner] 正在生成第 {batch_start}-{batch_end} 章大纲...")

        prev_context = ""
        if outline.get("chapters"):
            prev_chapters = outline["chapters"][-3:]
            prev_context = "\n前一批最后几章摘要（用于衔接）：\n"
            for ch in prev_chapters:
                prev_context += f"第{ch.get('chapter_number')}章《{ch.get('title')}》：{ch.get('summary', '')[:100]}...\n"

        prompt = f"""请根据以下世界观和角色设定，生成第{batch_start}章到第{batch_end}章的详细大纲。

世界观设定：
{world_json}

角色设定：
{chars_json}

故事前提：{NOVEL_PREMISE}

{prev_context}

请输出以下JSON结构：
{{
  "chapters": [
    {{
      "chapter_number": {batch_start},
      "title": "章节标题",
      "summary": "核心事件摘要（150-250字）",
      "characters_involved": ["角色名1", "角色名2"],
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
2. 情节要有起伏，有高潮有低谷，有扮猪吃虎的爽点
3. 主角的实力和技能要逐步成长，保持升级爽感
4. 伏笔要前后呼应，与前一批大纲自然衔接
5. 要有"强敌轻视主角，结果被主角以积累的实力碾压"的爽文桥段
6. 探索不同场景时要展现环境差异和世界多样性
7. 必须输出合法JSON，总共{batch_end - batch_start + 1}个章节对象"""

        content = call_mmx(system, prompt, max_tokens=8192, temperature=0.5)
        if not content:
            print(f"[Planner] 第 {batch_start}-{batch_end} 章大纲生成失败")
            continue

        try:
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            batch_outline = json.loads(content)
            new_chapters = batch_outline.get("chapters", [])
            outline["chapters"].extend(new_chapters)
            print(f"[Planner] 第 {batch_start}-{batch_end} 章大纲已生成（{len(new_chapters)}章）")
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(outline, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Planner] 第 {batch_start}-{batch_end} 章解析失败: {e}")
            with open(NOVELS_DIR / f"outline_batch_{batch_start:04d}.raw", "w", encoding="utf-8") as f:
                f.write(content)

    print(f"[Planner] 大纲范围 {start}-{end} 已完成，共 {len(outline['chapters'])} 章")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-p", type=str,
                        default=os.getenv("NOVEL_PROJECT_DIR", ""),
                        help="小说项目目录")
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=0, help="结束章节")
    parser.add_argument("--world-only", action="store_true", help="只生成世界观和角色")
    parser.add_argument("--outline-file", type=str, default="", help="指定大纲输出文件路径（用于并行生成）")
    args = parser.parse_args()

    if not args.project:
        print("错误: 必须指定 --project 或设置 NOVEL_PROJECT_DIR 环境变量")
        sys.exit(1)

    init_project(args.project)

    end = args.end or CONFIG["total_chapters"]

    print("=" * 60)
    print(f"Planner Agent 启动 - 范围: 第{args.start}章到第{end}章")
    print(f"项目: {NOVELS_DIR}")
    print("=" * 60)

    NOVELS_DIR.mkdir(parents=True, exist_ok=True)

    generate_world()
    generate_characters()

    if not args.world_only:
        outline_file = Path(args.outline_file) if args.outline_file else None
        generate_outline_range(args.start, end, outline_file)

    print("[Planner] 全部完成")


if __name__ == "__main__":
    main()
