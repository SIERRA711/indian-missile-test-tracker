import csv
from datetime import datetime
import re

with open('normalized_missiles.csv', 'r', encoding='utf-8') as f:
    db = list(csv.DictReader(f))

with open('notebooklm_candidates.csv', 'r', encoding='utf-8') as f:
    candidates = list(csv.DictReader(f))

def clean_family(s):
    return s.lower().replace('-', '').replace(' ', '').strip()

for c in candidates:
    fam = clean_family(c['family'])
    c_year = c['date'][:4]
    found = False
    for r in db:
        if clean_family(r['family']) == fam and r['date'].startswith(c_year):
            found = True
            
    if not found:
        print(f"NEW ENTRY: {c['date']} | {c['family']} {c['variant']} | {c['notes']}")
