"""Collect the "new in <category>" apps from Cafe Bazaar category pages into data/bazaar.json.

Each category page opens with a carousel of new apps; we read it, remember when each app was first
seen, and keep apps for KEEP_DAYS so the page shows what appeared recently.
"""
import concurrent.futures as cf
import html
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "bazaar.json"
KEEP_DAYS = 30
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
CATEGORIES = [
    ("finance", "Finance"), ("shopping", "Shopping"), ("food-drink", "Food & drink"), ("travel-local", "Travel & local"),
    ("education", "Education"), ("health-fitness", "Health & fitness"), ("medical", "Medical"), ("lifestyle", "Lifestyle"),
    ("tools", "Tools"), ("social", "Social"), ("maps-navigation", "Maps & transport"), ("entertainment", "Entertainment"),
    ("books-reference", "Books & reference"), ("photography", "Photography"), ("music-audio", "Music & audio"),
]
ITEM = re.compile(r'<a href="/app/([^"?/]+)"[^>]*class="SimpleAppItem[^"]*".*?<img alt="([^"]*)"[^>]*data-lazy-src="([^"]+)"', re.S)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_category(cat):
    slug, label = cat
    req = urllib.request.Request(f"https://cafebazaar.ir/cat/{slug}", headers={"User-Agent": UA})
    page = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    # the "new" carousel: from the first <h2> that mentions "new" (تازه) to the next <h2>
    m = re.search(r"<h2[^>]*>[^<]*(?:تازه|جدید)[^<]*</h2>(.*?)(?=<h2|$)", page, re.S)
    if not m:
        raise RuntimeError("no 'new apps' section")
    apps = []
    for pkg, name, icon in ITEM.findall(m.group(1)):
        if pkg not in (a["pkg"] for a in apps):
            apps.append({"pkg": pkg, "name": html.unescape(name).strip(), "icon": html.unescape(icon)})
    return slug, label, apps


def main():
    old = {}
    if OUT.exists():
        try:
            old = {(c["slug"], a["pkg"]): a for c in json.loads(OUT.read_text(encoding="utf-8"))["categories"] for a in c["apps"]}
        except (ValueError, KeyError):
            old = {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cats, errors = [], {}
    with cf.ThreadPoolExecutor(6) as ex:
        futures = {ex.submit(fetch_category, c): c for c in CATEGORIES}
        results = {}
        for f in cf.as_completed(futures):
            slug, label = futures[f]
            try:
                results[slug] = f.result()
            except Exception as e:
                errors[slug] = f"{type(e).__name__}: {str(e)[:120]}"
    for slug, label in CATEGORIES:
        current = results.get(slug, (slug, label, []))[2]
        merged = {}
        for a in current:
            prev = old.get((slug, a["pkg"]))
            a["first_seen"] = prev["first_seen"] if prev else now_iso()
            merged[a["pkg"]] = a
        for (s, pkg), a in old.items():  # keep apps that left the carousel recently
            if s == slug and pkg not in merged and a["first_seen"] >= cutoff:
                merged[pkg] = a
        apps = sorted(merged.values(), key=lambda a: a["first_seen"], reverse=True)
        cats.append({"slug": slug, "label": label, "apps": apps})
        print(f"{slug:16s} {len(current):3d} now, {len(apps):3d} kept" + (f"  ERR {errors[slug]}" if slug in errors else ""))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"generated": now_iso(), "errors": errors, "categories": cats},
                              ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
