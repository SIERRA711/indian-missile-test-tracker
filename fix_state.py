import json
import re

with open('.tracker_state.json', 'r', encoding='utf-8') as f:
    lines = f.readlines()
clean_lines = []
for line in lines:
    if line.startswith('<<<<<<<') or line.startswith('=======') or line.startswith('>>>>>>>'):
        continue
    clean_lines.append(line)
content = ''.join(clean_lines)
prids = re.findall(r'\"(\d+)\"', content)
prids = list(set(prids))
last_run = re.findall(r'\"last_run\":\s*\"([^\"]+)\"', content)
lr = last_run[-1] if last_run else None
with open('.tracker_state.json', 'w', encoding='utf-8') as f:
    json.dump({'seen_prids': prids, 'last_run': lr}, f, indent=2)
