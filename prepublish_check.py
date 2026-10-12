"""
Run before every push. Scans the files git is about to publish for secrets and private data.

  python prepublish_check.py

Checks:
  - every value in your .env (API key, proxy URLs, passwords) appears in no tracked file
  - no Zenrows API key-shaped string, proxy URL with a password, or real session cookie
  - .env itself, raw page samples and screenshots aren't tracked
Exits with status 1 and lists the problems if anything is found.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def tracked_files():
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit("Not a git repository yet. Run `git init` first.")
    return [ROOT / f for f in out.stdout.splitlines() if f]


def env_secrets():
    env = ROOT / ".env"
    if not env.exists():
        return {}
    secrets = {}
    for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if len(v) >= 6:                       # skip blanks and trivially short values
                secrets[k.strip()] = v
    return secrets


PATTERNS = {
    "proxy URL with a password": re.compile(r"https?://[^\s:/@\"']+:[^\s@\"']{4,}@[\w.-]+:\d+"),
    "session cookie value": re.compile(r"rack\.session=(?!\.\.\.)[A-Za-z0-9%+/=_-]{20,}"),
    "API key in a URL": re.compile(r"apikey=(?!\{|\*\*\*)[A-Za-z0-9]{20,}"),
    "Bearer token": re.compile(r"Bearer\s+(?!\{)[A-Za-z0-9._-]{20,}"),
}
BLOCKED_PATHS = [".env", "results/samples/", "results/screenshots/", "screenshots/"]

problems = []
files = tracked_files()
secrets = env_secrets()

for f in files:
    rel = f.relative_to(ROOT).as_posix()
    if rel == ".env" or any(rel.startswith(p) for p in BLOCKED_PATHS[1:]):
        problems.append(f"{rel}: should not be published (check .gitignore)")
        continue
    if rel in (".env.example", "prepublish_check.py"):
        continue
    try:
        text = f.read_text(encoding="utf-8", errors="ignore")
    except (IsADirectoryError, FileNotFoundError):
        continue
    for name, value in secrets.items():
        if value in text:
            problems.append(f"{rel}: contains your {name} from .env")
    for label, rx in PATTERNS.items():
        if rx.search(text):
            problems.append(f"{rel}: contains a {label}")

print(f"Checked {len(files)} files that would be published.")
if problems:
    print("\nFix these before pushing:")
    for p in problems:
        print("  -", p)
    sys.exit(1)
print("No secrets or private files found.")
