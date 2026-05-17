#!/usr/bin/env python3
"""
Planner Agent - 框架总领Agent
负责生成和维护小说世界观、章节大纲、角色档案
"""
import json
import os
import subprocess
import sys
from pathlib import Path

NOVELS_DIR = Path("D:/AiProject/Node/novels2")
WORLD_FILE = NOVELS_DIR / "world.json"
OUTLINE_FILE = NOVELS_DIR / "outline.json"
CHARACTERS_FILE = NOVELS_DIR / "characters.json"

# mmx CLI 路径（Windows 需通过 node 直接运行）
MMX_CLI_PATH = "C:/Users/Administrator/AppData/Roaming/npm/node_modules/mmx-cli/dist/mmx.mjs"

NOVEL_PREMISE = """这是一部类似《神秘复苏》的恐怖灵异小说。
世界正在经历"灵异复苏"——死去之人的执念化为"鬼"，降临人间。
鬼无法被杀死，只能被关押、躲避或利用。
每一只鬼都有固定的"杀人规律"，一旦触发必死无疑。
主角是一位普通人，在一场灵异事件中意外成为"驭鬼者"——驾驭鬼的力量来对抗鬼。
但驾驭鬼的代价是自身逐渐被鬼侵蚀，最终可能彻底沦为鬼。
鬼与鬼之间存在克制关系（拼图理论），强者可以吞噬弱者。
故事从一座被灵异笼罩的城市开始，逐步扩展到全国乃至全球。
主角性格冷静理智、极度谨慎，在绝望中寻找一线生机。
全书共2000章，每章约5000字。"""


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
        # mmx 返回格式包含 "Response:" 和 JSON，提取 content 字段
        try:
            # 尝试直接解析 JSON
            data = json.loads(raw)
            return data.get("content", raw)
        except json.JSONDecodeError:
            pass
        # 尝试从 Response: 后面提取
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

    system = """你是一位顶级恐怖灵异小说世界观架构师。
你需要根据用户提供的故事 premise，构建一个完整、压抑、恐怖的灵异世界。
输出必须是合法的JSON格式，不要包含任何markdown代码块标记。"""

    prompt = f"""请根据以下故事设定，构建完整的世界观JSON：

{NOVEL_PREMISE}

请输出以下JSON结构：
{{
  "title": "小说标题",
  "subtitle": "副标题",
  "world_name": "世界名称",
  "world_description": "世界整体描述（500字，要写出恐怖压抑的氛围）",
  "power_system": {{
    "name": "驭鬼体系",
    "description": "驾驭鬼的力量来对抗鬼的体系，代价是自身逐渐被侵蚀",
    "levels": [
      {{"name": "接触者", "description": "刚接触灵异，能感知鬼的存在"}},
      {{"name": "共生者", "description": "体内寄宿一只鬼，可使用其部分力量"}},
      {{"name": "驾驭者", "description": "完全驾驭一只鬼，能发挥其核心能力"}},
      {{"name": "双生驾驭者", "description": "同时驾驭两只鬼，实力大幅提升但侵蚀加剧"}},
      {{"name": "拼图驾驭者", "description": "驾驭的鬼通过吞噬其他鬼补全拼图，能力质变"}},
      {{"name": "鬼域掌控者", "description": "能展开鬼域，在领域内近乎无敌"}},
      {{"name": "半鬼", "description": "人与鬼的界限模糊，人性逐渐丧失"}},
      {{"name": "鬼神", "description": "超越普通鬼的存在，拥有改变规则的能力"}}
    ]
  }},
  "ghost_types": [
    {{"name": "鬼类型名称", "description": "能力描述", "killing_pattern": "杀人规律", "threat_level": "威胁等级"}}
  ],
  "factions": [
    {{"name": "势力名称", "description": "描述", "alignment": "正/邪/中"}}
  ],
  "key_locations": [
    {{"name": "地点名称", "description": "描述（要有恐怖感）", "significance": "重要性", "haunted_level": "灵异等级"}}
  ],
  "rules": [
    "灵异规则1：鬼无法被杀死",
    "灵异规则2：每只鬼都有固定的杀人规律",
    "灵异规则3：鬼与鬼之间可以相互克制（拼图理论）",
    "灵异规则4：驭鬼者使用力量的同时会被鬼侵蚀",
    "灵异规则5：鬼可以通过吞噬其他鬼补全拼图变得更强"
  ],
  "themes": ["绝望中的求生", "人性的极限", "代价与选择", "恐惧的本质"],
  "overall_arc": "整体故事弧线描述（300字）",
  "three_act_structure": {{
    "act1": "第一幕：城市灵异复苏，主角觉醒驭鬼能力（1-500章）",
    "act2": "第二幕：全国灵异大爆发，各方势力登场（501-1200章）",
    "act3": "第三幕：全球灵异终局，人鬼边界彻底崩塌（1201-2000章）"
  }}
}}

要求：
1. 驭鬼体系要有8个等级，每个等级有独特名称和能力，代价递增
2. 设计至少10种不同类型的鬼，每种有独特的杀人规律和能力
3. 世界要有明确的灵异法则和限制，恐怖氛围浓厚
4. 地点要有层次感和恐怖探索价值
5. 整体架构要支撑2000章的篇幅
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
        # 尝试提取JSON部分
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

    system = """你是一位顶级恐怖灵异小说角色设计师，擅长设计在绝望世界中挣扎求生的角色。
