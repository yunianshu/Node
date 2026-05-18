#!/usr/bin/env python3
"""
Planner Agent - 框架总领Agent
负责生成和维护小说世界观、章节大纲、角色档案
支持 --start/--end 参数，可并行生成指定范围的大纲
"""
import json
import sys
from pathlib import Path

from mmx_client import MmxError, call_mmx as call_mmx_client
from novel_config import load_config

NOVELS_DIR = Path("D:/AiProject/Node/projects/novels6")
WORLD_FILE = NOVELS_DIR / "world.json"
OUTLINE_FILE = NOVELS_DIR / "outline.json"
CHARACTERS_FILE = NOVELS_DIR / "characters.json"

CONFIG = load_config(NOVELS_DIR)

NOVEL_PREMISE = """《长生武道：虚空万界行》是《长生武道：从五禽养生拳开始》的续作/后传。

前作结局回顾：
主角苏长空，从黑铁山庄一个孱弱少年起步，修炼五禽养生拳、龟息真定功、天蚕神功等长生武学，靠"寿命增长则天赋无限提升"的金手指一步步崛起。历经百年，他达到了前无古人的"魂界"境界——在识海中开辟天地、演化世界，独立于天地之外。他斩杀了祸乱天地的大反派天魔神，拯救了人族。此时他100岁，寿命10000年，潜能值1000点。
苏长空的同伴包括：华善（古圣，生命之道，治愈大师，鹤发童颜的老者）、姬雪潇（神凰转世，掌握虚无之道，冰晶火焰构成的神凰本体）、战无双等古圣强者。他们炼化了天道碎片，原本无法离开故土天地，但苏长空的魂界可以收容他们，带他们一起离开。

续写设定：
苏长空带着华善、姬雪潇等同伴，离开了故乡天地，进入了无尽虚空。无尽虚空是一片浩瀚的黑暗空间，其中漂浮着无数"天地"（世界），每个天地都有独立的天道法则和修炼体系。虚空中存在着各种各样的文明、种族和强者，还有危险的虚空生物、虚空风暴等。
苏长空的魂界是一个还在成长中的独立世界，他需要在探索中不断壮大魂界。魂界境之上还有更高境界：界主境（完全掌控一方天地）、虚空境（在虚空中自由穿行，不惧虚空风暴）、混沌境（超越虚空，触及宇宙本源）。
全书共2000章，每章约5000字。风格延续前作的热血、升级、长生武道流，融合虚空万界、异界探索、文明碰撞等元素。"""


