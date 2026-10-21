"""
Screen watcher — proactive auto-actions on Dylan's screen.

Watches the focused window every ~1.5s, OCRs it with Apple Vision, runs a
small library of pattern detectors (YouTube Skip Ad, cookie banners,
"Are you still watching?", macOS update popups), and silently clicks the
right control via Quartz synthetic mouse events.

Designed to be CHEAP and CONSERVATIVE:
  - Only OCRs the focused window, not the full screen
  - Throttles re-triggers (won't double-click)
  - Each pattern has an explicit confidence gate
  - Single daemon thread, sleeps when paused or muted

Requires:
  - pyobjc-framework-Vision      (Apple OCR)
  - pyobjc-framework-Quartz      (CGEvent for clicks + window list)
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

try:
    import Vision
    import Quartz
    from Foundation import NSURL
    _DEPS_OK = True
except Exception as _e:
    print(f"[watcher] Vision/Quartz missing — disabling: {_e}", flush=True)
    _DEPS_OK = False


# ── Types ─────────────────────────────────────────────────────────────────────

@dataclass
class OCRBox:
    """A single OCR'd text observation with its bounding box in SCREEN pixels."""
    text: str
    confidence: float
    x: int          # top-left, screen coords (Quartz origin = top-left)
    y: int
    w: int
    h: int

    @property
    def cx(self) -> int: return self.x + self.w // 2
    @property
    def cy(self) -> int: return self.y + self.h // 2


@dataclass
class Pattern:
    """One auto-action: detector decides if/where to click, action does the click."""
    name: str
    enabled: bool
    detector: Callable[[list[OCRBox], "FocusedWindow"], Optional[tuple[int, int]]]
    cooldown_sec: float = 6.0          # min seconds between consecutive triggers


@dataclass
class FocusedWindow:
    app_name: str
    title: str
    x: int; y: int; w: int; h: int


# ── macOS helpers ─────────────────────────────────────────────────────────────

def _get_focused_window() -> Optional[FocusedWindow]:
    """Return geometry + app name of the frontmost window via Quartz."""
    if not _DEPS_OK:
        return None
    try:
        opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        windows = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
        # Collect every plausible layer-0 window, then pick the LARGEST rather than
        # the first. Chrome publishes several layer-0 windows for one browser window
        # — a 41px strip, an 81px strip, a 166px panel, and only then the real 920px
        # window carrying the page title. Taking the first match over 200x150 picked
        # the 166px panel, so the watcher OCR'd a thin band across the top of the
        # screen and never saw the video player. That silently disabled every
        # pattern: Skip Ad, cookie banners, "still watching", all of it.
        candidates = []
        for w in windows:
            if w.get("kCGWindowLayer", 0) != 0:
                continue
            bounds = w.get("kCGWindowBounds") or {}
            if not bounds: continue
            x = int(bounds.get("X", 0)); y = int(bounds.get("Y", 0))
            width = int(bounds.get("Width", 0)); height = int(bounds.get("Height", 0))
            if width < 200 or height < 150:
                continue   # too small to be a real window
            app = w.get("kCGWindowOwnerName", "") or ""
            # Skip our own JARVIS HUD
            if "Electron" in app or "JARVIS" in app:
                continue
            title = w.get("kCGWindowName", "") or ""
            candidates.append((width * height, bool(title), x, y, width, height, app, title))
        if candidates:
            # Largest area wins; a real title breaks ties, since the helper panels
            # are all untitled while the true window carries the page name.
            _, _, x, y, width, height, app, title = max(
                candidates, key=lambda c: (c[0], c[1])
            )
            return FocusedWindow(app_name=app, title=title, x=x, y=y, w=width, h=height)
    except Exception as e:
        print(f"[watcher] window probe failed: {e}", flush=True)
    return None


def _visible_rect(x: int, y: int, w: int, h: int) -> tuple[int, int, int, int]:
    """
    Clamp a window rect to the displays it actually appears on.

    `screencapture -R` silently clips to the display and returns only the visible
    intersection — no padding. If OCR is then told the UNCLIPPED size, every
    normalized box is mis-scaled, pushing detections toward the lower-right. That is
    worse than missing the button: the region gates get MORE likely to pass, so the
    watcher clicks a corrupted coordinate — on YouTube that means toggling playback
    instead of skipping. Only matters when a window overhangs a screen edge (a window
    sized on a since-disconnected external display, say), but the failure is silent.
    """
    if not _DEPS_OK:
        return x, y, w, h
    try:
        err, ids, cnt = Quartz.CGGetActiveDisplayList(16, None, None)
        rects = [Quartz.CGDisplayBounds(d) for d in ids[:cnt]]
        if not rects:
            return x, y, w, h
        left = min(r.origin.x for r in rects)
        top = min(r.origin.y for r in rects)
        right = max(r.origin.x + r.size.width for r in rects)
        bottom = max(r.origin.y + r.size.height for r in rects)
    except Exception:
        return x, y, w, h
    nx = max(x, int(left))
    ny = max(y, int(top))
    nw = min(x + w, int(right)) - nx
    nh = min(y + h, int(bottom)) - ny
    if nw <= 0 or nh <= 0:
        return x, y, w, h
    return nx, ny, nw, nh


