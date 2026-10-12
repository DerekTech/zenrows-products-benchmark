"""
Log in to the-internet.herokuapp.com directly (no Zenrows, no credits), verify the
session works on /secure, and print the Cookie value to paste into targets.yaml.

  python get_session_cookie.py
"""
import os
from pathlib import Path

import requests
import yaml

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"), override=True)
except ImportError:
    pass

BASE = "https://the-internet.herokuapp.com"
user, password = os.environ.get("SITE_USER"), os.environ.get("SITE_PASS")
if not user or not password:
    raise SystemExit("Set SITE_USER and SITE_PASS in .env first.")

# Log in with the SAME headers the benchmark will send through Zenrows, so sites that
# tie a session to the browser's User-Agent don't discard it.
HEADERS = yaml.safe_load(open("targets.yaml", encoding="utf-8"))["targets"]["session"].get("headers", {})
print("Logging in with headers:", HEADERS or "(none set in targets.yaml)")

import re

s = requests.Session()
s.headers.update(HEADERS)

# Behave like a browser: load the form first (gets the session + CSRF token), then submit it
# with every hidden field the form contains, plus the Origin/Referer a browser would send.
form_page = s.get(f"{BASE}/login", timeout=60)
hidden = dict(re.findall(r'<input[^>]*type=["\']hidden["\'][^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']',
                         form_page.text))
hidden.update(dict(re.findall(r'<input[^>]*name=["\']([^"\']+)["\'][^>]*type=["\']hidden["\'][^>]*value=["\']([^"\']*)["\']',
                              form_page.text)))
print("Login page:", form_page.status_code, "| hidden fields found:", list(hidden) or "none")

login = s.post(f"{BASE}/authenticate",
               data={**hidden, "username": user, "password": password},
               headers={"Origin": BASE, "Referer": f"{BASE}/login"}, timeout=60)
print("After login, landed on:", login.url, "| status", login.status_code)

cookie = "; ".join(f"{k}={v}" for k, v in s.cookies.items())
if not cookie or not login.url.endswith("/secure"):
    print("\nLogin did not reach /secure. First 400 characters of the response:\n")
    print(login.text[:400])
    raise SystemExit("\nLogin failed. If the text above mentions the password, check SITE_USER / SITE_PASS.")

# Verify with a fresh request that carries ONLY the cookie, exactly as Zenrows will send it.
check = requests.get(f"{BASE}/secure", headers={**HEADERS, "Cookie": cookie}, timeout=60)
logged_in = check.url.endswith("/secure") and 'href="/logout"' in check.text
print("Cookie works on /secure:", logged_in)

if logged_in:
    print("\nPaste this into the session block of targets.yaml:\n")
    print(f'    cookies: "{cookie}"')
else:
    print("\nLogin failed. Landed on:", check.url)
    print("Check that SITE_USER / SITE_PASS match the credentials shown on the login page exactly.")