"""
Merge benchmark rows from another results folder into ./results (the pilot data).

Copies every 'main' row from <source>/results.csv into results/results.csv (rows already
present are skipped), copies evidence files that don't exist yet, and appends the source's
usage snapshots. Probe rows stay in the source folder as evidence of that probe.

  python merge_results.py results_extra
"""
import csv
import shutil
import sys
from pathlib import Path

if len(sys.argv) != 2:
    raise SystemExit("Usage: python merge_results.py <source_folder>")
src, dst = Path(sys.argv[1]), Path("results")
if not (src / "results.csv").exists() or not (dst / "results.csv").exists():
    raise SystemExit(f"Need both {src / 'results.csv'} and {dst / 'results.csv'}.")

shutil.copy(dst / "results.csv", dst / "results.csv.premerge.bak")
with open(dst / "results.csv", newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    fields, existing = reader.fieldnames, list(reader)
seen = {(r.get("ts_utc"), r.get("product"), r.get("target"), r.get("url")) for r in existing}

with open(src / "results.csv", newline="", encoding="utf-8") as f:
    incoming = [r for r in csv.DictReader(f) if r.get("phase") == "main"]
new = [r for r in incoming if (r.get("ts_utc"), r.get("product"), r.get("target"), r.get("url")) not in seen]

with open(dst / "results.csv", "a", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
    for r in new:
        w.writerow({k: r.get(k, "") for k in fields})

copied = 0
for sub in ("samples", "screenshots"):
    if (src / sub).exists():
        (dst / sub).mkdir(exist_ok=True)
        for p in (src / sub).iterdir():
            if p.is_file() and not (dst / sub / p.name).exists():
                shutil.copy(p, dst / sub / p.name)
                copied += 1

if (src / "usage_snapshots.jsonl").exists():
    with open(dst / "usage_snapshots.jsonl", "a", encoding="utf-8") as out:
        out.write((src / "usage_snapshots.jsonl").read_text(encoding="utf-8"))

by_cell = {}
for r in new:
    key = f"{r['product']}/{r['target']}/{r['config']}"
    by_cell[key] = by_cell.get(key, 0) + 1
print(f"Merged {len(new)} rows ({len(incoming) - len(new)} already present): {by_cell}")
print(f"Copied {copied} evidence files. Backup: {dst / 'results.csv.premerge.bak'}")