def _screencap_region(x: int, y: int, w: int, h: int) -> Optional[str]:
    """
    Silent screen capture of a region → returns PNG path, or None on
    failure. If screencapture fails or produces a tiny file, the temp file
    is cleaned up here so /tmp/jarvis_watch_*.png doesn't accumulate.
    """
    fp = tempfile.mktemp(prefix="jarvis_watch_", suffix=".png")
    try:
        # -R for region, -x silent (no shutter sound), -t png
        subprocess.run(
            ["screencapture", "-x", "-R", f"{x},{y},{w},{h}", "-t", "png", fp],
            check=True, timeout=3, capture_output=True,
        )
        if os.path.exists(fp) and os.path.getsize(fp) > 100:
            return fp
    except Exception:
        pass
    # Cleanup on every failure path. screencapture may have created an
    # empty / partial file even when it crashed; either way we don't want
    # it lingering. _ocr() handles the success path (deletes after read).
    try:
        if os.path.exists(fp):
            os.unlink(fp)
    except Exception:
        pass
    return None


def _ocr(image_path: str, region_origin: tuple[int, int],
         region_size: tuple[int, int]) -> list[OCRBox]:
    """Run Apple Vision OCR on the captured image, return text boxes in screen coords."""
    if not _DEPS_OK:
        return []
    try:
        url = NSURL.fileURLWithPath_(image_path)
        req = Vision.VNRecognizeTextRequest.alloc().init()
        # Fast level is plenty — we're matching short button labels
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelFast)
        req.setUsesLanguageCorrection_(False)
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, None)
        ok, _ = handler.performRequests_error_([req], None)
        if not ok:
            return []
        boxes: list[OCRBox] = []
        rx, ry = region_origin
        rw, rh = region_size
        for obs in (req.results() or []):
            cands = obs.topCandidates_(1) or []
            if not cands: continue
            txt = cands[0].string() or ""
            if not txt.strip(): continue
            # Vision's bounding boxes are normalized 0-1 in IMAGE coords with
            # ORIGIN AT BOTTOM-LEFT. Convert to screen pixels (top-left origin).
            bb = obs.boundingBox()
            bx = bb.origin.x;    by = bb.origin.y
            bw = bb.size.width;  bh = bb.size.height
            # x in screen = region_x + (bx * region_w)
            sx = int(rx + bx * rw)
            sw_ = int(bw * rw)
            # y is flipped: image bottom corresponds to screen bottom of region
            sy = int(ry + (1.0 - by - bh) * rh)
            sh_ = int(bh * rh)
            boxes.append(OCRBox(
                text=txt, confidence=float(cands[0].confidence()),
                x=sx, y=sy, w=sw_, h=sh_,
            ))
        return boxes
    except Exception as e:
        print(f"[watcher] OCR failed: {e}", flush=True)
        return []
    finally:
        try: os.unlink(image_path)
        except Exception: pass


def _click(x: int, y: int) -> None:
    """Synthetic left-click via Quartz CGEvent."""
    if not _DEPS_OK:
        return
    try:
        pos = Quartz.CGPointMake(float(x), float(y))
        for evt_type in (Quartz.kCGEventMouseMoved,
                         Quartz.kCGEventLeftMouseDown,
                         Quartz.kCGEventLeftMouseUp):
            ev = Quartz.CGEventCreateMouseEvent(
                None, evt_type, pos, Quartz.kCGMouseButtonLeft,
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.02)
    except Exception as e:
        print(f"[watcher] click failed: {e}", flush=True)


# ── Pattern detectors ─────────────────────────────────────────────────────────
# Each returns the click target (screen px) if it fired, else None.

# Apple's Vision OCR reports ~0.5 for short, stylised strings — a real "Skip ad >"
# measured EXACTLY 0.50, which a strict `> 0.5` discarded. The button was found,
# correctly located, and correctly matched, then thrown away on the boundary, so ad
# skipping never fired once. Inclusive, and low enough to absorb that jitter while
# still rejecting genuine OCR noise (~0.3). The strict text match plus the geometry
# gates carry the real precision here.
_MIN_OCR_CONF = 0.45


