"""Fetch all RSS/Atom feeds in sources.json and merge them into data/news.json.

Standard library only. Keeps the last KEEP_HOURS of items, deduplicated by link.
"""
import concurrent.futures as cf
import email.utils
import gzip
import hashlib
import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "sources.json"
OUT = ROOT / "data" / "news.json"
KEEP_HOURS = 72
DEFAULT_MAX = 25
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Extra tags added on top of a source's own category, from title/summary keywords.
TAG_RULES = {
    "funding": r"\b(raises?|raised|funding|seed round|pre-seed|series [a-e]\b|valuation|unicorn|backed by|investors?)\b"
               r"|جذب سرمایه|سرمایه‌?گذاری خطرپذیر|کارآفرینی",
    "launch": r"\b(launch(es|ed)?|unveils?|introduces?|debuts?|founded|stealth|new startup|startup)\b"
              r"|استارتاپ|راه‌?اندازی|معرفی شد",
    "ai": r"\b(ai|a\.i\.|artificial intelligence|llm|gpt|openai|anthropic|claude|gemini|machine learning|chatbot|agents?)\b"
          r"|هوش مصنوعی",
}
TAG_RES = {k: re.compile(v, re.I) for k, v in TAG_RULES.items()}


def clean(text, limit=None):
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", text or "", flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(html.unescape(text))
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def download(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read()
        encoding = (r.headers.get("Content-Encoding") or "").lower()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    elif encoding == "br":  # some servers send brotli even when not asked
        import brotli
        raw = brotli.decompress(raw)
    return raw


def local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def parse_xml(raw):
    """Return [(title, link, date, summary)] using ElementTree."""
    root = ET.fromstring(raw)
    out = []
    for el in root.iter():
        name = local(el.tag)
        if name not in ("item", "entry"):
            continue
        f = {}
        for c in el:
            n = local(c.tag)
            if n == "link":
                href = c.get("href")
                if href and c.get("rel", "alternate") == "alternate":
                    f.setdefault("link", href)
                elif (c.text or "").strip():
                    f.setdefault("link", c.text.strip())
            elif n in ("title", "pubdate", "published", "updated", "date", "description", "summary", "content", "guid"):
                f.setdefault(n, "".join(c.itertext()))
        link = f.get("link") or (f.get("guid") if (f.get("guid") or "").startswith("http") else None)
        date = f.get("pubdate") or f.get("published") or f.get("date") or f.get("updated")
        out.append((f.get("title"), link, date, f.get("description") or f.get("summary") or f.get("content")))
    return out


def parse_regex(text):
    """Fallback for feeds that are not well-formed XML."""
    out = []
    for block in re.findall(r"<(?:item|entry)[\s>](.*?)</(?:item|entry)>", text, re.S | re.I):
        def g(*tags):
            for t in tags:
                m = re.search(rf"<{t}[^>]*>(.*?)</{t}>", block, re.S | re.I)
                if m:
                    return m.group(1)
            return None
        link = g("link")
        if not link:
            m = re.search(r'<link[^>]*href="([^"]+)"', block, re.I)
            link = m.group(1) if m else None
        out.append((g("title"), clean(link or ""), g("pubDate", "published", "dc:date", "updated"),
                    g("description", "summary", "content:encoded", "content")))
    return out


def fetch(src):
    try:
        raw = download(src["url"])
        try:
            entries = parse_xml(raw)
        except ET.ParseError:
            entries = parse_regex(raw.decode("utf-8", "replace"))
    except Exception as e:  # network errors, HTTP errors, decoding errors
        return src, [], f"{type(e).__name__}: {str(e)[:120]}"

    now = datetime.now(timezone.utc)
    items = []
    for title, link, date, summary in entries[: src.get("max", DEFAULT_MAX)]:
        title, link = clean(title), clean(link)
        if not title or not link:
            continue
        d = parse_date(clean(date)) or now
        if d > now:
            d = now
        summ = clean(summary, 280)
        if summ == title:
            summ = ""
        hay = f"{title} {summ}"
        tags = sorted(k for k, rx in TAG_RES.items() if rx.search(hay))
        items.append({
            "id": hashlib.sha1(link.encode()).hexdigest()[:12],
            "title": title,
            "link": link,
            "summary": summ,
            "source": src["name"],
            "region": src["region"],
            "cat": src["cat"],
            "tags": tags,
            "date": d.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    return src, items, None


def main():
    sources = json.loads(SOURCES.read_text(encoding="utf-8"))
    old = {}
    if OUT.exists():
        try:
            old = {i["id"]: i for i in json.loads(OUT.read_text(encoding="utf-8"))["items"]}
        except (ValueError, KeyError):
            old = {}

    status, merged = [], dict(old)
    with cf.ThreadPoolExecutor(12) as ex:
        for src, items, err in ex.map(fetch, sources):
            for it in items:
                prev = merged.get(it["id"])
                if prev:  # keep the first-seen date so items don't jump back to the top
                    it["date"] = min(prev["date"], it["date"])
                merged[it["id"]] = it
            status.append({"name": src["name"], "region": src["region"], "cat": src["cat"],
                           "ok": err is None, "count": len(items), "error": err})
            print(f"{'OK ' if err is None else 'ERR'} {len(items):3d}  {src['name']}" + (f"  ({err})" if err else ""))

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=KEEP_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    items = sorted((i for i in merged.values() if i["date"] >= cutoff), key=lambda i: i["date"], reverse=True)

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": status,
        "items": items,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{len(items)} items saved, {sum(not s['ok'] for s in status)} sources failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