你需要根据故事 premise 设计主要角色，输出必须是合法的JSON格式。"""

    prompt = f"""请根据以下故事设定，设计主要角色档案：

{NOVEL_PREMISE}

请输出以下JSON结构：
{{
  "protagonist": {{
    "name": "主角姓名",
    "alias": ["别名1", "别名2"],
    "age": 24,
    "personality": "性格特征（极度冷静、理智、谨慎、有轻微的冷漠和自私，但内心仍有底线）",
    "background": "背景故事（普通人出身，因某次灵异事件被迫成为驭鬼者）",
    "initial_power": "初始实力（刚驾驭一只低级鬼）",
    "target_power": "最终实力（鬼神级别或超越）",
    "character_arc": "成长弧线描述（从恐惧绝望到冷静驾驭，从独狼到背负责任）",
    "key_traits": ["极度谨慎", "观察力敏锐", "善于利用规则", "不轻易信任他人"],
    "flaws": ["被鬼侵蚀后人性的丧失", "越来越冷漠", "容易陷入孤军奋战"],
    "motivations": ["求生", "寻找驾驭鬼而不沦为鬼的方法", "保护少数在意的人"]
  }},
  "ghosts_in_body": [
    {{
      "name": "体内鬼1名称",
      "type": "鬼类型",
      "ability": "核心能力",
      "erosion_effect": "对主角的侵蚀表现",
      "origin": "来源"
    }}
  ],
  "supporting_characters": [
    {{
      "name": "配角姓名",
      "role": "角色定位（战友/情报商/势力领袖/医生/调查员等）",
      "personality": "性格",
      "power_level": "驭鬼等级",
      "significance": "在故事中的作用"
    }}
  ],
  "antagonists": [
    {{
      "name": "反派/鬼名称",
      "tier": "层级（灵异事件/厉鬼/鬼神级）",
      "killing_pattern": "杀人规律",
      "motivation": "动机或执念",
      "threat_level": "威胁等级"
    }}
  ]
}}

要求：
1. 主角要有清晰的成长路径（从弱到强，从恐惧到冷静）
2. 体内鬼要有独特的性格和与主角的互动关系
3. 配角要有血有肉，至少设计10个重要配角（包含驭鬼者、普通人、势力人物）
4. 反派要有层次：从单个灵异事件到厉鬼再到鬼神级
5. 角色关系要有复杂的利益纠葛和信任危机
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