def _detect_youtube_skip(boxes: list[OCRBox], win: FocusedWindow) -> Optional[tuple[int, int]]:
    """
    YouTube 'Skip Ad' / 'Skip' button — appears in the lower-right of the video
    player when an ad is playing. Match conservatively: text must be 'Skip',
    'Skip Ad', 'Skip Ads', or 'Skip >' AND located in the right third of the
    window's lower half (where YT places the button).
    """
    if "Chrome" not in win.app_name and "Safari" not in win.app_name and "Arc" not in win.app_name:
        return None
    right_third_x = win.x + int(win.w * 0.55)
    lower_half_y  = win.y + int(win.h * 0.40)
    for b in boxes:
        # Exact match after stripping trailing chevrons — NOT startswith. An
        # unanchored `startswith("skip ad")` also matches "Skip ads with YouTube
        # Premium", the upsell YouTube renders in that exact corner, so the watcher
        # would click it and navigate Dylan to the Premium purchase page mid-video.
        # The chevron strip still covers the "Skip >" / "Skip ad >" variants that the
        # prefix test was there for.
        t = b.text.strip().lower().rstrip(" >»›").strip()
        if t in ("skip", "skip ad", "skip ads"):
            if b.cx >= right_third_x and b.cy >= lower_half_y and b.confidence >= _MIN_OCR_CONF:
                return (b.cx, b.cy)
    return None


_COOKIE_ACCEPTS = (
    "accept all", "accept cookies", "accept all cookies",
    "i accept", "agree", "i agree", "got it", "ok",
)
_COOKIE_REJECTS = (
    "reject all", "reject cookies", "decline", "decline all", "necessary only",
    "only necessary", "reject non-essential",
)
_COOKIE_TRIGGERS = ("cookies", "cookie policy", "privacy policy",
                    "we use cookies", "this site uses cookies",
                    "by clicking accept")

def _detect_cookie_banner(boxes: list[OCRBox], win: FocusedWindow) -> Optional[tuple[int, int]]:
    """
    Cookie / consent banner. First confirm a banner is on screen by looking for
    a 'cookies' trigger phrase, then prefer the privacy-friendly REJECT button.
    Falls back to ACCEPT only if no reject button is present (some banners only
    offer accept).
    """
    if "Chrome" not in win.app_name and "Safari" not in win.app_name and "Arc" not in win.app_name:
        return None
    text_all = " ".join(b.text.lower() for b in boxes)
    if not any(t in text_all for t in _COOKIE_TRIGGERS):
        return None
    # Prefer REJECT to honor privacy default
    for b in boxes:
        if b.text.strip().lower() in _COOKIE_REJECTS and b.confidence >= _MIN_OCR_CONF:
            return (b.cx, b.cy)
    for b in boxes:
        if b.text.strip().lower() in _COOKIE_ACCEPTS and b.confidence >= _MIN_OCR_CONF:
            return (b.cx, b.cy)
    return None


_STILL_WATCHING_TRIGGERS = (
    "are you still watching", "still watching", "continue watching",
    "still there", "are you there",
)
_STILL_WATCHING_BUTTONS = ("continue", "yes", "i'm watching", "still watching", "keep watching")

def _detect_still_watching(boxes: list[OCRBox], win: FocusedWindow) -> Optional[tuple[int, int]]:
    """Netflix / YouTube / streaming services' continue-watching nags."""
    text_all = " ".join(b.text.lower() for b in boxes)
    if not any(t in text_all for t in _STILL_WATCHING_TRIGGERS):
        return None
    for b in boxes:
        if b.text.strip().lower() in _STILL_WATCHING_BUTTONS and b.confidence >= _MIN_OCR_CONF:
            return (b.cx, b.cy)
    return None


_UPDATE_TRIGGERS = ("software update", "update available", "macos", "restart now",
                    "install now", "system update")
_UPDATE_DISMISS = ("later", "not now", "install tomorrow", "remind me later",
                   "remind me tomorrow", "skip this version")

def _detect_macos_update(boxes: list[OCRBox], win: FocusedWindow) -> Optional[tuple[int, int]]:
    """macOS 'Software Update' / 'A new update is available' nag windows."""
    text_all = " ".join(b.text.lower() for b in boxes)
    if not any(t in text_all for t in _UPDATE_TRIGGERS):
        return None
    # Need at least two confirming hits to avoid false matches in random text
    hits = sum(1 for t in _UPDATE_TRIGGERS if t in text_all)
    if hits < 2:
        return None
    for b in boxes:
        if b.text.strip().lower() in _UPDATE_DISMISS and b.confidence >= _MIN_OCR_CONF:
            return (b.cx, b.cy)
    return None


