#!/usr/bin/env python3
"""补全缺失的大纲章节（检测缺失的chapter_number并补全）"""
import json
import subprocess
from pathlib import Path

NOVELS_DIR = Path("D:/AiProject/Node/novels")
OUTLINE_FILE = NOVELS_DIR / "outline.json"
MMX_CLI_PATH = "C:/Users/Administrator/AppData/Roaming/npm/node_modules/mmx-cli/dist/mmx.mjs"
NOVEL_PREMISE = """原先世界被超级进化的AGI替代了，AGI分为正义和邪恶两类。
在他们争斗过程中，主角被殃及，带着他们一起穿越到一个新的世界。
那是一个"吃人"的世界，主角的老实人性格，在正义和邪恶AGI的影响下实现自我成长，走到世界的尽头。
全书共2000章，每章约5000字。"""


def call_mmx(system_prompt, user_prompt, max_tokens=8192, temperature=0.5):
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


def find_missing_chapters(outline):
    """找出1-2000中缺失的chapter_number"""
    chapters = outline.get("chapters", [])
    existing = {ch.get("chapter_number", 0) for ch in chapters}
    missing = [i for i in range(1, 2001) if i not in existing]
    return missing


def find_gaps(missing):
    """将缺失的章节号分组为连续段落"""
    if not missing:
        return []
    gaps = []
    start = missing[0]
    end = missing[0]
    for i in range(1, len(missing)):
        if missing[i] == end + 1:
            end = missing[i]
        else:
            gaps.append((start, end))
            start = missing[i]
            end = missing[i]
    gaps.append((start, end))
    return gaps


world = load_json(NOVELS_DIR / "world.json")
chars = load_json(NOVELS_DIR / "characters.json")
outline = load_json(OUTLINE_FILE)

missing = find_missing_chapters(outline)
gaps = find_gaps(missing)

print(f"Current chapters: {len(outline['chapters'])}/2000")
print(f"Missing chapters: {len(missing)}")
print(f"Gap segments: {len(gaps)}")
for s, e in gaps:
    print(f"  {s}-{e} ({e-s+1} chapters)")

if not missing:
    print("Outline is complete!")
    exit(0)

system = """你是一位顶级玄幻小说大纲设计师。设计章节大纲，输出合法JSON。"""

MAX_BATCH_SIZE = 10

for start, end in gaps:
    # 将大段拆分成小段
    for batch_start in range(start, end + 1, MAX_BATCH_SIZE):
        batch_end = min(batch_start + MAX_BATCH_SIZE - 1, end)
        count = batch_end - batch_start + 1
        print(f"\nGenerating chapters {batch_start}-{batch_end} ({count} chapters)...")

        # 获取前一批最后几章的摘要作为衔接
        prev_context = ""
        prev_chapters = [ch for ch in outline["chapters"] if ch.get("chapter_number", 0) < batch_start]
        prev_chapters.sort(key=lambda x: x.get("chapter_number", 0), reverse=True)
        if prev_chapters:
            prev_context = "\n前一批最后几章摘要（用于衔接）：\n"
            for ch in prev_chapters[:3]:
                prev_context += f"第{ch.get('chapter_number')}章《{ch.get('title')}》：{ch.get('summary', '')[:100]}...\n"

        # 找 batch_end 之后最早的几章（用于反向衔接）
        next_context = ""
        next_chapters = [ch for ch in outline["chapters"] if ch.get("chapter_number", 0) > batch_end]
        next_chapters.sort(key=lambda x: x.get("chapter_number", 0))
        if next_chapters:
            next_context = "\n后一批前几章摘要（用于衔接）：\n"
            for ch in next_chapters[:2]:
                next_context += f"第{ch.get('chapter_number')}章《{ch.get('title')}》：{ch.get('summary', '')[:100]}...\n"

        prompt = f"""请根据以下世界观和角色设定，生成第{batch_start}章到第{batch_end}章的详细大纲。

世界观：
{json.dumps(world, ensure_ascii=False, indent=2)[:2000]}

角色：
{json.dumps(chars, ensure_ascii=False, indent=2)[:1500]}

故事前提：{NOVEL_PREMISE}

{prev_context}{next_context}

输出格式：
{{"chapters": [{{"chapter_number": {batch_start}, "title": "标题", "summary": "150-250字摘要", "characters_involved": ["角色名"], "location": "地点", "mood": "情感基调", "key_events": ["事件1"], "foreshadowing": "伏笔", "power_progression": "实力变化", "word_count_target": 5000}}]}}

要求：
1. 每章有独特核心事件，不能流水账
2. 情节有起伏，有高潮有低谷
3. 角色成长，实力逐步提升（从噬徒到噬源境界）
4. 伏笔前后呼应，与前一批大纲自然衔接
5. 合法JSON，共{count}个章节对象
6. 章节编号必须从{batch_start}开始，严格连续到{batch_end}，不能跳号"""

        content = ""
        for attempt in range(3):
            content = call_mmx(system, prompt, max_tokens=8192, temperature=0.5)
            if content:
                break
            print(f"  Attempt {attempt + 1} failed, retrying...")

        if not content:
            print(f"  Failed after retries: chapters {batch_start}-{batch_end}")
            continue

        # 保存原始输出
        with open(NOVELS_DIR / f"outline_batch_{batch_start:04d}.raw", "w", encoding="utf-8") as f:
            f.write(content)

        try:
            # 提取JSON
            extracted = content
            if "```json" in extracted:
                extracted = extracted.split("```json")[1].split("```")[0].strip()
            elif "```" in extracted:
                extracted = extracted.split("```")[1].split("```")[0].strip()

            # 尝试修复截断JSON：找最后一个完整的章节对象
            batch = json.loads(extracted)
            new_chapters = batch.get("chapters", [])

            # 验证章节编号
            expected_nums = set(range(batch_start, batch_end + 1))
            actual_nums = {ch.get("chapter_number", 0) for ch in new_chapters}
            missing_in_batch = expected_nums - actual_nums
            if missing_in_batch:
                print(f"  Warning: missing numbers in batch: {sorted(missing_in_batch)}")

            # 合并到现有大纲，保持排序
            outline["chapters"].extend(new_chapters)
            outline["chapters"].sort(key=lambda x: x.get("chapter_number", 0))

            print(f"  Success: {len(new_chapters)} chapters added")

            # 每批保存一次
            with open(OUTLINE_FILE, "w", encoding="utf-8") as f:
                json.dump(outline, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"  Parse error: {e}")
            # 已保存.raw文件，可手动修复

final_count = len(outline["chapters"])
missing_final = find_missing_chapters(outline)
print(f"\nOutline saved: {final_count}/2000 chapters")
print(f"Still missing: {len(missing_final)} chapters")
if missing_final:
    print(f"Missing: {missing_final[:20]}...")
