#!/usr/bin/env python3
"""
Planner Agent - 框架总领Agent
负责生成和维护小说世界观、章节大纲、角色档案
支持 --start/--end 参数，可并行生成指定范围的大纲
"""
import json
import os
import subprocess
import sys
from pathlib import Path

NOVELS_DIR = Path("D:/AiProject/Node/novels3")
WORLD_FILE = NOVELS_DIR / "world.json"
OUTLINE_FILE = NOVELS_DIR / "outline.json"
CHARACTERS_FILE = NOVELS_DIR / "characters.json"

# mmx CLI 路径（Windows 需通过 node 直接运行）
MMX_CLI_PATH = "C:/Users/Administrator/AppData/Roaming/npm/node_modules/mmx-cli/dist/mmx.mjs"

NOVEL_PREMISE = """主角原本是一个文弱书生，整日与诗书为伴，手无缚鸡之力，被人嘲笑为"百无一用是书生"。
然而机缘巧合之下，他获得了一部上古武道秘籍，从此踏上了一条"以文入武、以书证道"的逆天之路。
他虽外表文弱，却能在谈笑间一掌碎山河；虽不善言辞，却能让天下武夫尽低头。
在这个武道为尊的世界里，他要用一支笔、一卷书，证明书生亦可武道通神。
全书共2000章，每章约5000字。风格类似《一介书生，但武道通神》，融合儒道流、武道流、扮猪吃虎等元素。"""


def call_mmx(system_prompt: str, user_prompt: str, max_tokens: int = 8192, temperature: float = 0.4) -> str:
    """调用 mmx text chat 生成内容（通过 node 直接运行 mmx-cli）"""
    cmd = [
        "node", MMX_CLI_PATH, "text", "chat",
        "--model", "MiniMax-M2.7-highspeed",
        "--system", system_prompt,
        "--message", user_prompt,
        "--max-tokens", str(max_tokens),
        "--temperature", str(temperature),
        "--stream=false",
        "--quiet"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        if result.returncode != 0:
            err = result.stderr.strip() if result.stderr else "unknown error"
            print(f"[ERROR] mmx call failed (rc={result.returncode}): {err}", file=sys.stderr)
            return ""
        raw = result.stdout.strip()
        try:
            data = json.loads(raw)
            return data.get("content", raw)
        except json.JSONDecodeError:
            pass
        if "Response:" in raw:
            json_part = raw.split("Response:")[-1].strip()
            try:
                data = json.loads(json_part)
                return data.get("content", raw)
            except json.JSONDecodeError:
                return json_part
        return raw
    except Exception as e:
        print(f"[ERROR] mmx subprocess exception: {e}", file=sys.stderr)
        return ""


def generate_world():
    """生成世界观设定"""
    if WORLD_FILE.exists():
        print(f"[Planner] world.json 已存在，跳过生成")
        return

    system = """你是一位顶级东方玄幻/武侠世界观架构师。
你需要根据用户提供的故事 premise，构建一个完整、详细、有深度的玄幻世界。
这个世界要有"儒道"与"武道"的碰撞融合，有独特的修炼体系。
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
    "description": "修炼体系描述（儒道与武道融合的独特体系）",
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
    "act1": "第一幕描述（1-500章）：书生觉醒，初窥武道门径，以文入武",
    "act2": "第二幕描述（501-1200章）：儒武合一，名动天下，挑战各大武道圣地",
    "act3": "第三幕描述（1201-2000章）：武道通神，以书证道，最终成就无上儒武大道"
  }}
}}

要求：
1. 修炼体系要有12个以上的等级，融合"儒道文气"与"武道真气"，每个等级有独特名称和能力
2. 世界要有明确的法则和限制，儒道修行与武道修行相互制约又相辅相成
3. 地点要有层次感和探索价值，包括书院、武道圣地、秘境等
4. 整体架构要支撑2000章的篇幅，有明确的成长曲线和升级节奏
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
    """生成角色档案"""
    if CHARACTERS_FILE.exists():
        print(f"[Planner] characters.json 已存在，跳过生成")
        return

    system = """你是一位顶级角色设计师，擅长设计有深度、有成长弧线的角色。
你需要根据故事 premise 设计主要角色，主角是外表文弱但内心坚韧的书生。
输出必须是合法的JSON格式。"""

    prompt = f"""请根据以下故事设定，设计主要角色档案：

{NOVEL_PREMISE}

请输出以下JSON结构：
{{
  "protagonist": {{
    "name": "主角姓名",
    "alias": ["别名1", "别名2"],
    "age": 20,
    "personality": "性格特征（外表文弱、内心坚韧、不善言辞但腹有诗书、低调内敛）",
    "background": "背景故事（出身贫寒书生家庭，天资聪颖但体质孱弱）",
    "initial_power": "初始实力（手无缚鸡之力的普通书生）",
    "target_power": "最终实力（儒武通神，以书证道）",
    "character_arc": "成长弧线描述（从文弱书生到武道巨擘，始终保持书生本心）",
    "key_traits": ["特质1", "特质2", "特质3"],
    "flaws": ["缺点1", "缺点2"],
    "motivations": ["动机1", "动机2"]
  }},
  "mentors": [
    {{
      "name": "导师姓名",
      "type": "儒道导师/武道导师",
      "personality": "性格",
      "relationship_with_protagonist": "与主角关系"
    }}
  ],
  "supporting_characters": [
    {{
      "name": "配角姓名",
      "role": "角色定位（朋友/敌人/导师/爱人/同门等）",
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
1. 主角要有清晰的成长路径（从弱到强，从文弱到武道通神）
2. 主角要保持书生本色，即使实力通天也谦逊有礼
3. 配角要有血有肉，至少设计10个重要配角
4. 要有红颜知己角色，与主角产生情感羁绊
5. 反派要有层次，设计至少3个层级的反派
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
主角是文弱书生但武道通神，融合儒道与武道元素。
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
      "power_progression": "实力变化说明（儒道文气或武道真气的突破）",
      "word_count_target": 5000
    }},
    ...
  ]
}}

要求：
1. 每章必须有独特的核心事件，不能流水账
2. 情节要有起伏，有高潮有低谷，有扮猪吃虎的爽点
3. 角色要有成长，实力逐步提升，儒道与武道交替突破
4. 伏笔要前后呼应，与前一批大纲自然衔接
5. 要有"外人以为主角很弱，结果被主角震惊"的爽文桥段
6. 主角始终保留书生气质，即使实力强大也谦逊有礼
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
