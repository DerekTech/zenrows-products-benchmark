"""
Login-session benchmark: can each product log in to the-internet.herokuapp.com and stay logged in?

Each product logs in its own way, then requests the members-only page /secure every
--interval seconds for --minutes minutes. Every result records the minutes since that
product's login, and the script marks when 10 minutes pass (Zenrows' session_id IP window).

  Fetch                Logs in through Zenrows (POST via the API, documented workflow), then
                       reuses the returned session cookie with the same session_id.
  Extract              Same login (Extract has no POST, so the login is a Fetch POST with
                       Extract's own session_id), then requests /secure with extract=auto.
  Residential Proxies  Your own HTTP client through the proxy: logs in and keeps its own
                       cookie jar, with your own browser identity.

Each round also asks an echo service which IP and User-Agent each product presents, so any
change is visible. Base tier only: roughly 70-80 API credits for 15 minutes, plus a little
proxy traffic. Results: results/session_timing.jsonl.

  python session_timing.py                       # 15 minutes, every 60 seconds
  python session_timing.py --minutes 15 --interval 60
"""
import argparse
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"), override=True)
except ImportError:
    pass

API = "https://api.zenrows.com/v1/"
BASE = "https://the-internet.herokuapp.com"
ECHO = "https://httpbin.io/anything"
MARKER = 'href="/logout"'
WINDOW_MIN = 10
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
LOG = Path("results/session_timing.jsonl")
RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S")

KEY = (os.environ.get("ZENROWS_API_KEY") or "").strip()
USER, PASS = os.environ.get("SITE_USER"), os.environ.get("SITE_PASS")
PROXY = (os.environ.get("ZR_PROXY_URL") or "").strip()
if not (KEY and USER and PASS):
    raise SystemExit("Need ZENROWS_API_KEY, SITE_USER and SITE_PASS in .env.")


def proxy_problem(url):
    """Return a reason the proxy URL can't work, or '' if it looks usable."""
    from urllib.parse import urlparse
    if not url:
        return "no ZR_PROXY_URL in .env"
    try:
        url.encode("ascii")
    except UnicodeEncodeError:
        return ("ZR_PROXY_URL contains non-ASCII characters, usually a masked password (dots) "
                "or curly quotes copied from the dashboard")
    u = urlparse(url)
    if not (u.scheme and u.hostname and u.port and u.username and u.password):
        return "ZR_PROXY_URL isn't in the form http://username:password@host:port"
    return ""


PROXY_PROBLEM = proxy_problem(PROXY)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(text):
    text = text.replace(KEY, "***")
    return text.replace(PROXY, "***") if PROXY else text


def log(rec):
    LOG.parent.mkdir(exist_ok=True)
    rec = {"run_id": RUN_ID, "ts_utc": now(), **rec}
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps({k: v for k, v in rec.items() if k != "run_id"}))


def usage_snapshot(label):
    try:
        r = requests.get("https://api.zenrows.com/v1/subscriptions/self/details",
                         headers={"X-API-Key": KEY}, timeout=30)
        data = r.json()
    except Exception as e:  # noqa: BLE001
        data = {"error": type(e).__name__}
    log({"step": "usage_snapshot", "label": label, "usage": data})


def first(v):
    return v[0] if isinstance(v, list) and v else v


def zr_error(r):
    """Zenrows' own error code and message for non-200 responses (e.g. 400 REQS...)."""
    if r is None or r.status_code == 200:
        return ""
    try:
        j = r.json()
        return f"{j.get('code', '')} {j.get('title') or j.get('detail') or ''}".strip()[:200]
    except ValueError:
        return r.text[:200]


def rack_cookie(*texts):
    for text in texts:
        m = re.search(r"rack\.session=([^;,\s]+)", text or "")
        if m:
            return f"rack.session={m.group(1)}"
    return None


def logged_in(r, extract=False):
    if r is None or r.status_code != 200:
        return False
    if extract:                      # Extract returns JSON; the page itself is in the "html" field
        try:
            return MARKER in (r.json().get("html") or "")
        except ValueError:
            return False
    return MARKER in r.text


# ---------------------------------------------------------------- Zenrows (Fetch / Extract)

def zr(url, session_id, cookie=None, method="GET", data=None, extract=False):
    params = {"apikey": KEY, "url": url, "session_id": session_id}
    headers = {}
    if cookie:
        params["custom_headers"] = "true"
        headers["Cookie"] = cookie
    if extract:
        params["extract"] = "auto"
    start = time.perf_counter()
    try:
        r = requests.request(method, API, params=params, headers=headers, data=data, timeout=180)
        return r, round(time.perf_counter() - start, 2), ""
    except requests.RequestException as e:
        return None, round(time.perf_counter() - start, 2), clean(f"{type(e).__name__}: {e}")[:160]


def credits(r):
    return r.headers.get("X-Request-Credits") if r is not None else None


def zr_login(session_id):
    r, lat, err = zr(f"{BASE}/authenticate", session_id, method="POST",
                     data={"username": USER, "password": PASS})
    cookie = rack_cookie(r.headers.get("Zr-Cookies"), r.headers.get("Zr-Set-Cookie")) if r is not None else None
    return cookie, {"status": r.status_code if r is not None else "", "latency_s": lat,
                    "landed_logged_in": logged_in(r), "got_cookie": bool(cookie), "credits": credits(r),
                    "error": err or ("" if cookie else (r.text[:200] if r is not None else ""))}


def zr_echo(session_id):
    r, _, err = zr(ECHO, session_id)
    out = {"echo_credits": credits(r)}
    if r is None or r.status_code != 200:
        return {**out, "echo_error": err or f"http_{r.status_code}"}
    try:
        j = r.json()
        return {**out, "ip": first(j.get("origin")), "user_agent": first((j.get("headers") or {}).get("User-Agent"))}
    except ValueError:
        return {**out, "echo_error": "not_json"}


