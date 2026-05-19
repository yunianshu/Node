#!/usr/bin/env python3
"""快速补全大纲缺失区间。"""

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

import json, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from core.mmx_client import call_mmx, MmxError
from core.novel_config import load_config

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

novels_dir = Path('D:/AiProject/Node/projects/novels8')
outline_file = novels_dir / 'outline.json'
config = load_config(novels_dir)

with open(outline_file, 'r', encoding='utf-8') as f:
    outline = json.load(f)

chapters = outline.get('chapters', [])
existing = {c.get('chapter_number', 0): c for c in chapters}
missing = [i for i in range(1, 2001) if i not in existing]

intervals = []
start = missing[0]
prev = missing[0]
for m in missing[1:]:
    if m == prev + 1: prev = m
    else:
        intervals.append((start, prev))
        start = m; prev = m
intervals.append((start, prev))

world = json.load(open(novels_dir / 'world.json', 'r', encoding='utf-8'))
premise = (novels_dir / 'premise.txt').read_text(encoding='utf-8')
world_json = json.dumps(world, ensure_ascii=False, indent=2)

for s, e in intervals:
    log(f"生成 {s}-{e} ({e-s+1}章)...")
    before = existing.get(s-1)
    after = existing.get(e+1)
    ctx = ""
    if before: ctx += f"\n前一章({s-1}): {before.get('summary','')[:150]}"
    if after: ctx += f"\n后一章({e+1}): {after.get('summary','')[:150]}"

    system = "你是一位顶级东方玄幻/仙侠小说大纲设计师。输出合法JSON。"
    prompt = f"""生成第{s}到第{e}章大纲。

前提：{premise[:1800]}
世界观：{world_json[:1500]}
{ctx}
输出JSON：{{"chapters":[{{"chapter_number":{s},"title":"标题","summary":"摘要150-250字","characters_involved":["角色"],"location":"地点","mood":"基调","key_events":["事件1"],"foreshadowing":"伏笔","power_progression":"实力变化","word_count_target":5000}},...]}}
要求：1.独特核心事件 2.情节起伏 3.升级爽感 4.前后衔接 5.合法JSON，共{e-s+1}章"""

    try:
        content = call_mmx(system, prompt, model=config['model'], mmx_path=config['mmx_path'],
                           max_tokens=8192, temperature=0.5, retries=3, retry_delay=5, qps=config['api_qps'])
    except Exception as ex:
        log(f"API失败: {ex}"); continue

    if not content: log(f"{s}-{e} 返回空"); continue

    try:
        if '```json' in content: content = content.split('```json')[1].split('```')[0].strip()
        elif '```' in content: content = content.split('```')[1].split('```')[0].strip()
        batch = json.loads(content)
        for ch in batch.get('chapters', []):
            existing[ch.get('chapter_number', 0)] = ch
        log(f"{s}-{e} 成功，+{len(batch.get('chapters',[]))}章")
    except Exception as ex:
        log(f"{s}-{e} 解析失败: {ex}")

    # 保存
    outline['chapters'] = sorted(existing.values(), key=lambda c: c.get('chapter_number', 0))
    with open(outline_file, 'w', encoding='utf-8') as f:
        json.dump(outline, f, ensure_ascii=False, indent=2)

log(f"完成，共 {len(existing)} 章")
