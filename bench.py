#!/usr/bin/env python3
"""
Zenrows benchmark harness: Fetch, Extract, Batch, Browser Sessions and the standalone
Residential Proxies product, against the targets in targets.yaml.

Run in this order:
  python bench.py probe              # escalation ladder: cheapest Fetch config per target
  python bench.py run --rounds 5     # interleaved rounds of fetch, extract, browser
  python bench.py extend --to 20     # more attempts for mixed or always_full cells
  python bench.py batch              # one Batch job per target
  python bench.py analyze            # markdown tables for the article

Keep terminal evidence with tee:
  python bench.py run --rounds 5 | tee -a results/terminal.log

Setup:
  pip install requests pyyaml playwright python-dotenv
  put ZENROWS_API_KEY=your_key in a .env file next to bench.py (or set it in your shell)
  (session target only) export SITE_USER=... SITE_PASS=...
  (Residential Proxies) put ZR_PROXY_URL=http://user:pass@host:port in .env (copy it from the
  Residential Proxies page of the dashboard); targets can name another variable via proxy_env
"""
import argparse
import csv
import json
import os
import random
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

try:  # optional: load ZENROWS_API_KEY / SITE_USER / SITE_PASS from a .env file next to bench.py
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"), override=True)  # .env beats stale shell variables
except ImportError:
    pass

FETCH_URL = "https://api.zenrows.com/v1/"
BATCH_URL = "https://async.api.zenrows.com/v1"
USAGE_URL = "https://api.zenrows.com/v1/subscriptions/self/details"
BROWSER_WSS = "wss://browser.zenrows.com?apikey={key}"

OUT = Path("results")
RESULTS_CSV = OUT / "results.csv"
BATCH_CSV = OUT / "batch_jobs.csv"
CONFIGS_JSON = OUT / "configs.json"
USAGE_LOG = OUT / "usage_snapshots.jsonl"

# Fetch escalation ladder, cheapest first (1, 5, 10, 25 credits).
LADDER = [
    ("base", {}),
    ("js", {"js_render": "true"}),
    ("premium", {"premium_proxy": "true"}),
    ("js_premium", {"js_render": "true", "premium_proxy": "true"}),
]
LADDER_PARAMS = dict(LADDER)
TIER = {"base": 1, "js": 5, "premium": 10, "js_premium": 25}

FIELDS = ["run_id", "ts_utc", "location", "phase", "product", "target", "config",
          "url", "status", "passed", "latency_s", "credits", "credits_source",
          "cost_header_raw", "bytes", "minutes", "error", "evidence"]
BATCH_FIELDS = ["run_id", "ts_utc", "target", "config", "job_id", "urls", "successful",
                "failed", "duration_s", "spend_credits", "error"]

CFG = yaml.safe_load(Path("targets.yaml").read_text(encoding="utf-8"))
SET = CFG["settings"]
TARGETS = CFG["targets"]
RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S")
LOCAL_BROWSER = os.environ.get("LOCAL_BROWSER") == "1"
SAVED = set()                 # evidence already saved: (product, target, config, phase, passed)
EXTRACT_UNSUPPORTED = set()   # targets that returned AUTH010 for extract=auto


# Plain HTTP client headers used with the Residential Proxies product (no Zenrows fingerprinting).
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------------------------------------------------------------- helpers

