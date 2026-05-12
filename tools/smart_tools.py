"""
Smart utility tools — translation, unit conversion, definitions, news, focus mode.
"""
from __future__ import annotations
import subprocess, json, urllib.parse, urllib.request as ureq


def translate_text(text: str, target_language: str = "Spanish") -> str:
    """Translate text using MyMemory free API (no key needed, 5000 chars/day free)."""
    try:
        params = urllib.parse.urlencode({"q": text, "langpair": f"en|{_lang_code(target_language)}"})
        url = f"https://api.mymemory.translated.net/get?{params}"
        req = ureq.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with ureq.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        translated = data.get("responseData", {}).get("translatedText", "")
        if translated and translated.lower() != text.lower():
            return f"{translated}"
        return f"Translation unavailable for: {text}"
    except Exception as exc:
        return f"Translation failed: {exc}"


def _lang_code(lang: str) -> str:
    codes = {
        "spanish": "es", "french": "fr", "german": "de", "italian": "it",
        "portuguese": "pt", "japanese": "ja", "chinese": "zh", "korean": "ko",
        "arabic": "ar", "russian": "ru", "hindi": "hi", "dutch": "nl",
    }
    return codes.get(lang.lower(), lang[:2].lower())


def convert_units(value: float, from_unit: str, to_unit: str) -> str:
    """Convert between common units."""
    conversions = {
        # Length
        ("miles", "km"): 1.60934, ("km", "miles"): 0.621371,
        ("feet", "meters"): 0.3048, ("meters", "feet"): 3.28084,
        ("inches", "cm"): 2.54, ("cm", "inches"): 0.393701,
        ("yards", "meters"): 0.9144, ("meters", "yards"): 1.09361,
        # Weight
        ("lbs", "kg"): 0.453592, ("kg", "lbs"): 2.20462,
        ("oz", "grams"): 28.3495, ("grams", "oz"): 0.035274,
        # Temperature handled separately
        # Volume
        ("gallons", "liters"): 3.78541, ("liters", "gallons"): 0.264172,
        ("cups", "ml"): 236.588, ("ml", "cups"): 0.00422675,
        # Speed
        ("mph", "kph"): 1.60934, ("kph", "mph"): 0.621371,
    }
    f, t = from_unit.lower().rstrip("s"), to_unit.lower().rstrip("s")
    # Temperature special cases
    if f in ("f", "fahrenheit") and t in ("c", "celsius"):
        result = (value - 32) * 5/9
        return f"{value}°F = {result:.1f}°C"
    if f in ("c", "celsius") and t in ("f", "fahrenheit"):
        result = value * 9/5 + 32
        return f"{value}°C = {result:.1f}°F"
    # Try both plural and singular
    for (fu, tu), factor in conversions.items():
        if f in (fu, fu.rstrip("s")) and t in (tu, tu.rstrip("s")):
            result = value * factor
            return f"{value} {from_unit} = {result:.4g} {to_unit}"
    return f"Don't know how to convert {from_unit} to {to_unit}."


def define_word(word: str) -> str:
    """Get word definition using Free Dictionary API."""
    try:
        url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(word)}"
        req = ureq.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with ureq.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())
        if not data or not isinstance(data, list):
            return f"No definition found for '{word}'."
        entry = data[0]
        meanings = entry.get("meanings", [])
        result = []
        for meaning in meanings[:2]:
            pos = meaning.get("partOfSpeech", "")
            defs = meaning.get("definitions", [])
            if defs:
                result.append(f"{pos}: {defs[0]['definition']}")
                if defs[0].get("example"):
                    result.append(f'  e.g. "{defs[0]["example"]}"')
        return "\n".join(result) if result else f"No definition found for '{word}'."
    except Exception as exc:
        return f"Definition unavailable: {exc}"


def get_news(topic: str = "", count: int = 5) -> str:
    """Get news headlines using RSS feed from Google News (no API key)."""
    try:
        q = urllib.parse.quote(topic or "top news")
        url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
        req = ureq.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with ureq.urlopen(req, timeout=8) as resp:
            content = resp.read().decode("utf-8", errors="replace")
        # Parse RSS titles
        import re
        titles = re.findall(r"<title>(.*?)</title>", content)
        # Skip first (it's the feed title) and clean HTML entities
        headlines = []
        for t in titles[1:count+1]:
            t = t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
            t = re.sub(r" - .*$", "", t)  # Remove "- Source Name" suffix
            if t:
                headlines.append(f"• {t}")
        if headlines:
            label = f"Top news{' about ' + topic if topic else ''}:"
            return label + "\n" + "\n".join(headlines)
        return "Couldn't fetch news right now."
    except Exception as exc:
        return f"News unavailable: {exc}"


def set_focus_mode(enabled: bool, minutes: int = 0) -> str:
    """Enable/disable macOS Focus/Do Not Disturb mode."""
    try:
        if enabled:
            # Enable Do Not Disturb using macOS shortcuts
            result = subprocess.run(
                ["shortcuts", "run", "Turn On Do Not Disturb"],
                capture_output=True, timeout=5
            )
            if result.returncode != 0:
                # Fallback: use defaults command
                subprocess.run(
                    ["defaults", "-currentHost", "write",
                     "com.apple.notificationcenterui", "doNotDisturb", "-boolean", "true"],
                    capture_output=True, timeout=5
                )
                subprocess.run(["killall", "NotificationCenter"], capture_output=True, timeout=3)
            return f"Focus mode on{f' for {minutes} minutes' if minutes else ''}. Notifications silenced."
        else:
            result = subprocess.run(
                ["shortcuts", "run", "Turn Off Do Not Disturb"],
                capture_output=True, timeout=5
            )
            if result.returncode != 0:
                subprocess.run(
                    ["defaults", "-currentHost", "write",
                     "com.apple.notificationcenterui", "doNotDisturb", "-boolean", "false"],
                    capture_output=True, timeout=5
                )
                subprocess.run(["killall", "NotificationCenter"], capture_output=True, timeout=3)
            return "Focus mode off. Notifications restored."
    except Exception as exc:
        return f"Couldn't toggle focus mode: {exc}"


def random_number(min_val: int = 1, max_val: int = 100) -> str:
    """Generate a random number."""
    import random
    n = random.randint(min_val, max_val)
    return f"{n}"


def coin_flip() -> str:
    """Flip a coin."""
    import random
    result = random.choice(["Heads", "Tails"])
    return result


def get_public_ip() -> str:
    """Get the current public IP address."""
    try:
        with ureq.urlopen("https://api.ipify.org", timeout=5) as resp:
            return f"Your public IP: {resp.read().decode()}"
    except Exception:
        return "Couldn't fetch IP."


def get_word_count(text: str) -> str:
    """Count words, characters, sentences in text."""
    words = len(text.split())
    chars = len(text)
    sentences = text.count('.') + text.count('!') + text.count('?')
    reading_time = max(1, round(words / 200))
    return f"{words} words, {chars} chars, ~{sentences} sentences, ~{reading_time} min read"


def ping_site(url: str) -> str:
    """Check if a website is up."""
    import time
    try:
        if not url.startswith("http"):
            url = "https://" + url
        start = time.time()
        with ureq.urlopen(url, timeout=8) as resp:
            ms = int((time.time() - start) * 1000)
            return f"{url} is up — responded in {ms}ms (status {resp.status})"
    except Exception as exc:
        return f"{url} appears to be down or unreachable: {exc}"
