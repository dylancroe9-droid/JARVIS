"""
Health nudges — gentle recurring reminders that fire in the background:
hydration, stretch, posture, eye-rest (20-20-20), breathe.

Each nudge is a daemon thread that waits its interval, then fires a macOS
notification (or speaks, if the server wires up the `speak` callback), and loops.
Voice-controllable: start / stop / list.
"""
from __future__ import annotations

import subprocess
import threading

_NUDGES = {
    "water":   "Hydrate, sir — grab some water.",
    "stretch": "Stand up and stretch for a moment, sir.",
    "posture": "Posture check, sir — sit up straight.",
    "eyes":    "Rest your eyes, sir — look 20 feet away for 20 seconds.",
    "breathe": "Take a slow, deep breath, sir.",
}
_ALIASES = {
    "hydration": "water", "hydrate": "water", "drink": "water",
    "stand": "stretch", "move": "stretch", "break": "stretch",
    "eye": "eyes", "eye rest": "eyes", "20-20-20": "eyes",
    "breathing": "breathe", "breath": "breathe",
}


class _Nudger:
    def __init__(self) -> None:
        self._active: dict[str, tuple] = {}   # kind -> (thread, stop_event, interval_min)
        self._lock = threading.Lock()
        self.speak = None                     # optional callback(str); set by server to speak aloud

    def start(self, kind: str, interval_min: int):
        if kind not in _NUDGES:
            return None
        interval = max(1, int(interval_min or 30))
        self.stop(kind)
        stop = threading.Event()

        def loop():
            while not stop.wait(interval * 60):
                self._fire(kind)

        t = threading.Thread(target=loop, daemon=True, name=f"nudge-{kind}")
        with self._lock:
            self._active[kind] = (t, stop, interval)
        t.start()
        return (kind, interval)

    def stop(self, kind: str = None):
        with self._lock:
            targets = ([kind] if kind else list(self._active.keys()))
            for k in targets:
                v = self._active.pop(k, None)
                if v:
                    v[1].set()

    def list(self) -> dict:
        with self._lock:
            return {k: v[2] for k, v in self._active.items()}

    def _fire(self, kind: str):
        msg = _NUDGES[kind]
        if self.speak:
            try:
                self.speak(msg)
                return
            except Exception:
                pass
        try:
            subprocess.run(["osascript", "-e",
                            f'display notification "{msg}" with title "JARVIS"'], timeout=5)
        except Exception:
            pass


_singleton = _Nudger()

def get_nudger() -> _Nudger:
    return _singleton


def health_reminders(action: str = "start", kind: str = "", interval_min: int = 30) -> str:
    """Start / stop / list recurring health nudges."""
    n = get_nudger()
    act = (action or "start").lower().strip()
    k = _ALIASES.get((kind or "").lower().strip(), (kind or "").lower().strip())

    if act in ("stop", "cancel", "off", "quit"):
        n.stop(k or None)
        return f"Stopped your {k} reminders, sir." if k else "Stopped all your reminders, sir."
    if act in ("list", "status", "what"):
        active = n.list()
        if not active:
            return "No health reminders running, sir."
        return "Running: " + ", ".join(f"{kk} every {v} min" for kk, v in active.items())

    if k not in _NUDGES:
        return f"I can nudge you to: {', '.join(_NUDGES)}. Which one, sir?"
    res = n.start(k, interval_min)
    return f"Got it — I'll remind you to {k} every {res[1]} minutes, sir."