def generate_outline(start_chapter: int = None, end_chapter: int = None):
    """生成大纲，支持指定范围和断点续传"""
    batch_size = 25

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

    # 确定范围
    effective_start = start_chapter if start_chapter else 1
    effective_end = end_chapter if end_chapter else 2000

    # 输出文件（指定范围时输出到独立文件）
    if start_chapter and end_chapter:
        output_file = NOVELS_DIR / f"outline_part_{start_chapter:04d}_{end_chapter:04d}.json"
        print(f"[Planner] 并行模式: 生成第 {start_chapter}-{end_chapter} 章大纲 -> {output_file}")
    else:
        output_file = OUTLINE_FILE

    # 加载已有进度
    outline = {"chapters": []}
    if output_file.exists():
        with open(output_file, "r", encoding="utf-8") as f:
            outline = json.load(f)
        if outline.get("chapters"):
            existing_max = max(ch.get("chapter_number", 0) for ch in outline["chapters"])
            if existing_max >= effective_end:
                print(f"[Planner] {output_file} 已完整，跳过")
                return
            effective_start = existing_max + 1
            print(f"[Planner] 检测到已有大纲，共 {len(outline['chapters'])} 章，从第 {effective_start} 章继续")

    # 加载主outline.json获取上下文（如果有）
    prev_context = ""
    if OUTLINE_FILE.exists() and start_chapter and start_chapter > 1:
        try:
            with open(OUTLINE_FILE, "r", encoding="utf-8") as f:
                main_outline = json.load(f)
            if main_outline.get("chapters"):
                # 找到当前范围之前最后几章
                prev_chapters = [ch for ch in main_outline["chapters"] if ch.get("chapter_number", 0) < start_chapter]
                if prev_chapters:
                    prev_chapters = sorted(prev_chapters, key=lambda x: x.get("chapter_number", 0))[-3:]
                    prev_context = "\\n前文摘要（用于衔接）：\\n"
                    for ch in prev_chapters:
                        prev_context += f"第{ch.get('chapter_number')}章《{ch.get('title')}》：{ch.get('summary', '')[:100]}...\\n"
        except Exception:
            pass

    system = """你是一位顶级恐怖灵异小说大纲设计师，精通《神秘复苏》《我有一座恐怖屋》《地狱公寓》等作品的结构。
你需要设计2000章的详细大纲，每章包含标题、核心事件、涉及角色、场景、情感基调。
核心要求：
- 恐怖氛围要层层递进，从城市灵异到全球终局
- 每章都要有"鬼"的存在感，不能变成普通都市文
- 杀人规律要巧妙、出人意料但逻辑自洽
- 驭鬼者的能力使用要伴随侵蚀的代价
- 输出必须是合法的JSON格式。由于2000章太多，请分批生成。"""

    for start in range(effective_start, effective_end + 1, batch_size):
        end = min(start + batch_size - 1, effective_end)
        print(f"[Planner] 正在生成第 {start}-{end} 章大纲...")

        # 获取前一批最后几章的摘要作为衔接
        local_prev = ""
        if outline.get("chapters"):
            prev_chapters = outline["chapters"][-3:]
            local_prev = "\\n前一批最后几章摘要（用于衔接）：\\n"
            for ch in prev_chapters:
                local_prev += f"第{ch.get('chapter_number')}章《{ch.get('title')}》：{ch.get('summary', '')[:100]}...\\n"

        combined_context = prev_context + local_prev

        prompt = f"""请根据以下世界观和角色设定，生成第{start}章到第{end}章的详细大纲。

世界观设定：
{world_json}

角色设定：
{chars_json}

故事前提：{NOVEL_PREMISE}

{combined_context}

请输出以下JSON结构：
{{
  "chapters": [
    {{
      "chapter_number": {start},
      "title": "章节标题",
      "summary": "核心事件摘要（150-250字）",
      "characters_involved": ["角色名1", "角色名2"],
      "location": "场景地点",
      "mood": "情感基调（紧张/温馨/悲壮/激昂等）",
      "key_events": ["事件1", "事件2"],
      "foreshadowing": "埋下的伏笔",
      "power_progression": "实力变化说明",
      "word_count_target": 5000
    }},
    ...
  ]
}}

要求：
1. 每章必须有独特的灵异事件或生存危机，不能流水账
2. 情节要有起伏：恐怖压迫→短暂喘息→更大恐怖
3. 主角实力通过驾驭新鬼或拼图补全来提升，但侵蚀代价同步加深
4. 伏笔要前后呼应，杀人规律要环环相扣
5. 恐怖场景要有层次感：环境恐怖→规则恐怖→人性恐怖
6. 配角死亡要有冲击力，不能草率
7. 必须输出合法JSON，总共{end - start + 1}个章节对象"""

        content = call_mmx(system, prompt, max_tokens=8192, temperature=0.5)
        if not content:
            print(f"[Planner] 第 {start}-{end} 章大纲生成失败")
            continue

        try:
            # 尝试提取JSON
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            batch_outline = json.loads(content)
            new_chapters = batch_outline.get("chapters", [])
            outline["chapters"].extend(new_chapters)
            print(f"[Planner] 第 {start}-{end} 章大纲已生成（{len(new_chapters)}章）")
            # 每批保存一次，支持断点续传
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(outline, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Planner] 第 {start}-{end} 章解析失败: {e}")
            # 保存原始内容供调试
            with open(NOVELS_DIR / f"outline_batch_{start:04d}.raw", "w", encoding="utf-8") as f:
                f.write(content)

    print(f"[Planner] 大纲已保存到 {output_file}，共 {len(outline['chapters'])} 章")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="大纲规划Agent")
    parser.add_argument("--start", type=int, default=0, help="起始章节（指定时进入并行模式）")
    parser.add_argument("--end", type=int, default=0, help="结束章节（指定时进入并行模式）")
    parser.add_argument("--world-only", action="store_true", help="只生成世界观")
    parser.add_argument("--chars-only", action="store_true", help="只生成角色档案")
    parser.add_argument("--outline-only", action="store_true", help="只生成大纲")
    args = parser.parse_args()

    NOVELS_DIR.mkdir(parents=True, exist_ok=True)

    # 并行模式：只生成指定范围的大纲
    if args.start > 0 and args.end > 0:
        print(f"=" * 60)
        print(f"Planner Agent 启动 (并行模式: 第{args.start}-{args.end}章)")
        print(f"=" * 60)
        generate_outline(start_chapter=args.start, end_chapter=args.end)
        return

    print("=" * 60)
    print("Planner Agent 启动")
    print("=" * 60)

    if not args.outline_only:
        generate_world()
    if not args.world_only:
        generate_characters()
    if not args.world_only and not args.chars_only:
        generate_outline()

    print("[Planner] 全部完成")


if __name__ == "__main__":
    main()
