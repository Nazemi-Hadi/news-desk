"""Write data/events.json: important upcoming world and Iran events, researched by Claude with web search.

Runs weekly. Step 1 researches with the web search tool; step 2 turns the notes into structured JSON.
Needs ANTHROPIC_API_KEY. DIGEST_LANG sets the language of titles and notes (default Persian).
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "events.json"
MODEL = "claude-opus-5-5"
LANG = os.environ.get("DIGEST_LANG", "Persian (Farsi)")
DAYS_AHEAD = 45
FALLBACK = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

CATEGORIES = ["politics", "economy", "markets", "tech", "startups", "sports", "culture", "holiday"]
SCHEMA = {
    "type": "object",
    "properties": {
        "events": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD, or empty for a one-day event"},
                "title": {"type": "string"},
                "category": {"type": "string", "enum": CATEGORIES},
                "region": {"type": "string", "enum": ["world", "iran"]},
                "importance": {"type": "string", "enum": ["high", "medium"]},
                "why": {"type": "string"},
                "url": {"type": "string"},
            },
            "required": ["date", "end_date", "title", "category", "region", "importance", "why", "url"],
            "additionalProperties": False,
        }},
    },
    "required": ["events"],
    "additionalProperties": False,
}


def research(client, start, end):
    prompt = f"""Today is {start}. Research the important scheduled events between {start} and {end} that a
business-minded reader in Iran, who follows startups, AI, fintech, the economy, industry, politics and European
football, should know about. Use web search to confirm dates; do not rely on memory for dates.

Cover:
- politics: elections, major summits (G7, G20, UN, EU, BRICS, OPEC+, COP), key votes and deadlines, nuclear talks
- economy and markets: central bank decisions (Fed, ECB, Bank of England, Bank of Japan), major data releases,
  big-company earnings (Apple, Nvidia, Microsoft, Alphabet, Amazon, Meta, Tesla), IPOs
- tech and startups: major product events and conferences (e.g. Web Summit, big AI launches announced in advance),
  important fintech/payment events
- sports: the biggest European football fixtures (Champions League, El Clasico, Premier League top games), finals
- culture and ceremonies: award ceremonies (e.g. Nobel prizes), major international days and festivals
- holidays: Iranian official holidays and occasions (Persian calendar) and major world holidays in the window

For each event give the exact date (and end date if multi-day), what it is, why it matters, and a source URL.
List about 30-50 events, most important first within each category."""
    messages = [{"role": "user", "content": prompt}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 20}]
    for _ in range(6):  # continue while the server pauses a long tool-use turn
        with client.beta.messages.stream(model=MODEL, max_tokens=32000, tools=tools,
                                         output_config={"effort": "medium"}, messages=messages,
                                         **FALLBACK) as stream:
            response = stream.get_final_message()
        if response.stop_reason != "pause_turn":
            break
        messages = messages[:1] + [{"role": "assistant", "content": response.content}]
    if response.stop_reason not in ("end_turn", "max_tokens"):
        raise RuntimeError(f"research stopped: {response.stop_reason}")
    return "".join(b.text for b in response.content if b.type == "text")


def structure(client, notes, start, end):
    system = (f"Convert research notes into a list of events between {start} and {end}. Keep only events whose date "
              f"the notes state; drop anything uncertain or outside the window. Write \"title\" and \"why\" in {LANG}, "
              "keeping names of people, companies, teams and events in their usual Latin spelling where natural. "
              "\"why\" is one short sentence. \"importance\" is high only for events with broad impact. "
              "Use the source URL from the notes, or an empty string if none.")
    response = client.beta.messages.create(
        model=MODEL, max_tokens=16000, system=system,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": notes}], **FALLBACK)
    if response.stop_reason != "end_turn":
        raise RuntimeError(f"structuring stopped: {response.stop_reason}")
    return json.loads("".join(b.text for b in response.content if b.type == "text"))["events"]


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set; skipping events.")
        return 0
    now = datetime.now(timezone.utc)
    start, end = now.strftime("%Y-%m-%d"), (now + timedelta(days=DAYS_AHEAD)).strftime("%Y-%m-%d")
    client = anthropic.Anthropic()
    notes = research(client, start, end)
    events = structure(client, notes, start, end)
    events = sorted((e for e in events if start <= e["date"] <= end), key=lambda e: e["date"])
    OUT.write_text(json.dumps({"generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "from": start, "to": end,
                               "lang": LANG, "events": events}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(events)} events written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
