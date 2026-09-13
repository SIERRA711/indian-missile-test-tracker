import csv
import re
from collections import defaultdict, Counter

# Updated catMap matching current index.html
UI_CATMAP = [
    ('akash',           'Surface-to-Air'),
    ('kusha',           'Surface-to-Air'),
    ('xrsam',           'Surface-to-Air'),
    ('qrsam',           'Surface-to-Air'),
    ('anant shastra',   'Surface-to-Air'),
    ('vshorad',         'Surface-to-Air'),
    ('vshorads',        'Surface-to-Air'),
    ('vl-srsam',        'Surface-to-Air'),
    ('vlsrsam',         'Surface-to-Air'),
    ('mrsam',           'Surface-to-Air'),
    ('iadws',           'Surface-to-Air'),
    ('samar',           'Surface-to-Air'),
    ('astra',           'Air-to-Air'),
    ('ngccm',           'Air-to-Air'),
    ('sfdr',            'Air-to-Air'),
    ('brahmos',         'Anti-Ship/Cruise'),
    ('nasm-sr',         'Anti-Ship/Cruise'),
    ('nasm-mr',         'Anti-Ship/Cruise'),
    ('lr-ashm',         'Anti-Ship/Cruise'),
    ('rudram',          'Air-Launched Cruise'),
    ('rudram-ii',       'Air-Launched Cruise'),
    ('itcm',            'Air-Launched Cruise'),
    ('lr-lacm',         'Land-Attack Cruise'),
    ('slcm',            'Sub-Launched Cruise'),
    ('ulpgm',           'Precision Guided Munition'),
    ('agni',            'Ballistic'),
    ('agni-p',          'Ballistic'),
    ('agni-prime',      'Ballistic'),
    ('prithvi',         'Ballistic'),
    ('pralay',          'Ballistic'),
    ('k4',              'Sub-Launched Ballistic'),
    ('k 15',            'Sub-Launched Ballistic'),
    ('k-15',            'Sub-Launched Ballistic'),
    ('k-4',             'Sub-Launched Ballistic'),
    ('ad1',             'Ballistic Missile Defence'),
    ('ad2',             'Ballistic Missile Defence'),
    ('ad-1',            'Ballistic Missile Defence'),
    ('ad-2',            'Ballistic Missile Defence'),
    ('sea-based',       'Ballistic Missile Defence'),
    ('mpatgm',          'Anti-Tank'),
    ('nag',             'Anti-Tank'),
    ('samho',           'Anti-Tank'),
    ('amogha',          'Anti-Tank'),
    ('helina',          'Anti-Tank'),
    ('dhruvastra',      'Anti-Tank'),
    ('milan',           'Anti-Tank'),
    ('pinaka',          'Rocket/Artillery'),
    ('erasr',           'Rocket/Artillery'),
    ('122mm',           'Rocket/Artillery'),
    ('mr-mocr',         'Rocket/Artillery'),
    ('saaw',            'Glide Bomb'),
    ('sant',            'Glide Bomb'),
    ('gaurav',          'Glide Bomb'),
    ('smart',           'Torpedo'),
    ('ehwt',            'Torpedo'),
    ('alwt',            'Torpedo'),
    ('hstdv',           'Hypersonic'),
    ('hypersonic',      'Hypersonic'),
    ('et-ldhcm',        'Hypersonic'),
    ('et ldhcm',        'Hypersonic'),
]

VALID_RESULTS    = {'Success', 'Failure', 'Partial Success', 'Unknown'}
VALID_CONFIDENCE = {'High', 'Medium', 'Low', ''}
VALID_EVENT_TYPES = {
    'Development Test', 'User Trial', 'Training Launch', 'Technology Demonstration',
    'Induction Trial', 'Flight Trial', 'Release Flight Trial',
    'Internal Evaluation Trial', 'Instrumented Development Flight Trial',
    'User Evaluation Trial', 'Operational Launch', 'Captive Flight Trial',
}
VALID_SERVICES = {
    'DRDO', 'IAF', 'Indian Army', 'Indian Navy', 'SFC', 'BDL',
    'DRDO/Indian Army', 'DRDO/Indian Navy', ''
}

def get_ui_category(family):
    f = family.lower()
    for key, val in UI_CATMAP:
        if key in f:
            return val
    return 'Uncategorized'

