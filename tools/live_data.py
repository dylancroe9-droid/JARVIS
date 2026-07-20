"""
Live data — keyless real-time lookups: stock & crypto prices, live sports scores.

All use free, no-API-key public endpoints (Stooq, CoinGecko, ESPN). Every path
returns an honest message on failure — never a fabricated number.
"""
from __future__ import annotations

import json
import ssl
import urllib.parse
import urllib.request

_CTX = ssl.create_default_context()


def _get(url: str, timeout: int = 8) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "JARVIS/1.0 (personal assistant)"})
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return r.read().decode("utf-8", "replace")


def get_stock_price(symbol: str) -> str:
    """Current stock/ETF quote via Yahoo Finance (keyless). e.g. AAPL, TSLA, SPY."""
    sym = (symbol or "").strip().upper().lstrip("$")
    if not sym:
        return "Which ticker, sir?"
    try:
        data = json.loads(_get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}"))
        res = (data.get("chart") or {}).get("result") or []
        if not res:
            return f"Couldn't find a quote for {sym}, sir."
        meta = res[0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price is None:
            return f"No live quote for {sym} right now, sir."
        chg = (price - prev) / prev * 100 if prev else 0.0
        return f"{sym} is at ${price:,.2f} ({chg:+.1f}% on the day), sir."
    except Exception:
        return f"Couldn't pull {sym} right now, sir."


_CRYPTO_ALIASES = {
    "btc": "bitcoin", "eth": "ethereum", "sol": "solana", "doge": "dogecoin",
    "ada": "cardano", "xrp": "ripple", "bnb": "binancecoin", "ltc": "litecoin",
}

def get_crypto_price(coin: str) -> str:
    """Current crypto price via CoinGecko (keyless). e.g. bitcoin, eth, solana."""
    c = (coin or "").strip().lower()
    if not c:
        return "Which coin, sir?"
    cid = _CRYPTO_ALIASES.get(c, c)
    try:
        data = json.loads(_get(
            "https://api.coingecko.com/api/v3/simple/price?ids="
            f"{urllib.parse.quote(cid)}&vs_currencies=usd&include_24hr_change=true"))
        if cid not in data:
            return f"Couldn't find crypto '{coin}', sir."
        p = data[cid]["usd"]
        chg = data[cid].get("usd_24h_change")
        chg_s = f" ({chg:+.1f}% today)" if chg is not None else ""
        return f"{cid.title()} is ${p:,.2f}{chg_s}, sir."
    except Exception:
        return f"Couldn't pull {coin} right now, sir."


_LEAGUES = {
    "nba": ("basketball", "nba"), "wnba": ("basketball", "wnba"),
    "nfl": ("football", "nfl"), "cfb": ("football", "college-football"),
    "college football": ("football", "college-football"),
    "mlb": ("baseball", "mlb"), "nhl": ("hockey", "nhl"),
    "mls": ("soccer", "usa.1"), "epl": ("soccer", "eng.1"),
    "premier league": ("soccer", "eng.1"), "champions league": ("soccer", "uefa.champions"),
}

def get_sports_scores(query: str = "") -> str:
    """Live/recent scores via ESPN (keyless). query like 'nba', 'nfl', 'premier league'."""
    q = (query or "nba").strip().lower()
    sport = league = None
    for k, (s, l) in _LEAGUES.items():
        if k in q:
            sport, league = s, l
            break
    if not sport:
        sport, league = "basketball", "nba"
    try:
        data = json.loads(_get(
            f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"))
        events = data.get("events", [])[:6]
        if not events:
            return f"No {league.upper()} games on the board right now, sir."
        out = []
        for ev in events:
            try:
                comp = ev["competitions"][0]["competitors"]
                a, b = comp[0], comp[1]
                an = a["team"].get("abbreviation") or a["team"]["shortDisplayName"]
                bn = b["team"].get("abbreviation") or b["team"]["shortDisplayName"]
                status = ev["status"]["type"]["shortDetail"]
                out.append(f"{an} {a.get('score','0')}–{b.get('score','0')} {bn} ({status})")
            except (KeyError, IndexError):
                continue
        return " · ".join(out) if out else f"No {league.upper()} games right now, sir."
    except Exception:
        return "Couldn't pull scores right now, sir."