# ---------------------------------------------------------------- Residential Proxies

def proxy_session():
    s = requests.Session()
    s.proxies = {"http": PROXY, "https": PROXY}
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    return s


def proxy_req(s, url, method="GET", **kw):
    start = time.perf_counter()
    try:
        r = s.request(method, url, timeout=120, **kw)
        return r, round(time.perf_counter() - start, 2), ""
    except Exception as e:  # noqa: BLE001  (bad proxy credentials raise non-requests errors too)
        return None, round(time.perf_counter() - start, 2), clean(f"{type(e).__name__}: {e}")[:160]


def proxy_login(s):
    proxy_req(s, f"{BASE}/login")
    r, lat, err = proxy_req(s, f"{BASE}/authenticate", method="POST",
                            data={"username": USER, "password": PASS},
                            headers={"Origin": BASE, "Referer": f"{BASE}/login"})
    ok = bool(s.cookies.get("rack.session"))
    return ok, {"status": r.status_code if r is not None else "", "latency_s": lat,
                "landed_logged_in": logged_in(r), "got_cookie": ok,
                "bytes": len(r.content) if r is not None else 0, "error": err}


def proxy_echo(s):
    r, _, err = proxy_req(s, ECHO)
    out = {"echo_bytes": len(r.content) if r is not None else 0}
    if r is None or r.status_code != 200:
        return {**out, "echo_error": err or f"http_{r.status_code}"}
    try:
        j = r.json()
        return {**out, "ip": first(j.get("origin")), "user_agent": first((j.get("headers") or {}).get("User-Agent"))}
    except ValueError:
        return {**out, "echo_error": "not_json"}


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=15)
    ap.add_argument("--interval", type=float, default=60, help="seconds between rounds")
    args = ap.parse_args()

    products = {}
    usage_snapshot("start")

    # Logins: each product gets its own session and its own clock
    for name, extract in (("fetch", False), ("extract", True)):
        sid = random.randint(10_000, 99_999)
        cookie, info = zr_login(sid)
        products[name] = {"session_id": sid, "cookie": cookie, "extract": extract,
                          "login_t": time.time(), "flagged": False}
        log({"product": name, "step": "login", "session_id": sid, **info})
    if not PROXY_PROBLEM:
        s = proxy_session()
        ok, info = proxy_login(s)
        products["proxy"] = {"session": s, "cookie": ok, "login_t": time.time(), "flagged": False}
        log({"product": "proxy", "step": "login", **info})
    else:
        print(f"-- Residential Proxies skipped: {PROXY_PROBLEM}")

    end = time.time() + args.minutes * 60
    rnd = 0
    while time.time() < end:
        rnd += 1
        round_start = time.time()
        print(f"--- round {rnd}")
        for name, st in products.items():
            elapsed = (time.time() - st["login_t"]) / 60
            if elapsed >= WINDOW_MIN and not st["flagged"]:
                st["flagged"] = True
                print(f"==== {name}: {WINDOW_MIN} minutes since login (session_id IP window ends) ====")
            base = {"product": name, "step": "secure", "round": rnd,
                    "minutes_since_login": round(elapsed, 1), "after_10_min": elapsed >= WINDOW_MIN}
            if not st["cookie"]:
                log({**base, "passed": False, "error": "no_session_cookie_from_login"})
                continue
            if name == "proxy":
                r, lat, err = proxy_req(st["session"], f"{BASE}/secure")
                passed = logged_in(r)
                extra = {"bytes": len(r.content) if r is not None else 0, **proxy_echo(st["session"])}
            else:
                r, lat, err = zr(f"{BASE}/secure", st["session_id"], cookie=st["cookie"], extract=st["extract"])
                err = err or zr_error(r)
                passed = logged_in(r, extract=st["extract"])
                extra = {"credits": credits(r), **zr_echo(st["session_id"])}
            log({**base, "status": r.status_code if r is not None else "", "latency_s": lat,
                 "passed": passed, "error": err, **extra})
        wait = args.interval - (time.time() - round_start)
        if wait > 0 and time.time() + wait < end:
            time.sleep(wait)

    # Summary for this run
    usage_snapshot("end")
    allrows, rows = [], []
    for line in LOG.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            if rec.get("run_id") == RUN_ID:
                allrows.append(rec)
                if rec.get("step") == "secure":
                    rows.append(rec)

    def num(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0
    print("\n| Product | Passed before 10 min | Passed after 10 min | Distinct IPs | Distinct User-Agents | "
          "Credits (page + echo + login) | Proxy KB (page + echo + login) |")
    print("|---|---|---|---|---|---|---|")
    for name in products:
        mine = [r for r in rows if r["product"] == name]
        before = [r for r in mine if not r.get("after_10_min")]
        after = [r for r in mine if r.get("after_10_min")]
        ips = {str(first(r.get("ip"))).split(":")[0] for r in mine if r.get("ip")}
        uas = {str(first(r.get("user_agent"))) for r in mine if r.get("user_agent")}
        everything = [r for r in allrows if r.get("product") == name]
        creds = sum(num(r.get("credits")) + num(r.get("echo_credits")) for r in everything)
        kb = sum(num(r.get("bytes")) + num(r.get("echo_bytes")) for r in everything) / 1024
        print(f"| {name} | {sum(r['passed'] for r in before)}/{len(before)} | "
              f"{sum(r['passed'] for r in after)}/{len(after)} | {len(ips)} | {len(uas)} | "
              f"{creds:.0f} | {kb:.1f} |")
    print(f"\nFull log: {LOG}")


if __name__ == "__main__":
    main()