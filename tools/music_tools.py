"""
Music control — Apple Music (default) + Spotify (optional).
Apple Music is always primary. Spotify only if user explicitly says "Spotify."
"""
from __future__ import annotations
import subprocess
import urllib.parse
import time


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _run(script: str, timeout: int = 10) -> str:
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0 and r.stderr.strip():
            return f"__error__: {r.stderr.strip()}"
        return r.stdout.strip()
    except subprocess.TimeoutExpired:
        return "__error__: AppleScript timed out"
    except Exception as exc:
        return f"__error__: {exc}"


def _open_url(url: str) -> None:
    subprocess.run(["open", url], capture_output=True, timeout=5)


def _spotify_running() -> bool:
    try:
        return subprocess.run(
            ["pgrep", "-x", "Spotify"],
            capture_output=True, timeout=2,
        ).returncode == 0
    except Exception:
        return False


def _music_running() -> bool:
    try:
        return subprocess.run(
            ["pgrep", "-x", "Music"],
            capture_output=True, timeout=2,
        ).returncode == 0
    except Exception:
        return False


def _spotify_installed() -> bool:
    import os
    return os.path.exists("/Applications/Spotify.app")


# ── Query normalisation ───────────────────────────────────────────────────────

_STRIP_WORDS = [
    "play some ", "play a little ", "play a bit of ", "play me some ",
    "play me ", "put on some ", "put on ", "i want to hear ", "i wanna hear ",
    " music", " songs", " tracks", " playlist", " stuff",
    "some ", "a little ", "a bit of ",
]

# Genre terms where the library TITLE/ARTIST search should be SKIPPED.
# These words appear in song titles for unrelated reasons (e.g. "On The House Top")
# so searching by name produces wrong results — genre-field search only.
_GENRE_TERMS = frozenset({
    "house", "techno", "jazz", "blues", "rock", "pop", "rap", "hip hop",
    "hiphop", "r&b", "rnb", "classical", "country", "electronic", "edm",
    "reggae", "metal", "punk", "folk", "indie", "alternative", "soul",
    "funk", "disco", "lofi", "lo fi", "lo-fi", "ambient", "trap", "drill",
    "afrobeats", "dancehall", "latin", "salsa", "kpop", "k-pop", "ska",
    "gospel", "opera", "new age", "instrumental", "acoustic", "grunge",
    "emo", "hardcore", "trance", "dubstep", "drum and bass", "dnb",
    "garage", "deep house", "tech house", "progressive", "synthwave",
    "vaporwave", "chillout", "downtempo", "swing", "big band", "bluegrass",
    "bossa nova", "flamenco", "celtic", "phonk", "pluggnb", "jersey club",
    "hyperpop", "bedroom pop", "shoegaze", "noise", "math rock",
    # multi-word genre combos
    "hip hop", "r and b", "drum and bass", "deep house", "tech house",
    "new age", "big band", "bossa nova",
})


def _clean_query(query: str) -> str:
    """
    Strip filler words so 'play some house music' → 'house'.
    This makes genre searches (library + iTunes) much more accurate.
    """
    q = query.lower().strip()
    for word in _STRIP_WORDS:
        q = q.replace(word, " ")
    return q.strip()


def _is_genre_query(clean_q: str) -> bool:
    """
    True if the cleaned query is a music genre term.
    Genre queries should ONLY match the genre metadata field in the library,
    never the song title/artist/album fields (e.g. 'house' in 'On The House Top').
    """
    return clean_q.lower() in _GENRE_TERMS


# ── Spotify playlist search (requires credentials in .env) ─────────────────────