def call_mmx(system_prompt: str, user_prompt: str, max_tokens: int = 8192, temperature: float = 0.4) -> str:
    """调用 mmx text chat 生成内容（通过 node 直接运行 mmx-cli）"""
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
    """生成世界观设定"""
    if WORLD_FILE.exists():
        print(f"[Planner] world.json 已存在，跳过生成")
        return

    system = """你是一位顶级东方玄幻/武侠世界观架构师。
你需要根据用户提供的故事 premise（续作设定），构建一个完整、详细、有深度的虚空万界世界观。
这个世界是前作的延续，主角已经超脱原有天地，进入无尽虚空探索。
输出必须是合法的JSON格式，不要包含任何markdown代码块标记。"""

    prompt = f"""请根据以下故事设定，构建续作的完整世界观JSON：

{NOVEL_PREMISE}

请输出以下JSON结构：
{{
  "title": "小说标题",
  "subtitle": "副标题",
  "world_name": "世界名称",
  "world_description": "世界整体描述（500字）",
  "power_system": {{
    "name": "修炼体系名称",
    "description": "修炼体系描述（长生武道体系，寿命增长则天赋无限提升）",
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
  "rules": [
    "世界规则1",
    "世界规则2"
  ],
  "themes": ["主题1", "主题2", "主题3"],
  "overall_arc": "整体故事弧线描述（300字）",
  "three_act_structure": {{
    "act1": "第一幕描述（1-500章）：初入虚空，探索周边天地，遭遇虚空生物和异族文明，魂界初步成长",
    "act2": "第二幕描述（501-1200章）：深入虚空万界，结识各族强者，发现虚空中的大秘密，冲击更高境界",
    "act3": "第三幕描述（1201-2000章）：触及虚空本源，对抗虚空中的终极威胁，最终成就混沌境，开创永恒长生之道"
  }}
}}

要求：
1. 修炼体系要延续前作的长生武道：魂界境（已在识海开辟世界）-> 界主境（完全掌控一方天地，可在虚空中建立据点）-> 虚空境（在虚空中自由穿行，肉身可抗虚空风暴）-> 混沌境（超越虚空，触及宇宙本源，永恒不灭）。每个境界分初阶、中阶、高阶、巅峰
2. 虚空中有无数天地，每个天地有不同的天道法则，有的天地修炼体系完全不同（如魔法、科技、仙道等），增加文明碰撞的看点
3. 地点要有层次感和探索价值，包括虚空驿站、破碎天地、上古遗迹、虚空巨兽巢穴等
4. 要有虚空特有的危险：虚空风暴、虚空生物、维度裂缝、天道排斥等
5. 整体架构要支撑2000章的篇幅，有明确的成长曲线和升级节奏
6. 必须输出合法的JSON，不要任何注释或额外文本"""

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
    """生成角色档案"""
    if CHARACTERS_FILE.exists():
        print(f"[Planner] characters.json 已存在，跳过生成")
        return

    system = """你是一位顶级角色设计师，擅长设计有深度、有成长弧线的角色。
你需要根据故事 premise（续作设定）设计主要角色，主角是已经超脱天地的超级强者苏长空。
输出必须是合法的JSON格式。"""

    prompt = f"""请根据以下故事设定，设计续作的主要角色档案：

{NOVEL_PREMISE}

请输出以下JSON结构：
{{
  "protagonist": {{
    "name": "苏长空",
    "alias": ["刀兄", "苏前辈", "魂界之主"],
    "age": 100,
    "personality": "性格特征（沉稳内敛、坚韧不拔、重情重义、低调谦逊，但在关键时刻果决狠辣。虽已达魂界境，仍保持初心，对武道有着近乎偏执的追求）",
    "background": "背景故事（从黑铁山庄的孱弱少年，靠寿命增长天赋无限提升的金手指，历经百年达到魂界境，斩杀天魔神，拯救人族，超脱天地束缚）",
    "initial_power": "初始实力（魂界境初阶，识海中开辟了一方独立世界，寿命10000年，潜能值1000点）",
    "target_power": "最终实力（混沌境，触及宇宙本源，永恒不灭，魂界成长为真正的宇宙）",
    "character_arc": "成长弧线描述（从故乡天地的守护者，成长为虚空万界的探索者，最终成为超越虚空的永恒存在。在探索中保持本心，守护同伴，不断突破自我极限）",
    "key_traits": ["寿命增长则天赋无限提升", "在识海中开辟了魂界", "炼丹、锻造、阵法全能", "对长生武道有着近乎偏执的追求"],
    "flaws": ["过于执着于独自承担一切", "对故乡和旧友有着深沉的牵挂，有时会因此犹豫"],
    "motivations": ["追求武道终极境界和长生奥秘", "保护身边的同伴", "探索虚空万界的真相"]
  }},
  "companions": [
    {{
      "name": "华善",
      "role": "同伴/生命之道古圣",
      "personality": "温和慈祥，医者仁心，鹤发童颜，喜欢泡茶种药",
      "significance": "苏长空的良师益友，在虚空中负责治愈和培育灵药，是团队的精神支柱"
    }},
    {{
      "name": "姬雪潇",
      "role": "同伴/红颜知己/神凰转世",
      "personality": "高傲冷艳，外表冰冷但内心柔软，对苏长空有特殊的情感",
      "significance": "掌握虚无之道，战斗力极强，是苏长空的左膀右臂，两人在战斗中默契十足"
    }},
    {{
      "name": "战无双",
      "role": "同伴/战斗狂人",
      "personality": "豪爽直率，嗜战如命，崇拜强者",
      "significance": "团队中的战斗主力，负责正面作战"
    }}
  ],
  "new_characters": [
    {{
      "name": "新角色姓名",
      "role": "角色定位（虚空中的新盟友/敌人/导师等）",
      "personality": "性格",
      "significance": "在故事中的作用"
    }}
  ],
  "antagonists": [
    {{
      "name": "反派姓名",
      "tier": "反派层级（小BOSS/中BOSS/大BOSS）",
      "motivation": "动机",
      "power_level": "实力等级"
    }}
  ]
}}

要求：
1. 主角苏长空已有完整的成长路径，续作中他要继续突破魂界境，冲击界主境、虚空境、混沌境
2. 保留前作的核心同伴（华善、姬雪潇、战无双等），他们是苏长空的羁绊
3. 新角色要有血有肉，至少设计8个重要新角色（来自不同天地的强者）
4. 要有新的红颜知己或暧昧角色（来自其他天地的女性强者），与苏长空产生新的情感羁绊
5. 反派要有层次，设计至少3个层级的反派：虚空中的掠夺者、觊觎魂界的界主级强者、虚空深处的古老存在
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
    """生成指定范围的大纲，可指定输出文件（避免并行冲突）"""
    batch_size = 15
    output_file = outline_file or OUTLINE_FILE

    # 先读取世界观和角色
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

    # 加载已有进度（仅从指定输出文件读取）
    outline = {"chapters": []}
    last_chapter = 0
    if output_file.exists():
        with open(output_file, "r", encoding="utf-8") as f:
            outline = json.load(f)
        if outline.get("chapters"):
            last_chapter = max(ch.get("chapter_number", 0) for ch in outline["chapters"])

    # 如果已经生成到end以上，跳过
    if last_chapter >= end:
        print(f"[Planner] 大纲已生成到第{last_chapter}章，范围{start}-{end}已覆盖，跳过")
        return

    # 实际要生成的起始点
    actual_start = max(start, last_chapter + 1)

    system = """你是一位顶级东方玄幻/武侠小说大纲设计师。
