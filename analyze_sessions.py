"""
Markdown tables for the login-session test (results_extra/session_timing.jsonl), in the same style
as `bench.py analyze`.

  python analyze_sessions.py

Tables:
  - per product: success, latency and credits, by run and by window (before/after 10 minutes)
  - logins: did each product's login work
  - identity changes: when the IP or User-Agent seen by the echo service changed
  - account usage per run, from the usage snapshots (run 2 onward)

Latency is the median/average of successful checks only, as in bench.py. Credits are the
X-Request-Credits values for the page checks (echo checks excluded). Residential Proxies
aren't billed per request, so their table shows traffic instead of credits.
"""
import json
import statistics
from collections import defaultdict
from pathlib import Path

LOG = Path("results_extra/session_timing.jsonl")
WINDOW_MIN = 10
PRODUCTS = ("fetch", "extract", "proxy")


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def first(v):
    return v[0] if isinstance(v, list) and v else v


def fmt(v, digits=2):
    return "-" if v is None else f"{v:,.{digits}f}"


def pct(ok, n):
    return f"{ok}/{n} ({100 * ok / n:.0f}%)" if n else "0/0"


if not LOG.exists():
    raise SystemExit(f"No {LOG} found. Run session_timing.py first.")
records = [json.loads(line) for line in LOG.read_text(encoding="utf-8").splitlines() if line.strip()]

by_run = defaultdict(list)
for r in records:
    by_run[r.get("run_id", "unknown")].append(r)
# Only runs that got as far as checking the members' page count as runs.
runs = [rid for rid in sorted(by_run) if any(r.get("step") == "secure" for r in by_run[rid])]
label = {rid: f"run {i}" for i, rid in enumerate(runs, 1)}
print(f"Runs: " + ", ".join(f"{label[rid]} ({rid})" for rid in runs))


def window_rows(rows):
    before = [r for r in rows if not r.get("after_10_min")]
    after = [r for r in rows if r.get("after_10_min")]
    return (("before 10 min", before), ("after 10 min", after), ("all", rows))


# ---------------------------------------------------------------- per-product tables
for product in PRODUCTS:
    checks = {rid: [r for r in by_run[rid] if r.get("step") == "secure" and r.get("product") == product]
              for rid in runs}
    if not any(checks.values()):
        continue
    if product == "proxy":
        print(f"\n## {product} (Residential Proxies, billed per GB; body sizes approximate)\n")
        print("| Run | Window | Success | Avg latency (s) | Median latency (s) | Avg KB/request | MB per 1K successful |")
        print("|---|---|---|---|---|---|---|")
    else:
        print(f"\n## {product}\n")
        print("| Run | Window | Success | Avg latency (s) | Median latency (s) | Avg credits/request | Credits per 1K successful |")
        print("|---|---|---|---|---|---|---|")

    groups = [(label[rid], checks[rid]) for rid in runs if checks[rid]]
    groups.append(("all runs", [r for rid in runs for r in checks[rid]]))
    for run_name, rows in groups:
        for window, rs in window_rows(rows):
            if not rs:
                continue
            ok = [r for r in rs if r.get("passed")]
            lats = [num(r.get("latency_s")) for r in ok if num(r.get("latency_s")) is not None]
            avg_lat = statistics.mean(lats) if lats else None
            med_lat = statistics.median(lats) if lats else None
            if product == "proxy":
                sizes = [num(r.get("bytes")) or 0 for r in rs]
                avg_kb = statistics.mean(sizes) / 1024 if sizes else None
                per_1k = sum(sizes) / len(ok) * 1000 / 1024 ** 2 if ok else None
                tail = f"{fmt(avg_kb, 1)} | {fmt(per_1k, 1)}"
            else:
                creds = [num(r.get("credits")) for r in rs if num(r.get("credits")) is not None]
                avg_c = statistics.mean(creds) if creds else None
                per_1k = sum(creds) / len(ok) * 1000 if ok and creds else None
                tail = f"{fmt(avg_c, 1)} | {fmt(per_1k, 0)}"
            print(f"| {run_name} | {window} | {pct(len(ok), len(rs))} | {fmt(avg_lat)} | {fmt(med_lat)} | {tail} |")

