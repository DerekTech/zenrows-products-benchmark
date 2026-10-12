"""
Undo a double correction. fix_extract_credits.py added the 1-credit Extract surcharge to every
Extract row marked "tier", but rows logged after the harness started reading X-Request-Credits
were also (wrongly) marked "tier" while already holding Zenrows' real figure (rate card + 1).
Those rows ended up at rate card + 2. This sets them back to rate card + 1 and labels them.

  python fix_double_correction.py
"""
import csv
import shutil
from pathlib import Path

TIER = {"base": 1, "js": 5, "premium": 10, "js_premium": 25}
CORRECTED = "tier+1 (corrected: measured Extract surcharge)"

for path in [Path("results/results.csv"), Path("results/excluded_rows.csv")]:
    if not path.exists():
        continue
    shutil.copy(path, path.with_suffix(".csv.bak2"))
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields, rows = reader.fieldnames, list(reader)
    fixed = 0
    for r in rows:
        if r["product"] != "extract" or r["credits_source"] != CORRECTED:
            continue
        tier = TIER.get(r["config"])
        try:
            credits = float(r["credits"] or 0)
        except ValueError:
            continue
        if tier is not None and credits == tier + 2:
            r["credits"] = tier + 1
            r["credits_source"] = "header (X-Request-Credits; double correction undone)"
            fixed += 1
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"{path}: undid {fixed} double-corrected rows (backup: {path.with_suffix('.csv.bak2')})")