"""
Does Zenrows forward the session cookie? Two base requests (about 2 credits).

1. Through Zenrows to an echo service: shows the exact Cookie header the target receives.
2. Through Zenrows to /secure: shows where the session target actually lands.

  python check_cookie_forwarding.py
"""
import json
import os
from pathlib import Path

import requests
import yaml

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"), override=True)
except ImportError:
    pass

KEY = os.environ["ZENROWS_API_KEY"].strip()
session = yaml.safe_load(open("targets.yaml", encoding="utf-8"))["targets"]["session"]
cookie = session["cookies"]
extra = session.get("headers", {})
params = {"apikey": KEY, "custom_headers": "true", "session_id": session.get("session_id", 4242)}


def mask(c):
    return c[:20] + "..." + c[-6:] if len(c) > 30 else c


print("Cookie in targets.yaml:", mask(cookie))

# 1. Echo: what does the target see?
r = requests.get("https://api.zenrows.com/v1/", params={**params, "url": "https://httpbin.io/anything"},
                 headers={**extra, "Cookie": cookie}, timeout=180)
print("\n[echo] status", r.status_code)
try:
    seen = r.json().get("headers", {})
    received = seen.get("Cookie") or seen.get("cookie")
    if isinstance(received, list):
        received = "; ".join(received)
    print("[echo] Cookie the target received:", mask(received) if received else "NONE")
    print("[echo] Matches what we sent:", received == cookie)
    for h in extra:
        got = seen.get(h) or seen.get(h.lower())
        if isinstance(got, list):
            got = "; ".join(got)
        print(f"[echo] {h} received matches ours:", got == extra[h])
        if got != extra[h]:
            print(f"         sent:     {extra[h]}")
            print(f"         received: {got or 'NONE'}")
except ValueError:
    print("[echo] Response was not JSON:", r.text[:200])

# 2. The real target
r = requests.get("https://api.zenrows.com/v1/", params={**params, "url": "https://the-internet.herokuapp.com/secure"},
                 headers={**extra, "Cookie": cookie}, timeout=180)
body = r.text
print("\n[secure] status", r.status_code)
print("[secure] logged in (logout link present):", 'href="/logout"' in body)
print("[secure] login page returned instead:", "Login Page" in body or "You must login" in body)
Path("results").mkdir(exist_ok=True)
Path("results/cookie_check_secure.html").write_text(body, encoding="utf-8")
print("\nSaved the /secure response to results/cookie_check_secure.html")