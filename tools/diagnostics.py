"""
JARVIS system diagnostics — comprehensive health check.

Probes ~22 subsystems (APIs, threads, models, permissions, disk, network),
yields per-check events so the UI can animate live, narrates failures with
the impact on JARVIS's behavior, and attempts auto-fixes where safe.

Each Check has:
  - name     : machine-readable id ("notif_monitor")
  - label    : short human label ("Notification monitor")
  - probe    : () -> bool — returns True iff healthy
  - impact   : str — one-line description of what breaks when this fails
  - fix      : Optional[() -> bool] — try to repair; True if recovered
  - critical : bool — if True, a failure spoken with more urgency
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Generator, Optional

# Self-inspect helpers — used by _investigate_failure() to read captured logs
from tools.self_inspect import read_jarvis_log


# ── Helpers ───────────────────────────────────────────────────────────────────

def _thread_alive(name: str) -> bool:
    """True if a daemon thread with the given name is currently alive."""
    return any(t.is_alive() and t.name == name for t in threading.enumerate())


def _has_file(path: str) -> bool:
    p = os.path.expanduser(path)
    return os.path.exists(p) and os.path.getsize(p) > 0


def _app_installed(bundle: str) -> bool:
    """True if /Applications/<bundle>.app exists."""
    return os.path.exists(f"/Applications/{bundle}.app") or \
           os.path.exists(f"/System/Applications/{bundle}.app")


# ── Probes ────────────────────────────────────────────────────────────────────

def _probe_weather() -> bool:
    try:
        import ssl, certifi, urllib.request
        ctx = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.Request(
            "https://wttr.in/Atlanta?format=j1",
            headers={"User-Agent": "curl/7.68.0"},
        )
        with urllib.request.urlopen(req, timeout=5, context=ctx):
            return True
    except Exception:
        return False


def _probe_cloud_brain() -> bool:
    """Groq or Anthropic — fast ping."""
    try:
        from config import GROQ_API_KEY, ANTHROPIC_API_KEY
        if GROQ_API_KEY:
            from openai import OpenAI
            c = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
            r = c.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": "ok"}],
                max_tokens=2,
            )
            return bool(r.choices)
        if ANTHROPIC_API_KEY:
            import anthropic
            c = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            r = c.messages.create(
                model="claude-haiku-20240307",
                messages=[{"role": "user", "content": "ok"}],
                max_tokens=2,
            )
            return bool(r.content)
    except Exception as exc:
        # A 429 / rate-limit means the cloud brain is UP and reachable — just
        # throttled. Reporting that as "down" makes JARVIS falsely announce
        # "I'll fall back to the local brain" right after heavy use. Only real
        # connectivity/auth failures count as down.
        msg = str(exc).lower()
        if any(s in msg for s in ("429", "rate limit", "rate_limit",
                                  "too many requests", "quota")):
            return True
        return False
    return False


def _probe_local_brain() -> bool:
    try:
        from config import OLLAMA_URL
        import httpx
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


def _probe_tts_elevenlabs() -> bool:
    """API key set + library import + voice list reachable."""
    try:
        if not os.environ.get("ELEVENLABS_API_KEY"):
            return False
        import urllib.request
        req = urllib.request.Request(
            "https://api.elevenlabs.io/v1/voices",
            headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            return resp.status == 200
    except Exception:
        return False


def _probe_tts_edge() -> bool:
    try:
        import edge_tts   # noqa
        return True
    except Exception:
        return False


def _probe_whisper() -> bool:
    try:
        import whisper   # noqa
        # Confirm the small model file is on disk
        cache = os.path.expanduser("~/.cache/whisper")
        return os.path.isdir(cache) and any(
            f.endswith(".pt") for f in os.listdir(cache)
        )
    except Exception:
        return False


def _server_mod():
    """
    The RUNNING server module. Launched via `python server.py`, that's
    __main__ — NOT the "server" entry in sys.modules. An `import server`
    elsewhere creates a SECOND, un-started copy whose globals (audio,
    _system_status, _clients) are all at their initial empty values. Reading
    THAT copy is what made the mic + camera probes report false failures while
    the mic/camera were actually working. Prefer the module really running.
    """
    import sys
    main = sys.modules.get("__main__")
    if main is not None and str(getattr(main, "__file__", "")).endswith("server.py"):
        return main
    return sys.modules.get("server")


def _probe_mic_perm() -> bool:
    """
    Mic accessible. Inferred from the audio engine: `audio.ready` goes True once
    the input stream is created, which requires mic permission. (The old check
    looked for a non-existent `is_running` attribute AND read the wrong module
    copy — so it reported the mic as failed while it was working fine.)
    """
    srv = _server_mod()
    if not srv:
        return False
    audio = getattr(srv, "audio", None)
    if audio is None:
        return False
    return bool(getattr(audio, "ready", True))


def _probe_camera_perm() -> bool:
    """
    Camera permission. The renderer only sends `camera: true` while the
    camera is actively streaming, so we also accept `gesture: true` (which
    requires camera) as proof permission is granted.
    """
    srv = _server_mod()
    if not srv:
        return False
    status = getattr(srv, "_system_status", {})
    return bool(status.get("camera") or status.get("gesture"))


def _probe_notif_monitor() -> bool:
    return _thread_alive("notif-monitor")


def _probe_music_poller() -> bool:
    return _thread_alive("music-poller")


def _probe_system_audio() -> bool:
    return _thread_alive("sys-audio-watcher")


def _probe_screen_watcher() -> bool:
    return _thread_alive("screen-watcher")


def _probe_proactive() -> bool:
    return _thread_alive("proactive")


def _probe_voice_profile() -> bool:
    return _has_file("~/.jarvis_voice_profile.npy")


def _probe_history_file() -> bool:
    """Conversation history — failing means JARVIS forgets between restarts."""
    p = os.path.expanduser("~/.jarvis_history.json")
    # File may not exist on first run — that's fine. Failure = file exists but unreadable.
    if not os.path.exists(p):
        return True
    try:
        with open(p, "r") as f:
            f.read(1024)
        return True
    except Exception:
        return False


def _probe_disk_space() -> bool:
    """At least 5 GB free on the boot drive."""
    try:
        usage = shutil.disk_usage("/")
        return usage.free > 5 * 1024 ** 3
    except Exception:
        return False


def _probe_memory_pressure() -> bool:
    """Under 90% memory pressure."""
    try:
        r = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=3)
        # Sample: "System-wide memory free percentage: 12%"
        for ln in r.stdout.splitlines():
            if "free percentage" in ln:
                pct = int(ln.strip().rstrip("%").split()[-1])
                return pct > 10   # > 10% free = OK
        return True   # parse failed → assume OK
    except Exception:
        return True   # don't fail the check on a missing util


def _probe_network() -> bool:
    """Can we reach the public internet via DNS?"""
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=2).close()
        return True
    except Exception:
        return False


def _probe_apple_music() -> bool:
    return _app_installed("Music")


def _probe_calendar_app() -> bool:
    return _app_installed("Calendar")


def _probe_messages_app() -> bool:
    return _app_installed("Messages")


def _probe_mail_app() -> bool:
    return _app_installed("Mail")


def _probe_websocket_clients() -> bool:
    """Renderer is connected if there's at least one WS subscriber."""
    srv = _server_mod()
    if not srv:
        return False
    clients = getattr(srv, "_clients", None)
    if clients is None:
        return False
    try:
        return len(clients) > 0
    except Exception:
        return False


