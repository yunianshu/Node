#!/usr/bin/env python3
import json
import re

# 修复 world.json
with open('D:/AiProject/Node/novels/world.json', 'r', encoding='utf-8') as f:
    data = json.load(f)
content = data['content']
# 提取 ```json ... ``` 中的内容
match = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL)
if match:
    world = json.loads(match.group(1))
else:
    world = json.loads(content)
# 统一主角名字
world_str = json.dumps(world, ensure_ascii=False)
world_str = world_str.replace('叶凡', '林凡')
world = json.loads(world_str)
with open('D:/AiProject/Node/novels/world.json', 'w', encoding='utf-8') as f:
    json.dump(world, f, ensure_ascii=False, indent=2)
print('world.json 已修复，字数:', len(world_str))

# 修复 characters.json
with open('D:/AiProject/Node/novels/characters.json', 'r', encoding='utf-8') as f:
    data = json.load(f)
content = data['content']
match = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL)
if match:
    chars = json.loads(match.group(1))
else:
    chars = json.loads(content)
with open('D:/AiProject/Node/novels/characters.json', 'w', encoding='utf-8') as f:
    json.dump(chars, f, ensure_ascii=False, indent=2)
print('characters.json 已修复，字数:', len(json.dumps(chars, ensure_ascii=False)))
