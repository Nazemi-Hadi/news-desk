"""Weekly trends report (data/weekly.json) from the last 7 days of data/archive.json.

Python counts stories per topic and tag and finds rising terms (this week against the week before); Claude
reads the counts and this week's business and tech headlines and writes the trends with numbers.
"""
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = ROOT / "data" / "archive.json"
OUT = ROOT / "data" / "weekly.json"
MODEL = "claude-opus-5-5"
LANG = os.environ.get("DIGEST_LANG", "Persian (Farsi)")
DETAIL_TOPICS = {"startups", "fintech", "ai", "tech", "economy", "industry", "giants", "research"}

STOP = set("""a an the and or but if of to in on at by for with from as is are was were be been being it its this that these
those he she they them his her their our we you your i not no new says say said after over into about up out than
more most how why what when who will would can could may might has have had do does did just also amid against
under between before off via per vs one two three first last year years week day days amp s t u k m b us
this-week top big make makes made get gets got set sets back here there now still out how-to report reports""".split())
FA_STOP = set("""و در به از که این را با است برای آن یک تا می شد شده ها های هم بر نیز ای اما یا شود کرد کند دارد
بود باید پس خود ما ما چه چرا امروز درباره روی دو سه هر بی شدن کرده اند داد دهد گفت
جدید قیمت ایران کشور سال ماه روز هفته هزار میلیون میلیارد تومان دلار درصد مهر آبان آذر دی بهمن اسفند
فروردین اردیبهشت خرداد تیر مرداد شهریور خبر اخبار گزارش ویدیو عکس""".split())
WORD = re.compile(r"[A-Za-z][A-Za-z0-9&+.'-]{2,}|[؀-ۿ‌]{3,}")


def terms(title):
    words = [w.strip(".'-").lower() for w in WORD.findall(title)]
    words = [w for w in words if w and w not in STOP and w not in FA_STOP and not any(c.isdigit() for c in w)]
    grams = set(words) | {f"{a} {b}" for a, b in zip(words, words[1:])}
    return grams


def stats(this, last):
    def topic_counts(items):
        return Counter(t for i in items for t in i["topics"])

    def tag_counts(items):
        return Counter(t for i in items for t in i.get("tags", []))

    tc, tl = topic_counts(this), topic_counts(last)
    gc, gl = tag_counts(this), tag_counts(last)
    biz = lambda items: [i for i in items if DETAIL_TOPICS & set(i["topics"])]
    wc = Counter(g for i in biz(this) for g in terms(i["title"]))
    wl = Counter(g for i in biz(last) for g in terms(i["title"]))
    rising = sorted(((w, c, wl.get(w, 0)) for w, c in wc.items() if c >= 5),
                    key=lambda x: (x[1] + 1) / (x[2] + 1), reverse=True)
    # drop a unigram when a bigram containing it is listed just as strongly
    picked = []
    for w, c, l in rising:
        if " " not in w and any(w in p[0].split() and p[1] >= c * 0.8 for p in picked):
            continue
        picked.append((w, c, l))
        if len(picked) == 30:
            break
    top = [(w, c) for w, c in wc.most_common(60) if " " in w or c >= 10][:30]
    return {
        "topics": {t: [tc.get(t, 0), tl.get(t, 0)] for t in sorted(set(tc) | set(tl))},
        "tags": {t: [gc.get(t, 0), gl.get(t, 0)] for t in sorted(set(gc) | set(gl))},
        "rising": [{"term": w, "this": c, "last": l} for w, c, l in picked],
        "top": [{"term": w, "count": c} for w, c in top],
    }


BULLET = {"type": "object", "properties": {"text": {"type": "string"}, "refs": {"type": "array", "items": {"type": "integer"}}},
          "required": ["text", "refs"], "additionalProperties": False}
SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "trends": {"type": "array", "items": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "detail": {"type": "string"}, "numbers": {"type": "string"},
                           "topic": {"type": "string", "enum": sorted(DETAIL_TOPICS)},
                           "refs": {"type": "array", "items": {"type": "integer"}}},
            "required": ["title", "detail", "numbers", "topic", "refs"], "additionalProperties": False}},
        "funding": {"type": "array", "items": BULLET},
        "deals": {"type": "array", "items": BULLET},
        "iran": {"type": "array", "items": BULLET},
        "opportunities": {"type": "array", "items": BULLET},
    },
    "required": ["headline", "trends", "funding", "deals", "iran", "opportunities"],
    "additionalProperties": False,
}
SYSTEM = f"""You write a weekly trends report for an entrepreneur in Iran who wants to spot what is gaining momentum
in startups, fintech, AI, tech, big companies and the economy, in order to find business ideas.

You get counts (this week vs last week) and this week's headlines, numbered. Using only them:
- "headline": one sentence on the week.
- "trends": 6-10 trends that clearly grew this week. Each has a short "title", a "detail" of 2-3 sentences, and
  "numbers" quantifying it from the headlines (e.g. "12 AI legal-tech startups raised money, 4 more than last week",
  "funding stories up from 31 to 48"). Count from the headlines; never guess numbers.
- "funding": 3-6 bullets on the week's notable funding rounds and what sectors got money.
- "deals": 3-6 bullets on acquisitions, mergers, IPOs and valuations among big and mid-size companies.
- "iran": 3-5 bullets on the week's notable moves in Iran's startup, tech and business scene.
- "opportunities": 3-5 bullets on concrete openings these trends suggest for a business in Iran, each tied to a
  real company or deal in the headlines.
Every item cites the [numbers] of its headlines in "refs". Write in {LANG}; keep company and product names in Latin script."""


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set; skipping the weekly report.")
        return 0
    items = json.loads(ARCHIVE.read_text(encoding="utf-8"))["items"]
    now = datetime.now(timezone.utc)
    w1 = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    w2 = (now - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    this = [i for i in items if i["date"] >= w1]
    last = [i for i in items if w2 <= i["date"] < w1]
    st = stats(this, last)
    detail = [i for i in this if DETAIL_TOPICS & set(i["topics"])]
    lines = [f"[{n}] ({i['region']}/{','.join(i['topics'])}{' #' + ' #'.join(i['tags']) if i.get('tags') else ''}) "
             f"{i['source']}: {i['title']}" for n, i in enumerate(detail)]
    content = ("Counts per topic and tag, [this week, last week]:\n" + json.dumps({"topics": st["topics"], "tags": st["tags"]}) +
               "\n\nRising terms (this week vs last week):\n" + ", ".join(f"{r['term']} {r['this']}/{r['last']}" for r in st["rising"]) +
               ("\n\n(Last week has no data yet, so treat week-on-week comparisons as unavailable.)" if not last else "") +
               "\n\nThis week's headlines:\n" + "\n".join(lines))
    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=MODEL, max_tokens=16000, system=SYSTEM,
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": content}])
    if response.stop_reason != "end_turn":
        print(f"Weekly report not written: stop_reason={response.stop_reason}")
        return 1
    rep = json.loads("".join(b.text for b in response.content if b.type == "text"))

    def links(refs):
        return [{"title": detail[r]["title"], "link": detail[r]["link"], "source": detail[r]["source"]}
                for r in dict.fromkeys(refs) if 0 <= r < len(detail)]

    for t in rep["trends"]:
        t["links"] = links(t.pop("refs"))
    for k in ("funding", "deals", "iran", "opportunities"):
        rep[k] = [{"text": b["text"], "links": links(b["refs"])} for b in rep[k]]
    OUT.write_text(json.dumps({"generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "from": w1[:10], "to": now.strftime("%Y-%m-%d"),
                               "stories": len(this), "has_last_week": bool(last), "stats": st, "report": rep},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Weekly report written from {len(this)} stories ({len(detail)} business/tech headlines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