def _probe_speaker_module() -> bool:
    """TTS speaker module loaded and the live Speaker instance is responsive."""
    try:
        from voice.speaker import Speaker
        if not hasattr(Speaker, "stream_speak"):
            return False
        # If server is up, also confirm the singleton is alive
        try:
            from server import speaker as _s
            return _s is not None and hasattr(_s, "stream_speak")
        except Exception:
            return True   # module loads even if server hasn't started
    except Exception:
        return False


# ── Auto-fix functions ────────────────────────────────────────────────────────

def _fix_notif_monitor() -> bool:
    """
    Restart the notification monitor with the REAL announcement callback.

    Delegates to server._start_notif_monitor(), which stops any dead monitor
    and starts a fresh one wired to server._on_notification. (Previously this
    installed a no-op lambda because the callback was a nested function it
    couldn't reach — so it reported "fixed" while announcements stayed dead.)
    """
    try:
        import server
        starter = getattr(server, "_start_notif_monitor", None)
        if starter is None:
            return False              # honest: can't safely restart, don't claim success
        ok = bool(starter())
        time.sleep(0.5)
        return ok and _thread_alive("notif-monitor")
    except Exception:
        return False


def _fix_music_poller() -> bool:
    try:
        from server import _start_music_poller
        _start_music_poller()
        time.sleep(0.5)
        return _thread_alive("music-poller")
    except Exception:
        return False


