"""Fetch Iran open-market rates (Bonbast) and world markets (TradingView) into data/markets.json.

Bonbast quotes are in toman; the page converts to rial. Bonbast has no daily change, so it is
computed against our own snapshot from about a day earlier (kept in the file's "history").
"""
import http.cookiejar
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "markets.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
HISTORY_DAYS = 8

# (bonbast key, label, unit note). "<key>1" is sell, "<key>2" is buy; coins use "<key>" and "<key>2".
IRAN = [
    ("Currencies (open market)", "fx", [
        ("usd", "US Dollar", ""), ("eur", "Euro", ""), ("gbp", "British Pound", ""),
        ("aed", "UAE Dirham", ""), ("try", "Turkish Lira", ""), ("cny", "Chinese Yuan", ""),
        ("cad", "Canadian Dollar", ""), ("aud", "Australian Dollar", ""), ("chf", "Swiss Franc", ""),
        ("sar", "Saudi Riyal", ""), ("kwd", "Kuwaiti Dinar", ""), ("jpy", "Japanese Yen", "per 10"),
    ]),
    ("Gold coins", "coin", [
        ("emami1", "Emami coin", ""), ("azadi1", "Bahar Azadi coin", ""), ("azadi1_2", "Half Azadi", ""),
        ("azadi1_4", "Quarter Azadi", ""), ("azadi1g", "Gram coin", ""),
    ]),
    ("Gold", "single", [
        ("gol18", "18k gold", "per gram"), ("mithqal", "Mithqal", "per mithqal"),
    ]),
]
EXTRA = [("ounce", "Gold ounce", "USD"),
         ("bitcoin", "Bitcoin", "USD")]

