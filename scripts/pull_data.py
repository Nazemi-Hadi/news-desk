"""Download the current data files from the live site into data/ before a workflow updates them.

The site's data lives only in the deployed GitHub Pages artifact (never committed), so each run starts from
what is live. Missing files (first run) are skipped.
"""
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ["news.json", "archive.json", "markets.json", "digest.json", "events.json", "ideas.json",
         "reports.json", "weekly.json", "bazaar.json"]


def site_url():
    if os.environ.get("SITE_URL"):
        return os.environ["SITE_URL"].rstrip("/") + "/"
    owner, repo = os.environ["GITHUB_REPOSITORY"].split("/")
    return f"https://{owner.lower()}.github.io/{repo}/"


def main():
    base = site_url() + "data/"
    out = ROOT / "data"
    out.mkdir(exist_ok=True)
    for name in FILES:
        try:
            req = urllib.request.Request(base + name, headers={"Cache-Control": "no-cache"})
            body = urllib.request.urlopen(req, timeout=30).read()
            (out / name).write_bytes(body)
            print(f"pulled {name} ({len(body) // 1024} KB)")
        except urllib.error.HTTPError as e:
            print(f"skip {name}: HTTP {e.code}")
        except Exception as e:
            print(f"skip {name}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