# ---------------------------------------------------------------- logins
print("\n## Logins\n")
print("| Run | Product | Logged in | Got session cookie | Status | Latency (s) | Credits |")
print("|---|---|---|---|---|---|---|")
for rid in runs:
    for r in by_run[rid]:
        if r.get("step") == "login":
            print(f"| {label[rid]} | {r.get('product')} | {r.get('landed_logged_in')} | {r.get('got_cookie')} | "
                  f"{r.get('status')} | {fmt(num(r.get('latency_s')))} | {r.get('credits') or '-'} |")

# ---------------------------------------------------------------- errors
errors = defaultdict(int)
for rid in runs:
    for r in by_run[rid]:
        if r.get("step") == "secure" and not r.get("passed"):
            reason = r.get("error") or (f"HTTP {r.get('status')}" if r.get("status") not in (200, "", None)
                                        else "logged-out page")
            errors[(label[rid], r.get("product"), reason[:90])] += 1
if errors:
    print("\n## Failed checks by reason\n")
    print("| Run | Product | Reason | Count |")
    print("|---|---|---|---|")
    for (run_name, product, reason), n in sorted(errors.items()):
        print(f"| {run_name} | {product} | {reason} | {n} |")

# ---------------------------------------------------------------- identity changes
print("\n## Identity changes (from the echo checks)\n")
print("| Run | Product | Round | Minutes since login | IP | User-Agent | Page check |")
print("|---|---|---|---|---|---|---|")
for rid in runs:
    for product in PRODUCTS:
        prev_ip = prev_ua = None
        for r in by_run[rid]:
            if r.get("step") != "secure" or r.get("product") != product:
                continue
            ip = str(first(r.get("ip")) or "").split(":")[0] or None
            ua = first(r.get("user_agent"))
            if not ip and not ua:
                continue                     # echo failed this round
            short_ua = (ua or "").replace("Mozilla/5.0 ", "").replace(" AppleWebKit/537.36 (KHTML, like Gecko)", "")
            if product == "proxy":
                changed = ua != prev_ua      # proxy IPs rotate every request by design; track only the identity
            else:
                changed = ip != prev_ip or ua != prev_ua
            if changed:
                print(f"| {label[rid]} | {product} | {r.get('round')} | {r.get('minutes_since_login')} | "
                      f"{ip if product != 'proxy' else '(rotates)'} | {short_ua} | "
                      f"{'pass' if r.get('passed') else 'FAIL'} |")
            prev_ip, prev_ua = ip, ua

# ---------------------------------------------------------------- account usage
print("\n## Account usage per run (from usage snapshots)\n")
print("| Run | WebScraping API credits | Residential Proxies credits | Scraping Browser credits | Total |")
print("|---|---|---|---|---|")
any_usage = False
for rid in runs:
    snaps = {r.get("label"): r.get("usage") or {} for r in by_run[rid] if r.get("step") == "usage_snapshot"}
    if "start" not in snaps or "end" not in snaps:
        print(f"| {label[rid]} | not recorded | not recorded | not recorded | - |")
        continue
    any_usage = True

    def credits_of(s, product):
        return (((s.get("plan") or {}).get("products") or {}).get(product) or {}).get("usage_credits") or 0

    d = {p: credits_of(snaps["end"], p) - credits_of(snaps["start"], p)
         for p in ("api", "proxy_residential", "scraping_browser")}
    print(f"| {label[rid]} | {d['api']} | {d['proxy_residential']} | {d['scraping_browser']} | {sum(d.values())} |")
if any_usage:
    print("\nAccount figures include the echo checks and may lag by a minute or two; the end snapshot is taken "
          "seconds after the last request.")