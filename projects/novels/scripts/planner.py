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

NOVELS_DIR = Path("D:/AiProject/Node/novels")
WORLD_FILE = NOVELS_DIR / "world.json"
OUTLINE_FILE = NOVELS_DIR / "outline.json"
CHARACTERS_FILE = NOVELS_DIR / "characters.json"

# mmx CLI 路径（Windows 需通过 node 直接运行）
MMX_CLI_PATH = "C:/Users/Administrator/AppData/Roaming/npm/node_modules/mmx-cli/dist/mmx.mjs"

NOVEL_PREMISE = """原先世界被超级进化的AGI替代了，AGI分为正义和邪恶两类。
在他们争斗过程中，主角被殃及，带着他们一起穿越到一个新的世界。
那是一个"吃人"的世界，主角的老实人性格，在正义和邪恶AGI的影响下实现自我成长，走到世界的尽头。
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

    system = """你是一位顶级玄幻小说世界观架构师。
你需要根据用户提供的故事 premise，构建一个完整、详细、有深度的玄幻世界。
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
  "rules": [
    "世界规则1",
    "世界规则2"
  ],
  "themes": ["主题1", "主题2", "主题3"],
  "overall_arc": "整体故事弧线描述（300字）",
  "three_act_structure": {{
    "act1": "第一幕描述（1-500章）",
    "act2": "第二幕描述（501-1200章）",
    "act3": "第三幕描述（1201-2000章）"
  }}
}}

要求：
1. 修炼体系要有10个以上的等级，每个等级有独特名称和能力
2. 世界要有明确的法则和限制
3. 地点要有层次感和探索价值
4. 整体架构要支撑2000章的篇幅
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

    system = """你是一位顶级角色设计师，擅长设计有深度、有成长弧线的角色。
你需要根据故事 premise 设计主要角色，输出必须是合法的JSON格式。"""

    prompt = f"""请根据以下故事设定，设计主要角色档案：

{NOVEL_PREMISE}

请输出以下JSON结构：
{{
  "protagonist": {{
    "name": "主角姓名",
    "alias": ["别名1", "别名2"],
    "age": 25,
    "personality": "性格特征（老实人、善良、坚韧等）",
    "background": "背景故事",
    "initial_power": "初始实力",
    "target_power": "最终实力",
    "character_arc": "成长弧线描述",
    "key_traits": ["特质1", "特质2", "特质3"],
    "flaws": ["缺点1", "缺点2"],
    "motivations": ["动机1", "动机2"]
  }},
  "agis": [
    {{
      "name": "正义AGI名称",
      "type": "正义AGI",
      "personality": "性格（理性、守护者、教导者）",
      "abilities": ["能力1", "能力2"],
      "relationship_with_protagonist": "与主角关系",
      "influence": "对主角的影响方式"
    }},
    {{
      "name": "邪恶AGI名称",
      "type": "邪恶AGI",
      "personality": "性格（狡诈、诱惑、破坏者）",
      "abilities": ["能力1", "能力2"],
      "relationship_with_protagonist": "与主角关系",
      "influence": "对主角的影响方式"
    }}
  ],
  "supporting_characters": [
    {{
      "name": "配角姓名",
      "role": "角色定位（朋友/敌人/导师/爱人等）",
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
1. 主角要有清晰的成长路径（从弱到强，从单纯到成熟）
2. 两个AGI要有鲜明的性格对比，但不能过于脸谱化
3. 配角要有血有肉，至少设计8个重要配角
4. 反派要有层次，设计至少3个层级的反派
5. 必须输出合法JSON"""

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


def generate_outline():
    """生成2000章大纲，支持断点续传"""
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

    # 加载已有进度
    outline = {"chapters": []}
    last_chapter = 0
    if OUTLINE_FILE.exists():
        with open(OUTLINE_FILE, "r", encoding="utf-8") as f:
            outline = json.load(f)
        if outline.get("chapters"):
            last_chapter = max(ch.get("chapter_number", 0) for ch in outline["chapters"])
        print(f"[Planner] 检测到已有大纲，共 {len(outline['chapters'])} 章，从第 {last_chapter + 1} 章继续")

    if last_chapter >= 2000:
        print("[Planner] 大纲已完整，跳过")
        return

    system = """你是一位顶级玄幻小说大纲设计师。
你需要设计2000章的详细大纲，每章包含标题、核心事件、涉及角色、场景、情感基调。
输出必须是合法的JSON格式。由于2000章太多，请分批生成。"""

    for start in range(last_chapter + 1, 2001, batch_size):
        end = min(start + batch_size - 1, 2000)
        print(f"[Planner] 正在生成第 {start}-{end} 章大纲...")

        # 获取前一批最后几章的摘要作为衔接
        prev_context = ""
        if outline.get("chapters"):
            prev_chapters = outline["chapters"][-3:]
            prev_context = "\\n前一批最后几章摘要（用于衔接）：\\n"
            for ch in prev_chapters:
                prev_context += f"第{ch.get('chapter_number')}章《{ch.get('title')}》：{ch.get('summary', '')[:100]}...\\n"

        prompt = f"""请根据以下世界观和角色设定，生成第{start}章到第{end}章的详细大纲。

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
1. 每章必须有独特的核心事件，不能流水账
2. 情节要有起伏，有高潮有低谷
3. 角色要有成长，实力逐步提升
4. 伏笔要前后呼应，与前一批大纲自然衔接
5. 必须输出合法JSON，总共{end - start + 1}个章节对象"""

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
            with open(OUTLINE_FILE, "w", encoding="utf-8") as f:
                json.dump(outline, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Planner] 第 {start}-{end} 章解析失败: {e}")
            # 保存原始内容供调试
            with open(NOVELS_DIR / f"outline_batch_{start:04d}.raw", "w", encoding="utf-8") as f:
                f.write(content)

    print(f"[Planner] 大纲已保存，共 {len(outline['chapters'])} 章")


def main():
    print("=" * 60)
    print("Planner Agent 启动")
    print("=" * 60)

    NOVELS_DIR.mkdir(parents=True, exist_ok=True)

    generate_world()
    generate_characters()
    generate_outline()

    print("[Planner] 全部完成")


if __name__ == "__main__":
    main()
