#!/usr/bin/env python3
"""
JARVIS WebSocket server.
FastAPI backend that exposes JARVIS brain + voice to the Electron frontend.
Run: python server.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import uuid
from typing import Optional, Set
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from config import ANTHROPIC_API_KEY, GROQ_API_KEY, USER_ADDRESS, JARVIS_DIR, OLLAMA_URL

# Demo mode lets the user try JARVIS without an API key — voice in/out works
# (Whisper local + edge-tts free) and a small canned brain handles input.
DEMO_MODE = os.environ.get("JARVIS_DEMO_MODE", "").lower() in ("1", "true", "yes")

# No keys yet AND demo isn't on? Don't hard-exit — boot in "setup mode" so the
# Electron renderer can drive the first-run wizard via the /setup HTTP
# endpoints. The wizard writes ~/JARVIS/.env, then asks the user to relaunch.
SETUP_MODE = not (ANTHROPIC_API_KEY or GROQ_API_KEY) and not DEMO_MODE
if SETUP_MODE:
    print("[setup] No API key in .env — booting in setup mode (wizard only).")
elif DEMO_MODE:
    print("[demo] Demo mode active — using canned brain, no API calls will be made.")

from brain.jarvis import Jarvis
from voice.speaker import Speaker

# Setup-wizard router: implements /setup/* endpoints. Always available.
from setup_api import build_setup_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await startup()
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# First-run wizard endpoints. Mounted unconditionally so users can re-validate
# or rotate keys later from a Settings panel without rebooting.
app.include_router(build_setup_router())

# ── Global state ──────────────────────────────────────────────────────────────
# In setup mode the brain has no key to bind to — defer construction until the
# user finishes the wizard and relaunches. In demo mode use the canned brain.
def _make_brain():
    if SETUP_MODE:
        return None
    if DEMO_MODE:
        from brain.demo import DemoJarvis
        return DemoJarvis()
    return Jarvis()


jarvis    = _make_brain()
speaker   = Speaker()
audio     = None
_lock     = threading.Lock()
_loop: Optional[asyncio.AbstractEventLoop] = None
_clients: Set[WebSocket] = set()
_state    = "idle"
_study_mode = False
_pending_confirms: dict[str, list] = {}   # id → [threading.Event, approved: bool]
_last_reply: str = ""                      # JARVIS's most recent spoken response
_camera_frames: dict[str, list] = {}      # id → [threading.Event, image_b64 | None]

# ── System component status — updated by the Electron renderer + health checks ──
# This is the ground truth for JARVIS's self-awareness. Each field is None
# (not reported yet), True (online), or False (failed/offline).
_system_status: dict = {
    "camera":         None,
    "gesture":        None,
    "websocket":      None,
    "camera_error":   None,
    # API health — probed by _run_health_check() at startup
    "weather_api":    None,   # True = reachable, False = SSL/network error
    "ai_api":         None,   # True = Groq/Claude key works, False = bad key / offline
    "local_ai":       None,   # True = Ollama running locally, False = not found
    "tts":            None,   # True = edge-tts responding
    "voice":          None,   # True = mic detected
}
_status_received  = threading.Event()   # set once gesture reports in
_greeting_started = False              # guard — greet only on the first ever connection
_mic_muted        = False              # True while user has manually muted the mic
_cancelled_timers: set[str] = set()   # timer IDs cancelled by user

# Lip motion gating — fed by mouth_state WebSocket messages from the renderer.
# When face was visible and mouth was moving recently, voice commands are
# accepted. When face visible but mouth idle, the audio is dropped (someone
# else / a video / room voice). When no face signal at all (camera off), the
# gate disables itself — voice profile is the only verifier.
_last_mouth_active_at: float = 0.0      # last MAR-active timestamp
_last_mouth_signal_at: float = 0.0      # last time renderer sent any mouth_state
_face_visible:         bool  = False    # last reported face visibility


def _run_health_check() -> None:
    """
    Probe real system components and update _system_status + personality.
    Runs once on startup in a background thread. Broadcasts updated status to UI.
    """

    def _probe_weather() -> bool:
        try:
            import ssl, certifi, urllib.request, urllib.parse
            ctx = ssl.create_default_context(cafile=certifi.where())
            req = urllib.request.Request(
                "https://wttr.in/Atlanta?format=j1",
                headers={"User-Agent": "curl/7.68.0"},
            )
            with urllib.request.urlopen(req, timeout=6, context=ctx):
                pass
            return True
        except Exception:
            return False

    def _probe_ai() -> bool:
        try:
            if GROQ_API_KEY:
                # Groq is accessed via OpenAI SDK with custom base_url
                from openai import OpenAI as _OAI
                c = _OAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
                r = c.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=5,
                )
                return bool(r.choices)
            if ANTHROPIC_API_KEY:
                import anthropic as _ant
                c = _ant.Anthropic(api_key=ANTHROPIC_API_KEY)
                r = c.messages.create(
                    model="claude-haiku-20240307",
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=5,
                )
                return bool(r.content)
            return False
        except Exception as e:
            print(f"[health] AI probe error: {e}")
            return False

    def _probe_tts() -> bool:
        try:
            import edge_tts  # noqa
            return True
        except ImportError:
            return False

    def _probe_local_ai() -> bool:
        """Check whether Ollama is reachable on localhost."""
        try:
            import httpx as _hx
            r = _hx.get(f"{OLLAMA_URL}/api/tags", timeout=2.0)
            return r.status_code == 200
        except Exception:
            return False

    # Run all probes
    results = {}
    results["weather_api"] = _probe_weather()
    results["ai_api"]      = _probe_ai()
    results["tts"]         = _probe_tts()
    results["local_ai"]    = _probe_local_ai()

    _system_status.update(results)

    from brain.personality import set_system_status
    set_system_status(_system_status)

    # Broadcast real health to the UI
    broadcast({"type": "system_health", "status": _system_status})
    print(f"[health] weather={results['weather_api']} ai={results['ai_api']} "
          f"local_ai={results['local_ai']} tts={results['tts']}")

# ── Focus / Presentation mode ─────────────────────────────────────────────────
# When True, JARVIS only responds if "Jarvis" appears in the utterance.
# Use this in pitches, meetings, or anywhere other people are talking nearby.
_focus_mode = False
# After media opens (YouTube, videos), require "Jarvis" for this many seconds
# to prevent JARVIS from responding to video audio.
_media_mode_until: float = 0.0
# Sleep mode — mic stays open so JARVIS can hear "wake up", but ignores everything else
_sleep_mode = False
# Acknowledgment sound path — pre-rendered at startup
_ACK_SOUND_PATH = "/tmp/jarvis_ack.aiff"

# Music ducking during conversation — when a command survives the music gate,
# we duck Apple Music so JARVIS can hear the rest of the exchange over it, and
# restore the volume a few seconds after the conversation goes quiet. This
# timer is reset on every command so a back-and-forth keeps the music low.
_unduck_timer = None
_UNDUCK_GRACE_SEC = 10.0


def _duck_music_for_conversation() -> None:
    """Duck Apple Music now and (re)arm a timer to restore it after a lull."""
    global _unduck_timer
    try:
        from tools.music_tools import duck_music, unduck_music
        duck_music()
        if _unduck_timer is not None:
            _unduck_timer.cancel()
        _unduck_timer = threading.Timer(_UNDUCK_GRACE_SEC, unduck_music)
        _unduck_timer.daemon = True
        _unduck_timer.start()
    except Exception:
        pass


# ── Broadcast helpers ─────────────────────────────────────────────────────────

async def _broadcast(msg: dict) -> None:
    data = json.dumps(msg)
    dead = set()
    for ws in _clients.copy():
        try:
            await ws.send_text(data)
        except Exception:
            dead.add(ws)
    _clients.difference_update(dead)


def broadcast(msg: dict) -> None:
    """Thread-safe: send msg to all connected frontends."""
    if _loop and _loop.is_running():
        asyncio.run_coroutine_threadsafe(_broadcast(msg), _loop)


# ── Music now-playing poller ────────────────────────────────────────────────
# Polls Apple Music every 4s for current track + state and broadcasts to the
# HUD. Auto-starts when JARVIS plays something, stops when Music is paused
# for a while (no point polling silence forever).
_music_poller_thread = None
_music_poller_stop = threading.Event()

# True while ANY system audio is playing (Music, Safari/Chrome video,
# YouTube, podcasts, etc). Triggers wake-word mode on the mic so video
# lyrics/dialogue don't get transcribed as commands.
_music_is_playing: bool = False
_system_audio_playing: bool = False

# Notification announcement control — voice command can mute/unmute.
_notifications_muted: bool = False

# The running notification monitor, tracked at module scope so the diagnostics
# self-fixer can stop a dead one and restart it with the REAL callback (see
# _on_notification below). Previously the fixer couldn't reach the callback —
# it was a nested function — so it installed a no-op and falsely reported
# "fixed" while notifications stayed silently dead.
_notif_mon = None

# Screen-description monitor thread (started in work/watch/gaming so
# "what am I looking at" answers from a warm cache). Kept as a module ref so
# we never start two.
_screen_monitor_thread: "threading.Thread | None" = None
_screen_monitor_lock = threading.Lock()


def _extract_artwork_from_music_app() -> str:
    """
    Pull current track's artwork directly from Apple Music via AppleScript.
    Works for ANY track Apple Music can play (no iTunes Search dependency).
    Returns a data: URL or empty string.
    """
    import subprocess as _sp, os as _os, base64 as _b64
    art_path = "/tmp/jarvis_art.dat"
    try:
        r = _sp.run(["osascript", "-e",
            'tell application "Music"\n'
            '  try\n'
            '    set theArt to raw data of artwork 1 of current track\n'
            '    set f to (open for access POSIX file "' + art_path + '" with write permission)\n'
            '    set eof f to 0\n'
            '    write theArt to f\n'
            '    close access f\n'
            '    return "ok"\n'
            '  on error\n'
            '    return ""\n'
            '  end try\n'
            'end tell'
        ], capture_output=True, text=True, timeout=4)
        if (r.stdout or "").strip() != "ok" or not _os.path.exists(art_path):
            return ""
        size = _os.path.getsize(art_path)
        if size < 200:
            _os.unlink(art_path)
            return ""
        # Detect MIME from magic bytes (artwork can be PNG, JPEG, or TIFF)
        with open(art_path, "rb") as f:
            header = f.read(12)
        if header[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
        elif header[:3] == b"\xff\xd8\xff":
            mime = "image/jpeg"
        elif header[:4] in (b"II*\x00", b"MM\x00*"):
            mime = "image/tiff"
        else:
            mime = "image/jpeg"   # best guess
        with open(art_path, "rb") as f:
            data = _b64.b64encode(f.read()).decode("ascii")
        _os.unlink(art_path)
        return f"data:{mime};base64,{data}"
    except Exception:
        return ""


def _get_artwork_url(title: str, artist: str, album: str = "") -> str:
    """
    Get current track's artwork. Tries Apple Music app first (always accurate
    when track is present), falls back to iTunes Search API for catalog hits.
    """
    # Try Apple Music first — guaranteed to be the actual track's artwork
    art = _extract_artwork_from_music_app()
    if art:
        return art
    # Fall back to iTunes Search API with strict matching
    import urllib.request, urllib.parse, json as _json, re
    if not title or not artist:
        return ""
    # Strip "(feat. X)" / "[feat. X]" from the title for cleaner search hits
    title_q = re.sub(r"\s*[\(\[]\s*feat\.?[^\)\]]*[\)\]]", "", title, flags=re.I).strip()
    # Primary artist only (drop "& Lil Uzi Vert" type suffixes)
    artist_q = re.split(r"\s*(?:&|feat\.?|,)\s*", artist, flags=re.I)[0].strip()
    title_low  = title_q.lower()
    artist_low = artist_q.lower()

    def _to_500(url: str) -> str:
        return url.replace("100x100", "500x500") if url else ""

    try:
        # Search for songs specifically (entity=song)
        q = f"{title_q} {artist_q}".strip()
        url = ("https://itunes.apple.com/search?"
               + urllib.parse.urlencode({
                   "term": q, "media": "music", "entity": "song", "limit": 25,
               }))
        with urllib.request.urlopen(url, timeout=4) as r:
            data = _json.loads(r.read().decode("utf-8", "ignore"))
        results = data.get("results") or []

        # Pass 1: exact artist match + title contained in result track name
        for hit in results:
            ha = (hit.get("artistName") or "").lower()
            ht = (hit.get("trackName")  or "").lower()
            if ha == artist_low and title_low in ht:
                return _to_500(hit.get("artworkUrl100", ""))

        # Pass 2: artist substring + title substring (handles "Playboi Carti & X")
        for hit in results:
            ha = (hit.get("artistName") or "").lower()
            ht = (hit.get("trackName")  or "").lower()
            if (artist_low in ha or ha in artist_low) and title_low in ht:
                return _to_500(hit.get("artworkUrl100", ""))

        # Pass 3: exact artist match alone (track names sometimes vary)
        for hit in results:
            ha = (hit.get("artistName") or "").lower()
            if ha == artist_low:
                return _to_500(hit.get("artworkUrl100", ""))

        # No good match → return empty rather than wrong art
        return ""
    except Exception:
        return ""


def _start_music_poller() -> None:
    """
    Background poller that mirrors Apple Music to the HUD widget AND flips
    the mic to wake-word-required mode when music plays. Broadcasts a rich
    payload: track + artist + album + state + position + duration + artwork.
    """
    global _music_poller_thread
    if _music_poller_thread and _music_poller_thread.is_alive():
        return
    _music_poller_stop.clear()
    def _loop():
        global _music_is_playing
        import subprocess as _sp
        last_track = None
        art_cache: dict = {"key": None, "data": ""}   # avoid re-pulling art unchanged
        while not _music_poller_stop.is_set():
            try:
                r = _sp.run(
                    ["osascript", "-e",
                     'tell application "Music"\n'
                     '  if it is running then\n'
                     '    set s to player state as string\n'
                     '    if s is "playing" or s is "paused" then\n'
                     '      set n to name of current track\n'
                     '      set a to artist of current track\n'
                     '      set al to album of current track\n'
                     '      set p to player position as string\n'
                     '      set d to duration of current track as string\n'
                     '      return s & "|" & n & "|" & a & "|" & al & "|" & p & "|" & d\n'
                     '    else\n'
                     '      return s & "|||||"\n'
                     '    end if\n'
                     '  else\n'
                     '    return "not_running|||||"\n'
                     '  end if\n'
                     'end tell'],
                    capture_output=True, text=True, timeout=3,
                )
                out = (r.stdout or "").strip()
                parts = out.split("|")
                if len(parts) >= 6:
                    state, name, artist, album, pos, dur = parts[:6]
                    track_key = f"{name}|{artist}|{album}"
                    if state == "playing" and name:
                        # Look up artwork via iTunes Search API once per track
                        if track_key != art_cache["key"]:
                            art_cache["key"]  = track_key
                            art_cache["data"] = _get_artwork_url(name, artist, album)
                        try:
                            pos_f = float(pos) if pos else 0.0
                            dur_f = float(dur) if dur else 0.0
                        except Exception:
                            pos_f = 0.0; dur_f = 0.0
                        broadcast({
                            "type":     "now_playing",
                            "state":    "playing",
                            "title":    name,
                            "artist":   artist,
                            "album":    album,
                            "position": pos_f,
                            "duration": dur_f,
                            "artwork":  art_cache["data"],
                        })
                        last_track = track_key
                        if not _music_is_playing:
                            _music_is_playing = True
                            try:
                                if audio: audio.start_detecting()
                            except Exception: pass
                            print("[music] playing — switched to wake-word mode", flush=True)
                    elif state in ("paused", "stopped") and last_track is not None:
                        # Paused → hide widget AND restore conversing
                        broadcast({"type": "now_playing", "state": "stopped"})
                        last_track = None
                        art_cache["key"] = None
                        if _music_is_playing:
                            _music_is_playing = False
                            try:
                                if audio and not _mic_muted: audio.start_conversing()
                            except Exception: pass
                            print("[music] paused — back to always-on", flush=True)
                    else:
                        if last_track is not None:
                            broadcast({"type": "now_playing", "state": "stopped"})
                            last_track = None
                            art_cache["key"] = None
                        if _music_is_playing:
                            _music_is_playing = False
                            try:
                                if audio and not _mic_muted: audio.start_conversing()
                            except Exception: pass
                            print("[music] stopped — back to always-on", flush=True)
            except Exception:
                pass
            _music_poller_stop.wait(2.0)
    _music_poller_thread = threading.Thread(target=_loop, daemon=True, name="music-poller")
    _music_poller_thread.start()


_system_audio_thread = None
_system_audio_stop = threading.Event()

def _start_system_audio_watcher() -> None:
    """
    Polls CoreAudio every 2s for "is the output device running anywhere?"
    When ANY app is producing audio (Music, video, podcast, etc.), flips
    the mic to wake-word mode. When it stops, restores conversing.
    """
    global _system_audio_thread
    if _system_audio_thread and _system_audio_thread.is_alive():
        return
    _system_audio_stop.clear()
    def _loop():
        # Only _system_audio_playing is assigned here; _music_is_playing is
        # read-only in this scope so it doesn't need a global declaration.
        global _system_audio_playing
        from tools.system_audio import is_system_audio_playing
        consecutive_off = 0
        while not _system_audio_stop.is_set():
            try:
                playing = is_system_audio_playing()
            except Exception:
                playing = False
            if playing:
                consecutive_off = 0
                if not _system_audio_playing:
                    _system_audio_playing = True
                    if not _music_is_playing:
                        try:
                            if audio: audio.start_detecting()
                        except Exception: pass
                        print("[audio] system audio detected — wake-word mode", flush=True)
            else:
                # Require 3 consecutive off readings before clearing — avoids
                # flapping when audio momentarily dips between tracks.
                consecutive_off += 1
                if consecutive_off >= 3 and _system_audio_playing:
                    _system_audio_playing = False
                    # Only restore conversing if Apple Music isn't separately playing
                    if not _music_is_playing:
                        try:
                            if audio and not _mic_muted: audio.start_conversing()
                        except Exception: pass
                        print("[audio] system audio stopped — back to always-on", flush=True)
            _system_audio_stop.wait(2.0)
    _system_audio_thread = threading.Thread(target=_loop, daemon=True, name="sys-audio-watcher")
    _system_audio_thread.start()


def _music_control(action: str, value: float | None = None) -> None:
    """Execute an Apple Music control via AppleScript (from WebSocket)."""
    import subprocess as _sp
    cmd_map = {
        "play":     'tell application "Music" to play',
        "pause":    'tell application "Music" to pause',
        "playpause":'tell application "Music" to playpause',
        "next":     'tell application "Music" to next track',
        "previous": 'tell application "Music" to previous track',
    }
    try:
        if action in cmd_map:
            _sp.run(["osascript", "-e", cmd_map[action]], capture_output=True, timeout=3)
        elif action == "seek" and value is not None:
            # Validate the seek position before embedding in AppleScript.
            # The cmd_map keys above are static strings, but `value` flows
            # in from a WebSocket message and must be a finite number — a
            # malformed value could otherwise inject AppleScript fragments
            # OR cause Music to interpret garbage (e.g., NaN) as an error.
            try:
                pos = float(value)
            except (TypeError, ValueError):
                print(f"[music] seek rejected — non-numeric value: {value!r}",
                      flush=True)
                return
            import math
            if not math.isfinite(pos) or pos < 0 or pos > 86400:
                # 24h is well above any realistic track length; reject
                # NaN / inf / negative / absurdly large positions.
                print(f"[music] seek rejected — out-of-range value: {pos}",
                      flush=True)
                return
            _sp.run(
                ["osascript", "-e",
                 f'tell application "Music" to set player position to {pos}'],
                capture_output=True, timeout=3,
            )
    except Exception as exc:
        print(f"[music] control failed: {exc}", flush=True)


def notify(title: str, message: str, sound: bool = False, also_in_hud: bool = True) -> None:
    """
    Send a notification two ways:
      - Native macOS banner (top-right corner, persists even when HUD hidden)
      - In-HUD toast (instant, integrated with JARVIS visual)
    sound=True adds the macOS Glass notification chime.
    also_in_hud=False sends only the native banner.
    """
    # Native macOS banner — works even when JARVIS HUD is hidden / not focused.
    try:
        from tools.system_controls import show_notification
        import threading as _thr
        _thr.Thread(
            target=show_notification, args=(title, message, sound),
            daemon=True, name="notify-native",
        ).start()
    except Exception as exc:
        print(f"[notify] native notification failed: {exc}", flush=True)
    # In-HUD toast — pushes a proactive event the renderer renders as a toast.
    if also_in_hud:
        broadcast({"type": "proactive", "text": f"{title} — {message}" if title else message})


def request_permission(message: str, timeout: float = 30.0, title: str = "") -> bool:
    """
    Ask the user for permission via the Electron UI.
    Blocks the calling thread until the user responds (or timeout).
    Optional `title` overrides the default '⚠ ACCESS REQUEST' header.
    """
    req_id = str(uuid.uuid4())[:8]
    event  = threading.Event()
    _pending_confirms[req_id] = [event, False]

    payload: dict = {"type": "confirm_request", "id": req_id, "message": message}
    if title:
        payload["title"] = title
    broadcast(payload)

    granted = event.wait(timeout=timeout)
    entry   = _pending_confirms.pop(req_id, [None, False])
    if not granted:
        broadcast({"type": "confirm_expired", "id": req_id})
        return False
    return entry[1]


def set_state(state: str) -> None:
    global _state
    _state = state
    broadcast({"type": "state", "state": state})


_SHUTDOWN_PHRASES = frozenset({
    "shut down", "shutdown", "power off", "turn yourself off",
    "close yourself", "close down", "good night jarvis",
    "that's all jarvis", "that will be all", "goodbye jarvis",
    "good night", "goodbye",
})

# ── Coding agent triggers ─────────────────────────────────────────────────────
_CODING_REQUEST_PHRASES = frozenset({
    "add feature", "add a feature", "add this feature", "add that feature",
    "add to yourself", "add to jarvis", "add yourself",
    "build feature", "build a feature", "build yourself",
    "code yourself", "code jarvis", "code a feature",
    "implement feature", "implement this feature", "implement that feature",
    "update your code", "update jarvis code", "update jarvis's code",
    "modify your code", "modify yourself", "modify jarvis",
    "write a feature", "write code for jarvis", "write code for yourself",
    "program yourself", "program jarvis",
    "add functionality", "new feature for jarvis",
    "make yourself able", "teach yourself to",
    "give yourself the ability", "give jarvis the ability",
})


def _is_coding_request(text: str) -> bool:
    """Return True if the text is explicitly asking JARVIS to add/modify its own features."""
    t = text.lower().strip()
    return any(phrase in t for phrase in _CODING_REQUEST_PHRASES)


def _launch_coding_agent(request: str) -> None:
    """
    Launch the autonomous coding agent in a background thread.
    Broadcasts live progress and final result to all connected frontends.
    """
    from brain.coding_agent import run_coding_agent_background

    broadcast({"type": "coding_agent_start", "request": request})

    def _progress(phase: str, detail: str) -> None:
        broadcast({"type": "coding_agent_progress", "phase": phase, "detail": detail})

    def _done(result: dict) -> None:
        broadcast({
            "type":           "coding_agent_done",
            "summary":        result.get("summary", "Done."),
            "restart_needed": result.get("restart_needed", []),
            "success":        result.get("success", False),
        })

    run_coding_agent_background(request, ANTHROPIC_API_KEY, _progress, _done)

def _is_shutdown_request(text: str) -> bool:
    t = text.lower().strip()
    return any(phrase in t for phrase in _SHUTDOWN_PHRASES)


# Phrases that Whisper commonly transcribes from ambient noise / music / silence
_AMBIENT_NOISE_PATTERNS = frozenset({
    "thank you", "thanks", "amen", "hallelujah", "jesus", "oh my god", "oh god",
    "subscribe", "like and subscribe", "follow me", "click the bell",
    "music", "♪", "♫", "[music]", "(music)",
    "you", "yeah", "okay", "ok", "hmm", "mm", "um", "uh",
    "bye", "bye bye", "goodbye", "see you",
    "good", "great", "awesome", "nice",
})

def _is_ambient_noise(text: str) -> bool:
    """
    Return True if `text` looks like ambient sound picked up by the mic
    rather than a real command — e.g., background TV, music, or silence artifacts.
    """
    t = text.lower().strip(" .,!?")
    words = t.split()

    # Very short (already filtered upstream but double-check)
    if len(words) <= 1:
        return True

    # Exact match against known ambient phrases
    if t in _AMBIENT_NOISE_PATTERNS:
        return True

    # 2-3 word messages that are entirely common filler/ambient words
    if len(words) <= 3:
        filler = {"thank", "you", "thanks", "amen", "yeah", "okay", "ok",
                  "hmm", "mm", "um", "uh", "bye", "good", "great", "awesome",
                  "nice", "jesus", "christ", "lord", "god", "oh", "well",
                  "so", "hey", "hi", "hello", "right", "sure", "yep", "nope",
                  "music", "the", "a", "and", "or", "but"}
        if all(w in filler for w in words):
            return True

    return False


def _should_drop_for_lip_gate() -> bool:
    """
    NEW architecture: voice profile is the primary verifier; lip gate is only
    consulted when the voice match is MARGINAL (could be Dylan, could be
    someone else with a similar voice).

    Decision tree:
      1. Voice profile confidence HIGH (>= CONFIDENT_THRESHOLD)
         → strong voice match → accept regardless of lip state.
         (Dylan was sometimes off-camera or the lip detector missed his mouth.
         This used to drop his real speech. Not anymore.)
      2. Voice profile confidence MARGINAL (between PASS and CONFIDENT)
         → consult lip gate as tiebreaker.
         If face visible and mouth was active in the last 4s → accept.
         If face visible and mouth idle → drop (probably a video / stranger).
         If no face / no camera signal → accept (no backup available, give
         the marginal voice match the benefit of the doubt).
      3. Voice profile rejected (below PASS) → audio never reached this point;
         it was dropped at the VAD layer.
    """
    import time as _t
    import os as _os
    if _os.environ.get("JARVIS_DISABLE_LIP_GATE", "").lower() in ("1", "true", "yes"):
        return False

    # ── Step 1: get voice profile match strength ───────────────────────────
    try:
        from voice.voice_profile import last_similarity, CONFIDENT_THRESHOLD
        voice_score = last_similarity()
    except Exception:
        return False  # voice profile unavailable → don't gate at all

    now = _t.time()
    no_face_signal = (now - _last_mouth_signal_at > 30.0)

    # ── Step 2: if FaceMesh sees Dylan's face, require mouth motion ───────
    # This is the big rule. Even if voice profile thinks "yes that's Dylan,"
    # if his face IS in frame but mouth isn't moving, the audio is coming
    # from somewhere else (laptop video, room voice, music bleed).
    if not no_face_signal and _face_visible:
        if now - _last_mouth_active_at <= 4.0:
            return False   # face + mouth motion = Dylan really talking
        return True        # face + still mouth = drop (video bleed)

    # ── Step 3: camera unavailable or Dylan off-camera ────────────────────
    # No visual confirmation possible — voice profile must be confident.
    # If voice was marginal (between PASS and CONFIDENT), drop to be safe.
    if voice_score < CONFIDENT_THRESHOLD:
        return True
    return False


def _is_echo(text: str) -> bool:
    """
    Return True if `text` looks like JARVIS picking up his own TTS output.

    Triggers:
      1. Speaker is currently active (afplay running) → drop ALL transcriptions.
         These are almost guaranteed to be JARVIS hearing himself.
      2. Recent reply exists + short fragment (<3 words) → likely mic artifact.
      3. Recent reply exists + heavy word overlap (>45%) → echo.
    """
    # Most reliable guard: if afplay is mid-playback, this is JARVIS.
    try:
        if speaker._play_proc is not None and speaker._play_proc.poll() is None:
            return True
    except Exception:
        pass

    if not _last_reply or not text:
        return False
    words = text.split()
    # Very short utterances after speaking are almost always mic artifacts
    if len(words) < 3:
        return True
    t_words = set(text.lower().split())
    r_words = set(_last_reply.lower().split())
    if not r_words:
        return False
    overlap = len(t_words & r_words) / len(t_words)
    return overlap > 0.45


# ── Processing ────────────────────────────────────────────────────────────────

def _resume_audio() -> None:
    """
    Re-enable the mic after JARVIS finishes speaking.
    Respects the manual mute flag — if the user muted while JARVIS was
    talking, keep the mic suspended rather than silently un-muting them.

    Mode selection (in priority order):
      1. Muted → stay suspended
      2. Study mode → start_study
      3. Media just opened → start_detecting (require "Jarvis" for 60s)
      4. Focus/presentation mode → start_detecting (always require "Jarvis")
      5. Normal → start_conversing (always-on, no wake word needed)
    """
    import time as _t
    global _media_mode_until

    if not audio:
        return
    if _mic_muted:
        audio.suspend()
        return
    if _study_mode:
        audio.start_study()
        return
    # Atomic test-and-clear — using a plain `if x: x = False` here lost
    # media-open events when a tool thread set the flag between our read
    # and our clear (race on a flag with multiple producers).
    if jarvis.tools_executor.consume_media_opened():
        # An app/media just opened (YouTube, music, or any open_application).
        # Require the wake word for a SHORT window so video dialogue / song
        # lyrics that start playing don't get transcribed as commands. The
        # system-audio watcher (CoreAudio) takes over after this and keeps
        # wake-word mode active for as long as real audio actually plays.
        #
        # This window was 3600s (1 HOUR) — a typo against the documented
        # "60 seconds" intent below. Opening something as innocent as System
        # Settings left JARVIS requiring "hey jarvis" before every command
        # for a full hour, which read as "it can barely hear me". 30s is
        # plenty to bridge the gap until CoreAudio reports real playback.
        _media_mode_until = _t.time() + 30.0
        # Use wake-word mode (start_detecting) rather than a full suspend —
        # a full suspend made JARVIS completely deaf; wake-word mode still
        # lets "hey jarvis" through during the bridge window.
        audio.start_detecting()
        return
    if _focus_mode:
        audio.start_detecting()
        return
    # When ANY system audio is playing (Music, video, podcast, browser),
    # require wake word so video dialogue / song lyrics don't get
    # transcribed as commands. Pollers flip these flags automatically.
    if _music_is_playing or _system_audio_playing:
        audio.start_detecting()
        return
    # NOTE: work mode stays in CONVERSING (always-on) — the lip-motion gate in
    # _process_unsafe filters out video / room voices. Requiring a wake word
    # defeats the purpose: Dylan wants to work hands-free while videos play.
    audio.start_conversing()


def _broadcast_timer_countdown(timer_id: str, seconds: int, label: str) -> None:
    """Broadcast timer tick events every second for HUD countdown display."""
    import time as _time
    remaining = seconds
    while remaining >= 0:
        if timer_id in _cancelled_timers:
            _cancelled_timers.discard(timer_id)
            broadcast({"type": "timer_cancelled", "id": timer_id})
            return
        broadcast({"type": "timer_tick", "id": timer_id, "remaining": remaining, "total": seconds, "label": label})
        if remaining == 0:
            break
        _time.sleep(1)
        remaining -= 1
    # Timer done
    broadcast({"type": "timer_done", "id": timer_id, "label": label})
    broadcast({"type": "chunk", "text": f"⏰ {label} — time's up!"})
    broadcast({"type": "done",  "full_text": f"Timer done: {label}"})


# ── Info card extraction ───────────────────────────────────────────────────────
# After JARVIS responds with factual content, extract key facts and broadcast
# them to the frontend's info side panel. Best-effort — never crashes JARVIS.

_INFO_EXTRACT_PROMPT = """Extract key information from this AI response as JSON.
Output ONLY valid JSON — no markdown fences, no explanation.

