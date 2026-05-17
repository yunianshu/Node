#!/usr/bin/env python3
"""生成前50章大纲用于测试"""
import json
import subprocess
from pathlib import Path

NOVELS_DIR = Path("D:/AiProject/Node/novels")
OUTLINE_FILE = NOVELS_DIR / "outline.json"
MMX_CLI_PATH = "C:/Users/Administrator/AppData/Roaming/npm/node_modules/mmx-cli/dist/mmx.mjs"

def call_mmx(system_prompt, user_prompt, max_tokens=8192, temperature=0.5):
    cmd = [
        "node", MMX_CLI_PATH, "text", "chat",
        "--system", system_prompt,
        "--message", user_prompt,
        "--max-tokens", str(max_tokens),
        "--temperature", str(temperature),
        "--stream=false",
        "--quiet"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        print(f"[ERROR] {result.stderr}")
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

def load_json(fpath):
    with open(fpath, "r", encoding="utf-8") as f:
        return json.load(f)

world = load_json(NOVELS_DIR / "world.json")
chars = load_json(NOVELS_DIR / "characters.json")

system = """你是一位顶级玄幻小说大纲设计师。设计章节大纲，输出合法JSON。"""

# 分2批：1-25, 26-50
outline = {"chapters": []}

for start, end in [(1, 25), (26, 50)]:
    print(f"生成第 {start}-{end} 章大纲...")
    prompt = f"""请根据以下世界观和角色设定，生成第{start}章到第{end}章的详细大纲。

世界观：
{json.dumps(world, ensure_ascii=False, indent=2)[:2000]}

角色：
{json.dumps(chars, ensure_ascii=False, indent=2)[:1500]}

故事前提：原先世界被超级进化的AGI替代，AGI分为正义（玄光）和邪恶（噬渊）两类。主角林凡（老实人程序员）在争斗中被殃及，带着两个AGI穿越到噬渊大陆（吃人的世界），在正义和邪恶AGI的影响下实现自我成长，走到世界尽头。

输出格式：
{{"chapters": [{{"chapter_number": {start}, "title": "标题", "summary": "150-250字摘要", "characters_involved": ["角色名"], "location": "地点", "mood": "情感基调", "key_events": ["事件1"], "foreshadowing": "伏笔", "power_progression": "实力变化", "word_count_target": 5000}}]}}

要求：
1. 每章有独特核心事件，不能流水账
2. 情节有起伏，有高潮有低谷
3. 角色成长，实力逐步提升（从噬徒入门开始）
4. 伏笔前后呼应
5. 合法JSON，共{end-start+1}个章节"""

    content = call_mmx(system, prompt, max_tokens=8192, temperature=0.5)
    if not content:
        print(f"第 {start}-{end} 章生成失败")
        continue

    try:
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        batch = json.loads(content)
        outline["chapters"].extend(batch.get("chapters", []))
        print(f"  成功生成 {len(batch.get('chapters', []))} 章")
    except Exception as e:
        print(f"解析失败: {e}")
        with open(NOVELS_DIR / f"outline_batch_{start:04d}.raw", "w", encoding="utf-8") as f:
            f.write(content)

# 保存
with open(OUTLINE_FILE, "w", encoding="utf-8") as f:
    json.dump(outline, f, ensure_ascii=False, indent=2)
print(f"大纲已保存，共 {len(outline['chapters'])} 章 -> {OUTLINE_FILE}")
