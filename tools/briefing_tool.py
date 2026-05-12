"""
Daily briefing — combines weather, calendar, and top news into one fast digest.
Called when Dylan asks "what's my day look like", "morning briefing", etc.
"""


def get_daily_briefing() -> str:
    """
    Pull weather + today's calendar + top headlines in one shot.
    Returns a combined string JARVIS can summarize into a spoken briefing.
    """
    from datetime import datetime
    parts = []
    now   = datetime.now()

    # ── Time context ──────────────────────────────────────────────────────────
    parts.append(f"TIME: {now.strftime('%A, %B %d at %I:%M %p')}")

    # ── Weather ───────────────────────────────────────────────────────────────
    try:
        from tools.web_tools import get_weather
        parts.append("WEATHER: " + get_weather())
    except Exception as exc:
        parts.append(f"WEATHER: unavailable ({exc})")

    # ── Calendar — today only ─────────────────────────────────────────────────
    try:
        from tools.calendar_tool import get_calendar_events
        cal = get_calendar_events(days=1)
        parts.append("CALENDAR TODAY: " + cal)
    except Exception as exc:
        parts.append(f"CALENDAR: unavailable ({exc})")

    # ── Unread email count ────────────────────────────────────────────────────
    try:
        import subprocess
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "Mail" to count (messages of inbox whose read status is false)'],
            capture_output=True, text=True, timeout=8,
        )
        if r.returncode == 0 and r.stdout.strip().isdigit():
            count = int(r.stdout.strip())
            parts.append(f"UNREAD EMAILS: {count}")
    except Exception:
        pass

    # ── Top news headlines ────────────────────────────────────────────────────
    try:
        from tools.smart_tools import get_news
        news = get_news(count=3)
        parts.append("TOP NEWS: " + news)
    except Exception as exc:
        parts.append(f"NEWS: unavailable ({exc})")

    return "\n\n".join(parts)


def get_location() -> str:
    """
    Get approximate current location using CoreLocation via a Swift one-liner,
    falling back to IP geolocation if that's unavailable.
    """
    import subprocess, json, urllib.request

    # Try Swift CoreLocation (accurate, but requires Location Services permission)
    swift = (
        "import CoreLocation\n"
        "import Foundation\n"
        "let mgr = CLLocationManager()\n"
        "mgr.requestWhenInUseAuthorization()\n"
        "let loc = mgr.location\n"
        "if let l = loc { print(\"\\(l.coordinate.latitude),\\(l.coordinate.longitude)\") } "
        "else { print(\"unavailable\") }"
    )
    try:
        r = subprocess.run(["swift", "-"], input=swift, capture_output=True,
                           text=True, timeout=8)
        out = r.stdout.strip()
        if out and out != "unavailable" and "," in out:
            lat, lon = out.split(",")
            return f"Location: {lat.strip()},{lon.strip()} (GPS)"
    except Exception:
        pass

    # Fall back to IP-based geolocation
    try:
        req = urllib.request.Request(
            "https://ipapi.co/json/",
            headers={"User-Agent": "curl/7.68.0"},
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())
        city    = data.get("city", "")
        region  = data.get("region", "")
        country = data.get("country_name", "")
        lat     = data.get("latitude", "")
        lon     = data.get("longitude", "")
        return f"{city}, {region}, {country} ({lat},{lon})"
    except Exception as exc:
        return f"Location unavailable: {exc}"