def _fix_system_audio() -> bool:
    try:
        from server import _start_system_audio_watcher
        _start_system_audio_watcher()
        time.sleep(0.5)
        return _thread_alive("sys-audio-watcher")
    except Exception:
        return False


def _fix_screen_watcher() -> bool:
    try:
        from tools.screen_watcher import get_watcher
        w = get_watcher()
        if not w.running:
            w.start()
        time.sleep(0.5)
        return _thread_alive("screen-watcher")
    except Exception:
        return False


def _fix_local_brain() -> bool:
    """Try to spin up Ollama via the CLI."""
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(2.0)
        return _probe_local_brain()
    except Exception:
        return False


# ── Check registry ────────────────────────────────────────────────────────────

@dataclass
class Check:
    name: str
    label: str
    probe: Callable[[], bool]
    impact: str                                # spoken when this check fails
    fix: Optional[Callable[[], bool]] = None   # None = can't auto-fix
    critical: bool = False
    # ── Investigation hints — used by the auto-investigation step when this
    #    check fails. The runner uses these to grep logs + point the brain
    #    at the relevant source so "why did X fail" gets a real answer.
    log_keywords: list[str] = field(default_factory=list)
    source_files: list[str] = field(default_factory=list)


CHECKS: list[Check] = [
    # ── External APIs ─────────────────────────────────────────────────────────
    Check("cloud_brain", "Cloud reasoning core", _probe_cloud_brain,
          "I'll fall back to the local brain — replies may take longer and be shorter.",
          critical=True,
          log_keywords=["Rate limit", "BadRequest", "Groq", "Anthropic",
                        "Request too large", "401", "503", "Falling back"],
          source_files=["server.py", "brain/jarvis.py"]),
    Check("local_brain", "Local reasoning core", _probe_local_brain,
          "If the cloud rate-limits me, I won't have a fallback and may go silent briefly.",
          fix=_fix_local_brain,
          log_keywords=["Ollama", "qwen", "local_ai"],
          source_files=["tools/diagnostics.py", "config.py"]),
    Check("tts_elevenlabs", "ElevenLabs voice", _probe_tts_elevenlabs,
          "I'll drop to the macOS system voice — sounds more robotic.",
          critical=True,
          log_keywords=["ElevenLabs", "TTS", "afplay", "no audio"],
          source_files=["voice/speaker.py"]),
    Check("tts_edge", "Edge TTS backup", _probe_tts_edge,
          "Loss of the secondary TTS path. No immediate effect.",
          log_keywords=["edge_tts", "edge-tts"],
          source_files=["voice/speaker.py"]),
    Check("weather_api", "Weather API", _probe_weather,
          "I won't be able to answer weather questions until it's back.",
          log_keywords=["weather", "wttr"],
          source_files=["tools/web_tools.py"]),
    Check("network", "Internet connectivity", _probe_network,
          "Most cloud features disabled. I'll rely on what's installed locally.",
          critical=True,
          log_keywords=["timeout", "Connection", "DNS"]),

    # ── Voice / audio ─────────────────────────────────────────────────────────
    Check("whisper", "Whisper STT model", _probe_whisper,
          "I won't be able to hear you at all.",
          critical=True,
          log_keywords=["Whisper", "STT"],
          source_files=["voice/audio_engine.py"]),
    Check("microphone", "Microphone permission", _probe_mic_perm,
          "I can't hear you until permission is granted in System Settings.",
          log_keywords=["[mic]", "microphone", "PyAudio"],
          source_files=["voice/audio_engine.py"]),
    Check("voice_profile", "Voice profile", _probe_voice_profile,
          "Speaker recognition off — I may respond to other voices in the room.",
          log_keywords=["voice_profile", "voice profile", "resemblyzer"],
          source_files=["voice/voice_profile.py"]),
    Check("speaker_module", "TTS speaker module", _probe_speaker_module,
          "I can't speak responses until this is restored.",
          critical=True,
          log_keywords=["speaker.", "afplay", "stream_speak"],
          source_files=["voice/speaker.py"]),

    # ── Vision / camera ───────────────────────────────────────────────────────
    Check("camera", "Camera permission", _probe_camera_perm,
          "Gesture and lip-motion features disabled.",
          log_keywords=["[cam]", "camera", "gesture"],
          source_files=["jarvis-app/renderer/app.js"]),

    # ── Background threads ───────────────────────────────────────────────────
    Check("notif_monitor", "Notification monitor", _probe_notif_monitor,
          "I won't announce incoming messages and calls.",
          fix=_fix_notif_monitor,
          log_keywords=["[notif]", "notification"],
          source_files=["tools/notification_monitor.py"]),
    Check("music_poller", "Music state poller", _probe_music_poller,
          "The music widget won't update and the wake-word gate may misbehave.",
          fix=_fix_music_poller,
          log_keywords=["[music]", "music-poller", "music-gate"],
          source_files=["server.py"]),
    Check("system_audio_watcher", "System audio watcher", _probe_system_audio,
          "Video and podcast audio may bleed into my voice recognition.",
          fix=_fix_system_audio,
          log_keywords=["[audio]", "system audio", "sys-audio"],
          source_files=["server.py", "tools/system_audio.py"]),
    Check("screen_watcher", "Screen watcher", _probe_screen_watcher,
          "Auto skip-ad and banner dismissal disabled.",
          fix=_fix_screen_watcher,
          log_keywords=["[watcher]", "screen-watcher", "OCR"],
          source_files=["tools/screen_watcher.py"]),
    Check("proactive_engine", "Proactive engine", _probe_proactive,
          "I won't push reminders or proactive suggestions on my own.",
          log_keywords=["[proactive]", "proactive"],
          source_files=["proactive.py"]),

    # ── Frontend ───────────────────────────────────────────────────────────────
    Check("renderer", "Renderer connection", _probe_websocket_clients,
          "The HUD is detached — voice still works but you won't see overlays.",
          log_keywords=["WebSocket", "_clients", "WebSocketDisconnect"],
          source_files=["server.py", "jarvis-app/renderer/app.js"]),

    # ── Persistent state ──────────────────────────────────────────────────────
    Check("history_file", "Conversation memory", _probe_history_file,
          "I may forget recent conversation context across restarts.",
          source_files=["brain/jarvis.py"]),

    # ── Native apps ───────────────────────────────────────────────────────────
    Check("apple_music", "Apple Music app", _probe_apple_music,
          "Music commands won't work."),
    Check("calendar_app", "Calendar app", _probe_calendar_app,
          "Calendar reads and event lookups disabled."),
    Check("messages_app", "Messages app", _probe_messages_app,
          "Reading and sending iMessages disabled."),
    Check("mail_app", "Mail app", _probe_mail_app,
          "Mail summaries and unread reads disabled."),

    # ── System resources ──────────────────────────────────────────────────────
    Check("disk_space", "Disk space", _probe_disk_space,
          "Low on storage — model loads and audio captures may fail."),
    Check("memory_pressure", "Memory pressure", _probe_memory_pressure,
          "Memory pressure high — I may run slower than usual."),
]


