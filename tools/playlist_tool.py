"""
Apple Music playlist management via AppleScript.
- List playlists
- Play a playlist (with shuffle)
- Add current song or a search result to a playlist
- Create a new playlist
- Remove a song from a playlist
"""
from __future__ import annotations
import subprocess


def _run(script: str, timeout: int = 12) -> str:
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


def _esc(s: str) -> str:
    """Escape a string for use inside AppleScript double quotes."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ── Music taste profiling ────────────────────────────────────────────────────

def scan_music_taste(per_playlist_limit: int = 10) -> str:
    """
    Read Dylan's playlists, sample tracks from each, build a music profile.
    Returns a string summary suitable to save to memory.txt.
    """
    res = _run('''
tell application "Music"
    set output to ""
    set pls to (every user playlist whose special kind is none)
    repeat with pl in pls
        set output to output & (name of pl) & "\\n"
    end repeat
    return output
end tell
''')
    if res.startswith("__error__"):
        return f"Couldn't list playlists: {res}"
    playlists = [l.strip() for l in res.splitlines() if l.strip()]

    all_artists: dict[str, int] = {}
    playlist_summary: list[str] = []

    for pl_name in playlists:
        if "Untitled" in pl_name or "Test" in pl_name:
            continue
        tracks_raw = _run(f'''
tell application "Music"
    set output to ""
    set i to 0
    repeat with t in tracks of user playlist "{_esc(pl_name)}"
        if i ≥ {per_playlist_limit} then exit repeat
        set output to output & (artist of t) & "|" & (name of t) & "
"
        set i to i + 1
    end repeat
    return output
end tell
''', timeout=20)
        if tracks_raw.startswith("__error__"):
            continue
        local_counts: dict[str, int] = {}
        for ln in tracks_raw.splitlines():
            if "|" not in ln:
                continue
            artist, _ = ln.split("|", 1)
            artist = artist.strip()
            if not artist:
                continue
            all_artists[artist] = all_artists.get(artist, 0) + 1
            local_counts[artist] = local_counts.get(artist, 0) + 1
        top3 = sorted(local_counts.items(), key=lambda x: -x[1])[:3]
        if top3:
            playlist_summary.append(
                f"  {pl_name}: " + ", ".join(a for a, _ in top3)
            )

    top10 = sorted(all_artists.items(), key=lambda x: -x[1])[:10]
    lines = [
        "DYLAN'S MUSIC TASTE (auto-scanned from Apple Music):",
        "Top artists across all playlists: "
        + ", ".join(a for a, _ in top10) if top10 else "(no data)",
        "",
        "Per-playlist signature:",
        *playlist_summary,
    ]
    return "\n".join(lines)


# ── List ──────────────────────────────────────────────────────────────────────

def list_playlists() -> str:
    """Return all user-created playlists."""
    result = _run('''
tell application "Music"
    set output to ""
    set pls to (every playlist whose special kind is none)
    repeat with pl in pls
        set output to output & (name of pl) & "\\n"
    end repeat
    return output
end tell
''')
    if result.startswith("__error__"):
        return f"Couldn't get playlists: {result}"
    names = [l.strip() for l in result.splitlines() if l.strip()]
    if not names:
        return "No playlists found."
    return "Your playlists: " + ", ".join(names)


# ── Play ──────────────────────────────────────────────────────────────────────

def play_playlist(name: str, shuffle: bool = True) -> str:
    """
    Play a playlist by name. Matching priority:
      1. EXACT case-insensitive match (so "rock" → "Rock", not "Chill rock")
      2. Substring fallback (so "iron" → "Iron Man Workout")
    Also stops current playback first to prevent the previous playlist
    from resuming after a small playlist finishes.
    """
    esc_name = _esc(name.lower())
    result = _run(f'''
tell application "Music"
    activate
    set matchedPL to missing value
    set pls to (every playlist whose special kind is none)
    -- PASS 1: exact name match (case-insensitive)
    repeat with pl in pls
        set n to name of pl
        set nLower to (do shell script "echo " & quoted form of n & " | tr A-Z a-z")
        if nLower is equal to "{esc_name}" then
            set matchedPL to pl
            exit repeat
        end if
    end repeat
    -- PASS 2: substring fallback only if exact failed
    if matchedPL is missing value then
        repeat with pl in pls
            set n to name of pl
            set nLower to (do shell script "echo " & quoted form of n & " | tr A-Z a-z")
            if nLower contains "{esc_name}" then
                set matchedPL to pl
                exit repeat
            end if
        end repeat
    end if
    if matchedPL is missing value then
        return "not found"
    end if
    -- Stop current playback so the previous playlist doesn't resume later
    stop
    set shuffle enabled to {str(shuffle).lower()}
    play matchedPL
    delay 1
    if player state is playing then
        set t to name of current track
        set a to artist of current track
        return "playing:" & (name of matchedPL) & "|" & t & " — " & a
    end if
    return "playing:" & (name of matchedPL) & "|unknown"
end tell
''')

    if result == "not found":
        return f"No playlist matching '{name}' — say 'list my playlists' to see them."
    if result.startswith("__error__"):
        return f"Couldn't play playlist: {result}"
    if result.startswith("playing:"):
        parts = result[8:].split("|", 1)
        pl_name = parts[0]
        track_info = parts[1] if len(parts) > 1 else ""
        shuffle_str = " (shuffled)" if shuffle else ""
        if track_info and track_info != "unknown":
            return f"Playing '{pl_name}'{shuffle_str} — now on: {track_info}"
        return f"Playing '{pl_name}'{shuffle_str}."
    return result


# ── Add songs ─────────────────────────────────────────────────────────────────

def add_to_playlist(playlist_name: str, song: str = "") -> str:
    """
    Add a song to a playlist.
    - If song is empty: adds whatever is currently playing.
    - If song is specified: searches the library for it first, then tries the catalog.
    """
    esc_pl = _esc(playlist_name.lower())

    if not song:
        # Add currently playing track
        result = _run(f'''
tell application "Music"
    if player state is stopped then
        return "nothing playing"
    end if
    set currentTrack to current track
    set matchedPL to missing value
    set pls to (every playlist whose special kind is none)
    repeat with pl in pls
        set n to name of pl
        if (do shell script "echo " & quoted form of n & " | tr A-Z a-z") contains "{esc_pl}" then
            set matchedPL to pl
            exit repeat
        end if
    end repeat
    if matchedPL is missing value then
        return "playlist not found"
    end if
    duplicate currentTrack to matchedPL
    return "added:" & (name of currentTrack) & "|" & (name of matchedPL)
end tell
''')
    else:
        esc_song = _esc(song)
        result = _run(f'''
tell application "Music"
    -- Find playlist
    set matchedPL to missing value
    set pls to (every playlist whose special kind is none)
    repeat with pl in pls
        set n to name of pl
        if (do shell script "echo " & quoted form of n & " | tr A-Z a-z") contains "{esc_pl}" then
            set matchedPL to pl
            exit repeat
        end if
    end repeat
    if matchedPL is missing value then
        return "playlist not found"
    end if
    -- Search library for song
    set res to search playlist "Library" for "{esc_song}"
    if (count of res) = 0 then
        return "song not found"
    end if
    set theTrack to item 1 of res
    duplicate theTrack to matchedPL
    return "added:" & (name of theTrack) & " by " & (artist of theTrack) & "|" & (name of matchedPL)
end tell
''')

    if result == "nothing playing":
        return "Nothing is playing right now — play a song first or specify a song name."
    if result == "playlist not found":
        return f"No playlist matching '{playlist_name}' — say 'list my playlists' to see them."
    if result == "song not found":
        return f"'{song}' isn't in your library. It needs to be in your library to add it to a playlist."
    if result.startswith("__error__"):
        return f"Couldn't add to playlist: {result}"
    if result.startswith("added:"):
        parts = result[6:].split("|", 1)
        track_info = parts[0]
        pl_name = parts[1] if len(parts) > 1 else playlist_name
        return f"Added '{track_info}' to {pl_name}."
    return result


# ── Create ────────────────────────────────────────────────────────────────────

def create_playlist(name: str) -> str:
    """Create a new empty playlist."""
    esc_name = _esc(name)
    result = _run(f'''
tell application "Music"
    set newPL to make new playlist with properties {{name:"{esc_name}"}}
    return "created:" & (name of newPL)
end tell
''')
    if result.startswith("created:"):
        pl_name = result[8:]
        return f"Created playlist '{pl_name}'."
    if result.startswith("__error__"):
        return f"Couldn't create playlist: {result}"
    return result


# ── Remove ────────────────────────────────────────────────────────────────────

def remove_from_playlist(playlist_name: str, song: str = "") -> str:
    """
    Remove a song from a playlist.
    If song is empty, removes the currently playing track from that playlist.
    """
    esc_pl = _esc(playlist_name.lower())

    if not song:
        result = _run(f'''
tell application "Music"
    if player state is stopped then
        return "nothing playing"
    end if
    set currentTrack to current track
    set trackName to name of currentTrack
    set matchedPL to missing value
    set pls to (every playlist whose special kind is none)
    repeat with pl in pls
        set n to name of pl
        if (do shell script "echo " & quoted form of n & " | tr A-Z a-z") contains "{esc_pl}" then
            set matchedPL to pl
            exit repeat
        end if
    end repeat
    if matchedPL is missing value then
        return "playlist not found"
    end if
    set plTracks to tracks of matchedPL
    repeat with t in plTracks
        if name of t is trackName then
            delete t
            return "removed:" & trackName & "|" & (name of matchedPL)
        end if
    end repeat
    return "not in playlist"
end tell
''')
    else:
        esc_song = _esc(song)
        result = _run(f'''
tell application "Music"
    set matchedPL to missing value
    set pls to (every playlist whose special kind is none)
    repeat with pl in pls
        set n to name of pl
        if (do shell script "echo " & quoted form of n & " | tr A-Z a-z") contains "{esc_pl}" then
            set matchedPL to pl
            exit repeat
        end if
    end repeat
    if matchedPL is missing value then
        return "playlist not found"
    end if
    set plTracks to tracks of matchedPL
    repeat with t in plTracks
        if name of t contains "{esc_song}" then
            set removed to name of t
            delete t
            return "removed:" & removed & "|" & (name of matchedPL)
        end if
    end repeat
    return "not in playlist"
end tell
''')

    if result == "nothing playing":
        return "Nothing is playing — specify a song name to remove."
    if result == "playlist not found":
        return f"No playlist matching '{playlist_name}'."
    if result == "not in playlist":
        return f"That song isn't in '{playlist_name}'."
    if result.startswith("__error__"):
        return f"Couldn't remove from playlist: {result}"
    if result.startswith("removed:"):
        parts = result[8:].split("|", 1)
        return f"Removed '{parts[0]}' from {parts[1]}."
    return result
