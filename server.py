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

from config import ANTHROPIC_API_KEY, GROQ_API_KEY, USER_ADDRESS

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

# ── System component status — updated by the Electron renderer ────────────────
# This is the ground truth for JARVIS's self-awareness. Each field is None
# (not reported yet), True (online), or False (failed/offline).
_system_status: dict = {
    "camera":         None,
    "gesture":        None,
    "websocket":      None,
    "camera_error":   None,
}
_status_received  = threading.Event()   # set once gesture reports in
_greeting_started = False              # guard — greet only on the first ever connection
_mic_muted        = False              # True while user has manually muted the mic
_cancelled_timers: set[str] = set()   # timer IDs cancelled by user

# ── Focus / Presentation mode ─────────────────────────────────────────────────
# When True, JARVIS only responds if "Jarvis" appears in the utterance.
# Use this in pitches, meetings, or anywhere other people are talking nearby.
_focus_mode = False
# After media opens (YouTube, videos), require "Jarvis" for this many seconds
# to prevent JARVIS from responding to video audio.
_media_mode_until: float = 0.0


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


def request_permission(message: str, timeout: float = 30.0) -> bool:
    """
    Ask the user for permission via the Electron UI.
    Blocks the calling thread until the user responds (or timeout).
    """
    req_id = str(uuid.uuid4())[:8]
    event  = threading.Event()
    _pending_confirms[req_id] = [event, False]

    broadcast({"type": "confirm_request", "id": req_id, "message": message})

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
        # Require "Jarvis" for 60 seconds after media opens — stops video audio pickup
        _media_mode_until = _t.time() + 60.0
        audio.start_detecting()
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

def process(text: str, skip_echo: bool = False) -> None:
    """Top-level guard — wraps _process_unsafe so a single tool failure
    can't hang JARVIS or kill the worker thread silently."""

    # ── Setup reset shortcut ───────────────────────────────────────────────────
    low = text.lower().strip()

    # ── Self-update shortcut ───────────────────────────────────────────────────
    if any(low.startswith(p) or p in low for p in _UPDATE_PHRASES):
        import subprocess as _sp
        result = _sp.run(
            ["git", "-C", os.path.dirname(os.path.abspath(__file__)), "pull"],
            capture_output=True, text=True, timeout=30,
        )
        if "Already up to date" in result.stdout:
            msg = "Already up to date — you're running the latest version."
        elif result.returncode == 0:
            msg = "Updated! Restart JARVIS to apply the latest changes."
        else:
            msg = f"Update failed: {result.stderr.strip()[:120]}"
        broadcast({"type": "chunk", "text": msg})
        broadcast({"type": "done", "full_text": msg})
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

        def _gen():
            set_state("speaking")
            # Mic stays suspended during speech — barge-in self-triggers on
            # MacBook speakers. Post-speech echo detection handles the gap.
            profiler.mark("brain_start")
            for chunk in jarvis.chat(text):
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

        # ── Activate barge-in BEFORE speaking ─────────────────────────────
        # This puts the mic into energy-watch mode — if the user starts
        # talking while JARVIS is speaking, on_barge_in fires immediately,
        # speaker.stop() kills the TTS, and the user's command is heard.
        if audio:
            audio.start_barge_in()

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
                # Give it a tiny moment for the audio flush to hit STT, then
                # release the lock so the barge-in command thread can run.
                _time.sleep(0.2)
            else:
                # Normal finish — 1.0s lets TTS audio die out before mic opens.
                # Echo guard in _process_unsafe catches any residual bleed.
                _time.sleep(1.0)
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

    try:
        from voice.audio_engine import AudioEngine

        def on_transcription(kind: str, text: str) -> None:
            import time as _t
            stripped = text.strip()

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
            speaker.stop()
            if audio:
                audio.start_conversing()

        audio = AudioEngine(on_transcription=on_transcription, on_barge_in=on_barge_in)
        # Suspend immediately — startup greeting fires first, then _resume_audio()
        # re-enables listening.  Prevents ambient noise from triggering process()
        # and blocking the greeting with a "thinking" state race.
        audio.suspend()
        print("Audio engine ready — always-on mode.")

    except Exception as exc:
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

    # ── Fetch live weather + calendar and push to sidebar ─────────────────────
    asyncio.ensure_future(_push_sidebar_data())

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

    # ── Honest system status ───────────────────────────────────────────────────
    issues: list[str] = []

    cam  = _system_status.get("camera")
    gest = _system_status.get("gesture")

    # Camera
    if cam is False:
        err = _system_status.get("camera_error", "")
        if "denied" in str(err):
            issues.append("camera access denied — enable it in System Settings")
        else:
            issues.append("camera offline")

    # Gesture / pinch
    if gest is False:
        issues.append("gesture control unavailable")

    # ── Build greeting ─────────────────────────────────────────────────────────
    if not issues:
        greeting = f"{tod}, {USER_ADDRESS}. The time is {hhmm}. All systems operational."
    elif len(issues) == 1:
        greeting = (
            f"{tod}, {USER_ADDRESS}. The time is {hhmm}. "
            f"Systems online. One issue detected: {issues[0]}."
        )
    else:
        issue_list = "; ".join(issues)
        greeting = (
            f"{tod}, {USER_ADDRESS}. The time is {hhmm}. "
            f"Systems online. {len(issues)} issues detected: {issue_list}."
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
