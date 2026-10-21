"""
Streaming concierge — find where a movie/show is streaming and open it.

Keyless "where to watch" data via JustWatch (simple-justwatch-python-api).
Prioritises the services Dylan actually pays for, then opens the title's deep
link in his real (logged-in) Chrome, so it's one click — often zero — from play.

Design:
  • where_to_stream(title) → classify offers into yours / other-subscription / rent-buy
  • watch(title, and_play) → pick the best actionable option and open it, honestly
Never silently spends money: rent/buy just opens the page, it does not purchase.
"""
from __future__ import annotations

import os
import subprocess
import urllib.parse
from typing import Optional

_REGION = "US"
_LANG = "en"

# Canonical service name → substrings that appear in JustWatch package names.
_SERVICE_MATCH: dict[str, tuple[str, ...]] = {
    "Netflix":     ("netflix",),
    "Disney+":     ("disney",),
    "Peacock":     ("peacock",),
    "Prime Video": ("amazon prime video", "prime video"),
    "Max":         ("hbo", "max"),
    "Hulu":        ("hulu",),
    "Paramount+":  ("paramount",),
    "Apple TV+":   ("apple tv",),
}

# Per-service search URLs (fallback when there's no direct deep link).
_SEARCH_URLS: dict[str, str] = {
    "Netflix":     "https://www.netflix.com/search?q={q}",
    "Peacock":     "https://www.peacocktv.com/search?q={q}",
    "Prime Video": "https://www.amazon.com/s?k={q}&i=instant-video",
    "Disney+":     "https://www.disneyplus.com/search?q={q}",
    "Max":         "https://play.max.com/search?q={q}",
    "Hulu":        "https://www.hulu.com/search?q={q}",
    "Paramount+":  "https://www.paramountplus.com/search/?query={q}",
    "Apple TV+":   "https://tv.apple.com/search?term={q}",
}


def user_services() -> list[str]:
    """The subscriptions Dylan actually has. Override via STREAMING_SERVICES in .env."""
    raw = os.getenv("STREAMING_SERVICES", "Netflix,Disney+,Peacock,Prime Video")
    return [s.strip() for s in raw.split(",") if s.strip()]


def _service_for(package_name: str) -> Optional[str]:
    pn = (package_name or "").lower()
    for svc, keys in _SERVICE_MATCH.items():
        if any(k in pn for k in keys):
            return svc
    return None


def where_to_stream(title: str) -> dict:
    """
    Look up `title` and split its offers by how useful they are to Dylan:
      yours      — subscription offers on a service he HAS
      other_sub  — subscription offers on a service he does NOT have
      rent_buy   — rent/purchase offers (any service)
    Returns {"query", "match", "yours": [...], "other_sub": [...], "rent_buy": [...]}.
    Each entry is {"service", "url", optional "kind"}. Never raises.
    """
    out: dict = {"query": title, "match": None, "yours": [], "other_sub": [], "rent_buy": []}
    try:
        from simplejustwatchapi.justwatch import search
        results = search(title, _REGION, _LANG, 3, True)
    except Exception:
        return out
    if not results:
        return out

    top = results[0]
    yr = getattr(top, "release_year", None)
    out["match"] = f"{top.title} ({yr})" if yr else top.title

    mine = set(user_services())
    seen: set = set()
    for o in getattr(top, "offers", []) or []:
        svc = _service_for(getattr(o.package, "name", ""))
        if not svc:
            continue
        mon = (getattr(o, "monetization_type", "") or "").upper()
        url = getattr(o, "url", "") or ""
        key = (svc, mon)
        if key in seen:
            continue
        seen.add(key)
        entry: dict = {"service": svc, "url": url}
        if mon == "FLATRATE":
            (out["yours"] if svc in mine else out["other_sub"]).append(entry)
        elif mon in ("RENT", "BUY"):
            entry["kind"] = mon.lower()
            out["rent_buy"].append(entry)
    return out


def _open_in_chrome(url: str) -> bool:
    """Open a URL in Dylan's real Chrome (logged-in session). Falls back to default browser."""
    if not url:
        return False
    for cmd in (["open", "-a", "Google Chrome", url], ["open", url]):
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            continue
    return False


def _open_service_search(service: str, title: str) -> bool:
    tmpl = _SEARCH_URLS.get(service)
    q = urllib.parse.quote(title)
    url = tmpl.format(q=q) if tmpl else f"https://www.google.com/search?q={q}"
    return _open_in_chrome(url)


def watch(title: str, and_play: bool = False) -> str:
    """
    Find where `title` streams and open it — prioritising Dylan's own subscriptions.
    Returns a short spoken line for JARVIS. Honest when it's not on his services;
    never purchases anything (rent/buy only opens the page).
    """
    title = (title or "").strip()
    if not title:
        return "What are we watching, sir?"

    info = where_to_stream(title)
    label = info["match"] or title

    # 1) On a service he HAS (subscription) → open the deep link right at the title
    if info["yours"]:
        pick = info["yours"][0]
        if pick["url"] and _open_in_chrome(pick["url"]):
            verb = "Playing" if and_play else "Pulling up"
            return f"{verb} {label} on {pick['service']}, sir."
        if _open_service_search(pick["service"], label):
            return f"Pulling up {label} on {pick['service']}, sir."

    # 2) Rent/buy on a service he HAS → open the page (do NOT purchase)
    mine = set(user_services())
    rent_here = [r for r in info["rent_buy"] if r["service"] in mine]
    if rent_here:
        r = rent_here[0]
        if _open_in_chrome(r["url"]) or _open_service_search(r["service"], label):
            return (f"{label} isn't included in your subscriptions, sir — it's {r['kind']} "
                    f"on {r['service']}. I've opened the page; you can rent it there.")

    # 3) On a subscription he does NOT have → be honest, ask before going there
    if info["other_sub"]:
        svcs = ", ".join(dict.fromkeys(o["service"] for o in info["other_sub"]))
        return (f"{label} is streaming on {svcs}, sir — not one of your subscriptions. "
                f"Want me to open it anyway, or hunt for a rental?")

    # 4) Rent/buy anywhere else
    if info["rent_buy"]:
        r = info["rent_buy"][0]
        if _open_in_chrome(r["url"]) or _open_service_search(r["service"], label):
            return f"{label} is {r['kind']}-only, on {r['service']}, sir — opened the page."

    # 5) Nothing found → open a 'where to watch' search so it never dead-ends
    _open_in_chrome("https://www.google.com/search?q=" +
                    urllib.parse.quote(f"where to watch {label}"))
    return f"Couldn't pin down a stream for {label}, sir — opened a search to track it down."