def api_key():
    key = (os.environ.get("ZENROWS_API_KEY") or "").strip().strip('"').strip("'")
    if not key or key.lower().startswith("your_key"):
        sys.exit("ZENROWS_API_KEY is missing or still the placeholder. Check .env / setx.")
    return key


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_rows():
    if not RESULTS_CSV.exists():
        return []
    with open(RESULTS_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


EXCLUDED_CSV = OUT / "excluded_rows.csv"


def logged_credits():
    total = 0.0
    rows = load_rows()
    if EXCLUDED_CSV.exists():
        with open(EXCLUDED_CSV, newline="", encoding="utf-8") as f:
            rows += list(csv.DictReader(f))
    for r in rows:
        try:
            total += float(r["credits"] or 0)
        except ValueError:
            pass
    return total


SPENT = logged_credits()
# Remember domains that already returned AUTH010 so later runs go straight to Autoparse.
EXTRACT_UNSUPPORTED.update(r["target"] for r in load_rows()
                           if r["product"] == "extract"
                           and r["error"] in SET.get("extract_unsupported_codes", ["AUTH010"]))


def append_csv(path, fields, row):
    OUT.mkdir(exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow(row)


def log_row(row):
    global SPENT
    row = {**{k: "" for k in FIELDS}, "run_id": RUN_ID, "ts_utc": now(),
           "location": SET.get("location", ""), **row}
    append_csv(RESULTS_CSV, FIELDS, row)
    try:
        SPENT += float(row["credits"] or 0)
    except ValueError:
        pass
    lat = f"{float(row['latency_s']):7.2f}s" if row["latency_s"] != "" else "      -"
    print(f"{row['ts_utc']}  {row['phase']:<6} {row['product']:<9} {row['target']:<8} "
          f"{row['config']:<11} status={str(row['status']):<4} {lat}  "
          f"credits={str(row['credits'] or '-'):>6}  {'PASS' if row['passed'] else 'FAIL'}  {row['error']}")


def check_budget(extra=0):
    if SPENT + extra >= SET["budget_credits"]:
        snapshot_usage("budget_stop")
        sys.exit(f"Budget guard: {SPENT:.0f} logged (+{extra} pending) >= {SET['budget_credits']}. Stopping.")


def snapshot_usage(label):
    """Raw account usage before/after each block. Ground truth for products without a cost header."""
    try:
        r = requests.get(USAGE_URL, headers={"X-API-Key": api_key()}, timeout=30)
        data = r.json()
    except Exception as e:  # noqa: BLE001
        data = {"error": repr(e)}
    OUT.mkdir(exist_ok=True)
    with open(USAGE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts_utc": now(), "run_id": RUN_ID, "label": label, "data": data}) + "\n")


class RequirementsUnsatisfied(Exception):
    """REQS002: Zenrows requires js_render and/or premium_proxy for this domain."""
    def __init__(self, needs_js, needs_premium, title):
        super().__init__(title)
        self.needs_js, self.needs_premium, self.title = needs_js, needs_premium, title


class DomainForbidden(Exception):
    """Zenrows refuses this domain (REQS001). Stop sending to it: these errors count toward IP bans."""


FATAL_CODES = {"AUTH001", "AUTH002", "AUTH003", "AUTH004", "AUTH005", "AUTH006"}
CONN_ERRORS = 0


def preflight():
    """Stop before spending anything if the key is rejected or Zenrows is unreachable."""
    try:
        r = requests.get(USAGE_URL, headers={"X-API-Key": api_key()}, timeout=30)
    except requests.RequestException as e:
        sys.exit(f"Preflight: can't reach api.zenrows.com ({type(e).__name__}). "
                 "If you just had many failed requests, wait 10 minutes and retry.")
    if r.status_code != 200:
        sys.exit(f"Preflight: usage endpoint returned {r.status_code}: {r.text[:200]}\n"
                 "Your API key is probably wrong. Nothing was run.")
    print("Preflight OK: API key accepted.")


def stop_if_fatal(r, err):
    """Abort the whole run on auth/account errors or repeated connection failures."""
    global CONN_ERRORS
    if err:
        CONN_ERRORS += 1
        if CONN_ERRORS >= 3:
            snapshot_usage("aborted_connection")
            sys.exit("Aborted: 3 connection errors in a row. Possible temporary ban or network issue. "
                     "Wait 10 minutes, then run again.")
        return
    CONN_ERRORS = 0
    if r is not None and r.status_code == 400 and short_err(r) == "REQS001":
        raise DomainForbidden(r.text[:300])
    if r is not None and r.status_code == 400 and short_err(r) == "REQS002":
        try:
            title = r.json().get("title", "")
        except ValueError:
            title = ""
        low = title.lower()
        needs_js, needs_premium = "javascript" in low, "premium" in low
        if not (needs_js or needs_premium):
            needs_js = needs_premium = True
        raise RequirementsUnsatisfied(needs_js, needs_premium, title)
    if r is not None and r.status_code in (401, 402, 403):
        code = short_err(r)
        if code in FATAL_CODES or r.status_code == 401:
            sys.exit(f"Aborted: Zenrows returned {r.status_code} {code}. Fix the account/key issue first.")


def save_evidence(key, name, body, headers=None):
    """Keep one passing and one failing sample per cell."""
    if key in SAVED:
        return ""
    SAVED.add(key)
    d = OUT / "samples"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(body if isinstance(body, str) else json.dumps(body, indent=2), encoding="utf-8")
    if headers is not None:
        (d / f"{name}.headers.json").write_text(json.dumps(headers, indent=2), encoding="utf-8")
    return str(p)


def charged_credits(r, cname):
    """Credits per the documented tier when Zenrows reports a non-zero cost; raw header kept separately.

    The cost header is not in credits (it reads 0.001 for a 1-credit request), so it is logged
    as cost_header_raw and credits come from the rate card: 1 / 5 / 10 / 25.
    """
    if r is None:
        return "", "", ""
    raw = cost_from_headers(r.headers)
    reported = r.headers.get("X-Request-Credits")
    if reported not in (None, ""):
        try:
            return float(reported), raw, "header"   # Zenrows' own figure wins over the rate card
        except ValueError:
            pass
    try:
        charged = float(raw) > 0
    except (TypeError, ValueError):
        charged = r.status_code in (200, 404, 410)   # docs: these statuses are billed
    return (TIER[cname] if charged else 0), raw, "tier"


def cost_from_headers(h):
    # Matches any header with "cost" in its name, so a renamed header still gets logged.
    for k, v in h.items():
        if "cost" in k.lower():
            return v
    return ""


def short_err(r):
    if r is None:
        return ""
    if r.status_code != 200:
        try:
            return r.json().get("code") or f"http_{r.status_code}"
        except ValueError:
            return f"http_{r.status_code}"
    return "marker_missing"


def marker_ok(t, text):
    """Pass only if the marker appears often enough and no block-page signature is present."""
    if not text:
        return False
    if any(b in text for b in t.get("block_markers", [])):
        return False
    return text.count(t["marker"]) >= t.get("marker_min_count", 1)


def record_environment(extra=None):
    """Write client versions (and the remote browser version once known) to results/environment.json."""
    import platform
    from importlib.metadata import PackageNotFoundError, version
    path = OUT / "environment.json"
    env = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    env.update({"updated_utc": now(), "python": platform.python_version(),
                "os": f"{platform.system()} {platform.release()}", "location": SET.get("location", "")})
    for pkg in ("requests", "pyyaml", "playwright"):
        try:
            env[pkg] = version(pkg)
        except PackageNotFoundError:
            env[pkg] = "not installed"
    env.update(extra or {})
    OUT.mkdir(exist_ok=True)
    path.write_text(json.dumps(env, indent=2), encoding="utf-8")


def has_keys(obj, keys):
    found = set()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                found.add(str(k).lower())
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return all(k.lower() in found for k in keys)


def skip(t):
    text = json.dumps(t)
    return "REPLACE" in text or "PASTE_" in text


def products_for(t):
    return t.get("products", ["fetch", "extract", "batch", "browser"])


def load_configs(required=True):
    if CONFIGS_JSON.exists():
        return json.loads(CONFIGS_JSON.read_text(encoding="utf-8"))
    if required:
        sys.exit("No results/configs.json yet. Run: python bench.py probe")
    return {}


def attempt_counts():
    counts = defaultdict(int)
    for r in load_rows():
        if r["phase"] == "main":
            counts[(r["product"], r["target"])] += 1
    return counts


def batch_urls(t):
    if t.get("batch_urls_file"):
        return [u.strip() for u in Path(t["batch_urls_file"]).read_text().splitlines() if u.strip()]
    rng = t.get("batch_urls_range")
    if rng:
        return [rng["pattern"].format(n=n) for n in range(rng["start"], rng["end"] + 1)]
    if t.get("batch_repeat"):           # single-page targets: same URL N times
        return [t["urls"][i % len(t["urls"])] for i in range(t["batch_repeat"])]
    return t["urls"]


# ---------------------------------------------------------------- Fetch / Extract

def redact(text):
    key = (os.environ.get("ZENROWS_API_KEY") or "").strip()
    return text.replace(key, "***") if key else text


def zenrows_get(t, url, cparams, mode=None):
    params = {"apikey": api_key(), "url": url, **cparams}
    if cparams.get("js_render") == "true":
        params.update(t.get("js_params", {}))
    if cparams.get("premium_proxy") == "true":
        params.update(t.get("premium_params", {}))   # e.g. proxy_country
    headers = dict(t.get("headers", {}))       # e.g. a fixed User-Agent for session-bound sites
    if headers:
        params["custom_headers"] = "true"
    if t.get("cookies"):
        params["custom_headers"] = "true"
        params["session_id"] = t.get("session_id", 4242)
        headers["Cookie"] = t["cookies"]
    if mode == "extract":
        params["extract"] = "auto"
    elif mode == "autoparse":
        params["autoparse"] = "true"
    start = time.perf_counter()
    try:
        r = requests.get(FETCH_URL, params=params, headers=headers, timeout=SET["timeout_s"])
        return r, round(time.perf_counter() - start, 2), ""
    except requests.RequestException as e:
        return None, round(time.perf_counter() - start, 2), redact(f"{type(e).__name__}: {e}")[:160]


def attempt_fetch(tkey, phase, cname, url):
    t = TARGETS[tkey]
    r, lat, err = zenrows_get(t, url, LADDER_PARAMS[cname])
    if r is not None and r.status_code == 400 and short_err(r) in ("REQS001", "REQS002"):
        code = short_err(r)
        save_evidence(("fetch", tkey, code), f"fetch_{tkey}_{cname}_{code}.json", r.text, dict(r.headers))
        log_row({"phase": phase, "product": "fetch", "target": tkey, "config": cname, "url": url,
                 "status": 400, "passed": False, "latency_s": lat, "credits": 0,
                 "credits_source": "tier", "error": code})
    stop_if_fatal(r, err)
    status = r.status_code if r is not None else ""
    body = r.text if r is not None else ""
    passed = status == 200 and marker_ok(t, body)
    ev = save_evidence(("fetch", tkey, cname, phase, passed),
                       f"fetch_{tkey}_{cname}_{phase}_{'pass' if passed else 'fail'}.html",
                       body, dict(r.headers) if r is not None else {})
    credits, raw, source = charged_credits(r, cname)
    log_row({"phase": phase, "product": "fetch", "target": tkey, "config": cname, "url": url,
             "status": status, "passed": passed, "latency_s": lat,
             "credits": credits, "credits_source": source, "cost_header_raw": raw,
             "bytes": len(body.encode()),
             "error": err or ("" if passed else short_err(r)), "evidence": ev})
    return passed


def attempt_extract(tkey, phase, cname, url):
    """extract=auto; on AUTH010 (domain not enabled) fall back to Autoparse."""
    t = TARGETS[tkey]
    expect = t.get("extract_expect", [])
    modes = ["autoparse"] if tkey in EXTRACT_UNSUPPORTED else ["extract"]
    for mode in modes:
        r, lat, err = zenrows_get(t, url, LADDER_PARAMS[cname], mode=mode)
        stop_if_fatal(r, err)
        status = r.status_code if r is not None else ""
        body = r.text if r is not None else ""
        passed = False
        if r is not None and status == 200:
            try:
                data = r.json()
                if mode == "extract":
                    parsed = data.get("parsed")
                    passed = bool(parsed) and marker_ok(t, data.get("html", "")) and has_keys(parsed, expect)
                else:
                    passed = bool(data) and has_keys(data, expect)
                if not passed:
                    err = "parsed_empty_or_fields_missing"
            except ValueError:
                err = "not_json"
        elif r is not None:
            err = short_err(r)
        ev = save_evidence((mode, tkey, cname, phase, passed),
                           f"{mode}_{tkey}_{cname}_{phase}_{'pass' if passed else 'fail'}.json",
                           body, dict(r.headers) if r is not None else {})
        credits, raw, source = charged_credits(r, cname)
        log_row({"phase": phase, "product": mode, "target": tkey, "config": cname, "url": url,
                 "status": status, "passed": passed, "latency_s": lat,
                 "credits": credits, "credits_source": source, "cost_header_raw": raw,
                 "bytes": len(body.encode()), "error": err, "evidence": ev})
        unsupported_codes = SET.get("extract_unsupported_codes", ["AUTH010"])
        if mode == "extract" and err in unsupported_codes and SET.get("autoparse_fallback", True):
            EXTRACT_UNSUPPORTED.add(tkey)
            modes.append("autoparse")
    return passed


# ---------------------------------------------------------------- Browser Sessions

def proxy_url(t):
    return (os.environ.get(t.get("proxy_env", "ZR_PROXY_URL")) or "").strip()


def attempt_proxy(tkey, phase, url):
    """Standalone Residential Proxies: your own HTTP client, Zenrows only supplies the IP.
    No JS rendering and no fingerprinting, so this shows what the proxy alone gets through.
    Billed per GB, not in API credits: 'bytes' is the decompressed body size (an approximation;
    the dashboard's GB figure is ground truth)."""
    t = TARGETS[tkey]
    proxy = proxy_url(t)
    headers = {**DEFAULT_HEADERS, **t.get("headers", {})}
    if t.get("cookies"):
        headers["Cookie"] = t["cookies"]
    start = time.perf_counter()
    r, err = None, ""
    try:
        r = requests.get(url, headers=headers, proxies={"http": proxy, "https": proxy},
                         timeout=SET["timeout_s"])
    except Exception as e:  # noqa: BLE001  (bad proxy credentials raise non-requests errors too)
        err = (f"{type(e).__name__}: {e}").replace(proxy, "***")[:160]
    lat = round(time.perf_counter() - start, 2)
    status = r.status_code if r is not None else ""
    body = r.text if r is not None else ""
    passed = status == 200 and marker_ok(t, body)
    ev = save_evidence(("proxy", tkey, phase, passed),
                       f"proxy_{tkey}_{phase}_{'pass' if passed else 'fail'}.html",
                       body, dict(r.headers) if r is not None else {})
    log_row({"phase": phase, "product": "proxy", "target": tkey, "config": "residential", "url": url,
             "status": status, "passed": passed, "latency_s": lat, "credits": "",
             "credits_source": "per-GB (see bytes)", "bytes": len(r.content) if r is not None else 0,
             "error": err or ("" if passed else ("blocked_or_marker_missing" if r is not None else "")),
             "evidence": ev})
    return passed


def _req_bytes(req):
    try:
        s = req.sizes()
        return sum(max(0, s[k]) for k in ("requestHeadersSize", "requestBodySize",
                                           "responseHeadersSize", "responseBodySize"))
    except Exception:  # noqa: BLE001
        return 0


def attempt_browser(tkey, phase, url):
    from playwright.sync_api import sync_playwright

    t = TARGETS[tkey]
    sizes, page, status, err, passed = [], None, "", "", False
    shots = OUT / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    shot = shots / f"browser_{tkey}_{phase}_{datetime.now():%H%M%S}.png"
    start = time.perf_counter()
    with sync_playwright() as p:
        try:
            if LOCAL_BROWSER:   # control run: same script, your own local Chromium, no Zenrows
                browser = p.chromium.launch(headless=True)
            else:
                browser = p.chromium.connect_over_cdp(BROWSER_WSS.format(key=api_key()), timeout=60_000)
                record_environment({"zenrows_remote_browser": browser.version})
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            page.on("requestfinished", lambda req: sizes.append(_req_bytes(req)))
            login = t.get("login")
            if login:
                page.goto(login["url"], timeout=90_000)
                page.fill(login["user_selector"], os.environ[login["user_env"]])
                page.fill(login["pass_selector"], os.environ[login["pass_env"]])
                page.click(login["submit_selector"])
                page.wait_for_load_state("networkidle", timeout=60_000)
            resp = page.goto(url, timeout=90_000)
            status = resp.status if resp else ""
            if t.get("browser_ready"):        # e.g. wait until the infinite-scroll plugin has initialised
                page.wait_for_selector(t["browser_ready"], state="attached",
                                       timeout=int(t.get("browser_ready_timeout_s", 30) * 1000))
            if t.get("browser_scrolls"):
                # Scroll until enough content has loaded (like a user), instead of a fixed number of blind scrolls.
                need = t.get("marker_min_count", 1)
                deadline = time.time() + t.get("browser_scroll_timeout_s", 45)
                while time.time() < deadline and page.content().count(t["marker"]) < need:
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(1500)
            if t.get("browser_wait_for"):
                page.wait_for_selector(t["browser_wait_for"], timeout=30_000)
            passed = marker_ok(t, page.content())
            if not passed:
                err = "marker_missing"
            page.screenshot(path=str(shot))
            browser.close()
        except Exception as e:  # noqa: BLE001
            err = redact(f"{type(e).__name__}: {e}").replace("\n", " ")[:160]
            if page is not None:
                try:
                    page.screenshot(path=str(shot))
                except Exception:  # noqa: BLE001
                    pass
    elapsed = time.perf_counter() - start
    total_bytes = sum(sizes)
    minutes = elapsed / 60
    # Estimate only: 25,000 credits/GB + 5 credits/minute. Usage snapshots are the ground truth.
    credits_est = round(total_bytes / 1e9 * 25_000 + minutes * 5, 1)
    log_row({"phase": phase, "product": "browser_local" if LOCAL_BROWSER else "browser",
             "target": tkey, "config": "browser", "url": url,
             "status": status, "passed": passed, "latency_s": round(elapsed, 2),
             "credits": 0 if LOCAL_BROWSER else credits_est,
             "credits_source": "none (local control)" if LOCAL_BROWSER else "estimate", "bytes": total_bytes,
             "minutes": round(minutes, 2), "error": err,
             "evidence": str(shot) if shot.exists() else ""})
    return passed


# ---------------------------------------------------------------- commands

def run_cell(product, tkey, phase, i, configs):
    t = TARGETS[tkey]
    url = t["urls"][i % len(t["urls"])]
    cname = configs[tkey]
    if product == "fetch":
        attempt_fetch(tkey, phase, cname, url)
    elif product in ("extract", "autoparse"):
        attempt_extract(tkey, phase, cname, url)
    elif product == "browser":
        attempt_browser(tkey, phase, url)
    elif product == "proxy":
        attempt_proxy(tkey, phase, url)
    time.sleep(SET["pause_s"])


def exclude_rows(tkey, phase):
    """Move a target's earlier rows for this phase to excluded_rows.csv (kept for the record and budget)."""
    rows = load_rows()
    keep = [r for r in rows if not (r["target"] == tkey and r["phase"] == phase)]
    gone = [r for r in rows if r["target"] == tkey and r["phase"] == phase]
    if not gone:
        return
    for r in gone:
        append_csv(EXCLUDED_CSV, FIELDS, {k: r.get(k, "") for k in FIELDS})
    with open(RESULTS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(keep)
    print(f"-- {tkey}: moved {len(gone)} earlier {phase} rows to {EXCLUDED_CSV}")


def cmd_reset_extract(args):
    """Once Zenrows has prepared a domain, set its 'not prepared' Extract rows aside so Extract is tried again."""
    codes = SET.get("extract_unsupported_codes", ["AUTH010"])
    rows = load_rows()
    for tkey in args.targets:
        gone = [r for r in rows if r["target"] == tkey and r["product"] == "extract" and r["error"] in codes]
        if not gone:
            print(f"-- {tkey}: no 'not prepared' Extract rows")
            continue
        for r in gone:
            append_csv(EXCLUDED_CSV, FIELDS, {k: r.get(k, "") for k in FIELDS})
        rows = [r for r in rows if r not in gone]
        print(f"-- {tkey}: set aside {len(gone)} 'not prepared' Extract rows; Extract will be tried again")
    with open(RESULTS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def cmd_exclude_url(args):
    rows = load_rows()
    gone = [r for r in rows if args.substring in (r.get("url") or "")]
    if not gone:
        sys.exit(f"No rows with URL containing {args.substring!r}.")
    for r in gone:
        append_csv(EXCLUDED_CSV, FIELDS, {k: r.get(k, "") for k in FIELDS})
    keep = [r for r in rows if r not in gone]
    with open(RESULTS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(keep)
    print(f"-- moved {len(gone)} rows with URL containing {args.substring!r} to {EXCLUDED_CSV}")


def cmd_exclude(args):
    rows = load_rows()
    gone = [r for r in rows if r["phase"] == "main" and r["target"] == args.target and r["product"] == args.product]
    if not gone:
        sys.exit(f"No main rows for {args.product}/{args.target}.")
    for r in gone:
        append_csv(EXCLUDED_CSV, FIELDS, {k: r.get(k, "") for k in FIELDS})
    keep = [r for r in rows if r not in gone]
    with open(RESULTS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(keep)
    print(f"-- moved {len(gone)} {args.product}/{args.target} rows to {EXCLUDED_CSV}")


def cmd_probe(args):
    configs = load_configs(required=False)
    snapshot_usage("probe_start")
    k = SET.get("probe_attempts", 3)
    for tkey, t in TARGETS.items():
        if skip(t):
            print(f"-- skip {tkey}: REPLACE values left in targets.yaml")
            continue
        if tkey in configs and tkey not in args.redo:
            print(f"-- {tkey}: already probed ({configs[tkey]}); use --redo {tkey} to probe again")
            continue
        if tkey in args.redo:
            exclude_rows(tkey, "probe")
        if t.get("force_config"):
            configs[tkey] = t["force_config"]
            print(f"==> {tkey}: forced config {t['force_config']}")
            CONFIGS_JSON.write_text(json.dumps(configs, indent=2), encoding="utf-8")
            continue
        chosen, forbidden = None, False
        names = [c for c, _ in LADDER]
        start_at = names.index(t.get("probe_start", "base"))
        try:
            idx = start_at
            while idx < len(names):
                cname = names[idx]
                passes = 0
                try:
                    for i in range(k):
                        check_budget(TIER[cname])
                        if attempt_fetch(tkey, "probe", cname, t["urls"][i % len(t["urls"])]):
                            passes += 1
                        time.sleep(SET["pause_s"])
                except RequirementsUnsatisfied as req:
                    params_needed = {"js_render": req.needs_js, "premium_proxy": req.needs_premium}
                    nxt = next(j for j, n in enumerate(names)
                               if all(LADDER_PARAMS[n].get(p) == "true" for p, need in params_needed.items() if need))
                    print(f"    REQS002: {req.title}\n    Zenrows requires a higher tier; jumping to '{names[max(nxt, idx + 1)]}'.")
                    time.sleep(10)   # 400s count toward Zenrows' temporary IP blocks
                    idx = max(nxt, idx + 1)
                    continue
                if passes * 2 > k:
                    chosen = cname
                    break
                idx += 1
        except DomainForbidden as e:
            forbidden = True
            print(f"==> {tkey}: Zenrows refuses this domain (REQS001). Skipping it.\n    {e}")
            time.sleep(10)
        if forbidden:
            configs[tkey] = "forbidden"
            CONFIGS_JSON.write_text(json.dumps(configs, indent=2), encoding="utf-8")
            continue
        configs[tkey] = chosen or "js_premium"
        note = "" if chosen else "  (nothing passed; full run uses the strongest config)"
        print(f"==> {tkey}: config = {configs[tkey]}{note}")
        OUT.mkdir(exist_ok=True)
        CONFIGS_JSON.write_text(json.dumps(configs, indent=2), encoding="utf-8")
    snapshot_usage("probe_end")


def browser_cap(tkey):
    return TARGETS[tkey].get("browser_max", SET.get("browser_max", 10))


def cmd_run(args):
    configs = load_configs()
    wanted = args.products.split(",")
    only = set(args.targets.split(",")) if args.targets else None
    cells = [(prod, tkey) for tkey, t in TARGETS.items()
             if not skip(t) and configs.get(tkey) not in (None, "forbidden")
             and (only is None or tkey in only)
             for prod in wanted if prod in products_for(t) and prod != "batch"]
    no_proxy = sorted({tkey for prod, tkey in cells if prod == "proxy" and not proxy_url(TARGETS[tkey])})
    if no_proxy:
        print(f"-- skipping proxy for {no_proxy}: no proxy URL in .env (see proxy_env / ZR_PROXY_URL)")
        cells = [(p_, t_) for p_, t_ in cells if not (p_ == "proxy" and t_ in no_proxy)]
    counts = attempt_counts()
    snapshot_usage("run_start")
    print(f"run {RUN_ID} | location: {SET.get('location')} | credits logged so far: {SPENT:.0f}")
    for rnd in range(args.rounds):
        random.shuffle(cells)
        print(f"--- round {rnd + 1}/{args.rounds}")
        for prod, tkey in cells:
            if prod == "browser" and counts[(prod, tkey)] >= browser_cap(tkey):
                continue                      # Browser Sessions stop at browser_max attempts
            check_budget(TIER.get(configs[tkey], 25))
            run_cell(prod, tkey, "main", counts[(prod, tkey)], configs)
            counts[(prod, tkey)] += 1
    snapshot_usage("run_end")


def cmd_extend(args):
    configs = load_configs()
    results = defaultdict(list)
    for r in load_rows():
        if r["phase"] == "main" and r["product"] != "batch":
            results[(r["product"], r["target"])].append(r["passed"] == "True")
    todo = {}
    for (prod, tkey), res in results.items():
        cap = min(args.to, browser_cap(tkey)) if prod == "browser" else args.to
        mixed = 0 < sum(res) < len(res)
        if configs.get(tkey) == "forbidden":
            continue
        unsupported = prod == "extract" and tkey in EXTRACT_UNSUPPORTED
        if (mixed or TARGETS[tkey].get("always_full")) and len(res) < cap and not unsupported:
            todo[(prod, tkey)] = cap - len(res)
    if not todo:
        print("Nothing to extend: every cell is all-pass or all-fail.")
        return
    print("Extending:", {f"{p}/{t}": n for (p, t), n in todo.items()})
    counts = attempt_counts()
    snapshot_usage("extend_start")
    while todo:
        cells = list(todo)
        random.shuffle(cells)
        for cell in cells:
            prod, tkey = cell
            check_budget(TIER.get(configs[tkey], 25))
            run_cell(prod, tkey, "main", counts[cell], configs)
            counts[cell] += 1
            todo[cell] -= 1
            if todo[cell] == 0:
                del todo[cell]
    snapshot_usage("extend_end")


def wait_for_job(job_id, headers, timeout=3600):
    deadline, delay = time.time() + timeout, 2.0
    while True:
        job = requests.get(f"{BATCH_URL}/jobs/{job_id}", headers=headers, timeout=60).json()
        run = job.get("latest_run") or {}
        stats = run.get("stats", {})
        print(f"   job {job_id}: {run.get('status')} {stats.get('completed', 0)}/{stats.get('total', '?')}")
        if run.get("status") in ("completed", "stopped", "deleted"):
            return job
        if time.time() > deadline:
            raise TimeoutError(job_id)
        time.sleep(delay)
        delay = min(delay * 1.5, 15.0)


def cmd_batch(_):
    configs = load_configs()
    h = {"X-API-Key": api_key(), "Content-Type": "application/json"}
    snapshot_usage("batch_start")
    for tkey, t in TARGETS.items():
        if skip(t) or configs.get(tkey) in (None, "forbidden") or "batch" not in products_for(t):
            continue
        cname = configs[tkey]
        urls = batch_urls(t)
        estimate = len(urls) * TIER[cname]
        if SPENT + estimate >= SET["budget_credits"]:
            print(f"-- skip batch {tkey}: up to {estimate} credits would pass the budget")
            continue
        zp = dict(LADDER_PARAMS[cname])
        if zp.get("js_render") == "true":
            zp.update(t.get("js_params", {}))
        if zp.get("premium_proxy") == "true":
            zp.update(t.get("premium_params", {}))
        if t.get("headers") or t.get("cookies"):
            zp["custom_headers"] = {**t.get("headers", {}),
                                    **({"Cookie": t["cookies"]} if t.get("cookies") else {})}
        if t.get("cookies"):
            zp["session_id"] = t.get("session_id", 4242)
        body = {"type": "regular", "status": "closed", "zenrows_params": zp,
                "tasks": [{"url": u, "external_id": f"{tkey}-{i}"} for i, u in enumerate(urls)]}
        print(f"=== batch {tkey}: {len(urls)} URLs, config {cname}, up to {estimate} credits")
        t0 = time.perf_counter()
        r = requests.post(f"{BATCH_URL}/jobs", headers=h, json=body, timeout=120)
        if r.status_code not in (201, 202):
            print(f"   submit failed: {r.status_code} {r.text[:300]}")
            append_csv(BATCH_CSV, BATCH_FIELDS, {"run_id": RUN_ID, "ts_utc": now(), "target": tkey,
                                                 "config": cname, "urls": len(urls),
                                                 "error": f"submit_{r.status_code}"})
            continue
        job_id = r.json()["job_id"]
        job = wait_for_job(job_id, h)
        duration = round(time.perf_counter() - t0, 1)
        stats = job["latest_run"].get("stats", {})
        url_by_ext = {f"{tkey}-{i}": u for i, u in enumerate(urls)}
        cursor = None
        while True:
            page = requests.get(f"{BATCH_URL}/jobs/{job_id}/results", headers=h,
                                params={"status": "all", "cursor": cursor}, timeout=60).json()
            for row in page.get("results", []):
                credits = ((row.get("spend") or {}).get("total") or {}).get("credits", "")
                err_obj = row.get("error")
                passed, size, err = False, 0, ""
                if err_obj:
                    err = err_obj.get("code", "failed") if isinstance(err_obj, dict) else str(err_obj)
                elif row.get("result_url"):
                    content = requests.get(row["result_url"], timeout=120).text
                    size = len(content.encode())
                    passed = marker_ok(t, content)
                    err = "" if passed else "marker_missing"
                    save_evidence(("batch", tkey, cname, "main", passed),
                                  f"batch_{tkey}_{'pass' if passed else 'fail'}.html", content)
                log_row({"phase": "main", "product": "batch", "target": tkey, "config": cname,
                         "url": url_by_ext.get(row.get("external_id"), ""), "status": "",
                         "passed": passed, "latency_s": "", "credits": credits,
                         "credits_source": "batch_spend", "bytes": size, "error": err})
            cursor = page.get("next_cursor")
            if not cursor:
                break
        append_csv(BATCH_CSV, BATCH_FIELDS, {
            "run_id": RUN_ID, "ts_utc": now(), "target": tkey, "config": cname, "job_id": job_id,
            "urls": len(urls), "successful": stats.get("successful", ""), "failed": stats.get("failed", ""),
            "duration_s": duration, "spend_credits": (stats.get("spend") or {}).get("credits", ""), "error": ""})
    snapshot_usage("batch_end")


def fmt(x, nd=2):
    return "-" if x is None else f"{x:,.{nd}f}"


def cmd_analyze(_):
    rows = load_rows()
    if not rows:
        sys.exit("No results yet.")

    print("\n## Escalation ladder (probe)\n")
    print("| Target | Config | Passed |")
    print("|---|---|---|")
    probe = defaultdict(list)
    for r in rows:
        if r["phase"] == "probe":
            probe[(r["target"], r["config"])].append(r["passed"] == "True")
    for (tkey, cname), res in probe.items():
        print(f"| {tkey} | {cname} | {sum(res)}/{len(res)} |")

    groups = defaultdict(list)
    for r in rows:
        if r["phase"] == "main":
            groups[(r["product"], r["target"], r["config"])].append(r)
    proxy_cells = {k: v for k, v in groups.items() if k[0] == "proxy"}
    if proxy_cells:
        print("\n## proxy (Residential Proxies, billed per GB; body sizes approximate)\n")
        print("| Target | Success | Avg latency (s) | Median latency (s) | Avg KB/request | MB per 1K successful |")
        print("|---|---|---|---|---|---|")
        for (_, tkey, _), rs in sorted(proxy_cells.items(), key=lambda kv: kv[0][1]):
            ok = [r for r in rs if r["passed"] == "True"]
            lats = [float(r["latency_s"]) for r in ok if r["latency_s"]]
            sizes = [float(r["bytes"]) for r in rs if r["bytes"] not in ("", None)]
            avg_kb = statistics.mean(sizes) / 1024 if sizes else None
            mb_1k = sum(sizes) / len(ok) * 1000 / 1024 ** 2 if ok and sizes else None
            print(f"| {tkey} | {len(ok)}/{len(rs)} ({100 * len(ok) / len(rs):.0f}%) | "
                  f"{fmt(statistics.mean(lats) if lats else None)} | "
                  f"{fmt(statistics.median(lats) if lats else None)} | {fmt(avg_kb, 1)} | {fmt(mb_1k, 0)} |")

    # Batch: per-page rows carry no timing or credits (jobs run in the background and report
    # spend per job), so this table takes credits and time from the job totals in batch_jobs.csv.
    batch_cells = {k: v for k, v in groups.items() if k[0] == "batch"}
    if batch_cells:
        jobs = defaultdict(lambda: {"urls": 0, "ok": 0, "secs": 0.0, "credits": 0.0, "n": 0})
        if BATCH_CSV.exists():
            with open(BATCH_CSV, newline="", encoding="utf-8") as f:
                for b in csv.DictReader(f):
                    if b.get("error"):
                        continue
                    j = jobs[(b["target"], b["config"])]
                    j["n"] += 1
                    for key, field in (("urls", "urls"), ("ok", "successful"),
                                       ("secs", "duration_s"), ("credits", "spend_credits")):
                        try:
                            j[key] += float(b[field] or 0)
                        except ValueError:
                            pass
        print("\n## batch (credits and time from job totals)\n")
        print("| Target | Config | Success (our check) | Success (Zenrows) | Job time per page (s) | "
              "Credits per URL | Credits per 1K successful |")
        print("|---|---|---|---|---|---|---|")
        for (_, tkey, cname), rs in sorted(batch_cells.items(), key=lambda kv: kv[0][1]):
            ok = [r for r in rs if r["passed"] == "True"]
            j = jobs.get((tkey, cname))
            if j and j["urls"]:
                per_page = j["secs"] / j["urls"]
                per_url = j["credits"] / j["urls"]
                per_1k = j["credits"] / len(ok) * 1000 if ok else None
                zr = f"{j['ok']:.0f}/{j['urls']:.0f}"
            else:
                per_page = per_url = per_1k = None
                zr = "-"
            print(f"| {tkey} | {cname} | {len(ok)}/{len(rs)} ({100 * len(ok) / len(rs):.0f}%) | {zr} | "
                  f"{fmt(per_page)} | {fmt(per_url, 1)} | {fmt(per_1k, 0)} |")

    for product in ("fetch", "extract", "autoparse", "browser", "browser_local"):
        cells = {k: v for k, v in groups.items() if k[0] == product}
        if not cells:
            continue
        est = " (credits estimated, check usage snapshots)" if product == "browser" else ""
        print(f"\n## {product}{est}\n")
        print("| Target | Config | Success | Avg latency (s) | Median latency (s) | "
              "Avg credits/request | Credits per 1K successful |")
        print("|---|---|---|---|---|---|---|")
        for (_, tkey, cname), rs in sorted(cells.items(), key=lambda kv: kv[0][1]):
            ok = [r for r in rs if r["passed"] == "True"]
            lats = [float(r["latency_s"]) for r in ok if r["latency_s"]]
            creds = [float(r["credits"]) for r in rs if r["credits"] not in ("", None)]
            total = sum(creds)
            per_1k = total / len(ok) * 1000 if ok else None
            avg_c = total / len(creds) if creds else None
            print(f"| {tkey} | {cname} | {len(ok)}/{len(rs)} ({100 * len(ok) / len(rs):.0f}%) | "
                  f"{fmt(statistics.mean(lats) if lats else None)} | "
                  f"{fmt(statistics.median(lats) if lats else None)} | {fmt(avg_c, 1)} | {fmt(per_1k, 0)} |")

    if BATCH_CSV.exists():
        print("\n## Batch jobs\n")
        print("| Target | Config | URLs | Successful | Failed | Duration (s) | Credits |")
        print("|---|---|---|---|---|---|---|")
        with open(BATCH_CSV, newline="", encoding="utf-8") as f:
            for b in csv.DictReader(f):
                print(f"| {b['target']} | {b['config']} | {b['urls']} | {b['successful']} | "
                      f"{b['failed']} | {b['duration_s']} | {b['spend_credits']} |")

    batch_total = 0.0
    if BATCH_CSV.exists():
        with open(BATCH_CSV, newline="", encoding="utf-8") as f:
            for b in csv.DictReader(f):
                try:
                    batch_total += float(b["spend_credits"] or 0)
                except ValueError:
                    pass
    print(f"\nCredits logged per request: {logged_credits():,.0f} + Batch jobs: {batch_total:,.0f} "
          f"= {logged_credits() + batch_total:,.0f} (Browser Sessions estimated). "
          f"Compare against {USAGE_LOG} and the dashboard.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("probe")
    pr.add_argument("--redo", nargs="*", default=[], help="targets to probe again, e.g. --redo session")
    p = sub.add_parser("run")
    p.add_argument("--rounds", type=int, default=SET.get("n_smoke", 5))
    p.add_argument("--products", default="fetch,extract,browser,proxy")
    p.add_argument("--targets", default="", help="comma-separated subset, e.g. ecom")
    e = sub.add_parser("extend")
    e.add_argument("--to", type=int, default=SET.get("n_full", 20))
    sub.add_parser("batch")
    ex = sub.add_parser("exclude", help="set aside one cell's rows, e.g. after fixing a validation bug")
    ex.add_argument("target")
    ex.add_argument("product")
    eu = sub.add_parser("exclude-url", help="set aside every row whose URL contains a substring (retired targets)")
    eu.add_argument("substring")
    rx = sub.add_parser("reset-extract", help="retry Extract on targets whose domain is now prepared")
    rx.add_argument("targets", nargs="+")
    sub.add_parser("analyze")
    args = ap.parse_args()
    if args.cmd not in ("analyze", "reset-extract", "exclude", "exclude-url"):
        record_environment()
        preflight()
    {"probe": cmd_probe, "run": cmd_run, "extend": cmd_extend, "batch": cmd_batch,
     "analyze": cmd_analyze, "reset-extract": cmd_reset_extract, "exclude": cmd_exclude,
     "exclude-url": cmd_exclude_url}[args.cmd](args)


if __name__ == "__main__":
    main()