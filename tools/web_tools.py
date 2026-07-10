"""
Web tools — weather, Google search, YouTube. No API keys needed.
"""
import ssl
import subprocess
import urllib.request
import urllib.parse


def _build_ssl_ctx() -> ssl.SSLContext:
    """Build an SSL context that works on macOS python.org builds (missing system certs)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        # Last-resort: skip verification (better than a hard crash)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx


_SSL_CTX = _build_ssl_ctx()


_weather_cache: dict = {}   # {location_key: (timestamp, result)}
_WEATHER_TTL  = 600        # 10 minutes

def get_weather(location: str = "") -> str:
    """
    Get current weather + 3-day forecast using wttr.in (free, no API key).
    Results cached for 10 minutes so repeated queries are instant.
    """
    import json, time
    loc_key = (location.strip() or "Atlanta Georgia").lower()

    # Return cached result if fresh
    if loc_key in _weather_cache:
        ts, cached = _weather_cache[loc_key]
        if time.time() - ts < _WEATHER_TTL:
            return cached

    try:
        loc = urllib.parse.quote(location.strip() or "Atlanta Georgia")
        req = urllib.request.Request(
            f"https://wttr.in/{loc}?format=j1",
            headers={"User-Agent": "curl/7.68.0"},
        )
        with urllib.request.urlopen(req, timeout=8, context=_SSL_CTX) as resp:
            data = json.loads(resp.read())

        curr     = (data.get("current_condition") or [{}])[0]
        temp_f   = curr.get("temp_F", "?")
        feels_f  = curr.get("FeelsLikeF", "?")
        desc     = (curr.get("weatherDesc") or [{}])[0].get("value", "Unknown")
        humidity = curr.get("humidity", "?")
        wind     = curr.get("windspeedMiles", "?")
        uv       = curr.get("uvIndex", "?")

        area      = (data.get("nearest_area") or [{}])[0]
        area_name = (area.get("areaName") or [{}])[0].get("value", location or "your area")

        # Full 3-day forecast
        from datetime import datetime, timedelta
        forecast_lines = []
        weather_days = data.get("weather") or []
        for i, day in enumerate(weather_days[:3]):
            hi       = day.get("maxtempF", "?")
            lo       = day.get("mintempF", "?")
            hourly   = day.get("hourly") or [{}]
            # Get midday conditions
            mid      = hourly[4] if len(hourly) > 4 else (hourly[-1] if hourly else {})
            d_desc   = (mid.get("weatherDesc") or [{}])[0].get("value", "")
            rain_pct = mid.get("chanceofrain", "0")

            if i == 0:
                label = "Today"
            elif i == 1:
                label = "Tomorrow"
            else:
                label = (datetime.now() + timedelta(days=i)).strftime("%A")

            forecast_lines.append(
                f"{label}: {d_desc}, {lo}–{hi}°F, {rain_pct}% rain chance"
            )

        result = (
            f"Current ({area_name}): {desc}, {temp_f}°F (feels {feels_f}°F), "
            f"{humidity}% humidity, {wind}mph wind, UV index {uv}.\n"
            + "\n".join(forecast_lines)
        )

        _weather_cache[loc_key] = (time.time(), result)
        return result

    except Exception as exc:
        import sys
        print(f"[weather] fetch error: {exc}", file=sys.stderr)
        return "I couldn't pull up the weather right now — the service might be temporarily unavailable. Try again in a moment."


def get_forecast(location: str = "", days: int = 5) -> str:
    """
    Get a detailed multi-day forecast optimised for trip packing decisions.
    Returns temperatures, rain chance, wind, and packing recommendations.
    """
    import json
    try:
        loc = urllib.parse.quote(location.strip() or "Atlanta Georgia")
        req = urllib.request.Request(
            f"https://wttr.in/{loc}?format=j1",
            headers={"User-Agent": "curl/7.68.0"},
        )
        with urllib.request.urlopen(req, timeout=8, context=_SSL_CTX) as resp:
            data = json.loads(resp.read())

        area      = (data.get("nearest_area") or [{}])[0]
        area_name = (area.get("areaName") or [{}])[0].get("value", location or "your area")
        country   = (area.get("country") or [{}])[0].get("value", "")

        from datetime import datetime, timedelta
        lines = [f"Forecast for {area_name}{', ' + country if country else ''}:\n"]

        weather_days = (data.get("weather") or [])[:min(days, 3)]
        all_highs, all_lows, rain_chances = [], [], []

        for i, day in enumerate(weather_days):
            hi      = int(day.get("maxtempF", 70))
            lo      = int(day.get("mintempF", 55))
            hourly  = day.get("hourly") or [{}]
            mid     = hourly[4] if len(hourly) > 4 else (hourly[-1] if hourly else {})
            d_desc  = (mid.get("weatherDesc") or [{}])[0].get("value", "")
            rain    = int(mid.get("chanceofrain", 0))
            wind    = mid.get("windspeedMiles", "?")
            humidity = mid.get("humidity", "?")

            all_highs.append(hi)
            all_lows.append(lo)
            rain_chances.append(rain)

            if i == 0:
                label = "Today"
            elif i == 1:
                label = "Tomorrow"
            else:
                label = (datetime.now() + timedelta(days=i)).strftime("%A, %b %d")

            lines.append(
                f"{label}: {d_desc}, {lo}–{hi}°F, wind {wind}mph, "
                f"{humidity}% humidity, {rain}% rain"
            )

        # Packing recommendations
        avg_hi  = sum(all_highs) / len(all_highs) if all_highs else 70
        avg_lo  = sum(all_lows)  / len(all_lows)  if all_lows  else 55
        max_rain = max(rain_chances) if rain_chances else 0

        lines.append("\nPacking considerations:")
        if avg_hi > 80:
            lines.append("• Hot — light clothing, shorts, breathable fabrics, sunscreen")
        elif avg_hi > 65:
            lines.append("• Mild — light layers, jeans or chinos, a light jacket for evenings")
        elif avg_hi > 50:
            lines.append("• Cool — layers, a proper jacket, warmer options for nights")
        else:
            lines.append("• Cold — heavy coat, thermal layers, gloves, hat")

        if avg_lo < 50:
            lines.append("• Nights get cold — bring something warm even if days are mild")
        if max_rain > 50:
            lines.append("• High rain chance — bring a waterproof jacket or umbrella")
        elif max_rain > 25:
            lines.append("• Some rain possible — light rain jacket wouldn't hurt")

        return "\n".join(lines)

    except Exception as exc:
        import sys
        print(f"[forecast] fetch error: {exc}", file=sys.stderr)
        return "I couldn't pull up the forecast right now. Try again in a moment."


def _open_in_browser(url: str) -> str:
    """
    Open a URL in Chrome if available, otherwise system default browser.
    Uses macOS `open` command — most reliable approach.
    """
    import os

    # Try Chrome first (explicit -a flag is most reliable on macOS)
    chrome_paths = [
        "/Applications/Google Chrome.app",
        os.path.expanduser("~/Applications/Google Chrome.app"),
    ]
    if any(os.path.exists(p) for p in chrome_paths):
        try:
            result = subprocess.run(
                ["open", "-a", "Google Chrome", url],
                capture_output=True, timeout=8,
            )
            if result.returncode == 0:
                return "chrome"
        except Exception:
            pass

    # Try Arc, Brave, Firefox in order
    for name in ["Arc", "Brave Browser", "Firefox"]:
        app_path = f"/Applications/{name}.app"
        if os.path.exists(app_path):
            try:
                result = subprocess.run(
                    ["open", "-a", name, url],
                    capture_output=True, timeout=8,
                )
                if result.returncode == 0:
                    return name.lower()
            except Exception:
                continue

    # Absolute fallback — macOS default browser, never fails
    try:
        subprocess.run(["open", url], timeout=8)
        return "default"
    except Exception as exc:
        return f"error: {exc}"


# Known sites — when Dylan says "pull up X", go straight to the real domain
# instead of Googling the word. Maps short names → URL.
_KNOWN_SITES = {
    "youtube":         "https://www.youtube.com",
    "youtube music":   "https://music.youtube.com",
    "yt":              "https://www.youtube.com",
    "gmail":           "https://mail.google.com",
    "google mail":     "https://mail.google.com",
    "google drive":    "https://drive.google.com",
    "drive":           "https://drive.google.com",
    "google docs":     "https://docs.google.com",
    "docs":            "https://docs.google.com",
    "google sheets":   "https://sheets.google.com",
    "sheets":          "https://sheets.google.com",
    "google calendar": "https://calendar.google.com",
    "google maps":     "https://maps.google.com",
    "maps":            "https://maps.google.com",
    "instagram":       "https://www.instagram.com",
    "insta":           "https://www.instagram.com",
    "ig":              "https://www.instagram.com",
    "snapchat":        "https://web.snapchat.com",
    "snap":            "https://web.snapchat.com",
    "tiktok":          "https://www.tiktok.com",
    "twitter":         "https://x.com",
    "x":               "https://x.com",
    "reddit":          "https://www.reddit.com",
    "facebook":        "https://www.facebook.com",
    "messenger":       "https://www.messenger.com",
    "discord":         "https://discord.com/app",
    "twitch":          "https://www.twitch.tv",
    "amazon":          "https://www.amazon.com",
    "ebay":            "https://www.ebay.com",
    "netflix":         "https://www.netflix.com",
    "hulu":            "https://www.hulu.com",
    "disney plus":     "https://www.disneyplus.com",
    "disney+":         "https://www.disneyplus.com",
    "max":             "https://www.max.com",
    "hbo":             "https://www.max.com",
    "hbo max":         "https://www.max.com",
    "peacock":         "https://www.peacocktv.com",
    "peacock tv":      "https://www.peacocktv.com",
    "prime video":     "https://www.primevideo.com",
    "amazon prime":    "https://www.primevideo.com",
    "paramount plus":  "https://www.paramountplus.com",
    "paramount+":      "https://www.paramountplus.com",
    "apple tv":        "https://tv.apple.com",
    "crunchyroll":     "https://www.crunchyroll.com",
    "espn plus":       "https://plus.espn.com",
    "spotify":         "https://open.spotify.com",
    "soundcloud":      "https://soundcloud.com",
    "apple music":     "https://music.apple.com",
    "github":          "https://github.com",
    "linkedin":        "https://www.linkedin.com",
    "chatgpt":         "https://chatgpt.com",
    "claude":          "https://claude.ai",
    "perplexity":      "https://www.perplexity.ai",
    "wikipedia":       "https://www.wikipedia.org",
    "stackoverflow":   "https://stackoverflow.com",
    "stack overflow":  "https://stackoverflow.com",
    "canvas":          "https://canvas.instructure.com",
    "blackboard":      "https://blackboard.com",
    "weather":         "https://weather.com",
    "espn":            "https://www.espn.com",
    "venmo":           "https://venmo.com",
    "paypal":          "https://www.paypal.com",
    "robinhood":       "https://robinhood.com",
}


def _resolve_known_site(query: str) -> str | None:
    """
    Match query against the known-sites map. Strips leading verbs
    ('open', 'pull up', 'go to', 'launch') so "pull up youtube" works.
    Returns the URL or None.
    """
    import re
    q = query.strip().lower().rstrip(".!?,")
    # Strip leading verb phrases — anything left should be a bare site name
    q = re.sub(
        r"^(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?"
        r"(?:pull\s+up|open|go\s+to|launch|bring\s+up|navigate\s+to|"
        r"show\s+me|take\s+me\s+to|fire\s+up|start\s+up)\s+",
        "", q,
    )
    q = re.sub(r"^(?:the|my|some)\s+", "", q)
    q = q.strip()
    # Exact match
    if q in _KNOWN_SITES:
        return _KNOWN_SITES[q]
    # Bare domain ("youtube.com") → use it directly
    if re.fullmatch(r"[a-z0-9-]+\.[a-z]{2,}(?:/.*)?", q):
        return "https://" + q
    return None


def web_search(query: str) -> str:
    # Known-sites shortcut: "pull up youtube" → youtube.com, not Google search.
    site_url = _resolve_known_site(query)
    if site_url:
        result = _open_in_browser(site_url)
        if result.startswith("error"):
            return f"Couldn't open browser: {result}"
        return f"Opened {site_url}"
    url = "https://www.google.com/search?q=" + urllib.parse.quote(query)
    result = _open_in_browser(url)
    if result.startswith("error"):
        return f"Couldn't open browser: {result}"
    return f"Opened Google search for: {query}"


def _youtube_scrape(query: str):
    """
    Scrape YouTube's own search HTML to pull out the first real video ID + title.
    Used as last resort when all Invidious instances are down — so JARVIS never
    dumps the user on the search page without actually opening a video.
    Returns (video_id, title) or None.
    """
    import re
    try:
        url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query)
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=8, context=_SSL_CTX) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        # YouTube embeds structured data in the page source as JSON
        ids    = re.findall(r'"videoId"\s*:\s*"([a-zA-Z0-9_-]{11})"', html)
        titles = re.findall(
            r'"title"\s*:\s*\{"runs"\s*:\s*\[\{"text"\s*:\s*"([^"]+)"', html
        )
        for vid_id, title in zip(ids, titles):
            if len(vid_id) == 11:
                return vid_id, title
        if ids:
            return ids[0], query
    except Exception:
        pass
    return None


def youtube_search(query: str) -> str:
    """
    Search YouTube and open the best matching video directly (never just the search page).

    Strategy:
    1. Parse creator hints in the query ("by MrBeast", "from Zach D Films").
    2. Try 7 Invidious public API instances; score results by channel match if creator found.
    3. Fall back to scraping YouTube HTML to extract a real video ID.
    4. Open the search page only as the absolute last resort.
    """
    import json, re

    # Detect creator hints so we can prefer their channel
    creator_hint: str | None = None
    for pattern in [
        r'\bby\s+([A-Za-z][A-Za-z0-9 ]{1,28}?)(?:\s+(?:about|on|video|channel)|$)',
        r'\bfrom\s+([A-Za-z][A-Za-z0-9 ]{1,28}?)(?:\s+(?:about|on|video|channel)|$)',
        r'([A-Za-z][A-Za-z0-9 ]{2,28}?)\s+(?:channel|youtube)',
    ]:
        m = re.search(pattern, query, re.I)
        if m:
            creator_hint = m.group(1).strip()
            break

    # Invidious public instances — more = better uptime resilience
    instances = [
        "https://inv.nadeko.net",
        "https://invidious.nerdvpn.de",
        "https://invidious.privacydev.net",
        "https://yt.cdaut.de",
        "https://invidious.incogniweb.net",
        "https://iv.melmac.space",
        "https://invidious.fdn.fr",
    ]

    for instance in instances:
        try:
            api_url = (
                f"{instance}/api/v1/search"
                + "?q=" + urllib.parse.quote(query)
                + "&type=video&fields=videoId,title,author"
            )
            req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5, context=_SSL_CTX) as resp:
                results = json.loads(resp.read())

            if not (results and isinstance(results, list)):
                continue

            # If creator was mentioned, prefer a result from their channel
            if creator_hint:
                hint_lo = creator_hint.lower()
                channel_match = next(
                    (r for r in results
                     if hint_lo in r.get("author", "").lower() and r.get("videoId")),
                    None,
                )
                if channel_match:
                    vid   = channel_match["videoId"]
                    title = channel_match.get("title", query)
                    _open_in_browser(f"https://www.youtube.com/watch?v={vid}")
                    return f"Opening: {title}"

            # Take the first valid result
            first = next((r for r in results if r.get("videoId")), None)
            if first:
                _open_in_browser(f"https://www.youtube.com/watch?v={first['videoId']}")
                return f"Opening: {first.get('title', query)}"

        except Exception:
            continue

    # All Invidious instances failed — scrape YouTube directly
    scraped = _youtube_scrape(query)
    if scraped:
        vid_id, title = scraped
        _open_in_browser(f"https://www.youtube.com/watch?v={vid_id}")
        return f"Opening: {title}"

    # True last resort — open the search results page
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query)
    _open_in_browser(url)
    return f"Opened YouTube search for: {query}"


def web_search_results(query: str, max_results: int = 5) -> str:
    """
    Fetch real search results and extract useful text — Perplexity-style.
    Uses DuckDuckGo HTML search (no API key) then scrapes top pages for content.
    """
    import json, re
    snippets = []

    # Step 1: DuckDuckGo instant answer (fast, often has direct answer)
    try:
        ddg_url = (
            "https://api.duckduckgo.com/?q="
            + urllib.parse.quote(query)
            + "&format=json&no_html=1&skip_disambig=1"
        )
        req = urllib.request.Request(ddg_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6, context=_SSL_CTX) as resp:
            data = json.loads(resp.read())
        if data.get("AbstractText"):
            snippets.append(f"[Direct answer] {data['AbstractText'][:500]}")
        for topic in (data.get("RelatedTopics") or [])[:3]:
            if isinstance(topic, dict) and topic.get("Text"):
                snippets.append(f"• {topic['Text'][:200]}")
    except Exception:
        pass

    # Step 2: DuckDuckGo HTML search for page links + snippets
    try:
        html_url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
        req = urllib.request.Request(
            html_url,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                   "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"},
        )
        with urllib.request.urlopen(req, timeout=8, context=_SSL_CTX) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        # Extract snippets from search result entries
        result_blocks = re.findall(
            r'class="result__snippet"[^>]*>(.*?)</a>',
            html, re.DOTALL
        )
        for block in result_blocks[:max_results]:
            clean = re.sub(r"<[^>]+>", "", block).strip()
            clean = re.sub(r"\s+", " ", clean)
            if clean and len(clean) > 30:
                snippets.append(f"• {clean[:300]}")
    except Exception:
        pass

    if snippets:
        return f"Search results for '{query}':\n" + "\n".join(snippets[:max_results + 1])
    return f"No results found for: {query}"


def deep_research(query: str) -> str:
    """
    Multi-step research like Perplexity Pro — searches, fetches multiple pages,
    and returns synthesized content for Claude to summarize.
    """
    import re
    results = []

    # Get search results
    try:
        html_url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
        req = urllib.request.Request(
            html_url,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                   "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"},
        )
        with urllib.request.urlopen(req, timeout=8, context=_SSL_CTX) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        # Extract URLs from results
        urls = re.findall(r'class="result__url"[^>]*>(.*?)</a>', html)
        urls = [u.strip() for u in urls if u.strip()][:4]

        # Extract snippets
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)
        for s in snippets[:5]:
            clean = re.sub(r"<[^>]+>", "", s).strip()
            clean = re.sub(r"\s+", " ", clean)
            if clean:
                results.append(clean[:400])
    except Exception as exc:
        return f"Research unavailable: {exc}"

    # Fetch content from top result pages
    fetched = 0
    for url in urls[:3]:
        if not url.startswith("http"):
            url = "https://" + url
        try:
            page_req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(page_req, timeout=6) as resp:
                page_html = resp.read().decode("utf-8", errors="ignore")

            # Strip tags, get readable text
            text = re.sub(r"<script[^>]*>.*?</script>", "", page_html, flags=re.DOTALL)
            text = re.sub(r"<style[^>]*>.*?</style>",  "", text, flags=re.DOTALL)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > 200:
                results.append(f"[Source: {url[:60]}]\n{text[:800]}")
                fetched += 1
        except Exception:
            continue

    if not results:
        return f"Could not retrieve research on: {query}"

    return (
        f"RESEARCH: {query}\n\n"
        + "\n\n---\n\n".join(results)
        + f"\n\n[{fetched} sources fetched, {len(snippets if 'snippets' in dir() else [])} snippets]"
    )


def get_wikipedia_summary(query: str, sentences: int = 12) -> str:
    """
    Fetch a comprehensive Wikipedia summary for any topic.
    Free, accurate, no API key needed. Used before show_overlay to get real data
    for timelines, history, science, biographies, and blueprints.
    """
    import json as _json

    try:
        # Step 1: Search Wikipedia for best matching article
        search_url = (
            "https://en.wikipedia.org/w/api.php?action=query&list=search"
            "&srsearch=" + urllib.parse.quote(query) +
            "&format=json&srlimit=3&srprop=snippet"
        )
        req = urllib.request.Request(
            search_url,
            headers={"User-Agent": "JARVIS/1.0 (contact@jarvis.ai)"},
        )
        with urllib.request.urlopen(req, timeout=7, context=_SSL_CTX) as resp:
            search_data = _json.loads(resp.read())

        results = search_data.get("query", {}).get("search", [])
        if not results:
            return f"No Wikipedia article found for: {query}"

        page_title = results[0]["title"]

        # Step 2: Fetch the full article extract
        extract_url = (
            "https://en.wikipedia.org/w/api.php?action=query"
            "&prop=extracts&explaintext=1&exsectionformat=plain"
            "&exsentences=" + str(min(sentences * 4, 60)) +
            "&titles=" + urllib.parse.quote(page_title) +
            "&format=json"
        )
        req2 = urllib.request.Request(
            extract_url,
            headers={"User-Agent": "JARVIS/1.0 (contact@jarvis.ai)"},
        )
        with urllib.request.urlopen(req2, timeout=8) as resp:
            extract_data = _json.loads(resp.read())

        pages = extract_data.get("query", {}).get("pages", {})
        page  = next(iter(pages.values()))
        text  = page.get("extract", "").strip()

        if not text:
            return f"Wikipedia article '{page_title}' returned no text."

        # Return first ~4000 chars — enough for 7-panel overlay with real detail
        return f"[Wikipedia: {page_title}]\n\n{text[:4500]}"

    except Exception as exc:
        return f"Wikipedia unavailable: {exc}"


def open_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    result = _open_in_browser(url)
    if result.startswith("error"):
        return f"Couldn't open URL: {result}"
    return f"Opened: {url}"
