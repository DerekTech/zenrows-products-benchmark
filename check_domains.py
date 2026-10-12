"""
Send ONE plain Fetch request per candidate domain to see which ones Zenrows refuses (REQS001).
Costs at most 1 credit per allowed domain. Pauses between requests so refusals don't trigger an IP ban.

  python check_domains.py
"""
import json
import os
import time
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"), override=True)
except ImportError:
    pass

KEY = os.environ["ZENROWS_API_KEY"].strip()
CANDIDATES = [
    # current targets
    #"https://books.toscrape.com/",
    #"https://quotes.toscrape.com/",
    "https://www.walmart.com/",
    "https://www.leboncoin.fr/",
    # neutral fallbacks if the toscrape sandboxes are refused
    "https://the-internet.herokuapp.com/infinite_scroll",
    "https://the-internet.herokuapp.com/login",
    "https://www.scrapethissite.com/pages/simple/",
    "https://webscraper.io/test-sites/e-commerce/scroll",
]

results = {}
for url in CANDIDATES:
    try:
        r = requests.get("https://api.zenrows.com/v1/", params={"apikey": KEY, "url": url}, timeout=120)
        try:
            body = r.json()
            code, detail = body.get("code", ""), body.get("detail", "")
        except ValueError:
            code, detail = "", ""
        verdict = "REFUSED" if code == "REQS001" else ("ALLOWED" if r.status_code == 200 else "OTHER")
        results[url] = {"status": r.status_code, "code": code, "detail": detail[:200], "verdict": verdict}
        print(f"{verdict:<8} {r.status_code}  {code:<8} {url}")
        if detail:
            print(f"         {detail[:200]}")
    except requests.RequestException as e:
        print(f"ERROR    {type(e).__name__}  {url}  (possible temporary ban: wait 10 min, rerun)")
        break
    time.sleep(15)

Path("results").mkdir(exist_ok=True)
Path("results/domain_check.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
print("\nSaved results/domain_check.json")