# ── Runner ────────────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    name: str
    label: str
    status: str                       # "pass" | "fail" | "fixed"
    impact: str = ""
    fix_attempted: bool = False
    fix_succeeded: bool = False
    duration_ms: int = 0


# Minimum wall-clock dwell time per check so the UI doesn't flash through
# 17+ instant checks in milliseconds. Makes the diagnostic feel deliberate.
_MIN_PROBE_DURATION_SEC = 0.35     # ~350ms per check minimum
_FIX_VISIBLE_PAUSE_SEC = 0.7       # how long "ATTEMPTING REPAIR" shows
_INVESTIGATE_VISIBLE_SEC = 0.5     # how long "INVESTIGATING…" shows before findings


# ── Investigation stash — brain reads this when answering "why did X fail" ──
# Keyed by check name. Cleared at the start of each diagnostic run, OR when
# the stash expires (TTL below) so the brain doesn't reference findings from
# yesterday's diagnostic as if they're current.
_LAST_INVESTIGATIONS: dict[str, dict] = {}
_LAST_INVESTIGATIONS_RUN_AT: float = 0.0
_STASH_TTL_SEC = 30 * 60          # findings expire after 30 minutes


def get_last_investigations() -> dict[str, dict]:
    """
    Returns the in-memory stash of the most recent diagnostic investigations.
    Returns an empty dict if the stash has expired (older than _STASH_TTL_SEC)
    so the brain doesn't pretend stale findings are current.
    """
    if not _LAST_INVESTIGATIONS:
        return {}
    if _LAST_INVESTIGATIONS_RUN_AT == 0.0:
        return dict(_LAST_INVESTIGATIONS)
    age = time.time() - _LAST_INVESTIGATIONS_RUN_AT
    if age > _STASH_TTL_SEC:
        return {}
    return dict(_LAST_INVESTIGATIONS)


