"""Write the daily brief (data/digest.json) from the last day of data/news.json using Claude.

Needs ANTHROPIC_API_KEY in the environment. Set DIGEST_LANG=English for an English brief.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parent.parent
NEWS = ROOT / "data" / "news.json"
OUT = ROOT / "data" / "digest.json"
MODEL = "claude-opus-5-5"
LANG = os.environ.get("DIGEST_LANG", "Persian (Farsi)")
WINDOW_HOURS = 26

SECTIONS = {
    "startups_world": "Startups, new companies, funding rounds and new products - outside Iran",
    "startups_iran": "Startups, new companies, new products and investment - Iran",
    "fintech_world": "Fintech, payments, digital banking and crypto - outside Iran",
    "fintech_iran": "Fintech, payments, digital banking and crypto - Iran",
    "ai": "AI: notable launches, companies, research, policy (world and Iran)",
    "tech": "Technology beyond AI (world and Iran)",
    "industry": "Industry, manufacturing, energy, supply chains (world and Iran)",
    "economy_world": "World economy and markets",
    "economy_iran": "Iran economy, markets, currency, business",
    "politics_world": "World politics",
    "politics_iran": "Iran politics and foreign relations",
    "football": "European football: results, transfers, key stories",
}

BULLET = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "refs": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["text", "refs"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "top": {"type": "array", "items": BULLET},
        "sections": {
            "type": "object",
            "properties": {k: {"type": "array", "items": BULLET} for k in SECTIONS},
            "required": list(SECTIONS),
            "additionalProperties": False,
        },
        "ideas": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "area": {"type": "string", "enum": ["general", "fintech", "ai", "tech", "industry"]},
                "title": {"type": "string"},
                "example_company": {"type": "string"},
                "example": {"type": "string"},
                "what": {"type": "string"},
                "why_now": {"type": "string"},
                "iran_angle": {"type": "string"},
                "refs": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["area", "title", "example_company", "example", "what", "why_now", "iran_angle", "refs"],
            "additionalProperties": False,
        }},
        "watch": {"type": "string"},
    },
    "required": ["headline", "top", "sections", "ideas", "watch"],
    "additionalProperties": False,
}

SYSTEM = f"""You write a daily morning news brief for one reader who wants to stay on top of new companies,
new business ideas, startups, AI, the economy, industry and politics - in Iran and worldwide, kept separate.

You get a numbered list of headlines from the last day. Using only that list:
- "headline": one sentence capturing the day.
- "top": the 4-6 most important stories of the day across everything.
- "sections": for each section, 3-8 bullets. The reader cares most about the two "new_*" sections: name each new
  company/product, say what it does, who backs it and how much was raised when the headlines say so. Skip
  routine items (price ticks, minor sports/celebrity, duplicates). Merge stories covered by several sources into
  one bullet citing all of them. A section may be empty if nothing in the list fits.
- "ideas": the reader wants to start a business in Iran by copying proven models from abroad. Give 4-7 ideas, and
  include at least 2 in fintech. Every idea MUST be anchored in a real company outside Iran that a headline in
  this list reports as having just launched, released, raised money for, or expanded something. Never propose an
  idea without such a headline, and never invent a company or a detail. For each idea:
  "area" (general, fintech, ai, tech or industry); "example_company" (that company's name);
  "example" (what that company did, per the headline - product, funding amount, market - and that it is recent);
  "title" (the business idea for Iran); "what" the Iranian version would do; "why_now" (why the timing is right);
  "iran_angle" (how it fits Iran's market, with practical constraints such as sanctions, payments, licences or
  regulation). "refs" must include the headline about the example company.
- "watch": 1-3 sentences on a trend or signal worth watching, drawn from the list.
- Every bullet's "refs" lists the [numbers] of the headlines it is based on. Never invent facts beyond the headlines.

Write all text in {LANG}. Keep company, product and people names in their original Latin spelling.
Bullets are one or two short sentences each."""


def build_input(items):
    lines = []
    for n, it in enumerate(items):
        summ = "" if it["topics"][0] in ("politics", "football") else it.get("summary", "")[:180]
        tags = f" #{' #'.join(it['tags'])}" if it.get("tags") else ""
        lines.append(f"[{n}] ({it['region']}/{','.join(it['topics'])}{tags}) {it['source']}: {it['title']}"
                     + (f" - {summ}" if summ else ""))
    return "\n".join(lines)


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set; skipping the daily brief.")
        return 0
    news = json.loads(NEWS.read_text(encoding="utf-8"))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    items = [i for i in news["items"] if i["date"] >= cutoff]
    if not items:
        print("No recent items; skipping.")
        return 0

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        system=SYSTEM,
        messages=[{"role": "user", "content": f"Section keys:\n{json.dumps(SECTIONS, indent=1)}\n\nHeadlines:\n{build_input(items)}"}],
    )
    if response.stop_reason != "end_turn":
        print(f"Brief not written: stop_reason={response.stop_reason}")
        return 1
    text = "".join(b.text for b in response.content if b.type == "text")
    brief = json.loads(text)

    def resolve(bullet):
        links = [{"title": items[r]["title"], "link": items[r]["link"], "source": items[r]["source"]}
                 for r in dict.fromkeys(bullet["refs"]) if 0 <= r < len(items)]
        return {"text": bullet["text"], "links": links}

    brief["top"] = [resolve(b) for b in brief["top"]]
    brief["sections"] = {k: [resolve(b) for b in v] for k, v in brief["sections"].items()}
    for idea in brief["ideas"]:
        idea["links"] = resolve({"text": "", "refs": idea.pop("refs")})["links"]
    OUT.write_text(json.dumps({
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": response.model,
        "lang": LANG,
        "items_considered": len(items),
        "brief": brief,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    u = response.usage
    print(f"Brief written from {len(items)} items; tokens in={u.input_tokens} out={u.output_tokens}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