issues = []
seen_dates = defaultdict(list)   # family+date -> list of event_ids (for duplicate detection)

with open('normalized_missiles.csv', 'r', encoding='utf-8') as fp:
    reader = csv.DictReader(fp)
    fieldnames = reader.fieldnames
    rows = list(reader)

for i, r in enumerate(rows, 2):
    eid   = r['event_id']
    fam   = r['family'].strip()
    var   = r['variant'].strip()
    date  = r['date'].strip()
    plat  = r['platform'].strip()
    svc   = r['service'].strip()
    etype = r['event_type'].strip()
    result= r['result'].strip()
    conf  = r['confidence'].strip()
    notes = r['notes'].strip()
    src   = r['source_type'].strip()
    url   = r['source_url'].strip()

    cat = get_ui_category(fam)

    # --- 1. Uncategorized ---
    if cat == 'Uncategorized':
        issues.append(f"[UNCAT  ] ROW {i:3d} | {fam:20s} | {var}")

    # --- 2. Suspicious family/platform combos ---
    air_platforms = {'Su-30MKI', 'Tejas LCA', 'Sea King', 'Hawk-i', 'ALH MK-IV'}
    ground_cats   = {'Ballistic', 'Land-Attack Cruise', 'Rocket/Artillery', 'Anti-Tank'}
    sub_cats      = {'Sub-Launched Ballistic', 'Sub-Launched Cruise'}

    if cat in ground_cats and plat in air_platforms:
        issues.append(f"[PLAT?  ] ROW {i:3d} | {fam:20s} on air platform '{plat}'")
    if cat in sub_cats and plat not in {'Underwater Platform', 'INS Arihant', 'INS Arighaat', ''}:
        issues.append(f"[PLAT?  ] ROW {i:3d} | {fam:20s} sub-launched but platform='{plat}'")
    if cat == 'Air-Launched Cruise' and plat == 'Ground Launcher':
        issues.append(f"[PLAT?  ] ROW {i:3d} | {fam:20s} Air-Launched Cruise but platform=Ground Launcher")

    # --- 3. Bad / ambiguous dates ---
    if not re.match(r'^\d{4}-\d{2}(-\d{2})?$', date):
        issues.append(f"[DATE!  ] ROW {i:3d} | {fam:20s} | date='{date}'")

    # --- 4. Duplicate family+date ---
    key = (fam, date[:7])  # year-month
    seen_dates[key].append((eid, i))

    # --- 5. Missing/bad result ---
    if result not in VALID_RESULTS:
        issues.append(f"[RESULT?] ROW {i:3d} | {fam:20s} | result='{result}'")

    # --- 6. Unknown event_type ---
    if etype not in VALID_EVENT_TYPES:
        issues.append(f"[ETYPE? ] ROW {i:3d} | {fam:20s} | event_type='{etype}'")

    # --- 7. Unknown service ---
    if svc not in VALID_SERVICES:
        issues.append(f"[SVC?   ] ROW {i:3d} | {fam:20s} | service='{svc}'")

    # --- 8. High confidence but no source URL ---
    if conf == 'High' and not url and src not in ('Official', 'Media', 'OSINT'):
        issues.append(f"[SRC?   ] ROW {i:3d} | {fam:20s} | High confidence but no URL, src='{src}'")

    # --- 9. BrahMos ALCM on Ground Launcher ---
    if fam == 'BrahMos' and var == 'ALCM' and plat == 'Ground Launcher':
        issues.append(f"[PLAT?  ] ROW {i:3d} | BrahMos ALCM should NOT be on Ground Launcher")

    # --- 10. Agni-P in database both as 'Agni-P' and 'Agni' variant 'P' ---
    if fam == 'Agni' and var in ('P', 'Prime'):
        issues.append(f"[MERGE? ] ROW {i:3d} | Agni variant='{var}' — should this be family 'Agni-P'?")

# --- Duplicates ---
print("=" * 75)
print("DUPLICATE FAMILY+MONTH (potential double-counting):")
print("=" * 75)
for (fam, ym), entries in sorted(seen_dates.items()):
    if len(entries) > 1:
        ids = ', '.join(f"{e[0]}(r{e[1]})" for e in entries)
        print(f"  {fam:20s} {ym}  ->  {ids}")

print()
print("=" * 75)
print(f"OTHER ISSUES ({len(issues)} total):")
print("=" * 75)
for issue in issues:
    print(issue)
