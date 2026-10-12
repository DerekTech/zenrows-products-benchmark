"""
Extract domain support.

  python extract_domains.py list        # which of your target domains Extract serves today
  python extract_domains.py prepare     # ask Zenrows to prepare the unsupported ones, logs the time
  python extract_domains.py list        # run again later to see when they turn ready

Every call is appended to results/extract_domains_log.jsonl with a timestamp,
so you can report how long preparation took.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"), override=True)
except ImportError:
    pass

KEY = os.environ["ZENROWS_API_KEY"].strip()
BASE = "https://api.zenrows.com/v1/extract"
HEADERS = {"X-API-Key": KEY, "Content-Type": "application/json"}
TARGET_DOMAINS = [
    "infinite-scroll.com",
    "scrapethissite.com",
    "the-internet.herokuapp.com",
    "walmart.com",
    "leboncoin.fr",
]
LOG = Path("results/extract_domains_log.jsonl")


def log(action, payload):
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "action": action, "response": payload}) + "\n")


def call(method, path, **kw):
    r = requests.request(method, f"{BASE}{path}", headers=HEADERS, timeout=60, **kw)
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:500]}
    return r.status_code, data


cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

if cmd == "list":
    status, data = call("GET", "/domains")
    log("list", {"status": status, "data": data})
    print("GET /v1/extract/domains ->", status)
    text = json.dumps(data).lower()
    for d in TARGET_DOMAINS:
        print(f"  {d:<30} {'listed' if d in text else 'not listed'}")
    print("\nFull response saved in", LOG, "(check the format if anything looks off)")

elif cmd == "prepare":
    # Request only the domains Extract doesn't serve yet. Zenrows limits how many
    # domains an account can request, so don't spend requests on domains you won't report.
    # One example URL per page layout you want Extract to parse (the API accepts 1-5).
    examples = {
        "infinite-scroll.com": [
            "https://infinite-scroll.com/demo/full-page/",
        ],
    }
    for d, urls in examples.items():
        status, data = call("POST", "/prepared-domains", json={"domain": d, "example_urls": urls})
        log("prepare", {"domain": d, "status": status, "data": data})
        print(f"POST prepared-domains {d} -> {status}")
        print("  ", json.dumps(data)[:400])
    print("\nIf a request came back 400/422, the message above says what format the API wants.")
else:
    sys.exit("Use: list | prepare")