你需要设计详细的大纲，每章包含标题、核心事件、涉及角色、场景、情感基调。
主角是已经超脱天地的超级强者苏长空，续写他在无尽虚空中的冒险故事。
输出必须是合法的JSON格式。"""

    for batch_start in range(actual_start, end + 1, batch_size):
        batch_end = min(batch_start + batch_size - 1, end)
        print(f"[Planner] 正在生成第 {batch_start}-{batch_end} 章大纲...")

        # 获取前一批最后几章的摘要作为衔接
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
      "mood": "情感基调（紧张/温馨/悲壮/激昂/扮猪吃虎/打脸等）",
      "key_events": ["事件1", "事件2"],
      "foreshadowing": "埋下的伏笔",
      "power_progression": "实力变化说明（魂界成长/境界突破/新神通领悟）",
      "word_count_target": 5000
    }},
    ...
  ]
}}

要求：
1. 每章必须有独特的核心事件，不能流水账
2. 情节要有起伏，有高潮有低谷，有扮猪吃虎的爽点
3. 苏长空的魂界要逐步成长，实力逐步提升，向界主境、虚空境、混沌境迈进
4. 伏笔要前后呼应，与前一批大纲自然衔接
5. 要有"虚空中的强者轻视苏长空，结果被苏长空以魂界之力碾压"的爽文桥段
6. 苏长空始终保持低调谦逊，但在保护同伴和追求武道时会展现果决狠辣的一面
7. 探索不同天地时要展现文明差异（有的天地修仙、有的魔法、有的科技），增加趣味性
8. 华善、姬雪潇等同伴要有各自的剧情线和成长
9. 必须输出合法JSON，总共{batch_end - batch_start + 1}个章节对象"""

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
            # 每批保存一次，支持断点续传
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(outline, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Planner] 第 {batch_start}-{batch_end} 章解析失败: {e}")
            with open(NOVELS_DIR / f"outline_batch_{batch_start:04d}.raw", "w", encoding="utf-8") as f:
                f.write(content)

    print(f"[Planner] 大纲范围 {start}-{end} 已完成，共 {len(outline['chapters'])} 章")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=1, help="起始章节")
    parser.add_argument("--end", type=int, default=2000, help="结束章节")
    parser.add_argument("--world-only", action="store_true", help="只生成世界观和角色")
    parser.add_argument("--outline-file", type=str, default="", help="指定大纲输出文件路径（用于并行生成）")
    args = parser.parse_args()

    print("=" * 60)
    print(f"Planner Agent 启动 - 范围: 第{args.start}章到第{args.end}章")
    print("=" * 60)

    NOVELS_DIR.mkdir(parents=True, exist_ok=True)

    # 先生成世界观和角色（串行，只需一次）
    generate_world()
    generate_characters()

    if not args.world_only:
        outline_file = Path(args.outline_file) if args.outline_file else None
        generate_outline_range(args.start, args.end, outline_file)

    print("[Planner] 全部完成")


if __name__ == "__main__":
    main()
