"""
Unified mode manager — one system that watches what's on screen and puts
JARVIS into the right mode automatically:

  watch   — a video is playing (YouTube / Netflix / Peacock / a video app).
            → notifications silenced, and if it's YouTube, ad-skip is armed.
  gaming  — a game / game launcher is frontmost.
            → notifications silenced, compact corner HUD, waits for "exit".
  work    — code editor / docs / work browser is frontmost.
            → compact corner HUD, screen context available.
  normal  — desktop, chat, settings, anything else.
            → full HUD, notifications on.

Classification is by the FRONTMOST APP + WINDOW TITLE, not a vision call.
That's instant, deterministic, and free — polling with a 30s vision call
would burn the Groq rate limit and add latency for no accuracy gain here.

The manager polls on a background thread and calls an `on_mode_change`
callback (wired in server.py) whenever the detected mode changes. Voice
commands can force or exit a mode; a forced mode sticks until the user exits
it or the screen clearly moves to a different context.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

try:
    import Quartz
    _QUARTZ_OK = True
except Exception:
    _QUARTZ_OK = False


# ── Classification signals ────────────────────────────────────────────────────

# Streaming / video — matched against the window TITLE (browsers) or app name.
_WATCH_TITLE_HINTS = (
    "youtube", "netflix", "hulu", "peacock", "disney+", "disneyplus",
    "hbo max", "max", "prime video", "paramount+", "crunchyroll", "twitch",
    "apple tv", "espn", "- youtube", "vimeo", "plex",
)
_WATCH_APPS = (
    "QuickTime Player", "VLC", "IINA", "TV", "Infuse", "Plex",
    "Elmedia Player", "MPV",
)

# Games / launchers — matched against the app name.
_GAMING_APPS = (
    "Steam", "Epic Games Launcher", "Roblox", "Minecraft", "League of Legends",
    "Riot Client", "VALORANT", "Battle.net", "Overwatch", "Fortnite",
    "Genshin Impact", "Discord Overlay", "OpenEmu", "RetroArch", "Xbox",
    "EA app", "Rockstar Games Launcher", "Wreckfest", "Terraria", "Stardew Valley",
)
# App-name substrings that strongly imply a game
_GAMING_APP_HINTS = ("game", "launcher")

# Work — code editors, terminals, docs.
_WORK_APPS = (
    "Code", "Visual Studio Code", "Xcode", "Terminal", "iTerm2", "iTerm",
    "PyCharm", "IntelliJ IDEA", "WebStorm", "Sublime Text", "Cursor",
    "Notion", "Obsidian", "Pages", "Microsoft Word", "Microsoft Excel",
    "Numbers", "Keynote", "Microsoft PowerPoint",
)
# Work-ish browser titles
_WORK_TITLE_HINTS = (
    "github", "gmail", "google docs", "google sheets", "google slides",
    "stack overflow", "jira", "confluence", "figma", "canvas", "gradescope",
)

# Apps that mean "normal" even if frontmost (don't force a special mode)
_NORMAL_APPS = (
    "Finder", "System Settings", "System Preferences", "Messages", "Mail",
    "Notes", "Reminders", "Calendar", "Music", "Spotify", "Photos",
    "App Store", "Electron", "JARVIS",
)

# Modes that, once entered, PIN themselves and wait for an explicit voice exit
# instead of tracking the screen. Gaming is sticky because players alt-tab to
# Discord / a wiki / a loading screen constantly, and flapping out of gaming
# mode every 30s would be worse than useless. Watch and work track the screen
# fluidly, so they aren't sticky.
_STICKY_MODES = frozenset({"gaming"})


def _frontmost() -> tuple[str, str]:
    """
    Return (app_name, window_title) of the frontmost normal window.
    Falls back to ("", "") if it can't be determined.
    """
    if not _QUARTZ_OK:
        return "", ""
    try:
        opts = (Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements)
        windows = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
        for w in windows:
            if w.get("kCGWindowLayer", 0) != 0:
                continue                       # skip menubar/dock/overlays
            bounds = w.get("kCGWindowBounds") or {}
            if int(bounds.get("Width", 0)) < 200 or int(bounds.get("Height", 0)) < 150:
                continue
            app = w.get("kCGWindowOwnerName", "") or ""
            if "Electron" in app or "JARVIS" in app:
                continue                       # our own HUD
            title = w.get("kCGWindowName", "") or ""
            return app, title
    except Exception:
        pass
    return "", ""


def classify(app: str, title: str) -> tuple[str, str]:
    """
    Pure classifier: map an (app, window-title) pair to a mode + reason.
    Priority: watch > gaming > work > normal (most specific first). Kept free
    of any system calls so it's trivially unit-testable.
    """
    if not app:
        return "normal", "no frontmost window"
    app_l = app.lower()
    title_l = (title or "").lower()

    # A browser playing video → watch. Detect by title hints (browsers put the
    # site/video name in the title).
    is_browser = any(b in app_l for b in ("chrome", "safari", "arc", "firefox",
                                          "brave", "edge", "opera"))
    if app in _WATCH_APPS or any(h in title_l for h in _WATCH_TITLE_HINTS):
        # Only treat a browser as "watch" if a streaming hint is in the title
        if app in _WATCH_APPS or (is_browser and any(h in title_l for h in _WATCH_TITLE_HINTS)):
            return "watch", f"{app}: {title[:40]}"

    # Games / launchers
    if app in _GAMING_APPS or any(h in app_l for h in _GAMING_APP_HINTS):
        return "gaming", app

    # Work — editors/docs, or a browser on a work site
    if app in _WORK_APPS:
        return "work", app
    if is_browser and any(h in title_l for h in _WORK_TITLE_HINTS):
        return "work", f"{app}: {title[:40]}"

    return "normal", app


def classify_screen() -> tuple[str, str]:
    """Classify what's currently on screen. Returns (mode, reason)."""
    app, title = _frontmost()
    return classify(app, title)