{{
  "has_info": true or false,
  "title": "2-5 word subject name",
  "category": "person" | "company" | "product" | "place" | "science" | "medical" | "history" | "technology" | "concept" | "other",
  "facts": ["fact 1 under 70 chars", "fact 2", "fact 3"]
}}

Rules:
- has_info = true only when response contains substantial factual info about a specific subject
- has_info = false for: greetings, timer/alarm acks, short replies, task completions, weather queries, "sure / got it / done" responses
- title: the main subject (e.g. "Elon Musk", "iPhone 16", "Black Holes", "Ozempic")
- facts: 3-5 specific, useful facts — each under 70 characters
- If has_info is false, use empty strings for all other fields

Response to analyze:
{response}"""


def _python_extract_info(response: str) -> dict | None:
    """Pure-Python fallback info extraction — no API required.
    Splits response into sentences, guesses title from first sentence,
    picks 3-4 short facts. Used when the LLM API is unavailable or fails."""
    import re as _re
    # Split on sentence boundaries
    sents = [s.strip() for s in _re.split(r'(?<=[.!?])\s+', response) if len(s.strip()) > 25]
    if len(sents) < 2:
        return None

    # Try to infer title: look for "X is/was/are a..." at start of first sentence
    title = None
    m = _re.match(r'^([A-Z][A-Za-z0-9\s\-,\.]{2,39}?)\s+(?:is|was|are|were|has|have|stands|refers)\b',
                  sents[0])
    if m:
        title = m.group(1).strip().rstrip(',').strip()
    if not title:
        # Take first 4 words of the first sentence as the title
        words = sents[0].split()
        title = ' '.join(words[:4]).rstrip('.,')

    facts = []
    for s in sents[:10]:
        s = s.strip().rstrip('.')
        if len(s) < 25:
            continue
        if len(s) > 72:
            # Try to cut at a word boundary within 70 chars
            s = s[:69].rsplit(' ', 1)[0] + '…'
        facts.append(s)
        if len(facts) >= 4:
            break

    if not facts:
        return None

    # Guess category from keywords
    cat = "other"
    low = response.lower()
    if any(k in low for k in ["founded", "company", "corporation", "inc.", "startup", "revenue", "ceo"]):
        cat = "company"
    elif any(k in low for k in ["born", "biography", "politician", "scientist", "author", "artist"]):
        cat = "person"
    elif any(k in low for k in ["planet", "galaxy", "universe", "orbit", "nasa", "telescope"]):
        cat = "science"
    elif any(k in low for k in ["software", "algorithm", "computer", "programming", "cpu", "ai ", "machine learning"]):
        cat = "technology"
    elif any(k in low for k in ["country", "city", "capital", "ocean", "mountain", "located"]):
        cat = "place"
    elif any(k in low for k in ["drug", "medicine", "treatment", "symptom", "disease", "clinical"]):
        cat = "medical"

    return {"title": title[:45], "category": cat, "facts": facts}


def _extract_and_broadcast_info(response: str) -> None:
    """Extract key facts from a JARVIS response and push an info_card to the UI."""
    if len(response) < 120:
        return
    # Skip obviously non-informational responses
    _skip = ("sure,", "of course", "i'll ", "i've set", "done.", "ok,", "got it",
             "timer set", "alarm set", "reminder set", "setting a", "turning ",
             "opening ", "good morning", "good evening", "hello", "goodbye")
    if any(response.lower().startswith(s) for s in _skip):
        return

    data = None
    try:
        prompt = _INFO_EXTRACT_PROMPT.format(response=response[:2000])
        raw = None

        if ANTHROPIC_API_KEY:
            import anthropic as _ac
            msg = _ac.Anthropic(api_key=ANTHROPIC_API_KEY).messages.create(
                model="claude-haiku-4-5",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
        elif GROQ_API_KEY:
            import openai as _oa
            resp = _oa.OpenAI(
                api_key=GROQ_API_KEY,
                base_url="https://api.groq.com/openai/v1",
            ).chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300, timeout=8,
            )
            raw = resp.choices[0].message.content.strip()

        if raw:
            import re as _re
            raw = _re.sub(r"^```[^\n]*\n?", "", raw).rstrip("` \n").strip()
            parsed = json.loads(raw)
            if parsed.get("has_info") and parsed.get("title") and parsed.get("facts"):
                data = parsed
    except Exception as exc:
        print(f"[info] LLM extraction failed ({exc}), falling back to Python extractor", flush=True)

    # Fallback: pure-Python extraction when LLM is unavailable or returns has_info=false
    if not data:
        data = _python_extract_info(response)

    if data and data.get("title") and data.get("facts"):
        broadcast({
            "type":     "info_card",
            "title":    data["title"],
            "category": data.get("category", "other"),
            "facts":    data["facts"][:5],
        })
        print(f"[info] card pushed: {data['title']}", flush=True)


_RESET_SETUP_PHRASES = (
    "go back to setup", "reset setup", "redo setup", "re-run setup",
    "run setup again", "setup wizard", "rerun setup", "restart setup",
    "first run", "reconfigure jarvis", "setup mode",
)

_UPDATE_PHRASES = (
    "update jarvis", "update yourself", "pull updates",
    "check for updates", "update your code",
)

_HELP_PHRASES = (
    "what can you do", "what do you do", "help me", "list your tools",
    "what are your abilities", "what are your features", "show me what you can do",
    "what are you capable of", "how can you help", "what tools do you have",
)

_WHATS_NEW_PHRASES = (
    "what's new", "whats new", "any updates", "what changed",
    "show changelog", "show me the changelog", "what have you changed",
    "recent changes", "what did you update",
)

_MEMORY_RECALL_PHRASES = (
    "what do you remember", "what do you know about me",
    "show my memory", "what have you saved", "show me my memory",
    "what have you remembered", "what's in your memory",
)

_MEMORY_FORGET_PHRASES = (
    "forget that", "forget about", "don't remember", "remove from memory",
    "delete that memory", "wipe that",
)

_RESTART_PHRASES = (
    "restart jarvis", "restart yourself", "reboot jarvis",
    "reboot yourself", "restart the server",
)

_HIDE_PHRASES = (
    "hide yourself", "hide jarvis", "go away",
    "minimize yourself", "hide the window", "close the window",
)

_SLEEP_PHRASES = (
    "go to sleep", "go to sleep jarvis", "sleep mode",
    "stop listening", "be quiet", "shut up jarvis",
    "take a break", "pause yourself",
)

_WAKE_PHRASES = (
    "wake up", "wake up jarvis", "hey jarvis wake up",
    "start listening", "come back", "i'm back",
    "you can listen now",
)

_ENROLL_PHRASES = (
    "learn my voice", "remember my voice", "train my voice",
    "enroll my voice", "save my voice", "voice enrollment",
    "learn to recognize me", "who am i",
)
_FORGET_VOICE_PHRASES = (
    "forget my voice", "delete my voice", "clear my voice",
    "stop recognizing me", "remove voice profile",
)

_AR_ENTER_PHRASES = (
    "ar mode", "hologram mode", "enter ar", "start ar",
    "enter hologram", "ar build mode", "hologram build",
    "build mode", "design mode", "3d mode", "shape mode",
    "start building", "open ar", "launch ar",
)

_AR_EXIT_PHRASES = (
    "exit ar", "exit hologram", "close ar", "exit build mode",
    "leave ar", "close hologram", "ar off", "exit ar mode",
    "exit design mode", "exit shape mode", "exit 3d mode",
    "close build mode", "stop building", "leave build mode",
    "get out of build", "turn off build", "disable build",
    "back to normal", "go back to normal", "normal mode",
    "close the canvas", "close canvas", "done building",
    "finished building", "stop ar", "end ar", "end build",
    # word-level fallbacks so "exit the build mode" / "stop build" still match
    "stop build", "exit build", "leave build", "close build",
    "stop the build", "exit the build", "leave the build",
)

# Phrases that should INSTANTLY hide any overlay/blueprint/hologram on screen.
# Hard-coded so they fire even when the LLM is rate-limited or off the rails.
_OVERLAY_DISMISS_PHRASES = (
    "take it down", "take that down", "take this down",
    "get rid of it", "get rid of that", "get rid of this",
    "get rid of the blueprint", "get rid of the overlay",
    "get rid of the hologram", "get rid of the cards",
    "close the blueprint", "close the overlay", "close the hologram",
    "close the cards", "close the visual", "close the diagram",
    "hide the blueprint", "hide the overlay", "hide the hologram",
    "hide the cards", "hide the visual", "hide the diagram",
    "dismiss the blueprint", "dismiss the overlay", "dismiss the hologram",
    "remove the blueprint", "remove the overlay", "remove the hologram",
    "clear the blueprint", "clear the overlay", "clear the hologram",
    "blueprint off", "overlay off", "hologram off",
    "drop the blueprint", "drop the overlay",
)

# Word-set exit check — catches any phrasing that contains both an exit verb
# and "ar"/"build"/"hologram"/"canvas" without needing exact substring match
_AR_EXIT_VERBS  = {"exit", "leave", "close", "stop", "end", "quit", "cancel", "disable", "turn"}
_AR_EXIT_NOUNS  = {"ar", "build", "hologram", "canvas", "mode"}

def _is_ar_exit(text: str) -> bool:
    words = set(text.lower().split())
    return bool(words & _AR_EXIT_VERBS) and bool(words & _AR_EXIT_NOUNS)

# ── Shape types the AR parser recognises ─────────────────────────────────────
_AR_SHAPES = {"square", "circle", "triangle", "rectangle", "hexagon",
              "pentagon", "cube", "sphere", "line"}

# Keyword → hex colour
_AR_COLORS: dict[str, str] = {
    "red":    "#ff4060", "blue":   "#4488ff", "green":  "#00ff88",
    "yellow": "#ffdd00", "orange": "#ff8833", "purple": "#aa44ff",
    "cyan":   "#00d4ff", "white":  "#ffffff", "pink":   "#ff66aa",
    "gold":   "#ffcc00", "teal":   "#00ccbb", "violet": "#9966ff",
}

# Keyword → size override
_AR_SIZES: dict[str, int] = {
    "tiny": 40, "small": 60, "medium": 100,
    "big": 150, "large": 150, "huge": 200, "giant": 250,
}


_ar_labels_visible: bool = True   # toggled by "hide labels" / "show labels"

# ── Gaming / Work Mode ─────────────────────────────────────────────────────────
_gaming_mode:   bool = False   # compact HUD, no screen watching
_work_mode:     bool = False   # compact HUD + 24/7 screen watching
_screen_buffer: dict = {}       # {"path": str, "ts": float, "desc": str}
_screen_lock  = threading.Lock()

# Gaming mode — compact, no screen vision
_GAMING_ENTER_PHRASES = (
    "gaming mode", "game mode", "enter gaming mode",
    "start gaming mode", "gamer mode",
)
_GAMING_EXIT_PHRASES = (
    "exit gaming mode", "exit game mode", "leave gaming mode",
    "stop gaming mode",
)

# Work mode — compact + screen watching
_WORK_ENTER_PHRASES = (
    "work mode", "enter work mode", "focus mode",
    "watching mode", "ambient mode", "screen mode",
    "start work mode",
    # Common Whisper mishearings of "work mode"
    "work load", "workload", "work loads",
    "work modes", "work moat", "walk mode", "wark mode",
    "work note", "work code", "work mod",
    "enter work load", "start work load", "into work mode",
)
_WORK_EXIT_PHRASES = (
    "exit work mode", "leave work mode", "stop work mode",
    "exit work", "leave work", "stop work",
    "normal mode", "back to normal", "exit focus mode",
    "desktop mode", "go to desktop", "back to desktop",
    "switch to desktop", "regular mode", "go back",
    "exit this mode", "out of work mode",
    # Common Whisper mishearings of "exit work mode"
    "exit work load", "exit workload", "exit work loads",
    "leave work load", "stop work load",
    "exit walk mode", "exit work note", "exit work code",
)


def _capture_screen() -> bool:
    """Take a screenshot and store path+timestamp. Returns True on success."""
    import subprocess as _sp, time as _t
    _dst = "/tmp/jarvis_gaming_screen.png"
    try:
        r = _sp.run(
            ["screencapture", "-x", "-t", "png", _dst],
            capture_output=True, timeout=5,
        )
        if r.returncode != 0:
            return False
        if os.path.getsize(_dst) < 5000:
            return False
        with _screen_lock:
            _screen_buffer["path"] = _dst
            _screen_buffer["ts"]   = _t.time()
            _screen_buffer.pop("desc", None)   # invalidate cached description
        return True
    except Exception:
        return False


def _get_screen_desc() -> str:
    """
    Return a brief 1-2 sentence vision description of the current screen.
    Uses a 45-second cache so we don't call vision on every turn.
    Returns "" if no capture is available or capture is too stale (> 90s).
    """
    import time as _t, base64 as _b64
    with _screen_lock:
        ts   = _screen_buffer.get("ts",   0)
        desc = _screen_buffer.get("desc", "")
        path = _screen_buffer.get("path", "")

    now = _t.time()
    if not path or now - ts > 90:
        return ""

    # Cached description still fresh (< 45s since last vision call)?
    if desc and now - ts < 45:
        return desc

    # Run vision analysis
    try:
        with open(path, "rb") as _f:
            _b = _b64.standard_b64encode(_f.read()).decode()
        from openai import OpenAI as _OAI
        from config import GROQ_API_KEY as _GK
        from tools.screen_tool import VISION_MODEL as _VM
        _vc = _OAI(api_key=_GK, base_url="https://api.groq.com/openai/v1",
                   timeout=8.0)   # never block longer than 8s
        _r  = _vc.chat.completions.create(
            model=_VM,
            messages=[{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{_b}"}},
                {"type": "text",
                 "text": (
                     "Describe what the user is LOOKING AT in 1-2 sentences. "
                     "IMPORTANT: Ignore any JARVIS UI panel or overlay visible "
                     "at the right or bottom edge of the screen — that is my own interface. "
                     "Focus only on the main content: what app, game, or website is "
                     "visible, and what's happening in it. Under 35 words."
                 )},
            ]}],
            max_tokens=80,
        )
        new_desc = _r.choices[0].message.content.strip()
        with _screen_lock:
            _screen_buffer["desc"] = new_desc
        return new_desc
    except Exception:
        return ""


def _ensure_screen_monitor() -> None:
    """
    Start the screen-description monitor if it isn't already running. Guarded by
    a lock so two threads (the mode-manager poll thread and a request thread
    both entering _on_mode_change) can't start two monitors at once.
    """
    global _screen_monitor_thread
    with _screen_monitor_lock:
        if _screen_monitor_thread is not None and _screen_monitor_thread.is_alive():
            return
        import threading as _thr
        _screen_monitor_thread = _thr.Thread(
            target=_gaming_monitor, daemon=True, name="screen-monitor")
        _screen_monitor_thread.start()


def _enter_work_mode(spoken: str = "I'll be here when you need me, sir.") -> None:
    """
    Slip into work mode: hide the big HUD (compact strip only) and start the
    background screen monitor. Idempotent — a no-op if already in work/gaming
    mode. Used both by the explicit "work mode" command and automatically
    when the user asks to pull a site/app up (so the HUD gets out of the way
    of whatever they opened).

    The mechanical side-effects (flags, HUD broadcast, screen monitor) live in
    `_on_mode_change`; this helper just pins work mode in the manager (so a 30s
    auto-poll doesn't yank us back to normal) and speaks the custom line.

    force("work") handles every starting point: from normal it switches in; from
    watch/gaming it overrides (so "pull up github" while a video is up actually
    moves to work and un-mutes notifications — not just speaks the work line);
    already in work it's a no-op re-pin. We must NOT early-return on
    `_work_mode`, because watch/gaming also set that flag.
    """
    try:
        from tools.mode_manager import get_manager
        mgr = get_manager()
    except Exception:
        mgr = None
    if mgr is not None:
        mgr.force("work")            # → _on_mode_change("work", "forced by voice")
    elif not (_work_mode or _gaming_mode):
        # No manager (Quartz unavailable): best-effort direct apply, only if not
        # already in some active mode (can't reliably transition without it).
        _on_mode_change("work", "voice")
    if spoken:
        _say(spoken)


def _on_notification(app_name: str, content: str) -> None:
    """
    Announce an incoming Messages/Snapchat/FaceTime/etc. notification. Defined
    at MODULE scope (not nested in startup) so `server._on_notification` is a
    real attribute the diagnostics self-fixer can retrieve to restart the
    monitor with the correct callback.
    """
    # Skip JARVIS's own notifications (avoid feedback loops)
    if "jarvis" in (app_name or "").lower():
        return
    if _notifications_muted:
        return
    if content:
        line = f"New {app_name} {content}"
    else:
        line = f"New notification from {app_name}."
    try:
        notify(title=f"📬 {app_name}", message=content or "New notification",
               sound=False, also_in_hud=True)
        speaker.stream_speak(iter([line]))
    except Exception as exc:
        print(f"[notif] speak error: {exc}", flush=True)


def _start_notif_monitor() -> bool:
    """
    Start (or restart) the notification monitor with the real callback. Stops
    any existing monitor first so we never leak its `log stream` child or run
    two. Returns True if a monitor thread is alive afterward.
    """
    global _notif_mon
    try:
        from tools.notification_monitor import NotificationMonitor
        if _notif_mon is not None:
            try:
                _notif_mon.stop()
            except Exception:
                pass
        _notif_mon = NotificationMonitor(on_notification=_on_notification,
                                         cooldown_sec=8.0)
        _notif_mon.start()
        return True
    except Exception as exc:
        print(f"[notif] monitor failed to start: {exc}", flush=True)
        return False


def _on_mode_change(mode: str, reason: str) -> None:
    """
    Apply a mode detected by the ModeManager. Runs on the mode-manager thread.
      watch  → mute notifications, compact HUD (ad-skip is always armed in the
               screen watcher, so YouTube ads get skipped automatically here)
      gaming → mute notifications, compact HUD, remind how to exit
      work   → compact HUD, notifications stay on
      normal → full HUD, notifications on
    """
    global _notifications_muted, _work_mode, _gaming_mode
    try:
        if mode in ("watch", "gaming", "work"):
            _work_mode = True
            _gaming_mode = True   # compact corner HUD
            try:
                from brain.personality import set_work_mode
                set_work_mode(True)
            except Exception:
                pass
            broadcast({"type": "gaming_mode_enter", "mode": mode})
            # Keep the screen-description cache warm so "what am I looking at"
            # answers instantly in any active mode.
            _ensure_screen_monitor()
            # Silence notifications while watching or gaming (not for work).
            _notifications_muted = mode in ("watch", "gaming")
            # Announcements are deliberately minimal so JARVIS isn't chatty as
            # the screen changes:
            #   gaming → announce once (sticky; user needs to know how to exit)
            #   watch  → SILENT (the whole point is to be quiet; the WATCH badge
            #            and the muted notifications are the feedback). The
            #            pull-up path says its own "Opening X…" line separately.
            #   work   → SILENT/ambient; the explicit "work mode" / pull-up path
            #            speaks its own line via _enter_work_mode.
            if mode == "gaming":
                _say("Gaming mode, sir. Say 'exit gaming mode' when you're done.")
        else:  # normal
            _work_mode = False
            _gaming_mode = False
            _notifications_muted = False
            try:
                from brain.personality import set_work_mode
                set_work_mode(False)
            except Exception:
                pass
            broadcast({"type": "gaming_mode_exit"})
        broadcast({"type": "mode_status", "mode": mode, "reason": reason})
    except Exception as exc:
        print(f"[mode] apply failed: {exc}", flush=True)


def _gaming_monitor() -> None:
    """
    Background thread: keep a FRESH screenshot cached every 30s while any active
    mode is on, so an on-demand "what am I looking at" has a recent frame ready.

    It deliberately does NOT run the vision model here. Describing the screen
    every 30s would fire a Groq vision call ~120×/hour — pointlessly narrating a
    movie the user is just watching and draining the same rate limit that caused
    the old 47s hangs. Vision runs only when the user actually asks (the
    _SCREEN_DESC_PHRASES path calls _get_screen_desc on demand).
    """
    import time as _t
    while _gaming_mode:
        _capture_screen()
        # 30 s in 0.5 s ticks so thread exits promptly on mode-off
        for _ in range(60):
            if not _gaming_mode:
                return
            _t.sleep(0.5)


def _parse_ar_command(text: str) -> list[dict] | None:
    """
    Parse a natural-language AR build command into an ar_build ops list.
    Returns None if the text doesn't look like an AR command, or if it's
    too complex for the simple parser (let the AI handle it).

    Handles:
      add/build/make/draw/create/put/place + shape
      modify: "make it [bigger/red/…]", "rotate it …", "move it …"
      remove: "remove the [shape]", "delete the [shape]"
      clear:  "clear", "wipe", "erase", "reset", "start over"
    """
    import re as _re
    low = text.lower().strip()

    # ── Group shapes ───────────────────────────────────────────────────────────
    _group_triggers = ("make them one", "group the", "group them", "combine the",
                       "combine them", "merge the", "merge them", "make one item",
                       "make them one item", "one item", "link the", "link them")
    if any(t in low for t in _group_triggers):
        from tools.ar_builder_tool import get_scene as _gs
        scene = _gs()
        if not scene["shapes"]:
            return None
        # Find which shape types are mentioned
        mentioned = [s for s in _AR_SHAPES if s in low]
        if len(mentioned) >= 2:
            types_to_group = mentioned
        else:
            # Group the last two shapes
            types_to_group = [s["type"] for s in scene["shapes"][-2:]]
        return [{"action": "group", "types": types_to_group}]

    _ungroup_triggers = ("ungroup", "separate them", "split them", "unlink")
    if any(t in low for t in _ungroup_triggers):
        return [{"action": "ungroup", "ids": [], "types": []}]

    # ── Clear ──────────────────────────────────────────────────────────────────
    if any(w in low for w in ("clear the scene", "clear everything", "wipe the",
                               "erase everything", "start over", "reset the scene",
                               "clear it", "wipe it", "clear all")):
        return [{"action": "clear"}]

    # ── Remove a shape ─────────────────────────────────────────────────────────
    if any(w in low for w in ("remove the", "remove that", "delete the", "take away")):
        from tools.ar_builder_tool import get_scene as _gs
        scene = _gs()
        for stype in _AR_SHAPES:
            if stype in low and scene["shapes"]:
                for sh in reversed(scene["shapes"]):
                    if sh["type"] == stype:
                        return [{"action": "remove", "id": sh["id"]}]
        # remove last shape as fallback
        if scene["shapes"]:
            return [{"action": "remove", "id": scene["shapes"][-1]["id"]}]
        return [{"action": "clear"}]

    # ── Modify last shape ─────────────────────────────────────────────────────
    is_modify = any(low.startswith(p) or p in low for p in (
        "make it ", "make the ", "change it", "rotate it", "spin it",
        "move it", "shift it", "scale it", "resize it",
    ))
    if is_modify:
        from tools.ar_builder_tool import get_scene as _gs
        scene = _gs()
        if not scene["shapes"]:
            return None   # nothing to modify — let AI respond
        target = scene["shapes"][-1]
        tid    = target["id"]
        mods: dict = {}

        # Colour
        for name, hex_v in _AR_COLORS.items():
            if name in low:
                mods["color"] = hex_v
                break

        # ─ Dimension target: "height", "width", or generic "size" ──────────────
        _has_height = "height" in low or " tall" in low
        _has_width  = "width"  in low or " wide" in low
        _dim_target = ("height" if _has_height and not _has_width
                       else "width" if _has_width and not _has_height
                       else "size")

        # ─ Fraction / percentage parsing ────────────────────────────────────────
        _FRAC_MAP = {
            "two thirds":     2/3,  "2/3":  2/3,
            "three quarters": 3/4,  "3/4":  3/4,  "three fourths": 3/4,
            "one third":      1/3,  "1/3":  1/3,
            "one quarter":    1/4,  "1/4":  1/4,  "a quarter": 1/4,
            "one half":       1/2,  "a half": 1/2,
        }
        _frac = next((v for k, v in _FRAC_MAP.items() if k in low), None)
        _pct_m = _re.search(r'(\d+)\s*(?:percent|%)', low)
        if _pct_m:
            _frac = int(_pct_m.group(1)) / 100

        # ─ Size resolution ───────────────────────────────────────────────────────
        sz      = target.get("size", 100)
        new_dim = None

        if _frac is not None:
            new_dim = max(20, int(sz * _frac))
        elif "twice" in low or "2x" in low or "double" in low:
            new_dim = sz * 2
        elif "three times" in low or "triple" in low:
            new_dim = sz * 3
        elif "half" in low or "smaller" in low:
            new_dim = max(30, sz // 2)
        else:
            for label, label_sz in _AR_SIZES.items():
                if label in low:
                    new_dim = label_sz
                    break

        if new_dim is not None:
            if _dim_target == "height":
                mods["height"] = new_dim
                mods.setdefault("width",  target.get("width",  sz))
            elif _dim_target == "width":
                mods["width"]  = new_dim
                mods.setdefault("height", target.get("height", sz))
            else:
                mods["size"] = new_dim

        # Rotation
        rot_m = _re.search(r'rotate\s+(?:it\s+)?(\d+)\s+deg', low)
        if rot_m:
            mods["rotation"] = int(rot_m.group(1))

        # Move
        if "to the right" in low:
            mods["x"] = target.get("x", 0) + 80
        elif "to the left" in low:
            mods["x"] = target.get("x", 0) - 80
        elif "up" in low:
            mods["y"] = target.get("y", 0) - 80
        elif "down" in low:
            mods["y"] = target.get("y", 0) + 80

        if mods:
            return [{"action": "modify", "id": tid, "shape": mods}]
        return None   # can't parse modification — fall through to AI

    # ── Compound shapes: "house", "car", "tree", "person" ────────────────────
    # When user asks to build a recognisable object, decompose into primitives.
    _compound_triggers = any(t in low for t in ("build", "make", "create", "draw", "build me"))
    if _compound_triggers:
        if "house" in low and not any(s in low for s in ("square", "circle", "triangle",
                                                          "rectangle", "hexagon")):
            # Classic house: a square body + triangle roof perfectly aligned
            # Square top edge: 30 - 60 = -30.  Triangle bottom = y + sz/4.
            # For zero gap: y_tri = -30 - 120/4 = -60
            ops = [
                {"action": "add", "shape": {"type": "square",   "size": 120, "x": 0, "y": 30,  "color": "#00d4ff"}},
                {"action": "add", "shape": {"type": "triangle", "size": 120, "x": 0, "y": -60, "color": "#00ffaa"}},
            ]
            return ops

    # ── Add a shape ────────────────────────────────────────────────────────────
    add_triggers = (
        "build", "add", "draw", "create", "put", "place",
        "make a", "make an", "show me a", "show a", "give me a",
        # Conversational forms:
        "let's start with", "start with", "begin with",
        "i want to build", "i want to add", "i want to see",
        "want to build", "want to add", "want to see",
        "let's add", "let's put", "let's make", "let's draw",
        "how about a", "how about an",
        "first a", "first the", "first, a", "first, the",
    )
    if not any(t in low for t in add_triggers):
        return None   # not an AR command — let AI respond normally

    # Pick the shape word that appears FIRST in the sentence
    # (so "add a circle to the right of the square" → circle, not square)
    shape_type: str | None = None
    _first_pos = len(low)
    for s in _AR_SHAPES:
        pos = low.find(s)
        if pos != -1 and pos < _first_pos:
            shape_type = s
            _first_pos = pos
    if not shape_type:
        return None   # couldn't identify the shape

    # --- Build the shape definition ---
    shape: dict = {"type": shape_type}

    # Colour
    for name, hex_v in _AR_COLORS.items():
        if name in low:
            shape["color"] = hex_v
            break

    # Base size
    size = 100
    for label, sz in _AR_SIZES.items():
        if label in low:
            size = sz
            break
    # Size multipliers (after base)
    if "twice" in low or "2x" in low or "double" in low:
        size = int(size * 2)
    elif "three times" in low or "triple" in low:
        size = int(size * 3)
    elif "half" in low:
        size = max(30, size // 2)
    shape["size"] = size

    # Position relative to a referenced shape (or last shape as fallback)
    from tools.ar_builder_tool import get_scene as _gs
    scene = _gs()
    x, y = 0, 0
    if scene["shapes"]:
        # Try to find the specific shape the user is referencing
        # e.g. "to the right of the SQUARE" → find square, not just last shape
        ref = None
        for rt in _AR_SHAPES:
            # Reference shape appears AFTER the new shape's name in the text
            new_pos = low.find(shape_type) if shape_type else 0
            ref_pos = low.find(rt)
            if ref_pos != -1 and ref_pos > new_pos:
                for sh in reversed(scene["shapes"]):
                    if sh["type"] == rt:
                        ref = sh
                        break
                if ref:
                    break
        if ref is None:
            ref = scene["shapes"][-1]

        ref_size = ref.get("size", 100)
        gap = 0 if ("directly" in low or "right on" in low) else 2

        def _ext(stype: str, sz: int, direction: str) -> float:
            """Actual edge distance from shape center to its boundary.
            Triangles point UP: top vertex at sz/2, bottom edge at sz/4."""
            if stype == "triangle":
                if direction == "top":    return sz / 2      # apex
                if direction == "bottom": return sz / 4      # base midpoint (sin30° × r)
                return sz * 0.433                            # left/right (cos30° × r)
            if stype == "line":
                return (sz / 2) if direction in ("left", "right") else 2
            return sz / 2   # square, circle, hexagon, pentagon, cube, rectangle

        if "on top" in low or "above" in low:
            x = ref.get("x", 0)
            y = ref.get("y", 0) - _ext(ref["type"], ref_size, "top") \
                                 - _ext(shape_type, size, "bottom") - gap
        elif "below" in low or "under" in low or "beneath" in low:
            x = ref.get("x", 0)
            y = ref.get("y", 0) + _ext(ref["type"], ref_size, "bottom") \
                                 + _ext(shape_type, size, "top") + gap
        elif "to the right" in low or "right of" in low:
            x = ref.get("x", 0) + _ext(ref["type"], ref_size, "right") \
                                 + _ext(shape_type, size, "left") + gap
            y = ref.get("y", 0)
        elif "to the left" in low or "left of" in low:
            x = ref.get("x", 0) - _ext(ref["type"], ref_size, "left") \
                                 - _ext(shape_type, size, "right") - gap
            y = ref.get("y", 0)
    if x: shape["x"] = int(x)
    if y: shape["y"] = int(y)

    return [{"action": "add", "shape": shape}]

def _say(msg: str) -> None:
    """Broadcast text to UI AND speak it aloud. Used by all shortcut paths."""
    import time as _t
    set_state("speaking")
    speaker.resume()
    if audio:
        audio.suspend()
    broadcast({"type": "chunk", "text": msg})
    broadcast({"type": "done",  "full_text": msg})
    speaker.stream_speak(iter([msg]))
    set_state("idle")
    if audio:
        _t.sleep(0.5)
        _resume_audio()


def process(text: str, skip_echo: bool = False) -> None:
    """Top-level guard — wraps _process_unsafe so a single tool failure
    can't hang JARVIS or kill the worker thread silently."""

    # ── Setup reset shortcut ───────────────────────────────────────────────────
    low = text.lower().strip()

    # ── Self-update shortcut ───────────────────────────────────────────────────
    if any(low.startswith(p) or p in low for p in _UPDATE_PHRASES):
        import subprocess as _sp
        result = _sp.run(
            ["git", "-C", JARVIS_DIR, "pull"],
            capture_output=True, text=True, timeout=30,
        )
        if "Already up to date" in result.stdout:
            msg = "Already up to date — you're running the latest version."
        elif result.returncode == 0:
            msg = "Updated! Restart JARVIS to apply the latest changes."
        else:
            msg = f"Update failed: {result.stderr.strip()[:120]}"
        _say(msg); return

    # ── Help shortcut ─────────────────────────────────────────────────────────
    if any(low.startswith(p) or p in low for p in _HELP_PHRASES):
        msg = (
            "Here's what I can do: "
            "Voice and chat — just talk to me. "
            "Screen vision — ask what's on your screen. "
            "Web browsing — I can open sites and pull info. "
            "Email and calendar — read, search, add events. "
            "Music — control Apple Music and Spotify. "
            "Shell commands, research, math, and memory."
        )
        _say(msg); return

    # ── What's new shortcut ────────────────────────────────────────────────────
    if any(low.startswith(p) or p in low for p in _WHATS_NEW_PHRASES):
        import subprocess as _sp
        result = _sp.run(
            ["git", "-C", JARVIS_DIR, "log", "--oneline", "-10"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().splitlines()
            entries = "\n".join(f"  • {l.split(' ', 1)[-1]}" for l in lines)
            msg = f"Here's what changed recently:\n{entries}"
        else:
            msg = "No git history found — can't check what's changed."
        _say(msg); return

    # ── Memory recall shortcut ────────────────────────────────────────────────
    if any(low.startswith(p) or p in low for p in _MEMORY_RECALL_PHRASES):
        from brain.memory import load_memory
        facts = load_memory()
        msg = f"Here's what I remember about you:\n\n{facts}" if facts \
              else "My memory is empty — I haven't saved anything about you yet."
        _say(msg); return

    # ── Memory forget shortcut ────────────────────────────────────────────────
    if any(low.startswith(p) or p in low for p in _MEMORY_FORGET_PHRASES):
        from brain.memory import forget_fact
        keyword = low
        for p in sorted(_MEMORY_FORGET_PHRASES, key=len, reverse=True):
            if low.startswith(p):
                keyword = low[len(p):].strip(" .,"); break
            elif p in low:
                keyword = low.split(p, 1)[-1].strip(" .,"); break
        msg = forget_fact(keyword) if keyword else "What should I forget? Try: 'forget my gym schedule'."
        _say(msg); return

    # NOTE: _gaming_mode / _work_mode are only READ in this function now — all
    # writes live in _on_mode_change / the mode helpers — so no `global` needed.

    # ── Work mode: screen description shortcut ───────────────────────────────
    # Bypass LLM entirely — capture fresh frame, run vision, speak result.
    _SCREEN_DESC_PHRASES = (
        "what am i looking at", "tell me what i'm looking at",
        "what's on my screen", "what's on the screen", "what's on screen",
        "what do you see", "what can you see", "look at my screen",
        "describe my screen", "what's happening on screen",
        "what are you seeing", "what's in front of me",
        "what's going on on screen", "what's on my monitor",
        "can you see my screen", "can you see what i",
        "what game is this", "what app is this", "what website is this",
        "what am i playing", "what is this", "what are we looking at",
        "look at this", "look at what i",
        "tell me about what i'm looking at", "describe what i",
    )
    if _work_mode and any(p in low for p in _SCREEN_DESC_PHRASES):
        import time as _sv_t
        # Trigger a fresh capture in background while we check the cache
        threading.Thread(target=_capture_screen, daemon=True).start()
        # Use cached desc if recent (< 45s); otherwise wait up to 6s for fresh one
        _sdesc = _get_screen_desc()
        if not _sdesc:
            _sv_t.sleep(3)
            _capture_screen()
            _sdesc = _get_screen_desc()
        if _sdesc:
            _say(_sdesc)
        else:
            _say("Screen capture isn't ready yet — give me a second and ask again.")
        return

    # ── Mode voice overrides — route through the ModeManager so it pins the
    # mode and stops auto-switching until the user exits. If the manager isn't
    # available (Quartz missing), fall back to applying the mode directly.
    def _force_mode(mode: str) -> None:
        # _on_mode_change speaks the mode's own announcement (e.g. the gaming
        # line), so this helper never speaks itself.
        try:
            from tools.mode_manager import get_manager
            mgr = get_manager()
        except Exception:
            mgr = None
        if mgr is not None:
            mgr.force(mode)          # fires _on_mode_change (which announces)
        else:
            _on_mode_change(mode, "voice")

    def _exit_mode() -> None:
        # exit_forced() reliably drops to normal (pinned to the current app so it
        # won't flip back), so the result is always normal — one honest line.
        try:
            from tools.mode_manager import get_manager
            mgr = get_manager()
        except Exception:
            mgr = None
        if mgr is not None:
            mgr.exit_forced()
        else:
            _on_mode_change("normal", "voice")
        _say("Back to normal, sir.")

    # ── Auto-mode global on/off ──────────────────────────────────────────────
    # Lets Dylan stop JARVIS from auto-switching modes based on his screen.
    _AUTO_OFF_PHRASES = (
        "stop auto mode", "turn off auto mode", "disable auto mode",
        "stop switching modes", "stop changing modes",
        "quit auto mode", "auto mode off", "stop auto switching",
    )
    _AUTO_ON_PHRASES = (
        "resume auto mode", "turn on auto mode", "enable auto mode",
        "start auto mode", "auto mode on", "watch my screen again",
    )
    if any(p in low for p in _AUTO_OFF_PHRASES):
        try:
            from tools.mode_manager import get_manager
            mgr = get_manager()
            if mgr is not None:
                mgr.set_auto(False)
        except Exception:
            pass
        _say("Auto mode off, sir. I'll stay put until you turn it back on.")
        return
    if any(p in low for p in _AUTO_ON_PHRASES):
        try:
            from tools.mode_manager import get_manager
            mgr = get_manager()
            if mgr is not None:
                mgr.set_auto(True)
        except Exception:
            pass
        _say("Auto mode on, sir. I'll adjust as your screen changes.")
        return

    # ── Gaming Mode ──────────────────────────────────────────────────────────
    if any(p in low for p in _GAMING_EXIT_PHRASES) and _gaming_mode:
        _exit_mode()
        return
    if any(p in low for p in _GAMING_ENTER_PHRASES) and not _gaming_mode:
        _force_mode("gaming")
        return

    # ── Work Mode — compact HUD + 24/7 screen watching ───────────────────────
    # EXIT check runs FIRST. Otherwise "exit work load" matches both lists
    # (because "work load" is in ENTER as a Whisper-mishearing alias) and the
    # ENTER branch fires first, re-entering work mode instead of leaving.
    if any(p in low for p in _WORK_EXIT_PHRASES) and _work_mode:
        _exit_mode()
        return
    if any(p in low for p in _WORK_ENTER_PHRASES) and not _work_mode and not _gaming_mode:
        _enter_work_mode("Work mode. I'll be here when you need me, sir.")
        return
    # NOTE: a second `if _WORK_EXIT_PHRASES` block lived here previously —
    # it was an exact duplicate of the one above (lines ~1649-1656). The
    # top block always returns first, so this was unreachable. Removed.

    # ── Normalize for hard-coded intercepts ───────────────────────────────────
    # Whisper outputs U+2019 (curly apostrophe) and U+201C/D (curly quotes),
    # which never match our ASCII-apostrophe trigger phrases. Normalize once
    # so 'what's on my calendar' (curly) matches "what's on my calendar" (straight).
    _norm = low.replace("’", "'").replace("‘", "'") \
               .replace("“", '"').replace("”", '"')

    # ── Music routing (hard-coded — LLM kept inventing fake queries) ──────────
    # Maps verbal vibes to Dylan's ACTUAL Apple Music playlists. If he just
    # says "play music" with no vibe, it defaults to "Good rap" (his pick).
    _MUSIC_VIBE_MAP = (
        # Order matters — most specific first
        (("chill rock", "chill rocks"),                        "Chill rock"),
        (("chill", "relax", "low key", "lo-fi", "lofi", "ambient"), "Chill songs"),
        (("late night", "nighttime", "tonight", "going to sleep"), "Late night"),
        (("house music", "house", "edm", "electronic", "dance"),  "House"),
        (("rap", "hip hop", "hiphop"),                          "Good rap"),
        (("rock",),                                             "Rock"),
        (("beach", "summer", "pool"),                           "Beach music"),
        (("acoustic", "campfire", "guitar"),                    "Campfire songs"),
        (("spanish", "latin", "reggaeton"),                     "Spanish shit"),
        (("favorites", "my favorites", "favorite"),             "Favorite Songs"),
        (("jeep", "driving"),                                   "Jeep"),
    )
    _MUSIC_BARE_PHRASES = (
        "play me some music", "play some music", "play music",
        "play me something", "play something",  "put on some music",
        "put on music", "put music on", "play a song", "play me a song",
        "play any music", "shuffle music", "shuffle some music",
        "just play something", "play me anything",
        # Vibe-only variations (e.g. "put on some house music")
        "put on some rap", "put on some rock", "put on some house",
        "put on some chill", "put on some lofi", "put on some late night",
        "play me some rap", "play me some rock", "play me some house",
        "play me some chill", "play me some lofi",
    )
    # Catch-all: "play <something>" or "put on <music vibe>" combined with a known vibe
    _has_vibe_keyword = any(
        w in _norm for vw, _ in _MUSIC_VIBE_MAP for w in vw
    )
    _has_play_intent = (
        " play " in f" {_norm} " or _norm.startswith("play ")
        or "put on " in _norm or "shuffle" in _norm
    )
    # ── Music taste scan (one-time learn — saves to memory.txt) ──────────────
    _SCAN_TASTE_PHRASES = (
        "scan my music", "learn my music", "learn my music taste",
        "learn my taste", "study my music", "build my music profile",
        "figure out my music", "analyze my music",
        "know my music", "know what i like",
    )
    if any(p in _norm for p in _SCAN_TASTE_PHRASES):
        _say("Scanning your playlists. Give me a minute.")
        try:
            from tools.playlist_tool import scan_music_taste
            summary = scan_music_taste()
            # Append to memory.txt
            mem_path = os.path.join(JARVIS_DIR, "memory.txt")
            with open(mem_path, "a", encoding="utf-8") as f:
                from datetime import date as _d
                f.write(f"\n- [{_d.today().isoformat()}] {summary}\n")
            broadcast({"type": "chunk", "text": summary})
            broadcast({"type": "done", "full_text": summary})
            _say("Got it. I've got a feel for your music now.")
        except Exception as exc:
            _say(f"Couldn't scan — {type(exc).__name__}.")
        return

    # ── System volume controls (no LLM — instant response) ───────────────────
    # Affects the whole Mac (macOS system output volume), not just Apple Music.
    _VOL_UP_PHRASES = (
        "volume up", "turn volume up", "turn it up", "louder",
        "turn up the volume", "raise the volume", "raise volume",
        "make it louder", "crank it up", "crank it", "louder please",
    )
    _VOL_DOWN_PHRASES = (
        "volume down", "turn volume down", "turn it down", "quieter",
        "turn down the volume", "lower the volume", "lower volume",
        "make it quieter", "less loud", "softer",
    )
    _MUTE_PHRASES = (
        "mute volume", "mute the volume", "mute the audio",
        "mute everything", "silent mode",
    )
    _UNMUTE_PHRASES = (
        "unmute volume", "unmute the volume", "unmute the audio",
        "turn sound back on", "audio back on",
    )
    # "Set volume to X" — extract number
    _vol_set_match = re.match(r'.*?(?:set\s+)?(?:the\s+)?volume\s+(?:to\s+|at\s+)?(\d{1,3})\s*(?:percent|%)?\s*$', _norm)
    if _vol_set_match:
        try:
            level = int(_vol_set_match.group(1))
            from tools.music_tools import set_volume
            result = set_volume(level)
            _say(f"Volume at {max(0, min(100, level))} percent.")
        except Exception:
            _say("Couldn't set volume.")
        return
    if any(p in _norm for p in _VOL_UP_PHRASES):
        try:
            import subprocess as _sp
            # Get current volume, add 10, cap at 100
            r = _sp.run(["osascript", "-e", "output volume of (get volume settings)"],
                        capture_output=True, text=True, timeout=3)
            cur = int((r.stdout or "50").strip())
            new = min(100, cur + 10)
            _sp.run(["osascript", "-e", f"set volume output volume {new}"], capture_output=True, timeout=3)
            _say(f"Volume at {new}.")
        except Exception:
            _say("Couldn't change volume.")
        return
    if any(p in _norm for p in _VOL_DOWN_PHRASES):
        try:
            import subprocess as _sp
            r = _sp.run(["osascript", "-e", "output volume of (get volume settings)"],
                        capture_output=True, text=True, timeout=3)
            cur = int((r.stdout or "50").strip())
            new = max(0, cur - 10)
            _sp.run(["osascript", "-e", f"set volume output volume {new}"], capture_output=True, timeout=3)
            _say(f"Volume at {new}.")
        except Exception:
            _say("Couldn't change volume.")
        return
    if any(p in _norm for p in _MUTE_PHRASES):
        try:
            import subprocess as _sp
            _sp.run(["osascript", "-e", "set volume with output muted"], capture_output=True, timeout=3)
            _say("Muted.")
        except Exception:
            _say("Couldn't mute.")
        return
    if any(p in _norm for p in _UNMUTE_PHRASES):
        try:
            import subprocess as _sp
            _sp.run(["osascript", "-e", "set volume without output muted"], capture_output=True, timeout=3)
            _say("Unmuted.")
        except Exception:
            _say("Couldn't unmute.")
        return

    # ── Direct music controls (no LLM — instant response) ────────────────────
    # Catches common Whisper mishearings so weird phrasings still work.
    _PAUSE_PHRASES = (
        "pause music", "pause the music", "pause song", "pause the song",
        "pause it", "pause", "stop music", "stop the music",
        # mishearings
        "paws music", "paws the music", "paws it",
    )
    _PLAY_RESUME_PHRASES = (
        # Explicit resume verbs — never ambiguous
        "unpause", "un pause", "un-pause", "on pause", "in pause", "an pause",
        "resume", "resume music", "resume the music", "resume song",
        "keep playing", "continue music", "continue the music",
        "press play", "hit play",
        # mishearings of "unpause"
        "umpause", "unpaws", "on paws",
        # NOTE: "play music" / "play the music" removed — those fall to
        # the playlist intercept below which starts the default playlist.
    )
    _NEXT_PHRASES = (
        "next song", "next track", "skip song", "skip track", "skip this",
        "skip this song", "skip this track", "next one", "skip it",
        "next", "skip", "skip ahead",
    )
    _PREV_PHRASES = (
        "previous song", "previous track", "last song", "last track",
        "go back a song", "previous one", "back a song", "back a track",
        "play the last song",
    )
    # Distinguish pause vs resume by checking music state
    if any(p == _norm.strip() or _norm.endswith(p) or p in _norm.split() for p in _NEXT_PHRASES) \
        or any(p in _norm for p in ("next song", "next track", "skip song", "skip track", "skip this")):
        try:
            import subprocess as _sp
            _sp.run(["osascript", "-e", 'tell application "Music" to next track'], capture_output=True, timeout=3)
            _say("Skipped.")
        except Exception:
            _say("Couldn't skip.")
        return
    if any(p in _norm for p in _PREV_PHRASES):
        try:
            import subprocess as _sp
            _sp.run(["osascript", "-e", 'tell application "Music" to previous track'], capture_output=True, timeout=3)
            _say("Going back.")
        except Exception:
            _say("Couldn't go back.")
        return
    # Resume check runs FIRST. "unpause" contains "pause" as substring,
    # so without this order PAUSE would fire on "unpause my music".
    if any(p in _norm for p in _PLAY_RESUME_PHRASES):
        try:
            import subprocess as _sp
            _sp.run(["osascript", "-e", 'tell application "Music" to play'], capture_output=True, timeout=3)
            _say("Playing.")
        except Exception:
            _say("Couldn't resume.")
        return
    # PAUSE check uses word boundaries to be safe — even though RESUME runs
    # first, this prevents weird inputs like "pause-button" from matching.
    if re.search(r'\b(pause|stop)\b', _norm) and any(p in _norm for p in _PAUSE_PHRASES):
        try:
            import subprocess as _sp
            _sp.run(["osascript", "-e", 'tell application "Music" to pause'], capture_output=True, timeout=3)
            _say("Paused.")
        except Exception:
            _say("Couldn't pause.")
        return

    if any(p in _norm for p in _MUSIC_BARE_PHRASES) or (_has_vibe_keyword and _has_play_intent and "music" in _norm):
        # No vibe — default to his go-to playlist
        _picked_playlist = "Good rap"
        for vibe_words, pl_name in _MUSIC_VIBE_MAP:
            if any(w in _norm for w in vibe_words):
                _picked_playlist = pl_name
                break
        _say(f"{_picked_playlist} coming up.")
        try:
            from tools.playlist_tool import play_playlist
            play_playlist(_picked_playlist, shuffle=True)
            broadcast({"type": "now_playing", "text": f"{_picked_playlist} playlist"})
            _start_music_poller()
        except Exception as exc:
            _say(f"Couldn't start music — {type(exc).__name__}.")
        return

    # ── Catch-all: "play [song/artist]" → direct Apple Music search ──────────
    # Catches "play wagon wheel", "play drake", "play sicko mode", etc. The
    # LLM was slow and sometimes routed weirdly; this is instant and reliable.
    _play_match = re.match(r'^(?:hey jarvis[,\.]?\s+)?(?:can you\s+)?(?:please\s+)?(play|put on)\s+(.+?)[\.\?!]*$', _norm)
    if _play_match and not any(p in _norm for p in (
        # Exclude the cases already handled above
        "play music", "play the music", "play something", "play me",
        "play any music", "press play", "hit play", "play a song",
    )):
        query = _play_match.group(2).strip()
        if query and len(query) > 1:
            _say(f"Looking for {query}.")
            try:
                from tools.music_tools import play_music
                play_music(query=query)
                _start_music_poller()
            except Exception as exc:
                _say(f"Couldn't play that — {type(exc).__name__}.")
            return

    # ── Notification controls (hard-coded) ────────────────────────────────────
    _NOTIF_OFF_PHRASES = (
        "stop reading notifications", "stop reading my notifications",
        "stop my notifications", "mute notifications", "quiet notifications",
        "no more notifications", "shut up notifications",
        "stop announcing notifications",
    )
    _NOTIF_ON_PHRASES = (
        "read my notifications", "start reading notifications",
        "turn notifications back on", "resume notifications",
        "notifications on", "announce notifications",
    )
    if any(p in _norm for p in _NOTIF_OFF_PHRASES):
        global _notifications_muted
        _notifications_muted = True
        _say("Notifications muted.")
        return
    if any(p in _norm for p in _NOTIF_ON_PHRASES):
        _notifications_muted = False
        _say("Reading notifications again.")
        return

    # ── Screen watcher pause/resume (auto-skip ads, dismiss banners) ──────────
    _WATCHER_OFF_PHRASES = (
        "stop watching my screen", "stop the screen watcher", "disable auto skip",
        "stop auto skip", "don't skip ads", "pause screen watcher",
    )
    _WATCHER_ON_PHRASES = (
        "watch my screen", "resume screen watcher", "enable auto skip",
        "skip ads for me", "start watching my screen",
    )
    if any(p in _norm for p in _WATCHER_OFF_PHRASES):
        try:
            from tools.screen_watcher import get_watcher
            get_watcher().pause()
        except Exception: pass
        _say("Screen watcher paused.")
        return
    if any(p in _norm for p in _WATCHER_ON_PHRASES):
        try:
            from tools.screen_watcher import get_watcher
            w = get_watcher()
            if not w.running: w.start()
            else: w.resume()
        except Exception: pass
        _say("Watching your screen, sir.")
        return

    # ── System diagnostics ────────────────────────────────────────────────────
    # Voice command runs the full health check on the sphere overlay,
    # narrates each failure with its behavioral impact, and tries auto-fixes.
    _DIAG_PHRASES = (
        # diagnostic noun
        "run diagnostics", "run a diagnostic", "run a full diagnostic",
        "system diagnostics", "systems diagnostics", "full diagnostic",
        "diagnostic check", "diagnostics on yourself", "diagnose yourself",
        # check noun (singular AND plural — "systems check" was missing)
        "system check", "systems check",
        "run a system check", "run a systems check",
        "system status check", "systems status check",
        # self check
        "self check", "self-check", "check yourself",
        # imperative
        "check your systems", "check your system", "check all systems",
        # status query
        "are you healthy", "are you ok", "are you okay", "everything good",
        "all systems nominal", "system status",
    )
    # ── Deep scan (line-by-line code review) ─────────────────────────────────
    # Runs in a background thread so the user can keep talking to JARVIS.
    # Streams progress events to a bottom-of-screen loading bar; native
    # notification + voice line fire when complete; user can then ask
    # "what did the scan find" to see the results panel.
    _DEEPSCAN_PHRASES = (
        "deep scan", "deep diagnostic", "scan every line",
        "scan my code", "scan your code", "scan all your code",
        "scan the codebase", "scan everything",
        "run a deep scan", "do a deep scan",
        "review every line", "review all your code",
        "full code review", "deep code scan",
        "scan all of you", "scan all of yourself",
        # "deep system search/scan" — these were mis-routing to
        # open_application("System Settings") because "system search"
        # reads like a Mac command. Catch them here first.
        "deep system search", "deep system scan", "deep system check",
        "system search", "search every line", "search your code",
        "search the codebase", "search all your code",
        "scan your whole system", "scan the whole system",
        "search your whole system",
    )
    if any(p in _norm for p in _DEEPSCAN_PHRASES):
        _say("Starting a deep scan. First a fast pass for syntax and hygiene, "
             "then I'll actually read every line and reason about real bugs — "
             "that part takes a few minutes. I'll keep talking while it runs.")
        def _drive_deep_scan() -> None:
            from tools.deep_scan import run_deep_scan
            try:
                for event in run_deep_scan(deep=True):
                    broadcast(event)
            except Exception as exc:
                broadcast({"type": "deep_scan_error",
                           "message": f"{type(exc).__name__}: {exc}"})
                print(f"[deep-scan] crashed: {exc}", flush=True)
                return
            # Speak a tight summary + push native notif when complete.
            # Distinguish the AI-found semantic bugs (category ai:*) from the
            # mechanical hygiene findings so the user knows which is which.
            try:
                from tools.deep_scan import get_last_scan_summary, get_last_findings
                s = get_last_scan_summary()
                bs = s["by_severity"]
                total = s["total"]
                ai_findings = [f for f in get_last_findings()
                               if str(f.get("category", "")).startswith("ai:")]
                n_ai = len(ai_findings)
                n_ai_high = sum(1 for f in ai_findings if f["severity"] == "high")
                if total == 0:
                    line = "Deep scan complete. No issues found, sir."
                else:
                    pieces = []
                    if bs["high"]:   pieces.append(f"{bs['high']} high")
                    if bs["medium"]: pieces.append(f"{bs['medium']} medium")
                    if bs["low"]:    pieces.append(f"{bs['low']} low")
                    ai_note = ""
                    if n_ai:
                        ai_note = (f" {n_ai} of those came from actually reading "
                                   f"the code")
                        if n_ai_high:
                            ai_note += f", including {n_ai_high} high-severity"
                        ai_note += "."
                    line = (f"Deep scan complete. {total} issue"
                            f"{'s' if total != 1 else ''} found — "
                            + ", ".join(pieces) + "."
                            + ai_note
                            + " Say 'show me the scan results' to see them.")
                speaker.stream_speak(iter([line]))
                notify("Deep scan complete",
                       f"{total} issue(s) found ({n_ai} from AI review). "
                       f"Ask JARVIS to show them.",
                       sound=False, also_in_hud=False)
            except Exception as exc:
                print(f"[deep-scan] summary failed: {exc}", flush=True)
        threading.Thread(target=_drive_deep_scan, daemon=True,
                         name="deep-scan").start()
        return

    # ── Show deep scan results ───────────────────────────────────────────────
    _SHOW_DEEPSCAN_PHRASES = (
        "show me the scan results", "show me the scan",
        "show the scan results", "show scan results",
        "what did the scan find", "what did the deep scan find",
        "open the scan results", "show me the deep scan",
        "show me the findings", "show me what's broken",
        "what's broken", "scan results",
    )
    if any(p in _norm for p in _SHOW_DEEPSCAN_PHRASES):
        try:
            from tools.deep_scan import get_last_findings, get_last_scan_summary
            findings = get_last_findings()
            summary = get_last_scan_summary()
            if not findings:
                _say("No recent deep scan findings, sir — run one first.")
            else:
                broadcast({
                    "type": "deep_scan_show_results",
                    "findings": findings,
                    "summary": summary,
                })
                _say(f"Showing {len(findings)} finding{'s' if len(findings) != 1 else ''}.")
        except Exception as exc:
            _say(f"Couldn't pull the findings — {type(exc).__name__}.")
        return

    # ── Fix the confirmed bugs from the last scan ────────────────────────────
    # Every fix is proven against the test suite AND re-reviewed before it
    # sticks; anything that fails is auto-reverted. Only AI-confirmed findings
    # are attempted (the hygiene stuff isn't worth an LLM patch).
    _FIX_PHRASES = (
        "fix the bugs", "fix what you found", "fix those bugs",
        "fix the issues", "fix them", "go fix them", "fix the findings",
        "fix what's broken", "fix whats broken", "patch the bugs",
        "fix the code", "fix yourself",
    )
    if any(p in _norm for p in _FIX_PHRASES):
        try:
            from tools.deep_scan import get_last_findings
            ai_findings = [f for f in get_last_findings()
                           if str(f.get("category", "")).startswith("ai:")]
            if not ai_findings:
                _say("No confirmed bugs to fix — run a deep scan first, sir.")
                return
        except Exception:
            _say("Couldn't read the last scan.")
            return
        _say(f"Working on {len(ai_findings)} confirmed bug"
             f"{'s' if len(ai_findings) != 1 else ''}. Every fix has to pass "
             f"my tests before it sticks — I'll revert anything that doesn't.")
        def _drive_fixes() -> None:
            try:
                from tools.self_fix import fix_findings
                results = fix_findings(ai_findings, max_fixes=5)
                applied = [r for r in results if r.get("fix_status") == "applied"]
                rejected = [r for r in results if r.get("fix_status") != "applied"]
                broadcast({"type": "self_fix_results", "results": results})
                if applied:
                    lines = [f"Fixed {len(applied)} bug{'s' if len(applied)!=1 else ''} — "
                             f"all tests still pass."]
                    for r in applied[:3]:
                        lines.append(f"{r['file']} line {r['line']}: "
                                     f"{r.get('fix_explanation','')}")
                    if rejected:
                        lines.append(f"{len(rejected)} couldn't be fixed safely — "
                                     f"I left those alone. Restart to load the fixes.")
                    else:
                        lines.append("Restart me to load the fixes.")
                    speaker.stream_speak(iter([" ".join(lines)]))
                else:
                    speaker.stream_speak(iter([
                        "I couldn't safely fix any of them — every patch either "
                        "failed the tests or didn't hold up on review, so I reverted "
                        "them all. Nothing changed."
                    ]))
            except Exception as exc:
                _say(f"Fix run crashed — {type(exc).__name__}. Nothing changed.")
        threading.Thread(target=_drive_fixes, daemon=True, name="self-fix").start()
        return

    # Dismiss the diagnostic overlay (voice-driven)
    _DIAG_DISMISS_PHRASES = (
        "close diagnostics", "close the diagnostics", "close diagnostic",
        "dismiss diagnostics", "dismiss diagnostic", "hide diagnostics",
        "hide the diagnostic", "close that overlay", "dismiss that",
        "ok close that", "okay close that", "that's all jarvis",
        "thanks jarvis that's it",
    )
    if any(p in _norm for p in _DIAG_DISMISS_PHRASES):
        broadcast({"type": "diagnostic_dismiss"})
        _say("Closed.")
        return

    if any(p in _norm for p in _DIAG_PHRASES):
        _say("Running a full diagnostic now.")
        def _drive_diagnostic() -> None:
            from tools.diagnostics import run_full_diagnostic
            try:
                last_done = None
                for event in run_full_diagnostic():
                    broadcast(event)
                    etype  = event.get("type")
                    if etype == "done":
                        last_done = event
                        continue
                    if etype != "step":
                        continue
                    if event.get("status") == "running":
                        continue
                    label  = event.get("label", "")
                    status = event.get("status")
                    impact = event.get("impact", "")
                    if status == "fail":
                        msg = f"{label} failed. {impact}"
                        if event.get("fix_attempted") and not event.get("fix_succeeded"):
                            msg += " Auto-repair didn't take."
                        speaker.stream_speak(iter([msg]))
                    elif status == "fixed":
                        speaker.stream_speak(iter([
                            f"{label} was down. I restored it."
                        ]))
                if last_done:
                    p = last_done.get("passed", 0)
                    fx = last_done.get("fixed", 0)
                    f  = last_done.get("failed", 0)
                    if f == 0 and fx == 0:
                        summary = f"Diagnostics complete. All {p} systems nominal, sir."
                    elif f == 0:
                        summary = (f"Diagnostics complete. {p} systems nominal, "
                                   f"{fx} restored automatically.")
                    else:
                        bits = [f"{p} nominal"]
                        if fx: bits.append(f"{fx} restored")
                        bits.append(f"{f} still degraded")
                        summary = "Diagnostics complete. " + ", ".join(bits) + "."
                    speaker.stream_speak(iter([summary]))
            except Exception as exc:
                _say(f"Diagnostic crashed — {type(exc).__name__}.")
        threading.Thread(target=_drive_diagnostic, daemon=True,
                         name="diagnostic-runner").start()
        return

    # ── Self-inspection question intercept ───────────────────────────────────
    # When Dylan asks "why did X fail", "explain that failure", "look at your
    # code", etc. — pre-load relevant evidence (recent diagnostic findings or
    # nothing if no diagnostic was run) into the text before brain dispatch.
    # Does NOT bypass the brain — just enriches its context so the answer is
    # grounded in real evidence instead of a fresh LLM guess.
    _INVESTIGATE_PHRASES = (
        # explicit self-question phrasings. Bare why-are-you / why-did-you are
        # intentionally NOT here: they false-positive on flattery and on
        # general clarifying questions. The regex below catches genuine
        # failure-style questions even without these short prefixes.
        "what went wrong", "what's wrong with", "whats wrong with",
        "what is wrong with", "what happened with", "what happened to",
        "explain that failure", "explain the failure", "explain that",
        "look at your code", "look at your own code",
        "check your code", "check your own code",
        "show me your code", "show me your own code",
        "read your own code", "read your code",
        "what does your", "how does your",
    )
    # Catch "why X fail/failed/broken/down/off/stop/stopped" with anything in
    # between — covers "why the render connection failed", "why did the music
    # gate drop me", "why is my screen watcher off", etc.
    _INVESTIGATE_FAIL_RE = re.compile(
        r'\b(?:why|how)\s+(?:\w+\s+){0,6}'
        r'(?:fail|failed|failing|broke|broken|down|off|wrong|stop|stopped|drop|dropped|crash|crashed)\b',
        re.IGNORECASE,
    )
    # Phrases that prove the user is asking about JARVIS's *own* behavior,
    # not a generic "why did" question. Used to decide whether to speak the
    # "Let me check" acknowledgement and to filter out unrelated matches.
    _SELF_REFERENTIAL_HINTS = (
        "your code", "your own code", "your log", "your logs",
        "your state", "your behavior", "your behaviour",
        "you fail", "you failing", "you broke", "you crashed",
        "you down", "you off", "you just did", "you do that",
        "yourself", "explain that failure", "explain the failure",
        "what went wrong", "what happened with", "your music gate",
        "your gate", "your watcher", "your monitor", "your poller",
    )
    # Defensive trigger detection — if the regex or list operation ever
    # crashes (corrupt phrase list, bad import), don't take down the whole
    # _process_unsafe call. Fall through to normal brain dispatch instead.
    try:
        _phrase_hit = any(p in _norm for p in _INVESTIGATE_PHRASES)
        _regex_hit  = bool(_INVESTIGATE_FAIL_RE.search(_norm))
    except Exception as _trig_exc:
        print(f"[self-inspect-intercept] trigger detection failed: "
              f"{_trig_exc}", flush=True)
        _phrase_hit = False
        _regex_hit  = False
    if _phrase_hit or _regex_hit:
        try:
            from tools.diagnostics import get_last_investigations, CHECKS
            findings = get_last_investigations()
            matched = None
            if findings:
                for chk in CHECKS:
                    if chk.name not in findings:
                        continue
                    kws = {chk.name.lower()}
                    kws |= {w.lower() for w in chk.label.split() if len(w) > 3}
                    if any(kw in _norm for kw in kws):
                        matched = chk.name
                        break
            self_referential = (
                matched is not None
                or any(h in _norm for h in _SELF_REFERENTIAL_HINTS)
            )
            if self_referential:
                # ── DIRECT-ANSWER FAST PATH ─────────────────────────────
                # If we matched a specific check AND have stashed evidence,
                # answer from the evidence directly. No brain round-trip,
                # no token-budget blow-up, no fallback-to-8b risk.
                if matched and matched in findings:
                    f = findings[matched]
                    label  = f["label"]
                    impact = ""
                    for chk in CHECKS:
                        if chk.name == matched:
                            impact = chk.impact
                            break
                    log_evidence = ""
                    if f.get("log_matches"):
                        snippet = f["log_matches"][0].strip()
                        if len(snippet) > 140:
                            snippet = snippet[:140] + "…"
                        log_evidence = f" The log showed: {snippet}."
                    if impact:
                        answer = f"{label} failed.{log_evidence} {impact}"
                    else:
                        answer = f"{label} failed.{log_evidence}"
                    # Optional file pointer for follow-up questions
                    if f.get("source_files"):
                        files = f["source_files"][:2]
                        answer += f" The code lives in {', '.join(files)}."
                    print(f"[self-inspect-direct] answered {matched} from "
                          f"stashed evidence — bypassed brain", flush=True)
                    _say(answer)
                    return    # ← skip brain entirely

                # ── COMPACT CONTEXT FALLBACK ──────────────────────────
                # No matched check or no findings — defer to the brain but
                # with a minimal context block (≤ 40 tokens vs ≥ 100 before)
                # so the 8b fallback doesn't blow the 6000 TPM limit.
                if findings:
                    names = ", ".join(findings.keys())
                    context_block = (
                        f" [Self-Q: recent diag has findings: {names}. "
                        f"Use get_last_diagnostic_findings or read_my_logs. "
                        f"NEVER web_search. Brackets are internal.]"
                    )
                else:
                    context_block = (
                        " [Self-Q: no recent diag. Use read_my_logs / "
                        "read_my_code / search_my_code. NEVER web_search. "
                        "Brackets are internal.]"
                    )
                text = text + context_block
                _say("Let me check, sir.")
            # If not self-referential, just fall through silently — no context
            # injection, no acknowledgement; the brain handles it normally.
        except Exception as exc:
            print(f"[self-inspect-intercept] error: {exc}", flush=True)
        # Fall through to normal brain dispatch (with or without enrichment)

    # ── Overlay / blueprint dismiss (hard-coded, no LLM needed) ───────────────
    # This fires BEFORE the brain so it works even when Groq is rate-limited.
    if any(p in _norm for p in _OVERLAY_DISMISS_PHRASES):
        broadcast({"type": "overlay_close"})
        broadcast({"type": "hide_overlay"})
        _say("Done.")
        return

    # ── Calendar visual read (hard-coded — LLM kept ignoring the new tool) ────
    _CAL_VISUAL_TRIGGERS = (
        "next event", "next meeting",   # short bare matches catch most variants
        "coming up", "what's coming up", "whats coming up",
        "what's on my calendar", "whats on my calendar", "what is on my calendar",
        "what's my schedule", "whats my schedule", "what is my schedule",
        "anything tomorrow", "anything this week", "anything coming up",
        "do i have anything", "what do i have today", "what do i have tomorrow",
        "show me my calendar", "show me my schedule",
        "read my calendar", "check my calendar", "open my calendar",
        "pull up my calendar", "pull up the calendar",
        "calendar today", "calendar this week", "calendar tomorrow",
        "on my calendar", "my schedule today", "my schedule tomorrow",
    )
    if any(p in _norm for p in _CAL_VISUAL_TRIGGERS):
        # Pick view based on what the question is asking about
        if any(w in _norm for w in (" today", "right now", "today?")):
            cal_view = "day"; cal_range_label = "Today"
        elif any(w in _norm for w in (
            "this week", "tomorrow", "next week",
            "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday",
        )):
            cal_view = "week"; cal_range_label = "This Week"
        else:
            cal_view = "month"; cal_range_label = "This Month"
        _say("Pulling up your calendar.")
        try:
            from tools.calendar_tool import read_calendar_visually
            result = read_calendar_visually(view=cal_view)

            # ── Pop-up: rich calendar info card ──────────────────────────
            # Parse the vision-model output into bullet-point facts so the
            # card lists each event cleanly.
            _facts = []
            for ln in result.splitlines():
                ln = ln.strip().lstrip("•-*0123456789. ")
                if ln and len(ln) < 200:
                    _facts.append(ln)
                if len(_facts) >= 8:
                    break
            broadcast({
                "type":   "info_card",
                "title":  f"Calendar — {cal_range_label}",
                "category": "concept",   # uses cyan-blue color in existing IC_CAT_COLORS
                "facts":  _facts or [result[:240]],
            })

            # Speak the result directly — no LLM involved.
            broadcast({"type": "chunk", "text": result})
            broadcast({"type": "done", "full_text": result})
            speaker.stream_speak(iter([result]))
        except Exception as exc:
            err = f"Couldn't read your calendar — {type(exc).__name__}."
            broadcast({"type": "chunk", "text": err})
            speaker.stream_speak(iter([err]))
        return

    # ── Website open intercept (hard-coded — keeps the LLM from picking
    # show_overlay or some other wrong tool for "pull up YouTube") ────────────
    # Triggers on "pull up X" / "open X" / "go to X" where X is a known site.
    # The known-sites map lives in tools.web_tools._KNOWN_SITES so the brain's
    # web_search() tool also benefits from it.
    if re.match(r"^\s*(?:hey\s+jarvis,?\s+)?(?:can\s+you\s+|could\s+you\s+|please\s+)?"
                r"(?:pull\s+up|open|go\s+to|launch|bring\s+up|navigate\s+to|"
                r"take\s+me\s+to|fire\s+up)\b",
                _norm):
        # Strip the leading verb phrase + politeness wrappers, hand the rest
        # to the known-sites resolver. Returns None if it's not a known site —
        # in which case we fall through to the LLM.
        try:
            from tools.web_tools import _resolve_known_site, _KNOWN_SITES
            # Remove "hey jarvis" + politeness so the resolver sees just the
            # verb + target ("pull up youtube" not "hey jarvis can you pull...")
            _site_query = re.sub(
                r"^\s*hey\s+jarvis,?\s+(?:can\s+you\s+|could\s+you\s+|please\s+)?",
                "", _norm,
            )
            site_url = _resolve_known_site(_site_query)
            if site_url:
                # Resolve a friendly name for the spoken confirmation
                site_label = None
                for name, url in _KNOWN_SITES.items():
                    if url == site_url:
                        site_label = name.title()
                        break
                site_label = site_label or site_url.split("//")[-1].split("/")[0]
                import subprocess as _sp
                try:
                    _sp.run(["open", site_url], timeout=5)
                    # Get the big HUD out of the way of whatever they opened.
                    # A streaming site → watch mode (mute notifications + arm
                    # ad-skip); anything else → work mode. One combined line.
                    from tools.mode_manager import looks_like_watch_site, get_manager
                    if looks_like_watch_site(f"{site_label} {site_url}"):
                        _mgr = get_manager()
                        if _mgr is not None:
                            _mgr.force("watch", reason="pull-up")
                        else:
                            _on_mode_change("watch", "pull-up")
                        _say(f"Opening {site_label}. I'll keep it quiet while you watch, sir.")
                    else:
                        _enter_work_mode(
                            f"Opening {site_label}. I'll be here when you need me, sir."
                        )
                except Exception:
                    _say(f"Couldn't open {site_label}.")
                return
        except Exception:
            pass   # any failure → fall through to normal LLM routing

    # ── AR Build Mode shortcuts ───────────────────────────────────────────────
    # EXIT must be checked BEFORE ENTER — "exit build mode" contains "build mode"
    # which is also an enter phrase, so enter would fire first and re-enter instead.
    # _is_ar_exit() catches "exit the build mode", "stop build", etc. via word sets.
    if any(low.startswith(p) or p in low for p in _AR_EXIT_PHRASES) or _is_ar_exit(low):
        from brain.personality import set_ar_mode
        set_ar_mode(False)
        broadcast({"type": "ar_mode_exit"})
        _say("AR canvas closed.")
        return

    if any(low.startswith(p) or p in low for p in _AR_ENTER_PHRASES):
        from brain.personality import set_ar_mode
        from tools.ar_builder_tool import reset_scene, get_scene
        reset_scene()
        set_ar_mode(True)
        broadcast({"type": "ar_mode_enter"})
        broadcast({"type": "ar_scene_update", "scene": get_scene()})
        _say("Holographic canvas online. Tell me what to build.")
        return

    # ── AR label toggle ───────────────────────────────────────────────────────
    global _ar_labels_visible
    _label_hide = any(w in low for w in (
        "hide labels", "hide the labels", "take off the labels",
        "remove labels", "no labels", "turn off labels", "labels off",
    ))
    _label_show = any(w in low for w in (
        "show labels", "show the labels", "turn on labels", "labels on", "add labels",
    ))
    if _label_hide:
        _ar_labels_visible = False
        broadcast({"type": "ar_labels", "visible": False})
        _say("Labels hidden.")
        return
    if _label_show:
        _ar_labels_visible = True
        broadcast({"type": "ar_labels", "visible": True})
        _say("Labels on.")
        return

    # ── AR shape commands — direct parser (no AI round-trip) ─────────────────
    # When AR mode is active, intercept shape-building commands here and execute
    # them immediately. This is fast, reliable, and never "analyzes surroundings."
    # Complex commands that the parser can't handle fall through to the AI below.
    from brain.personality import is_ar_mode
    if is_ar_mode():
        try:
            ar_ops = _parse_ar_command(low)
        except Exception as _ar_parse_exc:
            import traceback as _ar_tb
            print(f"[AR parser] error: {_ar_parse_exc}", file=sys.stderr)
            _ar_tb.print_exc()
            ar_ops = None   # fall through to AI

        if ar_ops is not None:
            try:
                from tools.ar_builder_tool import apply_operations, get_scene
                apply_operations(ar_ops)
                broadcast({"type": "ar_scene_update", "scene": get_scene()})
                # Confirm with a brief natural-sounding line
                action = ar_ops[0].get("action", "done")
                _added_count = len([o for o in ar_ops if o.get("action") == "add"])
                if action == "add" and _added_count == 1:
                    stype = ar_ops[0].get("shape", {}).get("type", "shape")
                    _ar_confirms = [
                        f"{stype.capitalize()} — done.",
                        f"There's your {stype}.",
                        f"Got it — {stype} rendered.",
                        "Done.",
                        "There you go.",
                    ]
                    import random as _rnd
                    msg = _rnd.choice(_ar_confirms)
                elif action == "add" and _added_count > 1:
                    msg = "Done — shapes added."
                elif action == "modify":
                    msg = "Updated."
                elif action == "remove":
                    msg = "Removed."
                elif action == "clear":
                    msg = "Canvas cleared."
                else:
                    msg = "Done."
                _say(msg)
            except Exception as _ar_exec_exc:
                import traceback as _ar_tb2
                print(f"[AR exec] error: {_ar_exec_exc}", file=sys.stderr)
                _ar_tb2.print_exc()
                _say("Something went wrong with that shape — check the server log.")
            return
        # Parser returned None → inject a hard directive so the AI calls ar_build()
        # immediately instead of generating a long text response.
        text = (
            f"[AR MODE — USE ar_build() NOW. DO NOT write text, DO NOT explain. "
            f"Call ar_build() immediately with the shapes the user is asking for. "
            f"User said:] {text}"
        )

    # ── Sleep / wake mode ─────────────────────────────────────────────────────
    if any(p in low for p in _SLEEP_PHRASES):
        global _sleep_mode
        _sleep_mode = True
        if audio:
            audio.start_detecting()   # keep mic open so "wake up" can be heard
        _say("Sleeping. Say 'wake up' when you need me.")
        return

    if any(p in low for p in _WAKE_PHRASES):
        _sleep_mode = False
        if audio:
            audio.start_conversing()
        _say("Back.")
        return

    # ── Hide window shortcut ──────────────────────────────────────────────────
    if any(low.startswith(p) or p in low for p in _HIDE_PHRASES):
        msg = "Going dark. Press ⌘⇧J to bring me back."
        broadcast({"type": "chunk", "text": msg})
        broadcast({"type": "done", "full_text": msg})
        broadcast({"type": "hide_window"})
        return

    # ── Voice enrollment ──────────────────────────────────────────────────────
    if any(p in low for p in _ENROLL_PHRASES):
        def _run_enrollment():
            from voice.voice_profile import ENROLL_SECS, SAMPLE_RATE, enroll_from_audio
            _say(f"Okay. Talk to me normally for {ENROLL_SECS} seconds — tell me about something, whatever. Go.")
            if audio:
                audio.start_recording()
            # Collect raw audio for ENROLL_SECS seconds
            import sounddevice as _sd
            recording = _sd.rec(
                int(ENROLL_SECS * SAMPLE_RATE),
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
            )
            _sd.wait()
            flat = recording.flatten()
            success = enroll_from_audio(flat)
            if success:
                _say("Got it. I'll know your voice from now on. Say 'forget my voice' to turn it off.")
            else:
                _say("Something went wrong with enrollment. Try again.")
            if audio:
                audio.start_conversing()
        threading.Thread(target=_run_enrollment, daemon=True, name="enroll").start()
        return

    if any(p in low for p in _FORGET_VOICE_PHRASES):
        from voice.voice_profile import clear_profile, has_profile
        if has_profile():
            clear_profile()
            _say("Voice profile deleted. I'll respond to anyone now.")
        else:
            _say("No voice profile saved.")
        return

    # ── Restart shortcut ──────────────────────────────────────────────────────
    if any(low.startswith(p) or p in low for p in _RESTART_PHRASES):
        broadcast({"type": "chunk", "text": "Restarting now — back in a moment."})
        broadcast({"type": "done", "full_text": "Restarting now — back in a moment."})
        import threading as _th
        def _do_restart():
            import time as _t; _t.sleep(1.0)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        _th.Thread(target=_do_restart, daemon=True, name="restart").start()
        return

    if any(low.startswith(p) or p in low for p in _RESET_SETUP_PHRASES):
        from profile import reset_setup
        reset_setup()
        broadcast({"type": "show_first_run", "reset": True})
        broadcast({"type": "chunk", "text": "Opening the setup wizard."})
        broadcast({"type": "done", "full_text": "Opening the setup wizard."})
        return

    try:
        _process_unsafe(text, skip_echo=skip_echo)
    except Exception as exc:
        import traceback as _tb
        print(f"[process] unhandled exception: {exc}", file=sys.stderr)
        _tb.print_exc()
        # Tell the user something went wrong instead of letting the HUD spin.
        try:
            broadcast({
                "type": "chunk",
                "text": (
                    "Something went wrong on my end while I was working on "
                    f"that — {type(exc).__name__}. Try again, and if it "
                    "keeps happening, check the server log."
                ),
            })
            broadcast({"type": "done", "full_text": ""})
            set_state("idle")
        except Exception:
            pass
        # Resume mic so the next utterance has a chance.
        try:
            if audio:
                _resume_audio()
        except Exception:
            pass


def _process_unsafe(text: str, skip_echo: bool = False) -> None:
    global _last_reply, _focus_mode

    # Preserve the original user-visible text. Intercepts (self-inspect
    # enrichment etc.) may append internal-context blocks to `text` for the
    # brain — those must NEVER appear in the chat as part of the user's
    # message bubble.
    _display_text = text

    # In setup mode there's no brain to dispatch to; tell the renderer
    # politely instead of throwing.
    if jarvis is None:
        broadcast({
            "type":     "chunk",
            "text":     "Setup isn't finished yet — open the wizard and add an API key.",
        })
        broadcast({"type": "done", "full_text": ""})
        return

    # In demo mode, skip every special-case intercept (coding agent, blueprint,
    # build mode, computer agent) — they all need real API keys. Stream the
    # canned reply straight to the renderer + speaker.
    if DEMO_MODE:
        from voice.latency import profiler
        profiler.start_turn(text)
        with _lock:
            if audio:
                audio.suspend()
            speaker.stop()
            speaker.resume()
            broadcast({"type": "user_message", "text": _display_text})
            set_state("thinking")
            full: list[str] = []

            def _gen():
                set_state("speaking")
                profiler.mark("brain_start")
                first = True
                # Streaming filter: Groq llama 8b sometimes emits
                # `<function=name>{...}</function>` as TEXT instead of using
                # the structured tool-call API. That markup leaks into the
                # chat display and is ugly. We strip it on the fly using a
                # tiny state machine that buffers across chunks.
                fn_buffer = [""]      # holds chunks while inside <function...>
                in_function = [False]
                def _sanitize(chunk: str) -> str:
                    out = ""
                    for ch in chunk:
                        if not in_function[0]:
                            fn_buffer[0] += ch
                            # Did we just complete the opening tag start?
                            if fn_buffer[0].endswith("<function"):
                                # Strip the "<function" we accidentally accumulated
                                out += fn_buffer[0][:-len("<function")]
                                fn_buffer[0] = "<function"
                                in_function[0] = True
                            elif "<function".startswith(fn_buffer[0]) and len(fn_buffer[0]) < len("<function"):
                                # Partial match — keep buffering, emit nothing yet
                                pass
                            else:
                                out += fn_buffer[0]
                                fn_buffer[0] = ""
                        else:
                            fn_buffer[0] += ch
                            if fn_buffer[0].endswith("</function>"):
                                in_function[0] = False
                                fn_buffer[0] = ""
                    return out

                for chunk in jarvis.chat(text):
                    if first:
                        profiler.mark("brain_first_chunk")
                        first = False
                    full.append(chunk)
                    clean = _sanitize(chunk)
                    if clean:
                        broadcast({"type": "chunk", "text": clean})
                        yield clean
                # Flush any leftover buffer (in case stream ended mid-state)
                if fn_buffer[0] and not in_function[0]:
                    broadcast({"type": "chunk", "text": fn_buffer[0]})
                    yield fn_buffer[0]

            speaker.stream_speak(_gen())
            profiler.mark("brain_done")
            profiler.end_turn()
            reply_text = "".join(full)
            _last_reply = reply_text
            broadcast({"type": "done", "full_text": reply_text})
            set_state("idle")
            if audio:
                import time as _time
                _time.sleep(2.5)
                _resume_audio()
        return

    # ── Echo guard: drop anything that's just JARVIS hearing himself ─────────
    # Only runs for voice/mic input — WebSocket typed messages bypass this.
    if not skip_echo and _is_echo(text):
        print(f"[echo] Dropped self-pickup: {text!r}")
        if audio:
            import time as _time
            _time.sleep(0.3)
            _resume_audio()
        return

    # ── Ambient noise filter: drop background TV/music/silence artifacts ──────
    # skip_echo=True means it came from the typed UI — never ambient noise.
    if not skip_echo and _is_ambient_noise(text):
        print(f"[ambient] Dropped noise: {text!r}")
        if audio:
            _resume_audio()
        return

    # ── Lip motion gate: was the user actually speaking? ──────────────────────
    # If the renderer's FaceMesh saw Dylan's face but his mouth wasn't moving
    # in the last ~1.2s, this transcription almost certainly came from a
    # course video, room voice, or speaker bleed. Drop it.
    if not skip_echo and _should_drop_for_lip_gate():
        print(f"[lip-gate] Dropped non-speaker audio: {text!r}")
        if audio:
            _resume_audio()
        return

    # ── Music/system audio gate: belt-and-suspenders wake-word enforcement ────
    # When music or system audio is playing, drop ANY transcript that doesn't
    # contain a wake word OR isn't a short bare music control.
    if not skip_echo:
        # Use ONLY the debounced flags set by the music poller + system-audio
        # watcher threads. We deliberately do NOT call is_system_audio_playing()
        # live here: CoreAudio's IsRunningSomewhere stays True for 5-15s AFTER
        # audio actually stops, so a live check keeps the wake-word gate armed
        # long after music/TTS ends and silently drops the user's next few
        # sentences ("it can barely hear me"). The watcher thread already
        # clears _system_audio_playing after 3 consecutive off-readings, which
        # is the debounced truth — trust that instead of the laggy live call.
        _audio_active_now = _music_is_playing or _system_audio_playing
        if _audio_active_now:
            from voice.audio_engine import check_wake_word
            triggered, remainder = check_wake_word(text)
            # Allow short bare music controls without wake word — Dylan
            # shouldn't have to say "Hey JARVIS" just to pause his own music.
            # Strategy: text contains a control keyword AND is ≤ 6 words.
            # That catches "pause", "pause it", "pause the music", "skip
            # this song", etc. while still dropping long song lyrics.
            _norm_strip = text.lower().strip(".,!? ")
            _word_count = len(_norm_strip.split())
            _CONTROL_WORDS_RE = re.compile(
                r'\b(pause|unpause|resume|skip|next|previous|stop|play|continue)\b'
            )
            _is_bare_music = (
                _word_count <= 4
                and bool(_CONTROL_WORDS_RE.search(_norm_strip))
            )
            print(f"[music-gate-check] music={_music_is_playing} sys={_system_audio_playing} live={_audio_active_now} wake={triggered} bare={_is_bare_music} text={text[:50]!r}", flush=True)
            if not triggered and not _is_bare_music:
                print(f"[music-gate] Dropped: {text!r}", flush=True)
                if audio:
                    _resume_audio()
                return
            # A wake-word command survived the gate while music plays → duck
            # the music so JARVIS hears the rest of the exchange (and any quick
            # follow-up) over it. Restores itself after a short lull. We skip
            # bare music controls ("pause"/"skip") — those are about the music
            # itself, not a conversation, so ducking them would be pointless.
            if _music_is_playing and triggered:
                _duck_music_for_conversation()
            if remainder:
                text = remainder

    # ── Shutdown command ──────────────────────────────────────────────────────
    if _is_shutdown_request(text):
        farewell = f"Goodbye, {USER_ADDRESS}. Shutting down."
        broadcast({"type": "chunk",   "text": farewell})
        broadcast({"type": "done",    "full_text": farewell})
        broadcast({"type": "state",   "state": "speaking"})
        if audio:
            audio.suspend()
        speaker.stream_speak(iter([farewell]))
        broadcast({"type": "state",   "state": "idle"})
        broadcast({"type": "shutdown"})
        import time as _t; _t.sleep(0.5)
        import os; os.kill(os.getpid(), 15)   # SIGTERM — clean exit
        return

    # ── Focus / Presentation mode toggle ─────────────────────────────────────
    # "presentation mode" / "focus mode" / "meeting mode" / "pitch mode"
    #   → JARVIS only responds when directly addressed ("Jarvis, ...")
    # "conversation mode" / "always on" / "normal mode"
    #   → back to always-on (default)
    _text_lower = text.lower()
    _FOCUS_ON_PHRASES  = ("presentation mode", "focus mode", "meeting mode",
                          "pitch mode", "quiet mode", "boardroom mode")
    _FOCUS_OFF_PHRASES = ("conversation mode", "always on mode", "normal mode",
                          "always on", "always-on", "turn off focus",
                          "turn off presentation", "disable focus mode")
    if any(p in _text_lower for p in _FOCUS_ON_PHRASES):
        _focus_mode = True
        ack = "Presentation mode on. I'll only respond when you address me directly by name."
        broadcast({"type": "chunk", "text": ack})
        broadcast({"type": "done",  "full_text": ack})
        broadcast({"type": "state", "state": "speaking"})
        speaker.stream_speak(iter([ack]))
        broadcast({"type": "state", "state": "idle"})
        if audio:
            import time as _t_focus
            _t_focus.sleep(0.8)
            audio.start_detecting()
        return
    if any(p in _text_lower for p in _FOCUS_OFF_PHRASES):
        _focus_mode = False
        ack = "Conversation mode on. I'm back to always-on — I'll catch everything."
        broadcast({"type": "chunk", "text": ack})
        broadcast({"type": "done",  "full_text": ack})
        broadcast({"type": "state", "state": "speaking"})
        speaker.stream_speak(iter([ack]))
        broadcast({"type": "state", "state": "idle"})
        if audio:
            import time as _t_focus2
            _t_focus2.sleep(0.8)
            _resume_audio()
        return

    # ── Computer agent intercept — detect before LLM so HUD shows immediately ─
    from brain.jarvis import _COMPUTER_AGENT_PHRASES
    _is_computer_agent = any(phrase in _text_lower for phrase in _COMPUTER_AGENT_PHRASES)
    if _is_computer_agent:
        broadcast({"type": "computer_agent_start", "request": text})

    # ── Homework auto-answer loop intercept ──────────────────────────────
    _HW_START_PHRASES = (
        "answer my homework", "answer my quiz", "answer my assignment",
        "start answering", "keep answering", "auto answer", "auto-answer",
        "do my homework", "do my quiz", "do my assignment",
        "answer all the questions", "answer all these questions",
        "answer the questions", "start the loop", "start homework loop",
        "answer my questions",
    )
    _HW_STOP_PHRASES = (
        "stop answering", "stop the loop", "stop homework", "stop the homework",
        "stop auto answer", "stop the quiz", "cancel homework",
    )
    _hw_start = any(_text_lower.startswith(p) or p in _text_lower for p in _HW_START_PHRASES)
    _hw_stop  = any(_text_lower.startswith(p) or p in _text_lower for p in _HW_STOP_PHRASES)

    # Also stop the loop if it's running and the user just says "stop"
    if not _hw_stop:
        try:
            from tools.homework_loop import is_running as _hw_is_running
            if _hw_is_running() and _text_lower.strip() in ("stop", "stop.", "stop!", "ok stop", "okay stop"):
                _hw_stop = True
        except Exception:
            pass

    if _hw_start or _hw_stop:
        try:
            from tools import homework_loop as _hw_mod
            if _hw_stop:
                _hw_mod.stop_loop()
                ack = "Auto-answer stopped."
            elif _hw_mod.is_running():
                ack = "Already running — I'm still working through the questions."
            else:
                _hw_mod.start_loop(broadcast_fn=broadcast)
                ack = "Auto-answer mode active. I'll work through every question — just say stop when you're done."
        except Exception as _hw_exc:
            ack = f"Homework loop error: {_hw_exc}"
        broadcast({"type": "chunk",    "text": ack})
        broadcast({"type": "done",     "full_text": ack})
        broadcast({"type": "state",    "state": "speaking"})
        speaker.stream_speak(iter([ack]))
        broadcast({"type": "state",    "state": "idle"})
        if audio:
            import time as _time_hw
            _time_hw.sleep(1.0)
            _resume_audio()
        return

    # ── AP Classroom intercept ────────────────────────────────────────────
    # Detect: "do progress check unit 5 in ap classroom", "do ap world unit 3 progress check", etc.
    _AP_TRIGGERS = (
        "ap classroom", "progress check", "ap world", "ap gov", "ap chem",
        "ap history", "ap government", "ap chemistry", "collegeboard", "college board",
        "myap", "my ap",
    )
    _is_ap_request = any(t in _text_lower for t in _AP_TRIGGERS)

    if _is_ap_request:
        import re as _re

        # ── Parse course ─────────────────────────────────────────────────
        _course_map = {
            "ap world":        ("AP World History", "AP World History"),
            "ap gov":          ("AP Government",    "AP Government and Politics"),
            "ap government":   ("AP Government",    "AP Government and Politics"),
            "ap chem":         ("AP Chemistry",     "AP Chemistry"),
            "ap chemistry":    ("AP Chemistry",     "AP Chemistry"),
            "ap history":      ("AP World History", "AP World History"),
            "apush":           ("AP US History",    "AP US History"),
            "ap us history":   ("AP US History",    "AP US History"),
            "ap bio":          ("AP Biology",       "AP Biology"),
            "ap biology":      ("AP Biology",       "AP Biology"),
            "ap calc":         ("AP Calculus",      "AP Calculus"),
            "ap calculus":     ("AP Calculus",      "AP Calculus"),
            "ap stats":        ("AP Statistics",    "AP Statistics"),
            "ap statistics":   ("AP Statistics",    "AP Statistics"),
            "ap lang":         ("AP Lang",          "AP English Language"),
            "ap language":     ("AP Lang",          "AP English Language"),
            "ap lit":          ("AP Lit",           "AP English Literature"),
            "ap literature":   ("AP Lit",           "AP English Literature"),
            "ap psychology":   ("AP Psychology",    "AP Psychology"),
            "ap psych":        ("AP Psychology",    "AP Psychology"),
            "ap macro":        ("AP Macro",         "AP Macroeconomics"),
            "ap micro":        ("AP Micro",         "AP Microeconomics"),
            "ap econ":         ("AP Economics",     "AP Economics"),
            "ap computer":     ("AP CSP",           "AP Computer Science Principles"),
            "ap csp":          ("AP CSP",           "AP Computer Science Principles"),
        }
        _course_short = "AP Classroom"
        _course_full  = "AP Classroom"
        for _kw, (_short, _full) in _course_map.items():
            if _kw in _text_lower:
                _course_short = _short
                _course_full  = _full
                break

        # ── Parse unit number ──────────────────────────────────────────────
        _unit_match = _re.search(r'unit\s*(\d+)', _text_lower)
        _unit_num   = _unit_match.group(1) if _unit_match else None

        # ── Parse section (MCQ / FRQ / Part A / Part B) ───────────────────
        _section = "MCQ"
        if "frq" in _text_lower or "free response" in _text_lower:
            _section = "FRQ"
        elif "part b" in _text_lower:
            _section = "MCQ Part B"
        elif "part a" in _text_lower:
            _section = "MCQ Part A"

        # ── Build human-readable task description ─────────────────────────
        if _unit_num:
            _task_desc = f"{_course_short} Unit {_unit_num} Progress Check {_section}"
        else:
            _task_desc = f"{_course_short} Progress Check"

        ack = f"On it. Opening AP Classroom — {_task_desc}. I'll navigate there and answer every question."
        broadcast({"type": "chunk",    "text": ack})
        broadcast({"type": "done",     "full_text": ack})
        broadcast({"type": "state",    "state": "speaking"})
        broadcast({"type": "computer_agent_start", "request": text})
        _last_reply = ack
        speaker.stream_speak(iter([ack]))
        broadcast({"type": "state",    "state": "idle"})

        # Build the injected navigation context for the LLM
        _unit_context = f"Unit {_unit_num}" if _unit_num else "the correct unit"
        _ap_nav_prompt = (
            f"[AP CLASSROOM TASK — NAVIGATE AND ANSWER]\n"
            f"Course: {_course_full}\n"
            f"Target: {_task_desc}\n"
            f"URL: https://myap.collegeboard.org/student/classroom\n\n"
            f"STEP-BY-STEP PLAN:\n"
            f"1. take_screenshot to see current screen state.\n"
            f"2. If Chrome is not on AP Classroom, call web_search or open_application to open "
            f"   https://myap.collegeboard.org/student/classroom in Chrome.\n"
            f"3. take_screenshot — find the {_course_full} course card and click it.\n"
            f"4. Locate {_unit_context} in the left sidebar or unit list — click it.\n"
            f"5. Find 'Progress Check' link — click it.\n"
            f"6. Find '{_section}' option — click 'Start' or 'Resume'.\n"
            f"7. Now you're in the quiz. Answer EVERY question using the standard loop:\n"
            f"   a. take_screenshot(for_control=True) — read question + options.\n"
            f"   b. Determine correct answer from your AP knowledge.\n"
            f"   c. click_screen(x, y) on the correct radio button.\n"
            f"   d. take_screenshot to confirm selection.\n"
            f"   e. Click 'Next Question' button, or scroll down to next question.\n"
            f"   f. Repeat until all questions done.\n"
            f"8. Click 'Submit' when all questions are answered. take_screenshot to confirm.\n"
            f"9. Report: 'Done — {_task_desc} submitted.'\n\n"
            f"RULES:\n"
            f"- Never skip a question. Never stop early.\n"
            f"- If already on AP Classroom, skip to the course navigation step.\n"
            f"- If a login screen appears, stop and tell Dylan — you cannot enter credentials.\n"
            f"- Radio buttons: click dead center. If it doesn't register, click the option label.\n"
            f"- Use your full AP knowledge — you know this material cold.\n"
            f"The user said: {text}"
        )

        # Inject this as a new user turn and run the LLM in heavy-task mode
        def _run_ap_agent():
            try:
                # Temporarily prepend the nav prompt so the LLM has full instructions
                jarvis.run(_ap_nav_prompt)
            except Exception as _exc:
                broadcast({"type": "chunk", "text": f"AP Classroom agent error: {_exc}"})
                broadcast({"type": "done",  "full_text": f"AP Classroom agent error: {_exc}"})

        import threading as _t_ap
        _t_ap.Thread(target=_run_ap_agent, daemon=True).start()

        if audio:
            import time as _time_ap
            _time_ap.sleep(2.0)
            _resume_audio()
        return

    # ── Coding agent intercept ────────────────────────────────────────────
    if _is_coding_request(_text_lower):
        ack = "Starting the coding agent. I'll implement that now — watch the coding panel on the right."
        broadcast({"type": "chunk",    "text": ack})
        broadcast({"type": "done",     "full_text": ack})
        broadcast({"type": "state",    "state": "speaking"})
        _last_reply = ack
        if audio:
            audio.suspend()
        speaker.stream_speak(iter([ack]))
        broadcast({"type": "state",    "state": "idle"})
        _launch_coding_agent(text)
        if audio:
            import time as _time
            _time.sleep(2.5)
            _resume_audio()
        return

    from voice.latency import profiler
    profiler.start_turn(text)

    with _lock:
        # Suspend mic while we're thinking/speaking — avoids self-pickup
        if audio:
            audio.suspend()

        speaker.stop()
        speaker.resume()

        broadcast({"type": "user_message", "text": _display_text})
        set_state("thinking")

        full: list[str] = []
        _json_overflow = [False]   # flag: we're accumulating raw JSON, suppress it
        _first_chunk_seen = [False]

        # ── Work mode: fetch screen context in parallel, don't block response ──
        # Run vision lookup in a background thread; if it resolves before the
        # LLM generates its first token, the context gets injected. If not,
        # JARVIS just answers without screen context — never blocks.
        _screen_ctx: list[str] = [""]   # mutable container so _gen() can read it
        if _work_mode:
            threading.Thread(target=_capture_screen, daemon=True).start()
            def _fetch_ctx():
                desc = _get_screen_desc()
                if desc:
                    _screen_ctx[0] = desc
            _ctx_thread = threading.Thread(target=_fetch_ctx, daemon=True, name="screen-ctx")
            _ctx_thread.start()
            _ctx_thread.join(timeout=0.25)  # cached = instant; stale/missing = skip and reply now

        def _gen():
            set_state("speaking")
            profiler.mark("brain_start")
            _ai_text = text
            if _work_mode and _screen_ctx[0]:
                _ai_text = (
                    f"[WORK MODE — SCREEN: {_screen_ctx[0]}] "
                    f"Use this screen context if relevant to what the user is asking. "
                    f"User said: {text}"
                )
            for chunk in jarvis.chat(_ai_text):
                if not _first_chunk_seen[0]:
                    _first_chunk_seen[0] = True
                    profiler.mark("brain_first_chunk")
                # ── JSON leak guard ──────────────────────────────────────────
                # If Groq falls back and streams show_overlay JSON as plain
                # text, we must not broadcast that to the chat UI.
                # Detect by looking for {"type":"overlay"…} JSON fragments.
                if not _json_overflow[0]:
                    # Check if accumulated reply looks like it's turning into JSON
                    so_far = "".join(full) + chunk
                    _json_overflow[0] = (
                        '"overlay_type"' in so_far
                        or '"show_overlay"' in so_far
                        or ('"label":' in so_far and '"category":' in so_far and '"detail":' in so_far)
                    )
                    if _json_overflow[0]:
                        # Don't emit this chunk; future chunks also suppressed
                        full.append(chunk)
                        continue
                elif _json_overflow[0]:
                    full.append(chunk)
                    continue
                # ── Normal path ──────────────────────────────────────────────
                full.append(chunk)
                broadcast({"type": "chunk", "text": chunk})
                yield chunk

        # ── Activate barge-in AFTER a short delay so TTS is already playing ─
        # If we start immediately, the measurement window captures dead silence
        # Barge-in RE-ENABLED with voice-profile guard. The audio engine now
        # only counts loud audio as barge-in if voice_profile says it sounds
        # like Dylan. JARVIS's own speaker bleed → fails voice profile → no
        # self-interruption. Dylan's voice → passes → JARVIS stops talking.
        if audio:
            def _delayed_barge_in():
                import time as _bt
                _bt.sleep(0.6)   # let first audio start playing first
                if audio:
                    audio.start_barge_in()
            threading.Thread(target=_delayed_barge_in, daemon=True,
                             name="barge-in-start").start()

        speaker.stream_speak(_gen())
        profiler.mark("brain_done")
        profiler.end_turn()

        reply_text = "".join(full)
        _last_reply = reply_text          # record what JARVIS just said

        broadcast({"type": "done", "full_text": reply_text})
        set_state("idle")

        # ── Info card: pull key facts and push to side panel ──────────────────
        threading.Thread(
            target=_extract_and_broadcast_info,
            args=(reply_text,), daemon=True,
        ).start()

        # Close computer-agent HUD if it was open
        if _is_computer_agent:
            broadcast({"type": "computer_agent_done"})

        if audio:
            import time as _time
            _barged_in = speaker._stop_flag.is_set()
            speaker.resume()   # clear stop flag so next stream_speak works

            if _barged_in:
                # User interrupted — on_barge_in already called start_conversing().
                # Short pause lets the audio flush reach STT, then release lock.
                _time.sleep(0.15)
                audio.start_conversing()
                # Safety net: if no speech follows within 2.5s (e.g. self-trigger),
                # fall back to normal listening so we don't get stuck.
                # Use non-blocking acquire (atomic test-and-grab) instead of
                # `not _lock.locked()` — the previous check-then-act left a
                # window where another thread could take the lock between the
                # check and the call, defeating the safety it was meant to
                # provide.
                def _barge_fallback():
                    _time.sleep(2.5)
                    if not audio:
                        return
                    if _lock.acquire(blocking=False):
                        try:
                            _resume_audio()
                        finally:
                            _lock.release()
                threading.Thread(target=_barge_fallback, daemon=True,
                                 name="barge-fallback").start()
            else:
                # Normal finish — 0.5s lets TTS audio die out before mic opens.
                _time.sleep(0.5)
                _resume_audio()


# ── WebSocket endpoint ────────────────────────────────────────────────────────

def _launch_startup_greeting() -> None:
    """
    Start the greeting countdown thread.  Called once when the first Electron
    client connects so the countdown begins from actual connection time,
    not server-process start time.  MediaPipe Hands typically reports in ~3–4s.
    """
    import time as _time
    _status_received.wait(timeout=10.0)
    # Small buffer after gesture status arrives
    _time.sleep(0.5)
    # Always greet at startup — don't let ambient-audio race block it
    _startup_speak()


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    global _greeting_started, _mic_muted
    await websocket.accept()
    _clients.add(websocket)
    # Send current state to new client
    await websocket.send_text(json.dumps({"type": "state", "state": _state}))

    # ── First-run wizard ───────────────────────────────────────────────────────
    from profile import is_setup_complete, load_profile
    if not is_setup_complete():
        await websocket.send_text(json.dumps({"type": "show_first_run"}))
    else:
        # Pass saved name/city to the renderer sidebar (cosmetic)
        p = load_profile()
        if p.get("name"):
            await websocket.send_text(json.dumps({
                "type": "profile_loaded",
                "name": p["name"],
                "city": p.get("city", ""),
            }))

    # Push sidebar data (weather, calendar, files) to THIS client.
    # Done here — not at server start — so the client is definitely connected.
    asyncio.ensure_future(_push_sidebar_data())

    # Fire startup greeting on the very first client connection.
    # Using a thread so the async WebSocket handler is never blocked.
    if not _greeting_started:
        _greeting_started = True
        threading.Thread(target=_launch_startup_greeting,
                         daemon=True, name="startup-greeting").start()

    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                raise  # re-raise to be caught by the outer handler
            except Exception as exc:
                print(f"[WS] receive error: {exc}")
                break

            try:
                msg = json.loads(raw)
            except Exception as exc:
                print(f"[WS] bad JSON from client (ignored): {exc}")
                continue

            try:
                kind = msg.get("type")

                if kind == "message":
                    text = msg.get("text", "").strip()
                    if text:
                        # skip_echo=True: typed/UI messages are never mic bleed
                        threading.Thread(target=process, args=(text,), kwargs={"skip_echo": True}, daemon=True).start()

                elif kind == "overlay_close":
                    # Frontend is closing the overlay — no action needed backend-side
                    pass

                elif kind == "log":
                    # Renderer forwarding a console message for visibility in server log
                    level = msg.get("level", "info")
                    text  = msg.get("msg", "")
                    print(f"[renderer:{level}] {text}")

                elif kind == "camera_frame":
                    # Electron renderer responded to a camera_capture_request
                    req_id = msg.get("id")
                    image  = msg.get("image")   # base64 JPEG (may include data URL prefix)
                    if req_id and req_id in _camera_frames:
                        _camera_frames[req_id][1] = image
                        _camera_frames[req_id][0].set()

                elif kind == "mouth_state":
                    # Lip motion signal from MediaPipe FaceMesh in the renderer.
                    # Used by _process_unsafe() to drop audio that didn't come from
                    # the on-camera user (videos, room voices, phone speakers).
                    global _last_mouth_active_at, _last_mouth_signal_at, _face_visible
                    import time as _t
                    _last_mouth_signal_at = _t.time()
                    _face_visible = bool(msg.get("face_visible", False))
                    if bool(msg.get("active", False)):
                        _last_mouth_active_at = _t.time()

                elif kind == "music_control":
                    # Renderer's now-playing widget sent a control command
                    # (play/pause/next/previous/seek). _music_control shells out
                    # to osascript (up to 3s) — run it in the default executor so
                    # it never blocks the event loop. Rapid scrubbing would
                    # otherwise freeze the whole UI (no frames read, broadcasts
                    # stalled) for seconds at a time.
                    _mc_action = msg.get("action", "")
                    _mc_value = msg.get("value")
                    asyncio.get_event_loop().run_in_executor(
                        None, lambda: _music_control(_mc_action, _mc_value)
                    )

                elif kind == "mute":
                    _mic_muted = True
                    if audio:
                        audio.suspend()
                    await websocket.send_text(json.dumps({"type": "muted"}))

                elif kind == "unmute":
                    _mic_muted = False
                    if audio:
                        _resume_audio()
                    await websocket.send_text(json.dumps({"type": "unmuted"}))

                elif kind == "confirm_response":
                    req_id   = msg.get("id")
                    approved = bool(msg.get("approved", False))
                    if req_id in _pending_confirms:
                        _pending_confirms[req_id][1] = approved
                        _pending_confirms[req_id][0].set()

                elif kind == "interrupt":
                    speaker.stop()
                    set_state("idle")

                elif kind == "reset":
                    jarvis.reset()

                elif kind == "study_start":
                    global _study_mode
                    _study_mode = True
                    if audio:
                        audio.start_study()
                    from tools.study_tool import get_session
                    get_session().start()
                    set_state("studying")
                    await websocket.send_text(json.dumps({"type": "study_started"}))

                elif kind == "study_stop":
                    _study_mode = False
                    from tools.study_tool import get_session
                    session = get_session()
                    transcript = session.get_transcript()
                    if audio:
                        audio.start_detecting()
                    set_state("idle")
                    if transcript:
                        prompt = (
                            f"[STUDY MODE] Summarize what was said in this class/lecture. "
                            f"Be thorough — cover all main points, key terms, and anything important. "
                            f"Here's the transcript: {transcript[:4000]}"
                        )
                        threading.Thread(target=process, args=(prompt,), kwargs={"skip_echo": True}, daemon=True).start()
                    else:
                        await websocket.send_text(json.dumps({"type": "chunk", "text": "No speech was captured during study mode."}))
                        await websocket.send_text(json.dumps({"type": "done", "full_text": "No speech was captured."}))

                elif kind == "study_summarize":
                    from tools.study_tool import get_session
                    session = get_session()
                    chunks = session.clear_chunks()
                    transcript = " ".join(chunks)
                    if transcript:
                        prompt = (
                            f"[STUDY MODE] Quick summary of what was just said: {transcript[:3000]}"
                        )
                        threading.Thread(target=process, args=(prompt,), kwargs={"skip_echo": True}, daemon=True).start()
                    else:
                        await websocket.send_text(json.dumps({"type": "chunk", "text": "Nothing recorded yet."}))
                        await websocket.send_text(json.dumps({"type": "done", "full_text": "Nothing recorded."}))

                elif kind == "study_practice":
                    # Generate practice problems / flashcards for a class.
                    # IMPORTANT: runs in its own thread WITHOUT TTS — display-only content.
                    # We bypass process() to avoid the lock and speaker.
                    subject = msg.get("subject", "").strip() or "the current subject"
                    mode    = msg.get("mode", "problems")   # "problems" | "flashcards"
                    count   = int(msg.get("count", 5))
                    if mode == "flashcards":
                        prompt = (
                            f"[STUDY] Generate exactly {count} flashcard pairs for: {subject}. "
                            f"Format each as: FRONT: [question/term] | BACK: [answer/definition]. "
                            f"One per line. Make them challenging and educational."
                        )
                    else:
                        prompt = (
                            f"[STUDY] Generate {count} practice problems for: {subject}. "
                            f"Include a mix of multiple choice, short answer, and application questions. "
                            f"After each question, put the answer on the next line starting with 'ANSWER:'. "
                            f"Make them AP/college-level difficulty."
                        )
                    def _run_study(p=prompt):
                        """Generate study content WITHOUT TTS — display-only."""
                        broadcast({"type": "user_message", "text": p[:80] + "..."})
                        set_state("thinking")
                        chunks: list[str] = []
                        set_state("speaking")
                        for chunk in jarvis.chat(p):
                            chunks.append(chunk)
                            broadcast({"type": "chunk", "text": chunk})
                        reply = "".join(chunks)
                        broadcast({"type": "done", "full_text": reply})
                        set_state("idle")
                        # Emit a structured study_content event for the UI panel
                        broadcast({"type": "study_content", "mode": mode,
                                   "subject": subject, "content": reply})
                    threading.Thread(target=_run_study, daemon=True).start()

                elif kind == "first_run_reset":
                    # Explicit re-open via WS (complements the voice command path)
                    from profile import reset_setup
                    reset_setup()
                    await websocket.send_text(json.dumps({"type": "show_first_run", "reset": True}))

                elif kind == "timer_cancel":
                    timer_id = msg.get("id", "")
                    _cancelled_timers.add(timer_id)

                elif kind == "system_status":
                    # Electron renderer reporting component health.
                    # Merge into the global status dict and signal startup.
                    incoming = msg.get("status", {})
                    _system_status.update(
                        {k: v for k, v in incoming.items() if v is not None}
                    )
                    # Keep personality module in sync so JARVIS's prompts are honest
                    from brain.personality import set_system_status
                    set_system_status(_system_status)
                    # Signal readiness once gesture has reported (True or False).
                    # MediaPipe Hands reports at ~3–4s after Electron loads.
                    _gest_done = _system_status.get("gesture") is not None
                    if not _status_received.is_set() and _gest_done:
                        _status_received.set()
                    print(f"[sys] status update: "
                          f"cam={_system_status.get('camera')} "
                          f"gesture={_system_status.get('gesture')}")

            except Exception as exc:
                print(f"[WS] message handler error (connection kept alive): {exc}")

    except WebSocketDisconnect:
        _clients.discard(websocket)
    except Exception as exc:
        print(f"[WS] unexpected connection error: {exc}")
        _clients.discard(websocket)


# ── Health check (used by Electron to know when server is ready) ──────────────

@app.get("/health")
async def health() -> dict:
    return {
        "ok":         True,
        "setup_mode": SETUP_MODE,
        "brain":      None if SETUP_MODE else (
            "anthropic" if ANTHROPIC_API_KEY else "groq"
        ),
    }


# ── Live sidebar data — weather + calendar ────────────────────────────────────

_WEATHER_CODES = {
    0: ("Clear", "☀️"), 1: ("Mostly Clear", "🌤️"), 2: ("Partly Cloudy", "⛅"),
    3: ("Overcast", "☁️"), 45: ("Foggy", "🌫️"), 48: ("Icy Fog", "🌫️"),
    51: ("Light Drizzle", "🌦️"), 53: ("Drizzle", "🌦️"), 55: ("Heavy Drizzle", "🌦️"),
    61: ("Light Rain", "🌧️"), 63: ("Rain", "🌧️"), 65: ("Heavy Rain", "🌧️"),
    71: ("Light Snow", "🌨️"), 73: ("Snow", "🌨️"), 75: ("Heavy Snow", "❄️"),
    80: ("Rain Showers", "🌦️"), 81: ("Showers", "🌦️"), 82: ("Heavy Showers", "⛈️"),
    85: ("Snow Showers", "🌨️"), 86: ("Heavy Snow Showers", "❄️"),
    95: ("Thunderstorm", "⛈️"), 96: ("Thunderstorm", "⛈️"), 99: ("Severe Storm", "⛈️"),
}


async def _push_sidebar_data() -> None:
    """Fetch weather + today's calendar events and broadcast to the frontend sidebar."""
    import asyncio as _aio

    # Small delay — let WebSocket client connect first
    await _aio.sleep(3.0)

    # ── Weather via Open-Meteo (free, no key) ──────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=8) as cli:
            # Step 1: get approximate lat/lon from IP
            geo = await cli.get("https://ipinfo.io/json")
            geo_data = geo.json()
            loc_str   = geo_data.get("loc", "33.749,-84.388")   # fallback: Atlanta
            city      = geo_data.get("city",   "Atlanta")
            region    = geo_data.get("region", "GA")
            lat, lon  = loc_str.split(",")

            # Step 2: Open-Meteo current weather
            wx_url = (
                f"https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat}&longitude={lon}"
                f"&current=temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m"
                f"&temperature_unit=fahrenheit&wind_speed_unit=mph&timezone=auto"
            )
            wx = await cli.get(wx_url)
            wx_data = wx.json().get("current", {})
            temp_f   = round(wx_data.get("temperature_2m", 0))
            wcode    = wx_data.get("weather_code", 0)
            wind     = round(wx_data.get("wind_speed_10m", 0))
            humidity = round(wx_data.get("relative_humidity_2m", 0))
            desc, icon = _WEATHER_CODES.get(wcode, ("Unknown", "🌡️"))

            broadcast({
                "type":     "weather_update",
                "temp_f":   temp_f,
                "desc":     desc,
                "icon":     icon,
                "wind":     wind,
                "humidity": humidity,
                "city":     city,
                "region":   region,
            })
            print(f"[sidebar] weather: {temp_f}°F {desc} in {city}, {region}")
    except Exception as e:
        print(f"[sidebar] weather fetch failed: {e}")
        broadcast({"type": "weather_update", "temp_f": None, "desc": "Unavailable",
                   "icon": "🌡️", "city": "", "region": ""})

    # ── Calendar via macOS osascript ───────────────────────────────────────────
    try:
        from tools.calendar_tool import get_calendar_events
        raw = get_calendar_events(days=1)
        print(f"[sidebar] calendar raw: {repr(raw[:200])}", flush=True)
        import re as _re
        events = []
        for line in raw.split("\n"):
            # Format: "  • Title — Sunday, May 11, 2026 at 2:00:00 PM (Calendar)"
            m = _re.match(r"\s*•\s+(.+?)\s+—\s+(.+?)\s+\(", line)
            if m:
                title, when = m.group(1).strip(), m.group(2).strip()
                # Handle both "2:00 PM" and "2:00:00 PM" (AppleScript adds seconds)
                t = _re.search(r'(\d{1,2}:\d{2})(?::\d{2})?\s*([AP]M)', when)
                short_time = f"{t.group(1)} {t.group(2)}" if t else when[:10]
                events.append({"title": title, "time": short_time})
        broadcast({"type": "agenda_update", "events": events[:6]})
        print(f"[sidebar] calendar: {len(events)} events today")
    except Exception as e:
        print(f"[sidebar] calendar fetch failed: {e}")
        broadcast({"type": "agenda_update", "events": []})

    # ── Recent files via mdfind ────────────────────────────────────────────────
    try:
        import subprocess as _sp
        import os as _os
        home = _os.path.expanduser("~")
        result = _sp.run(
            ["mdfind", "-attr", "kMDItemLastUsedDate",
             "kMDItemLastUsedDate >= $time.now(-1209600)",
             "-onlyin", home],
            capture_output=True, text=True, timeout=6,
        )
        _EXT_ICONS = {
            "pdf": "📄", "doc": "📝", "docx": "📝", "txt": "📝", "md": "📝",
            "xls": "📊", "xlsx": "📊", "csv": "📊", "numbers": "📊",
            "ppt": "📊", "pptx": "📊", "key": "📊",
            "jpg": "🖼️", "jpeg": "🖼️", "png": "🖼️", "gif": "🖼️", "heic": "🖼️",
            "mp4": "🎬", "mov": "🎬", "avi": "🎬",
            "mp3": "🎵", "wav": "🎵", "m4a": "🎵",
            "zip": "📦", "dmg": "📦",
            "py": "💻", "js": "💻", "ts": "💻", "html": "💻", "css": "💻",
            "json": "💻", "sh": "💻",
        }
        _SKIP_PATHS = (
            "/Library/", "/System/", "/.Trash/", "/Caches/",
            "com.apple.", ".DS_Store", "/tmp/", "/.git/",
            "node_modules", "/.venv/", "/__pycache__/", "/Logs/",
        )
        # Preferred extensions: real documents come before screenshots
        _DOC_EXTS = {"pdf","doc","docx","xls","xlsx","csv","ppt","pptx","key",
                     "numbers","txt","md","py","js","ts","html","mp4","mov"}

        import re as _re2
        candidates = []  # (ts_str, path, is_doc)
        for raw_line in result.stdout.splitlines():
            parts = raw_line.split("\t")
            path = parts[0].strip()
            ts_raw = parts[1].strip() if len(parts) > 1 else ""
            if not path or any(skip in path for skip in _SKIP_PATHS):
                continue
            basename = _os.path.basename(path)
            if not basename or basename.startswith(".") or not _os.path.isfile(path):
                continue
            ext = basename.rsplit(".", 1)[-1].lower() if "." in basename else ""
            is_doc = ext in _DOC_EXTS
            # Extract timestamp for sorting: "2026-05-11 13:44:21 +0000"
            ts_m = _re2.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', ts_raw)
            ts_str = ts_m.group(1) if ts_m else "0000-00-00 00:00:00"
            candidates.append((ts_str, path, basename, ext, is_doc))

        # Sort: docs first, then by timestamp descending
        candidates.sort(key=lambda c: (0 if c[4] else 1, c[0]), reverse=True)
        # Re-sort so docs are first but still newest within each group
        candidates.sort(key=lambda c: (not c[4], c[0] if not c[4] else c[0]), reverse=True)

        seen_names: set[str] = set()
        files = []
        for ts_str, path, basename, ext, is_doc in candidates:
            if basename in seen_names:
                continue
            seen_names.add(basename)
            icon = _EXT_ICONS.get(ext, "📄")
            files.append({"name": basename, "icon": icon, "path": path})
            if len(files) >= 7:
                break

        broadcast({"type": "files_update", "files": files})
        print(f"[sidebar] recent files: {len(files)} found", flush=True)
    except Exception as e:
        print(f"[sidebar] recent files fetch failed: {e}", flush=True)
        broadcast({"type": "files_update", "files": []})


# ── Startup: launch audio engine ──────────────────────────────────────────────

async def startup() -> None:
    global audio, _loop
    _loop = asyncio.get_running_loop()

    # In setup mode there is no brain to talk to; skip voice + tool wiring so
    # the wizard endpoints are the only thing accepting requests.
    if SETUP_MODE:
        print("[setup] Skipping audio + tool wiring until wizard finishes.")
        return

    # Start proactive intelligence engine (calendar, email, memory checks)
    import proactive
    proactive.start()

    # ── Notification monitor — announces incoming Messages, Snapchat, etc. ────
    # _on_notification + _start_notif_monitor live at module scope now so the
    # diagnostics self-fixer can restart this correctly.
    try:
        if _start_notif_monitor():
            print("[notif] monitor started", flush=True)
    except Exception as exc:
        print(f"[notif] monitor failed to start: {exc}", flush=True)

    # ── Background health check — non-blocking ─────────────────────────────────
    # Probes weather API, AI API, and TTS on startup so JARVIS knows what's
    # actually working before the user asks.
    threading.Thread(target=_run_health_check, daemon=True, name="health-check").start()

    # ── Always-on music poller — surfaces now-playing widget regardless of who
    # started the music (JARVIS intercept, Dylan clicking Apple Music, AirPods
    # pause/play, etc.). Polls every 4s; auto-pauses after 2 min of silence.
    _start_music_poller()
    # ── System audio detector — catches video / browser / podcast audio
    # so the mic switches to wake-word mode for ANY audio, not just Music.
    _start_system_audio_watcher()

    # ── Screen watcher — silently auto-skips YouTube ads, dismisses cookie
    # banners, clicks "Continue Watching" prompts, hides macOS update nags.
    # Polls the focused window every ~1.5s; cheap, conservative, no chatter.
    try:
        from tools.screen_watcher import get_watcher as _get_screen_watcher
        _get_screen_watcher().start()
    except Exception as exc:
        print(f"[watcher] failed to start: {exc}", flush=True)

    # ── Unified mode manager — auto-detects watch / gaming / work / normal
    # from the frontmost app every 30s and applies the right behavior
    # (compact HUD, notification silencing, ad-skip).
    try:
        from tools.mode_manager import get_manager
        get_manager(on_mode_change=_on_mode_change, poll_sec=30.0).start()
        print("[mode] manager started", flush=True)
    except Exception as exc:
        print(f"[mode] failed to start: {exc}", flush=True)

    try:
        from voice.audio_engine import AudioEngine

        def on_transcription(kind: str, text: str) -> None:
            import time as _t
            stripped = text.strip()

            # ── Sleep mode: only process wake-up phrases ──────────────────────
            if _sleep_mode:
                low_s = stripped.lower()
                if any(p in low_s for p in _WAKE_PHRASES):
                    threading.Thread(target=process, args=(stripped,), daemon=True).start()
                return  # silently drop everything else

            # ── Truncated speech guard ────────────────────────────────────────
            # Whisper sometimes cuts off mid-utterance ("hey jarvis, run a-")
            # when the user pauses or trails off. Sending that to the brain
            # makes it guess wildly (saw take_screenshot called on "run a-").
            # Drop if the text ends mid-word: trailing dash, ellipsis, or a
            # tiny utterance that ends on a stop-word like 'a', 'the', 'my'.
            # Check trail BEFORE stripping punctuation — ellipsis is itself
            # a signal of truncation.
            _raw_lower = stripped.lower()
            _trails = _raw_lower.rstrip().endswith(("-", "—", "...", "…"))
            _norm_strip = _raw_lower.strip(".,!?")
            _ends_open = _norm_strip.endswith((
                " a", " the", " my", " your", " an", " of", " to", " in", " on",
                " for", " with", " from", " is", " are", " was", " were",
                " can", " can you", " could", " could you", " would", " should",
            ))
            _too_short = len(_norm_strip.split()) <= 4
            if (_trails or (_ends_open and _too_short)):
                print(f"[truncated] Dropped mid-utterance: {stripped!r}", flush=True)
                if audio:
                    audio.start_conversing() if not _focus_mode else audio.start_detecting()
                return

            # ── Single-word noise filter ──────────────────────────────────────
            # Whisper hallucinates single words from silence, so we usually
            # drop them. EXCEPTION: during music playback, allow short music
            # control verbs through (Dylan saying "pause" by itself).
            if len(stripped.split()) < 2:
                _control_words = {
                    "pause", "unpause", "resume", "skip", "next",
                    "previous", "stop", "play", "back", "continue",
                }
                _bare = stripped.lower().strip(".,!? ")
                _allow_bare = (_music_is_playing or _system_audio_playing) and _bare in _control_words
                if not _allow_bare:
                    if audio:
                        audio.start_conversing() if not _focus_mode else audio.start_detecting()
                    return

            # ── Echo guard: drop JARVIS hearing himself ───────────────────────
            if _is_echo(stripped):
                print(f"[echo] Dropped self-pickup: {stripped!r}")
                if audio:
                    audio.start_conversing() if not _focus_mode else audio.start_detecting()
                return

            # ── Focus / media mode: require "Jarvis" to be addressed ──────────
            # In focus mode (pitch/meeting), JARVIS ignores room conversation.
            # After media opens, JARVIS requires the wake word for ~30s (see
            # _resume_audio) so video/lyrics don't fire commands.
            _in_media_mode = _t.time() < _media_mode_until
            if _focus_mode or _in_media_mode:
                _has_wake = any(w in stripped.lower() for w in ["jarvis", "hey jarvis", "ok jarvis"])
                if not _has_wake:
                    # Silently drop — user is talking to someone else or video is playing
                    if audio:
                        audio.start_detecting()
                    return

            threading.Thread(target=process, args=(stripped,), daemon=True).start()

        def on_barge_in() -> None:
            global _last_reply
            speaker.stop()
            _last_reply = ""   # clear echo filter so user's first words aren't dropped
            if audio:
                audio.start_conversing()

        def on_speech_end() -> None:
            """Play a very subtle click the instant speech ends so there's no dead silence."""
            if _sleep_mode:
                return
            # Suppress the click when music or system audio is playing — most
            # captured "speech" during music is actually lyrics that'll be
            # dropped by the music gate, so the click is just annoying noise.
            if _music_is_playing or _system_audio_playing:
                return
            try:
                import subprocess as _sp
                # Tink is a tiny built-in macOS click — 0.12 volume keeps it subtle
                _sp.Popen(
                    ["afplay", "-v", "0.12", "/System/Library/Sounds/Tink.aiff"],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                )
            except Exception:
                pass

        audio = AudioEngine(
            on_transcription=on_transcription,
            on_barge_in=on_barge_in,
            on_speech_end=on_speech_end,
        )
        # Suspend immediately — startup greeting fires first, then _resume_audio()
        # re-enables listening.  Prevents ambient noise from triggering process()
        # and blocking the greeting with a "thinking" state race.
        audio.suspend()
        _system_status["voice"] = True
        print("Audio engine ready — always-on mode.")

    except Exception as exc:
        _system_status["voice"] = False
        print(f"Voice unavailable: {exc}")

    # Wire up file/shell permission prompts to the UI
    from tools.permissions import set_callback
    set_callback(request_permission)

    # Wire up broadcast callback for show_overlay tool
    jarvis.tools_executor.broadcast_callback = broadcast

    # Wire up computer-agent live HUD callback
    def _computer_agent_action(action: str, detail: str) -> None:
        broadcast({"type": "computer_agent_action", "action": action, "detail": detail})
    jarvis.tools_executor.computer_agent_callback = _computer_agent_action

    # Wire up camera capture callback for read_camera tool
    def _capture_camera_frame(timeout: float = 5.0):
        """
        Ask the Electron renderer to snapshot the live camera, wait for the frame.
        Returns base64 JPEG string or None on timeout.
        """
        import uuid as _uuid
        req_id = _uuid.uuid4().hex[:8]
        event  = threading.Event()
        _camera_frames[req_id] = [event, None]
        broadcast({"type": "camera_capture_request", "id": req_id})
        ok = event.wait(timeout=timeout)
        entry = _camera_frames.pop(req_id, [None, None])
        return entry[1] if ok else None

    jarvis.tools_executor.camera_capture_callback = _capture_camera_frame

    # NOTE: _push_sidebar_data() is now called per-connection (in ws_endpoint)
    # so sidebar data actually reaches the client. Calling it here (before any
    # client is connected) would broadcast to 0 clients and data would be lost.

    # Startup greeting is triggered when the first WebSocket client connects
    # (see ws_endpoint below) so the countdown starts from actual connection
    # time, not server start time.


def _startup_speak() -> None:
    """
    JARVIS announces himself on boot.
    Checks actual component status — never lies about what's working.
    """
    from datetime import datetime
    now  = datetime.now()
    hour = now.hour
    hhmm = now.strftime("%I:%M %p").lstrip("0")

    # ── Time-of-day greeting ───────────────────────────────────────────────────
    if hour < 12:
        tod = "Good morning"
    elif hour < 17:
        tod = "Good afternoon"
    else:
        tod = "Good evening"

    # ── Wait briefly for health check thread to report ────────────────────────
    # Health check runs concurrently; give it up to 8s before greeting fires.
    import time as _t
    _deadline = _t.time() + 8
    while _t.time() < _deadline:
        if _system_status.get("ai_api") is not None:
            break
        _t.sleep(0.25)

    # ── Honest system status — check everything ────────────────────────────────
    issues: list[str] = []

    cam     = _system_status.get("camera")
    gest    = _system_status.get("gesture")
    ai_api  = _system_status.get("ai_api")
    weather = _system_status.get("weather_api")
    tts     = _system_status.get("tts")
    voice   = _system_status.get("voice")

    if cam is False:
        err = _system_status.get("camera_error", "")
        issues.append("camera access denied" if "denied" in str(err) else "camera offline")
    if gest is False:
        issues.append("gesture control unavailable")
    if ai_api is False:
        issues.append("AI API unreachable — check your API key")
    if weather is False:
        issues.append("weather service offline")
    if tts is False:
        issues.append("text-to-speech unavailable")
    if voice is False:
        issues.append("microphone unavailable")

    # ── Build greeting ─────────────────────────────────────────────────────────
    if not issues:
        greeting = f"{tod}, {USER_ADDRESS}. The time is {hhmm}. All systems operational."
    elif len(issues) == 1:
        greeting = (
            f"{tod}, {USER_ADDRESS}. The time is {hhmm}. "
            f"One issue: {issues[0]}."
        )
    else:
        issue_list = "; ".join(issues)
        greeting = (
            f"{tod}, {USER_ADDRESS}. The time is {hhmm}. "
            f"{len(issues)} issues detected: {issue_list}."
        )

    print(f"[startup] greeting: {greeting!r}")
    broadcast({"type": "chunk",    "text": greeting})
    broadcast({"type": "done",     "full_text": greeting})
    broadcast({"type": "state",    "state": "speaking"})

    global _last_reply  # noqa: PLW0603
    _last_reply = greeting

    # Suspend mic before speaking so JARVIS doesn't hear himself through the
    # MacBook speakers and fire process() mid-greeting (which calls speaker.stop()
    # at the top, killing the greeting sentence before it finishes).
    if audio:
        audio.suspend()

    speaker.stream_speak(iter([greeting]))
    broadcast({"type": "state", "state": "idle"})

    # Same 2.5s buffer as process() — lets TTS audio die out before re-opening mic
    import time as _t
    _t.sleep(2.5)
    _resume_audio()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