WORLD = [
    ("Oil & energy", [("ICEEUR:BRN1!", "Brent crude (ICE)"), ("NYMEX:CL1!", "WTI crude (NYMEX)"),
                      ("NYMEX:NG1!", "Natural gas (Henry Hub)")]),
    ("Metals", [("TVC:GOLD", "Gold"), ("TVC:SILVER", "Silver"), ("COMEX:HG1!", "Copper")]),
    ("Stock indices", [("SP:SPX", "S&P 500"), ("NASDAQ:NDX", "Nasdaq 100"), ("DJ:DJI", "Dow Jones"),
                       ("TVC:UKX", "FTSE 100"), ("XETR:DAX", "DAX"), ("TVC:NI225", "Nikkei 225")]),
    ("Big tech stocks", [("NASDAQ:NVDA", "Nvidia"), ("NASDAQ:AAPL", "Apple"), ("NASDAQ:MSFT", "Microsoft"),
                         ("NASDAQ:GOOGL", "Alphabet"), ("NASDAQ:AMZN", "Amazon"), ("NASDAQ:META", "Meta"),
                         ("NASDAQ:TSLA", "Tesla")]),
    ("Currencies, rates & crypto", [("TVC:DXY", "US Dollar Index"), ("FX:EURUSD", "EUR / USD"),
                                    ("TVC:US10Y", "US 10Y yield %"), ("BITSTAMP:BTCUSD", "Bitcoin"),
                                    ("BITSTAMP:ETHUSD", "Ethereum")]),
]


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_bonbast():
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    page = opener.open(urllib.request.Request("https://www.bonbast.com/", headers={"User-Agent": UA}), timeout=25)
    html = page.read().decode("utf-8", "replace")
    token = re.search(r'param:\s*"([^"]+)"', html)
    if not token:
        raise RuntimeError("Bonbast page layout changed (no token)")
    req = urllib.request.Request(
        "https://www.bonbast.com/json",
        data=urllib.parse.urlencode({"param": token.group(1)}).encode(),
        headers={"User-Agent": UA, "Referer": "https://www.bonbast.com/", "X-Requested-With": "XMLHttpRequest",
                 "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    data = json.load(opener.open(req, timeout=25))
    if "usd1" not in data:
        raise RuntimeError(f"Unexpected Bonbast reply: {str(data)[:120]}")
    return data


def fetch_tse():
    """Tehran Stock Exchange and Fara Bourse indices from tablokhani.com's public endpoint."""
    req = urllib.request.Request("https://api.tablokhani.com/public/market-indices", headers={
        "User-Agent": UA, "Accept": "application/json", "Origin": "https://tablokhani.com",
        "Referer": "https://tablokhani.com/"})
    data = json.load(urllib.request.urlopen(req, timeout=25))["data"]

    def n(v):
        return num(str(v).replace(",", "")) if v is not None else None

    b, f = data["bourse"], data["fara_bourse"]
    return {
        "source": "tablokhani.com", "fetched": now_iso(), "state": b.get("state"),
        "tedpix": n(b.get("index")), "tedpix_change": n(b.get("index_change")),
        "tedpix_pct": n(b.get("index_change_percent")),
        "equal": n(b.get("index_h")), "equal_pct": n(b.get("index_h_change_percent")),
        "trade_value": b.get("trade_value"), "trade_number": b.get("trade_number"),
        "market_value": b.get("market_value"),
        "fara": n(f.get("index")), "fara_pct": n(f.get("index_change_percent")),
        "fara_trade_value": f.get("trade_value"),
    }


def fetch_tradingview():
    tickers = [t for _, rows in WORLD for t, _ in rows]
    body = json.dumps({"symbols": {"tickers": tickers}, "columns": ["close", "change", "currency"]}).encode()
    req = urllib.request.Request("https://scanner.tradingview.com/global/scan", data=body,
                                 headers={"User-Agent": UA, "Content-Type": "application/json"})
    data = json.load(urllib.request.urlopen(req, timeout=25))
    return {row["s"]: row["d"] for row in data.get("data", [])}


def day_ago_snapshot(history):
    """The snapshot closest to 24h ago (at least 18h old), or None."""
    target = datetime.now(timezone.utc) - timedelta(hours=24)
    best = None
    for snap in history:
        t = datetime.fromisoformat(snap["t"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - t < timedelta(hours=18):
            continue
        if best is None or abs(t - target) < abs(best[0] - target):
            best = (t, snap)
    return best[1] if best else None


GIANTS = [
    ("NASDAQ:NVDA", "Nvidia"), ("NASDAQ:AAPL", "Apple"), ("NASDAQ:GOOGL", "Alphabet"), ("NASDAQ:MSFT", "Microsoft"),
    ("NASDAQ:AMZN", "Amazon"), ("NYSE:TSM", "TSMC"), ("NASDAQ:META", "Meta"), ("TADAWUL:2222", "Saudi Aramco"),
    ("NASDAQ:AVGO", "Broadcom"), ("NASDAQ:TSLA", "Tesla"), ("NYSE:LLY", "Eli Lilly"), ("NYSE:BRK.B", "Berkshire Hathaway"),
    ("KRX:005930", "Samsung"), ("NASDAQ:WMT", "Walmart"), ("NYSE:JPM", "JPMorgan"), ("NYSE:V", "Visa"),
    ("NYSE:XOM", "ExxonMobil"), ("NASDAQ:ASML", "ASML"), ("HKEX:700", "Tencent"), ("NYSE:ORCL", "Oracle"),
    ("NASDAQ:NFLX", "Netflix"), ("NYSE:BABA", "Alibaba"), ("NYSE:SAP", "SAP"),
]


def fetch_giants():
    body = json.dumps({"symbols": {"tickers": [t for t, _ in GIANTS]},
                       "columns": ["close", "change", "market_cap_basic", "currency"]}).encode()
    req = urllib.request.Request("https://scanner.tradingview.com/global/scan", data=body,
                                 headers={"User-Agent": UA, "Content-Type": "application/json"})
    rows = {r["s"]: r["d"] for r in json.load(urllib.request.urlopen(req, timeout=25)).get("data", [])}
    out = []
    for sym, name in GIANTS:
        d = rows.get(sym)
        if d and d[2]:
            out.append({"sym": sym, "name": name, "price": d[0], "change": round(d[1], 2) if d[1] is not None else None,
                        "cap": d[2], "currency": d[3]})
    return sorted(out, key=lambda r: r["cap"], reverse=True)


def main():
    old = {}
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
        except ValueError:
            old = {}
    history = old.get("history", [])
    errors = {}

    iran = old.get("iran")
    try:
        b = fetch_bonbast()
        prev = (day_ago_snapshot(history) or {}).get("v", {})
        snap, groups = {}, []
        for title, kind, rows in IRAN:
            out_rows = []
            for key, label, note in rows:
                if kind == "fx":
                    sell, buy = num(b.get(key + "1")), num(b.get(key + "2"))
                elif kind == "coin":
                    sell, buy = num(b.get(key)), num(b.get(key + "2"))
                else:
                    sell, buy = num(b.get(key)), None
                if sell is None:
                    continue
                snap[key] = sell
                p = prev.get(key)
                out_rows.append({"key": key, "label": label, "note": note, "sell": sell, "buy": buy,
                                 "change": round((sell - p) / p * 100, 2) if p else None})
            groups.append({"title": title, "rows": out_rows})
        extra = []
        for key, label, note in EXTRA:
            v = num(b.get(key))
            if v is None:
                continue
            snap[key] = v
            p = prev.get(key)
            extra.append({"key": key, "label": label, "note": note, "value": v,
                          "change": round((v - p) / p * 100, 2) if p else None})
        iran = {"source": "Bonbast", "unit": "toman", "updated": b.get("last_modified"), "fetched": now_iso(),
                "groups": groups, "extra": extra}
        history.append({"t": now_iso(), "v": snap})
    except Exception as e:
        errors["bonbast"] = f"{type(e).__name__}: {str(e)[:150]}"

    giants = old.get("giants")
    try:
        giants = fetch_giants()
    except Exception as e:
        errors["giants"] = f"{type(e).__name__}: {str(e)[:150]}"

    tse = old.get("tse")
    try:
        tse = fetch_tse()
    except Exception as e:
        errors["tablokhani"] = f"{type(e).__name__}: {str(e)[:150]}"

    world = old.get("world")
    try:
        tv = fetch_tradingview()
        groups = []
        for title, rows in WORLD:
            out_rows = []
            for sym, label in rows:
                d = tv.get(sym)
                if not d or d[0] is None:
                    continue
                out_rows.append({"sym": sym, "label": label, "price": d[0],
                                 "change": round(d[1], 2) if d[1] is not None else None, "currency": d[2]})
            groups.append({"title": title, "rows": out_rows})
        world = {"source": "TradingView", "fetched": now_iso(), "groups": groups}
    except Exception as e:
        errors["tradingview"] = f"{type(e).__name__}: {str(e)[:150]}"

    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    history = [h for h in history if h["t"] >= cutoff]
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"generated": now_iso(), "errors": errors, "iran": iran, "tse": tse, "world": world, "giants": giants,
                               "history": history}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print("markets saved", errors or "OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