def looks_like_watch_site(text: str) -> bool:
    """
    True if `text` (a site label / URL) names a streaming / video service.
    Used by the pull-up intercept to pick watch mode over work mode when the
    user opens Netflix / YouTube / Peacock / etc.
    """
    t = (text or "").lower()
    return any(h in t for h in _WATCH_TITLE_HINTS)


# ── Manager ───────────────────────────────────────────────────────────────────

class ModeManager:
    """
    Polls the screen and reports mode changes. Respects a forced mode set by
    voice (which suppresses auto-switching until the user exits it or the
    screen clearly changes context).
    """

    def __init__(self,
                 on_mode_change: Callable[[str, str], None],
                 poll_sec: float = 30.0):
        self.on_mode_change = on_mode_change
        self.poll_sec = poll_sec
        self._mode = "normal"
        self._forced: Optional[str] = None       # mode pinned by voice / pull-up
        self._forced_app: Optional[str] = None   # frontmost app when pinned
        self._paused = False                     # auto-switching globally off
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()            # guards mode/forced state
        self._fire_lock = threading.Lock()       # serializes callback invocations

    @property
    def mode(self) -> str:
        return self._mode

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="mode-manager")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def force(self, mode: str, reason: str = "forced by voice") -> None:
        """
        Pin a mode (voice command or pull-up). A sticky mode (gaming) holds
        until an explicit exit; a non-sticky pin (watch / work) holds until the
        frontmost APP changes, then auto-detection takes over again.

        `reason` is passed through to the callback so callers (e.g. the pull-up
        intercept) can suppress the default spoken line and say their own.
        """
        app, _ = _frontmost()
        with self._lock:
            self._forced = mode
            self._forced_app = app
            prev = self._mode
            changed = mode != self._mode
            if changed:
                self._mode = mode
        # Fire the callback OUTSIDE the lock — it speaks (blocking TTS) and we
        # must not hold the lock across that or the poll thread stalls.
        if changed:
            self._fire(mode, reason, prev)

    def exit_forced(self) -> None:
        """
        Explicit "exit / back to normal". Force NORMAL and pin it to the current
        app, so auto-detection can't immediately re-enter the mode the user just
        left — e.g. saying "exit gaming mode" while the game is still frontmost
        must actually leave gaming, not flip back to it on the next 30s poll
        (gaming is sticky, so a plain un-pin would re-detect and re-stick it).

        The normal pin is NOT sticky, so it releases the moment the user switches
        to a different app, at which point auto-detection resumes normally (and
        will re-enter gaming if they return to the game — that's intended).
        """
        app, _ = _frontmost()
        with self._lock:
            self._forced = "normal"
            self._forced_app = app
            prev = self._mode
            changed = self._mode != "normal"
            if changed:
                self._mode = "normal"
        if changed:
            self._fire("normal", "exited by voice", prev)

    def set_auto(self, enabled: bool) -> None:
        """
        Globally enable/disable auto screen-based switching. When disabled we
        drop any pin and fall back to normal so the user isn't stuck in a mode
        they can't leave. Returns nothing; caller speaks the confirmation.
        """
        prev = None
        mode = None
        with self._lock:
            self._paused = not enabled
            if not enabled:
                self._forced = None
                self._forced_app = None
                if self._mode != "normal":
                    prev, self._mode, mode = self._mode, "normal", "normal"
        if mode is not None:
            self._fire(mode, "auto mode off", prev)

    @property
    def paused(self) -> bool:
        return self._paused

    def _fire(self, mode: str, reason: str, prev: str) -> None:
        """
        Invoke the mode-change callback. Never call while holding self._lock
        (the callback speaks — blocking TTS — which would stall the poll thread).

        Serialized by _fire_lock so two transitions can't interleave, and each
        fire re-checks that it still matches the manager's current mode: if a
        newer transition superseded this one (e.g. a voice `force()` raced an
        auto-poll), the stale callback is dropped so the applied state, the HUD
        badge, and the spoken line all agree with self._mode.
        """
        with self._fire_lock:
            with self._lock:
                if self._mode != mode:
                    return                       # superseded by a newer change
            try:
                self.on_mode_change(mode, reason)
            except Exception as exc:
                print(f"[mode] callback error: {exc}", flush=True)
            print(f"[mode] {prev} → {mode} ({reason})", flush=True)

    def _poll_once(self) -> None:
        """
        One auto-detection pass. Factored out of the loop so it's unit-testable
        without a live thread. Fires the callback OUTSIDE the lock (TTS blocks).

        A single _frontmost() probe drives both the pin-release check and the
        classification, so we never read the window list twice per tick.
        """
        app, title = _frontmost()
        mode = reason = prev = None
        changed = False
        with self._lock:
            if self._paused:
                return                           # auto-switching turned off
            forced = self._forced
            if forced is not None:
                # Sticky pins (gaming) never auto-release. Non-sticky pins
                # (watch / work) release as soon as the frontmost app changes.
                if forced in _STICKY_MODES or app == self._forced_app:
                    return                       # honor the pin, do nothing
                self._forced = None
                self._forced_app = None
            m, r = classify(app, title)
            if m != self._mode:
                prev, mode, reason = self._mode, m, r
                self._mode = m
                changed = True
                # A sticky mode pins itself once auto-entered, so it waits for
                # an explicit "exit …" instead of flapping every poll.
                if m in _STICKY_MODES:
                    self._forced = m
                    self._forced_app = app
        if changed:
            self._fire(mode, reason, prev)

    def _loop(self) -> None:
        # Small initial delay so the app is fully up
        self._stop.wait(8)
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as exc:
                print(f"[mode] loop error: {exc}", flush=True)
            self._stop.wait(self.poll_sec)


_singleton: Optional[ModeManager] = None


def get_manager(on_mode_change: Callable[[str, str], None] = None,
                poll_sec: float = 30.0) -> ModeManager:
    global _singleton
    if _singleton is None and on_mode_change is not None:
        _singleton = ModeManager(on_mode_change, poll_sec)
    return _singleton
