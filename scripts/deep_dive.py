"""Deep-dive research on one business idea; the report is added to data/reports.json.

Runs from the "Research this" button (a workflow_dispatch). IDEA_ID names an idea in data/ideas.json;
IDEA_TEXT is the reader's own idea in their words (it is also added to the Idea bank).
Step 1 researches with web search; step 2 turns the notes into a structured report.
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parent.parent
IDEAS = ROOT / "data" / "ideas.json"
REPORTS = ROOT / "data" / "reports.json"
MODEL = "claude-opus-5-5"
LANG = os.environ.get("DIGEST_LANG", "Persian (Farsi)")
FALLBACK = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

S = {"type": "string"}
NAMED = {"type": "array", "items": {"type": "object", "properties": {"name": S, "what": S, "url": S},
                                    "required": ["name", "what", "url"], "additionalProperties": False}}
SCHEMA = {
    "type": "object",
    "properties": {
        "title": S,
        "summary": S,
        "examples_abroad": NAMED,
        "competitors_iran": NAMED,
        "competitors_global": NAMED,
        "market": {"type": "object", "properties": {"iran": S, "global": S, "trend": S},
                   "required": ["iran", "global", "trend"], "additionalProperties": False},
        "business_model": S,
        "startup_cost": {"type": "object", "properties": {
            "total": S,
            "items": {"type": "array", "items": {"type": "object", "properties": {"item": S, "cost": S},
                                                 "required": ["item", "cost"], "additionalProperties": False}}},
            "required": ["total", "items"], "additionalProperties": False},
        "regulation": {"type": "array", "items": S},
        "risks": {"type": "array", "items": S},
        "first_steps": {"type": "array", "items": {"type": "object", "properties": {"step": S, "detail": S},
                                                   "required": ["step", "detail"], "additionalProperties": False}},
        "verdict": S,
        "sources": {"type": "array", "items": {"type": "object", "properties": {"title": S, "url": S},
                                               "required": ["title", "url"], "additionalProperties": False}},
    },
    "required": ["title", "summary", "examples_abroad", "competitors_iran", "competitors_global", "market",
                 "business_model", "startup_cost", "regulation", "risks", "first_steps", "verdict", "sources"],
    "additionalProperties": False,
}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(path, key, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))[key]
        except (ValueError, KeyError):
            pass
    return default


def describe(idea):
    parts = [f"Idea: {idea['title']}"]
    for k, label in (("what", "What it does"), ("example_company", "Example abroad"), ("example", "What they did"),
                     ("why_now", "Why now"), ("iran_angle", "Iran angle")):
        if idea.get(k):
            parts.append(f"{label}: {idea[k]}")
    for l in idea.get("links", []):
        parts.append(f"Source headline: {l['title']} ({l['link']})")
    return "\n".join(parts)


def research(client, brief):
    prompt = f"""You are helping an entrepreneur in Iran decide whether to build this business. Research it
thoroughly with web search, then write detailed notes.

{brief}

Find and report, with sources and figures wherever possible:
1. Real companies abroad already doing this - what they do, when they launched, funding, traction.
2. Competitors and near-substitutes already operating in Iran (search in Persian too, e.g. on Cafe Bazaar,
   Iranian tech news such as Digiato, Zoomit, Peivast, TechRasa) and the strongest global players.
3. Market size: Iran (users, spending, or a reasoned estimate with stated assumptions) and global, plus the trend.
4. A workable business model for Iran.
5. A realistic startup cost estimate for a lean first version in Iran, itemised (team, development, hosting,
   licences, marketing), in toman and approximate US dollars at today's open-market rate.
6. Regulation and licences in Iran (e.g. Central Bank / Shaparak rules for payments, Enamad, sector regulators),
   and sanctions or infrastructure constraints.
7. Main risks.
8. The first concrete steps for the next 90 days.
9. An honest verdict."""
    messages = [{"role": "user", "content": prompt}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 25},
             {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 10}]
    for _ in range(6):  # continue while the server pauses a long tool-use turn
        with client.beta.messages.stream(model=MODEL, max_tokens=48000, tools=tools,
                                         output_config={"effort": "high"}, messages=messages,
                                         **FALLBACK) as stream:
            response = stream.get_final_message()
        if response.stop_reason != "pause_turn":
            break
        messages = messages[:1] + [{"role": "assistant", "content": response.content}]
    if response.stop_reason not in ("end_turn", "max_tokens"):
        raise RuntimeError(f"research stopped: {response.stop_reason}")
    return "".join(b.text for b in response.content if b.type == "text")


def structure(client, notes):
    system = (f"Turn these research notes into a structured report. Write every text field in {LANG}; keep names of "
              "companies, products and people in their usual Latin spelling where natural. Keep figures and their "
              "sources; do not add facts that are not in the notes. Use empty strings or empty lists where the notes "
              "have nothing. Use full URLs from the notes in url fields.")
    response = client.beta.messages.create(
        model=MODEL, max_tokens=24000, system=system,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": notes}], **FALLBACK)
    if response.stop_reason != "end_turn":
        raise RuntimeError(f"structuring stopped: {response.stop_reason}")
    return json.loads("".join(b.text for b in response.content if b.type == "text"))


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set; cannot research.")
        return 1
    ideas = load(IDEAS, "ideas", [])
    idea_id, text = os.environ.get("IDEA_ID", "").strip(), os.environ.get("IDEA_TEXT", "").strip()
    if idea_id:
        idea = next((i for i in ideas if i["id"] == idea_id), None)
        if not idea:
            print(f"No idea with id {idea_id}")
            return 1
    elif text:
        idea = {"id": hashlib.sha1(text.encode()).hexdigest()[:10], "title": text[:200], "what": text,
                "area": "mine", "date": now_iso()[:10], "links": [], "custom": True}
        if idea["id"] not in {i["id"] for i in ideas}:
            ideas.insert(0, idea)
            IDEAS.write_text(json.dumps({"ideas": ideas}, ensure_ascii=False, indent=1), encoding="utf-8")
    else:
        print("Nothing to research: set IDEA_ID or IDEA_TEXT")
        return 1

    client = anthropic.Anthropic()
    report = structure(client, research(client, describe(idea)))
    report.update({"id": idea["id"], "generated": now_iso(), "lang": LANG})
    reports = load(REPORTS, "reports", {})
    reports[idea["id"]] = report
    REPORTS.write_text(json.dumps({"reports": reports}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Report written for {idea['id']}: {report['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
