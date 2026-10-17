"""Fetch all RSS/Atom feeds in sources.json and merge them into data/news.json.

Each source belongs to one topic (politics, economy, tech, ai, industry, startups) and one region
(iran, world). Stories can also be routed into extra topics by keyword - e.g. an AI story from a tech
site also appears under AI. Keeps the last KEEP_HOURS of items, deduplicated by link.
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

# Badges shown on a story.
TAG_RULES = {
    "funding": r"\b(raises?|raised|funding|seed round|pre-seed|series [a-e]\b|valuation|unicorn|backed by)\b"
               r"|جذب سرمایه|سرمایه‌?گذاری خطرپذیر|تامین مالی جمعی",
    "launch": r"\b(launch(es|ed)?|unveils?|debuts?|founded|stealth|new startup)\b"
              r"|راه‌?اندازی شد|رونمایی|معرفی شد",
}
# Extra topics a story is routed into, and which source topics may route into them.
ROUTE_RULES = {
    "ai": (r"\b(ai|a\.i\.|artificial intelligence|llms?|gpt|openai|anthropic|claude|gemini|machine learning|chatbots?|ai agents?)\b"
           r"|هوش مصنوعی|یادگیری ماشین|چت‌?بات", {"tech", "startups", "economy", "industry"}),
    "startups": (r"\b(startups?|raises?|raised|seed round|pre-seed|series [a-e]\b|venture capital|unicorn|founders?|accelerator)\b"
                 r"|استارتاپ|استارت‌آپ|کارآفرین|شتاب‌?دهنده|سرمایه‌?گذاری خطرپذیر|دانش‌?بنیان|جذب سرمایه",
                 {"tech", "ai", "economy"}),
    "industry": (r"\b(manufactur\w*|factory|factories|steel|automakers?|refiner\w*|mining|petrochemical\w*|semiconductor plant)\b"
                 r"|خودروساز|فولاد|پتروشیمی|معدن|کارخانه|پالایشگاه|سیمان|نیروگاه|شهرک صنعتی",
                 {"economy", "tech"}),
}
TAG_RES = {k: re.compile(v, re.I) for k, v in TAG_RULES.items()}
ROUTE_RES = {k: (re.compile(rx, re.I), allowed) for k, (rx, allowed) in ROUTE_RULES.items()}
IMG_IN_HTML = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)


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


def image_from_html(fragment):
    m = IMG_IN_HTML.search(html.unescape(fragment or ""))
    return m.group(1) if m else None


def parse_xml(raw):
    """Return [(title, link, date, summary, image)] using ElementTree."""
    root = ET.fromstring(raw)
    out = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        f, image = {}, None
        for c in el.iter():
            if c is el:
                continue
            n = local(c.tag)
            if n == "link" and c in list(el):
                href = c.get("href")
                if href and c.get("rel", "alternate") == "alternate":
                    f.setdefault("link", href)
                elif (c.text or "").strip():
                    f.setdefault("link", c.text.strip())
            elif n in ("content", "thumbnail") and c.get("url") and not image:
                kind = (c.get("medium") or c.get("type") or "image").lower()
                if kind.startswith("image"):
                    image = c.get("url")
            elif n == "enclosure" and (c.get("type") or "").startswith("image") and not image:
                image = c.get("url")
            if c in list(el) and n in ("title", "pubdate", "published", "updated", "date", "description",
                                       "summary", "content", "encoded", "guid"):
                f.setdefault(n, "".join(c.itertext()))
        link = f.get("link") or (f.get("guid") if (f.get("guid") or "").startswith("http") else None)
        date = f.get("pubdate") or f.get("published") or f.get("date") or f.get("updated")
        body = f.get("description") or f.get("summary") or f.get("content") or f.get("encoded")
        image = image or image_from_html(f.get("encoded")) or image_from_html(body)
        out.append((f.get("title"), link, date, body, image))
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
        m = re.search(r'<(?:media:content|media:thumbnail|enclosure)[^>]*url="([^"]+)"', block, re.I)
        body = g("description", "summary", "content:encoded", "content")
        out.append((g("title"), clean(link or ""), g("pubDate", "published", "dc:date", "updated"), body,
                    m.group(1) if m else image_from_html(body)))
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
    for title, link, date, summary, image in entries[: src.get("max", DEFAULT_MAX)]:
        title, link = clean(title), clean(link)
        if not title or not link or (src.get("skip") and re.search(src["skip"], link)):
            continue
        d = parse_date(clean(date)) or now
        if d > now:
            d = now
        summ = clean(summary, 300)
        if summ == title:
            summ = ""
        hay = f"{title} {summ}"
        topics = [src["topic"]] + [t for t, (rx, allowed) in ROUTE_RES.items()
                                   if t != src["topic"] and src["topic"] in allowed and rx.search(hay)]
        image = html.unescape(image).strip() if image else None
        if image and image.startswith("//"):
            image = "https:" + image
        items.append({
            "id": hashlib.sha1(link.encode()).hexdigest()[:12],
            "title": title,
            "link": link,
            "summary": summ,
            "image": image if image and image.startswith("http") else None,
            "source": src["name"],
            "region": src["region"],
            "topics": topics,
            "tags": sorted(k for k, rx in TAG_RES.items() if rx.search(hay)),
            "date": d.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    return src, items, None


def main():
    sources = json.loads(SOURCES.read_text(encoding="utf-8"))
    old = {}
    if OUT.exists():
        try:
            old = {i["id"]: i for i in json.loads(OUT.read_text(encoding="utf-8"))["items"] if "topics" in i}
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
            status.append({"name": src["name"], "url": src["url"], "region": src["region"], "topic": src["topic"],
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
