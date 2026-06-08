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


def _run_health_check() -> None:
    """
    Probe real system components and update _system_status + personality.
    Runs once on startup in a background thread. Broadcasts updated status to UI.
    """
    import time as _t

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


def _is_echo(text: str) -> bool:
    """
    Return True if `text` looks like JARVIS picking up his own TTS output.
    Uses word-overlap against the last spoken reply — >45% overlap = echo.
    Also catches very short fragments that are almost certainly mic bleed.
    """
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
    if jarvis.tools_executor.media_opened:
        jarvis.tools_executor.media_opened = False
        # Music/media is playing through speakers — suspend mic entirely.
        # Whisper would transcribe lyrics and music as commands otherwise.
        # Mic resumes when pause_music() calls _resume_audio() again.
        _media_mode_until = _t.time() + 3600.0
        audio.suspend()
        return
    if _focus_mode:
        audio.start_detecting()
        return
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
)
_WORK_EXIT_PHRASES = (
    "exit work mode", "leave work mode", "stop work mode",
    "normal mode", "back to normal", "exit focus mode",
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


def _gaming_monitor() -> None:
    """Background thread: capture + describe screen every 30 s in work mode."""
    import time as _t
    while _gaming_mode:
        if _capture_screen():
            # Pre-fetch description now so it's cached when user speaks
            _get_screen_desc()
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
        from tools.ar_builder_tool import get_scene as _gs, apply_operations as _ao
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

    # ── Declare globals before any read/write of them ────────────────────────
    global _gaming_mode, _work_mode

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

    # ── Gaming Mode — compact HUD, no screen watching ────────────────────────
    if any(p in low for p in _GAMING_ENTER_PHRASES) and not _gaming_mode and not _work_mode:
        _gaming_mode = True
        broadcast({"type": "gaming_mode_enter"})
        _say("Gaming mode. I'm here.")
        return
    if any(p in low for p in _GAMING_EXIT_PHRASES) and _gaming_mode:
        _gaming_mode = False
        broadcast({"type": "gaming_mode_exit"})
        _say("Back to normal.")
        return

    # ── Work Mode — compact HUD + 24/7 screen watching ───────────────────────
    if any(p in low for p in _WORK_ENTER_PHRASES) and not _work_mode and not _gaming_mode:
        _work_mode = True
        _gaming_mode = True   # reuse the same compact HUD
        from brain.personality import set_work_mode
        set_work_mode(True)
        broadcast({"type": "gaming_mode_enter"})
        import threading as _thr
        _thr.Thread(target=_gaming_monitor, daemon=True, name="screen-monitor").start()
        _say("Work mode. I'm watching your screen.")
        return
    if any(p in low for p in _WORK_EXIT_PHRASES) and _work_mode:
        _work_mode = False
        _gaming_mode = False
        from brain.personality import set_work_mode
        set_work_mode(False)
        broadcast({"type": "gaming_mode_exit"})
        _say("Back to normal.")
        return

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
            import time as _t
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
            broadcast({"type": "user_message", "text": text})
            set_state("thinking")
            full: list[str] = []

            def _gen():
                set_state("speaking")
                profiler.mark("brain_start")
                first = True
                for chunk in jarvis.chat(text):
                    if first:
                        profiler.mark("brain_first_chunk")
                        first = False
                    full.append(chunk)
                    broadcast({"type": "chunk", "text": chunk})
                    yield chunk

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
                result = _hw_mod.stop_loop()
                ack = "Auto-answer stopped."
            elif _hw_mod.is_running():
                ack = "Already running — I'm still working through the questions."
                result = ack
            else:
                result = _hw_mod.start_loop(broadcast_fn=broadcast)
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

        broadcast({"type": "user_message", "text": text})
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
        # (TTS synthesis hasn't started yet) and the threshold stays at the floor.
        # Delaying 600ms means the first sentence of audio IS playing when we
        # sample, so the dynamic threshold properly accounts for speaker bleed.
        def _delayed_barge_in():
            import time as _bt
            _bt.sleep(0.6)
            if audio:
                audio.start_barge_in()
        if audio:
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
                def _barge_fallback():
                    _time.sleep(2.5)
                    if audio and not _lock.locked():
                        _resume_audio()
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
        import subprocess as _sp, os as _os, datetime as _dt
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

    # ── Background health check — non-blocking ─────────────────────────────────
    # Probes weather API, AI API, and TTS on startup so JARVIS knows what's
    # actually working before the user asks.
    threading.Thread(target=_run_health_check, daemon=True, name="health-check").start()

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

            # ── Single-word noise filter ──────────────────────────────────────
            if len(stripped.split()) < 2:
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
            # After media opens, JARVIS ignores video audio for 60 seconds.
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