# Streaming skip buttons — Netflix / Hulu / Prime / Peacock / Disney+ / Max.
# "Skip Intro / Recap / Credits / Ad" and "Next Episode" all sit in the lower
# ~65% of the player. Exact-phrase + region + confidence gates keep it from
# firing on the word "skip" buried in ordinary page text.
_STREAM_SKIP_BUTTONS = (
    "skip intro", "skip recap", "skip credits", "skip preview", "skip trailer",
    "skip ad", "skip ads", "skip advert", "skip advertisement",
    "next episode", "play next episode", "play next",
)
_STREAM_APPS = ("Chrome", "Safari", "Arc", "Brave", "Firefox", "Netflix", "TV")

def _detect_streaming_skip(boxes: list[OCRBox], win: FocusedWindow) -> Optional[tuple[int, int]]:
    """Auto-click Skip Intro/Recap/Credits/Ad and Next-Episode across streaming sites."""
    if not any(a in win.app_name for a in _STREAM_APPS):
        return None
    lower_y = win.y + int(win.h * 0.35)   # player controls live in the bottom ~65%
    for b in boxes:
        t = b.text.strip().lower().rstrip(" >»").strip()
        if t in _STREAM_SKIP_BUTTONS and b.confidence >= _MIN_OCR_CONF and b.cy >= lower_y:
            return (b.cx, b.cy)
    return None


_PATTERNS = [
    Pattern("youtube_skip_ad",  True,  _detect_youtube_skip,    cooldown_sec=4.0),
    Pattern("streaming_skip",   True,  _detect_streaming_skip,  cooldown_sec=3.0),
    Pattern("cookie_banner",    True,  _detect_cookie_banner,   cooldown_sec=15.0),
    Pattern("still_watching",   True,  _detect_still_watching,  cooldown_sec=30.0),
    Pattern("macos_update",     True,  _detect_macos_update,    cooldown_sec=60.0),
]


# ── Daemon ────────────────────────────────────────────────────────────────────

class ScreenWatcher:
    """
    Background polling thread. Public surface is minimal so server.py can
    just .start() / .stop() / .pause() / .resume() it.
    """

    def __init__(self, poll_interval: float = 1.5,
                 on_action: Optional[Callable[[str], None]] = None):
        self.poll_interval = poll_interval
        self.on_action = on_action       # called with pattern name after each action
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._last_fired: dict[str, float] = {}

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def start(self) -> None:
        if not _DEPS_OK:
            print("[watcher] not starting — Vision/Quartz unavailable", flush=True)
            return
        if self.running:
            return
        self._stop.clear()
        self._paused.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="screen-watcher")
        self._thread.start()
        print("[watcher] started", flush=True)

    def stop(self) -> None:
        self._stop.set()

    def pause(self) -> None:  self._paused.set()
    def resume(self) -> None: self._paused.clear()

    def enable_pattern(self, name: str, on: bool) -> None:
        for p in _PATTERNS:
            if p.name == name: p.enabled = on

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._paused.is_set():
                    self._tick()
            except Exception as e:
                print(f"[watcher] tick error: {e}", flush=True)
            self._stop.wait(self.poll_interval)

    def _tick(self) -> None:
        win = _get_focused_window()
        if not win:
            return
        # Capture and OCR must agree on the SAME rect — see _visible_rect.
        rx, ry, rw, rh = _visible_rect(win.x, win.y, win.w, win.h)
        path = _screencap_region(rx, ry, rw, rh)
        if not path:
            return
        boxes = _ocr(path, (rx, ry), (rw, rh))
        if not boxes:
            return
        now = time.time()
        for pat in _PATTERNS:
            if not pat.enabled: continue
            if now - self._last_fired.get(pat.name, 0) < pat.cooldown_sec:
                continue
            target = pat.detector(boxes, win)
            if target:
                cx, cy = target
                print(f"[watcher] firing {pat.name} at ({cx}, {cy})", flush=True)
                _click(cx, cy)
                self._last_fired[pat.name] = now
                if self.on_action:
                    try: self.on_action(pat.name)
                    except Exception: pass
                # Only one action per tick — don't chain-click
                return


# Singleton accessor so server.py can grab the same instance everywhere
_singleton: Optional[ScreenWatcher] = None

def get_watcher() -> ScreenWatcher:
    global _singleton
    if _singleton is None:
        _singleton = ScreenWatcher()
    return _singleton
