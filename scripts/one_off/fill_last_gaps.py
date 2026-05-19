

from pathlib import Path
import sys

TOOLS_ROOT = Path(__file__).resolve().parents[1]
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

import json, sys
sys.path.insert(0, 'D:/AiProject/Node/scripts')
from pathlib import Path
from core.mmx_client import call_mmx
from core.novel_config import load_config

novels_dir = Path('D:/AiProject/Node/projects/novels8')
config = load_config(novels_dir)
outline_file = novels_dir / 'outline.json'
world = json.load(open(novels_dir / 'world.json', 'r', encoding='utf-8'))
premise = (novels_dir / 'premise.txt').read_text(encoding='utf-8')
world_json = json.dumps(world, ensure_ascii=False, indent=2)

with open(outline_file, 'r', encoding='utf-8') as f:
    outline = json.load(f)
existing = {c.get('chapter_number', 0): c for c in outline.get('chapters', [])}

for s, e in [(1146, 1160), (1461, 1475)]:
    print(f'生成 {s}-{e}...', flush=True)
    before = existing.get(s-1)
    after = existing.get(e+1)
    ctx = ''
    if before: ctx += f'前({s-1}): {before.get("summary", "")[:150]}\n'
    if after: ctx += f'后({e+1}): {after.get("summary", "")[:150]}'

    system = '你是一位顶级东方玄幻/仙侠小说大纲设计师。输出合法JSON。'
    prompt = f'''生成第{s}到第{e}章大纲。
前提：{premise[:1800]}
世界观：{world_json[:1500]}
{ctx}
输出JSON：{{"chapters":[{{"chapter_number":{s},"title":"标题","summary":"摘要150-250字","characters_involved":["角色"],"location":"地点","mood":"基调","key_events":["事件1"],"foreshadowing":"伏笔","power_progression":"实力变化","word_count_target":5000}},...]}}
要求：独特事件、情节起伏、升级爽感、前后衔接、合法JSON，共{e-s+1}章'''

    content = call_mmx(system, prompt, model=config['model'], mmx_path=config['mmx_path'],
                       max_tokens=8192, temperature=0.5, retries=3, retry_delay=5, qps=config['api_qps'])
    if not content:
        print(f'{s}-{e} 失败'); continue

    try:
        if '```json' in content: content = content.split('```json')[1].split('```')[0].strip()
        elif '```' in content: content = content.split('```')[1].split('```')[0].strip()
        batch = json.loads(content)
        for ch in batch.get('chapters', []):
            existing[ch.get('chapter_number', 0)] = ch
        print(f'{s}-{e} 成功 +{len(batch.get("chapters",[]))}章', flush=True)
    except Exception as ex:
        print(f'{s}-{e} 解析失败: {ex}')

outline['chapters'] = sorted(existing.values(), key=lambda c: c.get('chapter_number', 0))
with open(outline_file, 'w', encoding='utf-8') as f:
    json.dump(outline, f, ensure_ascii=False, indent=2)
print(f'完成: {len(existing)} 章', flush=True)
