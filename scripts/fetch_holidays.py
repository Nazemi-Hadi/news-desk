"""Free holiday and observance calendar (data/holidays.json) from Google's public holiday calendars.

Used by the World events page and the mini calendar; needs no API key.
"""
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "holidays.json"
DAYS_AHEAD = 120
CALENDARS = [
    ("en.ir#holiday@group.v.calendar.google.com", "iran", "Iran"),
    ("en.usa#holiday@group.v.calendar.google.com", "world", "United States"),
    ("en.uk#holiday@group.v.calendar.google.com", "world", "United Kingdom"),
    ("en.christian#holiday@group.v.calendar.google.com", "world", "Christian"),
]
EVENT = re.compile(r"BEGIN:VEVENT(.*?)END:VEVENT", re.S)


def field(block, name):
    m = re.search(rf"^{name}[^:\r\n]*:(.*)$", block, re.M)
    return m.group(1).strip().replace("\\,", ",") if m else ""


def main():
    today = datetime.now(timezone.utc).date()
    start, end = (today - timedelta(days=1)).strftime("%Y%m%d"), (today + timedelta(days=DAYS_AHEAD)).strftime("%Y%m%d")
    seen, out, errors = set(), [], {}
    for cal, region, label in CALENDARS:
        url = "https://calendar.google.com/calendar/ical/" + urllib.parse.quote(cal) + "/public/basic.ics"
        try:
            text = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30).read().decode("utf-8", "replace")
        except Exception as e:
            errors[label] = f"{type(e).__name__}: {str(e)[:120]}"
            continue
        for block in EVENT.findall(text):
            day = field(block, "DTSTART")[:8]
            title = field(block, "SUMMARY")
            if not (start <= day <= end) or not title or "Daylight Saving" in title:
                continue
            key = (day, title.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append({"date": f"{day[:4]}-{day[4:6]}-{day[6:]}", "end_date": "", "title": title, "category": "holiday",
                        "region": region, "importance": "high" if region == "iran" else "medium",
                        "why": f"Holiday / observance ({label})", "url": "", "official_holiday": region == "iran"})
    out.sort(key=lambda e: e["date"])
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                               "errors": errors, "events": out}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(out)} holidays saved", errors or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
