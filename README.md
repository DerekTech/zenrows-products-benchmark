# Zenrows benchmark

The test harness and data behind a hands-on review of Zenrows (October 2026). It measures success rate, latency and cost for each Zenrows product against five targets, using Zenrows' own billing figures wherever the API provides them.

## What's tested

**Products:** Fetch, Extract, Batch, Browser Sessions and the legacy standalone Residential Proxies.

| Target | What it tests | Protection |
|---|---|---|
| scrapethissite.com hockey table | Static, paginated HTML | None |
| infinite-scroll.com demo | Content loaded by JavaScript as you scroll | None |
| Walmart product pages | A large retail site | PerimeterX/HUMAN and Akamai Bot Manager (seen in returned cookies) |
| Leboncoin car listings | A site that requires a French IP | DataDome |
| the-internet.herokuapp.com/secure | Staying logged in | None; the site rejects a session cookie used from a different browser identity |

## Headline results

Tested October 2, 2026, from Montreal over Starlink, on Zenrows' Build plan ($19/month, $0.000422 per credit). Latency is the median of successful requests.

| | Static | Dynamic | Walmart | Leboncoin | Login session* |
|---|---|---|---|---|---|
| **Fetch** | 5/5 · 1.5 s | 7/7 · 12.6 s | 5/5 · 16.2 s | 19/20 · 8.7 s (JS + premium); 20/20 · 2.4 s (base) | 32/33 in first 10 min, 0/17 after |
| **Extract** | 5/5 · 2.3 s | 5/5 · 23.5 s | 5/5 · 11.3 s | 20/20 · 15.6 s | 0/50 |
| **Batch** | 23/24 · 0.3 s/page | 20/20 · 1.3 s/page | 30/30 · 3.1 s/page | 30/30 · 2.1 s/page | 0/20 |
| **Browser Sessions** | Not tested | 5/5 · 31.2 s | 5/5 · 45.8 s | 4/10 · 36.6 s | 9/10 · 76.0 s |
| **Residential Proxies** | 20/20 · 3.8 s | Not applicable | Not tested | 4/20 · 4.8 s | 49/50 |

\*Fetch, Extract and Residential Proxies login results come from `session_timing.py` (two 15-minute runs). Batch and Browser Sessions come from the main benchmark.

Credits per request, from Zenrows' `X-Request-Credits` header:

| | Static (base) | Dynamic (JS) | Walmart / Leboncoin (JS + premium) |
|---|---|---|---|
| Fetch and Batch | 1 | 5 | 25 |
| Extract | 2 | 6 | 26 |
| Browser Sessions (per session, estimated†) | – | ~47 | ~123 / ~150 |

†Browser Sessions are billed at 25,000 credits per GB plus 5 credits per minute. The harness estimates each session from measured data and time; for the billing period checked, its total (1,393 credits) matched the account's billed figure (1,398) to within 0.5%.

Full tables: run `python bench.py analyze` and `python analyze_sessions.py` against the data in `results/`.

## Running it

Requires Python 3.10+ and a Zenrows account. A full run used about 6,500 credits.

```bash
pip install -r requirements.txt
playwright install chromium        # only needed for the local-browser control runs
cp .env.example .env               # then fill in your own key and credentials
```

Edit `targets.yaml`: any target that still contains `REPLACE` is skipped. Then:

```bash
python check_domains.py                      # 1 request per domain: does Zenrows accept it?
python bench.py probe                        # escalation ladder: cheapest tier that passes 3/3
python bench.py run --rounds 5               # main benchmark
python bench.py extend --to 20               # more rounds where results were mixed
python bench.py batch                        # Batch jobs
python session_timing.py --minutes 15 --interval 30   # login-session test
python bench.py analyze                      # markdown tables
python analyze_sessions.py
```

A budget guard stops the harness at `budget_credits` in `targets.yaml`.

## Files

| File | Purpose |
|---|---|
| `bench.py` | Main harness: probe, run, extend, batch, analyze, plus `exclude`, `exclude-url` and `reset-extract` for setting rows aside |
| `targets.yaml` | Targets, pass markers and settings |
| `session_timing.py` / `analyze_sessions.py` | Login-session test and its tables |
| `check_domains.py`, `extract_domains.py` | Domain checks and Extract domain preparation |
| `get_session_cookie.py`, `check_cookie_forwarding.py` | Cookie helpers for the login target |
| `verify_extract_billing.py` | Compares Extract's header-reported cost with account usage (not run for the published results) |
| `fix_extract_credits.py`, `fix_double_correction.py`, `merge_results.py` | One-off data corrections, kept so every change to the data is traceable |
| `prepublish_check.py` | Scans files for secrets before pushing |

## How a pass is decided

A response passes only if it contains content that appears on the real page (a minimum number of table rows, the product title, the listing gallery, the Logout link) and none of the target's known block-page markers. HTTP 200 alone isn't enough, because block and login pages often return 200. Extract must also return the expected fields.

## Corrections made during testing

These are recorded in the data (`excluded_rows.csv` and the `credits_source` column), not hidden:

- **Dynamic target replaced twice.** the-internet.herokuapp.com's infinite scroll failed to initialise in 12 of 20 runs with a local Chromium browser and no Zenrows involved, and webscraper.io's scroll page didn't load content on scroll. The final target passed 5/5 locally before any credits were spent.
- **Scroll wait raised** from 1.5 to 3 seconds after captured pages showed content still loading.
- **Walmart Extract re-run** after a field-name error in the harness's own check (`price` vs `current_price`).
- **Early Extract credits corrected** from the rate card to the measured header value; a script error that double-counted some rows was then undone.

## Limitations

- One location, one connection and essentially one test day. Sites change; Leboncoin passed at the 1-credit tier on a later run.
- Small samples in some cells (5 rounds; 10 for Browser Sessions).
- Extract's extra credit comes from Zenrows' billing header and wasn't verified against billed account totals.
- Residential Proxies were tested with Python `requests`, which sends Chrome headers but doesn't reproduce Chrome's TLS fingerprint or run JavaScript.
- Starlink dropped 3 long-running requests; it's not possible to tell from the client side which end closed them.
- Raw page samples and screenshots aren't published, because they're full copies of third-party pages.