def _spotify_search_playlist(query: str) -> str | None:
    """Returns a spotify:playlist:... URI or None."""
    import json, base64, urllib.request as ureq
    try:
        from config import SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET
    except ImportError:
        return None
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    try:
        creds = base64.b64encode(
            f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()
        ).decode()
        token_req = ureq.Request(
            "https://accounts.spotify.com/api/token",
            data=b"grant_type=client_credentials",
            headers={"Authorization": f"Basic {creds}",
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        with ureq.urlopen(token_req, timeout=6) as resp:
            token = json.loads(resp.read()).get("access_token")
        if not token:
            return None

        search_req = ureq.Request(
            "https://api.spotify.com/v1/search?" + urllib.parse.urlencode(
                {"q": query, "type": "playlist", "limit": 5}
            ),
            headers={"Authorization": f"Bearer {token}", "User-Agent": "Mozilla/5.0"},
        )
        with ureq.urlopen(search_req, timeout=6) as resp:
            data = json.loads(resp.read())

        items = data.get("playlists", {}).get("items", [])
        for item in items:
            if item and item.get("owner", {}).get("id") == "spotify":
                return item["uri"]
        for item in items:
            if item:
                return item["uri"]
    except Exception:
        pass
    return None


# ── iTunes catalog lookup (free, no auth) ─────────────────────────────────────

def _itunes_track_url(query: str) -> str | None:
    """
    Search iTunes API for a track matching the query.
    Returns an Apple Music deep-link URL or None.
    """
    import json, urllib.request as ureq
    try:
        url = (
            "https://itunes.apple.com/search?"
            + urllib.parse.urlencode({
                "term":   query,
                "entity": "musicTrack",
                "media":  "music",
                "limit":  25,
            })
        )
        req = ureq.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with ureq.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())

        results = data.get("results", [])
        if not results:
            return None

        # Pick from top 5 most relevant results so repeat plays vary
        import random as _random
        track = _random.choice(results[:min(5, len(results))])
        view_url = track.get("trackViewUrl", "")
        if view_url:
            # Convert https → music:// so it opens directly in Apple Music
            return view_url.replace("https://music.apple.com",
                                    "music://music.apple.com")
    except Exception:
        pass
    return None


# ── Main play function ────────────────────────────────────────────────────────

def play_music(query: str = "") -> str:
    """
    Play music. Default: Apple Music.
    Spotify used only when user explicitly says 'spotify'.
    """
    try:
        q = (query or "").strip()
        if _spotify_installed() and "spotify" in q.lower():
            return _play_spotify(q.lower().replace("spotify", "").strip())

        # Playlist shortcuts — these genres have no useful iTunes catalog results
        # but Dylan has playlists for them. Route directly.
        q_low = q.lower()
        _PLAYLIST_SHORTCUTS = {
            "house":        "House music",
            "house music":  "House music",
            "deep house":   "House music",
            "edm":          "House music",
            "rap":          "Good rap",
            "hip hop":      "Good rap",
            "hiphop":       "Good rap",
            "hip-hop":      "Good rap",
            "chill":        "Chill songs",
            "relaxing":     "Chill songs",
            "gym":          "gym",
            "workout":      "gym",
            "hype":         "Hype rap",
            "driving":      "driving",
            "drive":        "driving",
            "late night":   "Late night",
        }
        clean_q = _clean_query(q_low)
        for keyword, playlist in _PLAYLIST_SHORTCUTS.items():
            if clean_q == keyword or q_low == keyword or q_low.startswith(keyword + " "):
                from tools.playlist_tool import play_playlist
                return play_playlist(name=playlist, shuffle=True)

        return _play_apple_music(q)
    except Exception as exc:
        return f"Couldn't play that — ({exc})"


# ── Apple Music ───────────────────────────────────────────────────────────────

def _play_apple_music(query: str = "") -> str:
    """
    Play via Apple Music.

    Strategy order:
    1a. Library genre search  — AppleScript 'whose genre contains X'
    1b. Library name search   — AppleScript 'search playlist Library for X'
    2.  iTunes catalog lookup — find a real track URL → open it → play
    3.  Search page fallback  — open search, user taps play
    """
    if not query:
        _run('tell application "Music" to activate')
        _run('tell application "Music" to play')
        return "Resumed Apple Music."

    # Normalise: "play some house music" → "house"
    clean = _clean_query(query)
    is_genre = _is_genre_query(clean)
    esc_full  = query.replace("\\", "\\\\").replace('"', '\\"')
    esc_clean = clean.replace("\\", "\\\\").replace('"', '\\"')

    # ── Strategy 1a: Library GENRE field search ───────────────────────────────
    # 'whose genre contains X' searches the genre metadata field directly.
    # Always tried first — reliable for genre terms like "house", "jazz", etc.
    if esc_clean:
        result = _run(f'''
tell application "Music"
    activate
    set genreTracks to (tracks of playlist "Library" whose genre contains "{esc_clean}")
    if (count of genreTracks) > 0 then
        set shuffle enabled to true
        play (item 1 of genreTracks)
        return "genre:" & (count of genreTracks)
    end if
    return "empty"
end tell
''')
        if result.startswith("genre:"):
            count = result.split(":")[1]
            time.sleep(0.8)
            np = get_now_playing()
            return np if "Now playing:" in np else f"Shuffling {count} {clean} tracks."

    # ── Strategy 1b: Library name/artist/album search ─────────────────────────
    # SKIPPED for pure genre queries — "house" would match "On The House Top"
    # (a Christmas song), which is wrong. Genre queries use 1a + catalog only.
    if not is_genre:
        for search_term in ([esc_clean, esc_full] if esc_clean != esc_full else [esc_full]):
            if not search_term:
                continue
            result = _run(f'''
tell application "Music"
    activate
    set res to search playlist "Library" for "{search_term}"
    set n to count of res
    if n > 0 then
        set shuffleIdx to (random number from 1 to n)
        set shuffle enabled to true
        play (item shuffleIdx of res)
        return "ok:" & n
    end if
    return "empty"
end tell
''')
            if result.startswith("ok:"):
                time.sleep(1.2)
                np = get_now_playing()
                return np if "Now playing:" in np else f"Playing {query} from library."

    # ── Strategy 2: iTunes catalog → specific track deep link ────────────────
    # Do NOT call stop here — stop clears the queue so play resumes nothing.
    # Let Music stay in whatever state it's in; opening the URL will navigate it.

    catalog_url = _itunes_track_url(clean or query)
    if catalog_url:
        # Use AppleScript open location — more tightly integrated than subprocess open.
        # This tells Music.app directly to handle the URL rather than routing via macOS.
        _run(f'''
tell application "Music"
    activate
    open location "{catalog_url}"
end tell
''')
        # Poll up to 6s instead of a flat sleep — exit early if Music starts playing
        for _ in range(12):
            time.sleep(0.5)
            state = _run('tell application "Music" to get player state as string')
            if state and state not in ("stopped", "__error__"):
                np = get_now_playing()
                if "Now playing:" in np:
                    return np
                break

        # Attempt 1: plain play
        _run('''
tell application "Music"
    try
        play
    end try
end tell
''')
        time.sleep(1.0)
        np = get_now_playing()
        if "Now playing:" in np:
            return np

        # Attempt 2: play current track explicitly (handles when Music has the track
        # loaded but play didn't trigger from queue context)
        _run('''
tell application "Music"
    try
        play current track
    end try
end tell
''')
        time.sleep(1.0)
        np = get_now_playing()
        if "Now playing:" in np:
            return np

        # Attempt 3: Space keystroke via System Events (needs Accessibility permission)
        _run('''
try
    tell application "System Events"
        tell process "Music"
            keystroke " "
        end tell
    end tell
end try
''')
        time.sleep(1.5)
        np = get_now_playing()
        if "Now playing:" in np:
            return np

    # ── Strategy 3: Open search page — user taps play ─────────────────────────
    encoded = urllib.parse.quote(clean or query)
    _open_url(f"music://music.apple.com/search?term={encoded}")
    return f"Opened Apple Music search for '{clean or query}' — tap a result to play."


# ── Spotify ───────────────────────────────────────────────────────────────────

def _play_spotify(query: str = "") -> str:
    if not query:
        if _spotify_running():
            _run('tell application "Spotify" to play')
        else:
            subprocess.run(["open", "-a", "Spotify"], capture_output=True, timeout=5)
        return "Resumed Spotify."

    # Try playlist first (full queue)
    playlist_uri = _spotify_search_playlist(query)
    if playlist_uri:
        _open_url(playlist_uri)
        time.sleep(1.0)
        _run('tell application "Spotify" to play')
        return f"Playing Spotify playlist for: {query}"

    # Fall back to search
    _open_url("spotify:search:" + urllib.parse.quote(query))
    return f"Searching Spotify for: {query}"


# ── Controls ──────────────────────────────────────────────────────────────────

def pause_music() -> str:
    try:
        if _spotify_running():
            _run('tell application "Spotify" to pause')
            return "Paused."
        if _music_running():
            _run('tell application "Music" to pause')
            return "Paused."
        return "Nothing is playing."
    except Exception as exc:
        return f"Couldn't pause: {exc}"


def next_track() -> str:
    try:
        if _spotify_running():
            result = _run('tell application "Spotify" to next track')
            if result.startswith("__error__"):
                return f"Spotify didn't respond: {result}"
            return "Skipped."
        if _music_running():
            result = _run('tell application "Music" to next track')
            if result.startswith("__error__"):
                return f"Apple Music didn't respond: {result}"
            return "Skipped."
        return "Nothing is playing."
    except Exception as exc:
        return f"Couldn't skip: {exc}"


def previous_track() -> str:
    try:
        if _spotify_running():
            result = _run('tell application "Spotify" to previous track')
            if result.startswith("__error__"):
                return f"Spotify didn't respond: {result}"
            return "Back one."
        if _music_running():
            result = _run('tell application "Music" to previous track')
            if result.startswith("__error__"):
                return f"Apple Music didn't respond: {result}"
            return "Back one."
        return "Nothing is playing."
    except Exception as exc:
        return f"Couldn't go back: {exc}"


def get_now_playing() -> str:
    try:
        if _spotify_running():
            result = _run('''
tell application "Spotify"
    set t to name of current track
    set a to artist of current track
    return t & " — " & a
end tell''')
            if result.startswith("__error__"):
                return "Spotify is open but couldn't get track info."
            return f"Now playing: {result}" if result else "Spotify is open but nothing is playing."

        if _music_running():
            result = _run('''
tell application "Music"
    if player state is playing then
        set t to name of current track
        set a to artist of current track
        return t & " — " & a
    end if
    return ""
end tell''')
            if result.startswith("__error__"):
                return "Apple Music is open but couldn't get track info."
            return f"Now playing: {result}" if result else "Apple Music is open but nothing is playing."

        return "Nothing is playing."
    except Exception as exc:
        return f"Couldn't get now playing: {exc}"


# ── Volume ────────────────────────────────────────────────────────────────────

def set_volume(level: int) -> str:
    try:
        level = max(0, min(100, int(level)))
        # Set macOS system output volume
        r = subprocess.run(
            ["osascript", "-e", f"set volume output volume {level}"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return f"Couldn't set volume: {r.stderr.strip()}"
        # Also set Apple Music's internal volume slider (separate from system vol)
        subprocess.run(
            ["osascript", "-e",
             f'tell application "Music" to if it is running then set sound volume to {level}'],
            capture_output=True, timeout=5,
        )
        return f"Volume at {level}%."
    except Exception as exc:
        return f"Couldn't set volume: {exc}"


def mute_volume() -> str:
    try:
        subprocess.run(["osascript", "-e", "set volume with output muted"],
                       capture_output=True, timeout=5)
        subprocess.run(
            ["osascript", "-e",
             'tell application "Music" to if it is running then set sound volume to 0'],
            capture_output=True, timeout=5,
        )
        return "Muted."
    except Exception as exc:
        return f"Couldn't mute: {exc}"


def unmute_volume() -> str:
    try:
        subprocess.run(["osascript", "-e", "set volume without output muted"],
                       capture_output=True, timeout=5)
        subprocess.run(
            ["osascript", "-e",
             'tell application "Music" to if it is running then set sound volume to 80'],
            capture_output=True, timeout=5,
        )
        return "Unmuted."
    except Exception as exc:
        return f"Couldn't unmute: {exc}"
