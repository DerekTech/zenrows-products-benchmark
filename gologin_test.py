import asyncio
import os
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv(override=True)

TOKEN = os.environ["GL_API_TOKEN"]          # dev token from the dashboard's API Token page
PROFILE_ID = os.environ["GL_PROFILE_ID"]    # an existing profile, with its proxy attached
CDP_URL = f"wss://cloudbrowser.gologin.com/connect?token={TOKEN}&profile={PROFILE_ID}"
STOP_URL = f"https://api.gologin.com/browser/{PROFILE_ID}/web"
SCREENSHOT_DIR = Path("screenshots")


def check_start():
    r = requests.get(CDP_URL.replace("wss://", "https://"), timeout=60)
    if not r.ok:
        reason = r.headers.get("X-Error-Reason") or r.status_code
        raise RuntimeError(f"Cloud Browser didn't start: {reason}")


async def main(url):
    check_start()
    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
            # Use the profile's own context so its saved cookies and storage apply
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded")
            print(await page.title())

            SCREENSHOT_DIR.mkdir(exist_ok=True)
            shot = SCREENSHOT_DIR / f"cloudbrowser_{datetime.now():%Y%m%d-%H%M%S}.png"
            await page.screenshot(path=str(shot), full_page=True)
            print(f"Screenshot saved: {shot}")

            await browser.close()
    finally:
        requests.delete(STOP_URL, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=60)


asyncio.run(main("https://www.leboncoin.fr/ad/voitures/3267212200"))