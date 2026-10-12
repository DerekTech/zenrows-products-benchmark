"""
One-off correction: Extract rows logged before the harness read X-Request-Credits used the
rate table (1/5/10/25). Measured runs show Extract is billed 1 credit above Fetch at every tier,
so add 1 to those rows and mark them as corrected. Rows Zenrows didn't bill (0 credits) are untouched.

  python fix_extract_credits.py
"""
import csv
import shutil
from pathlib import Path

for path in [Path("results/results.csv"), Path("results/excluded_rows.csv")]:
    if not path.exists():
        continue
    shutil.copy(path, path.with_suffix(".csv.bak"))      # keep the original
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields, rows = reader.fieldnames, list(reader)
    fixed = 0
    for r in rows:
        if r["product"] == "extract" and r["credits_source"] == "tier":
            try:
                credits = float(r["credits"] or 0)
            except ValueError:
                continue
            if credits > 0:
                r["credits"] = credits + 1
                r["credits_source"] = "tier+1 (corrected: measured Extract surcharge)"
                fixed += 1
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"{path}: corrected {fixed} Extract rows (backup: {path.with_suffix('.csv.bak')})")