def _investigate_failure(chk: Check) -> dict:
    """
    Given a failed check, grep the captured server log for relevant keywords
    and return a structured finding. Safe to call from the diagnostic loop —
    doesn't raise on missing log files or empty matches.
    """
    finding = {
        "check": chk.name,
        "label": chk.label,
        "log_matches": [],
        "source_files": list(chk.source_files),
        "summary": "",
    }
    if not chk.log_keywords:
        finding["summary"] = "No log keywords registered for this check."
        return finding
    # Pull recent log lines and filter to ones containing any keyword
    try:
        log_text = read_jarvis_log(lines=400)
        if log_text.startswith("__error__"):
            finding["summary"] = "No log file captured this session."
            return finding
        kws = [k.lower() for k in chk.log_keywords]
        matches = []
        for raw in log_text.splitlines():
            low = raw.lower()
            if any(k in low for k in kws):
                matches.append(raw.strip()[:240])
                if len(matches) >= 10:
                    break
        finding["log_matches"] = matches
        if matches:
            finding["summary"] = f"Found {len(matches)} related log line(s)."
        else:
            finding["summary"] = "No related log lines in the captured output."
    except Exception as exc:
        finding["summary"] = f"Investigation error: {type(exc).__name__}"
    return finding


def run_full_diagnostic() -> Generator[dict, None, None]:
    """
    Run every check in order, yielding events as we go. Event shapes:
      {type: "diagnostic_start", total: N}
      {type: "diagnostic_step",  index, total, name, label, status, impact,
                                 fix_attempted, fix_succeeded, fixable}
      {type: "diagnostic_done",  passed, failed, fixed, results: [...]}

    Status transitions a check can go through:
      running  →  fixing  →  fixed | fail        (when fix() is defined)
      running  →  fail                            (when fix() is None)
      running  →  pass                            (probe succeeded)
    """
    total = len(CHECKS)
    # Reset investigation stash for this run so old findings don't leak through
    global _LAST_INVESTIGATIONS_RUN_AT
    _LAST_INVESTIGATIONS.clear()
    _LAST_INVESTIGATIONS_RUN_AT = time.time()
    yield {"type": "diagnostic_start", "total": total}

    results: list[CheckResult] = []
    for i, chk in enumerate(CHECKS):
        # Emit "running" so the UI can show a spinner immediately
        yield {
            "type": "diagnostic_step", "index": i, "total": total,
            "name": chk.name, "label": chk.label, "status": "running",
            "impact": "", "fix_attempted": False, "fix_succeeded": False,
            "fixable": chk.fix is not None,
        }

        t0 = time.time()
        try:
            ok = bool(chk.probe())
        except Exception:
            ok = False
        probe_elapsed = time.time() - t0
        # Hold "running" visible long enough for the user to read it
        remaining = _MIN_PROBE_DURATION_SEC - probe_elapsed
        if remaining > 0:
            time.sleep(remaining)
        duration_ms = int((time.time() - t0) * 1000)

        if ok:
            res = CheckResult(chk.name, chk.label, "pass", duration_ms=duration_ms)
        else:
            fix_attempted = False
            fix_succeeded = False
            if chk.fix:
                # Show "ATTEMPTING REPAIR" so it's obvious JARVIS is trying
                yield {
                    "type": "diagnostic_step", "index": i, "total": total,
                    "name": chk.name, "label": chk.label, "status": "fixing",
                    "impact": chk.impact,
                    "fix_attempted": True, "fix_succeeded": False,
                    "fixable": True,
                }
                fix_t0 = time.time()
                fix_attempted = True
                try:
                    fix_succeeded = bool(chk.fix())
                except Exception:
                    fix_succeeded = False
                # Hold the "fixing" state visible long enough to read
                fix_elapsed = time.time() - fix_t0
                fix_remaining = _FIX_VISIBLE_PAUSE_SEC - fix_elapsed
                if fix_remaining > 0:
                    time.sleep(fix_remaining)
            status = "fixed" if fix_succeeded else "fail"
            res = CheckResult(
                chk.name, chk.label, status,
                impact=chk.impact,
                fix_attempted=fix_attempted,
                fix_succeeded=fix_succeeded,
                duration_ms=duration_ms,
            )
        results.append(res)

        yield {
            "type": "diagnostic_step", "index": i, "total": total,
            "name": res.name, "label": res.label, "status": res.status,
            "impact": res.impact,
            "fix_attempted": res.fix_attempted,
            "fix_succeeded": res.fix_succeeded,
            "fixable": chk.fix is not None,
            "duration_ms": res.duration_ms,
        }

        # ── Auto-investigation: when a check ends as 'fail', look in the
        #    captured log for related lines and stash the finding so the
        #    brain can answer "why did X fail" later from real evidence.
        if res.status == "fail":
            yield {
                "type": "diagnostic_investigation_start",
                "index": i, "name": chk.name, "label": chk.label,
                "source_files": list(chk.source_files),
            }
            time.sleep(_INVESTIGATE_VISIBLE_SEC)
            finding = _investigate_failure(chk)
            _LAST_INVESTIGATIONS[chk.name] = finding
            yield {
                "type": "diagnostic_investigation_done",
                "index": i, "name": chk.name, "label": chk.label,
                "summary": finding["summary"],
                "log_matches": finding["log_matches"],
                "source_files": finding["source_files"],
            }

    passed = sum(1 for r in results if r.status == "pass")
    fixed  = sum(1 for r in results if r.status == "fixed")
    failed = sum(1 for r in results if r.status == "fail")
    yield {
        "type": "diagnostic_done",
        "passed": passed, "fixed": fixed, "failed": failed,
        "results": [
            {
                "name": r.name, "label": r.label, "status": r.status,
                "impact": r.impact,
                "fix_attempted": r.fix_attempted, "fix_succeeded": r.fix_succeeded,
                "duration_ms": r.duration_ms,
            }
            for r in results
        ],
    }
