import json
import glob
import re

def fix_quotes_in_line(line):
    m = re.match(r'^(\s*"[\w_]+"\s*:\s*")(.*)("\s*,?\s*)$', line)
    if not m:
        return line
    prefix = m.group(1)
    value = m.group(2)
    suffix = m.group(3)
    value = value.replace('\\"', '\x00ESCAPED\x00')
    value = value.replace('"', '\\"')
    value = value.replace('\x00ESCAPED\x00', '\\"')
    return prefix + value + suffix

files = sorted(glob.glob('D:/AiProject/Node/projects/novels7/output/outline_chapters/chapter_*.json'))
fixed = 0
failed = []
for f in files:
    try:
        with open(f, 'r', encoding='utf-8') as fp:
            json.load(fp)
    except Exception:
        with open(f, 'r', encoding='utf-8') as fp:
            lines = fp.readlines()
        new_lines = [fix_quotes_in_line(line) for line in lines]
        new_content = ''.join(new_lines)
        try:
            data = json.loads(new_content)
            with open(f, 'w', encoding='utf-8') as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
            fixed += 1
        except Exception as e:
            failed.append((f, str(e)))

print(f'Fixed {fixed} files')
if failed:
    print(f'Failed to fix {len(failed)} files')
    for f, e in failed[:5]:
        print(f'  {f}: {e}')
