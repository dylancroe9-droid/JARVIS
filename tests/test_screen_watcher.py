"""
Tests for the screen_watcher auto-action detectors (YouTube Skip Ad, cookie
banners, "still watching"). These are pure functions of (OCR boxes, window), so
they run headless — no Vision/Quartz needed. Locks in the ad-skip behavior and
its conservative gates (right-lower position, browser-only, confidence floor).
"""

from tools.screen_watcher import (
    OCRBox, FocusedWindow,
    _detect_youtube_skip, _detect_cookie_banner, _detect_still_watching,
    _detect_streaming_skip,
)


def _box(text, cx, cy, conf=0.9, w=100, h=40):
    return OCRBox(text=text, confidence=conf, x=cx - w // 2, y=cy - h // 2, w=w, h=h)


# 1000x800 browser window at the origin
CHROME = FocusedWindow(app_name="Google Chrome", title="video - YouTube",
                       x=0, y=0, w=1000, h=800)
NOTES = FocusedWindow(app_name="Notes", title="Untitled", x=0, y=0, w=1000, h=800)


# ── YouTube Skip Ad ─────────────────────────────────────────────────────────

def test_skip_ad_fires_in_lower_right_of_browser():
    # right_third_x = 550, lower_half_y = 320 → a button at (800, 600) qualifies
    boxes = [_box("Skip Ad", 800, 600)]
    assert _detect_youtube_skip(boxes, CHROME) == (800, 600)


def test_skip_ad_ignored_in_upper_left():
    boxes = [_box("Skip Ad", 100, 100)]     # wrong region (an ad link in content)
    assert _detect_youtube_skip(boxes, CHROME) is None


def test_skip_ad_ignored_outside_browser():
    boxes = [_box("Skip Ad", 800, 600)]
    assert _detect_youtube_skip(boxes, NOTES) is None


def test_skip_ad_ignored_low_confidence():
    boxes = [_box("Skip Ad", 800, 600, conf=0.3)]
    assert _detect_youtube_skip(boxes, CHROME) is None


# ── Streaming skip: Netflix / Hulu / Prime / Peacock / Disney+ / Max ────────

NETFLIX = FocusedWindow(app_name="Google Chrome", title="Wednesday - Netflix",
                        x=0, y=0, w=1000, h=800)

def test_streaming_skip_intro_fires():
    assert _detect_streaming_skip([_box("Skip Intro", 850, 650)], NETFLIX) == (850, 650)

def test_streaming_skip_recap_fires():
    assert _detect_streaming_skip([_box("Skip Recap", 850, 700)], NETFLIX) == (850, 700)

def test_streaming_next_episode_fires():
    assert _detect_streaming_skip([_box("Next Episode", 880, 720)], NETFLIX) == (880, 720)

def test_streaming_skip_ad_fires():
    assert _detect_streaming_skip([_box("Skip Ad", 700, 640)], NETFLIX) == (700, 640)

def test_streaming_skip_ignored_outside_streaming_app():
    assert _detect_streaming_skip([_box("Skip Intro", 850, 650)], NOTES) is None

def test_streaming_skip_ignored_in_upper_region():
    assert _detect_streaming_skip([_box("Skip Intro", 850, 100)], NETFLIX) is None

def test_streaming_skip_ignored_stray_skip_text():
    assert _detect_streaming_skip([_box("skip to main content", 850, 650)], NETFLIX) is None


def test_skip_variants_match():
    for txt in ("Skip", "Skip Ad", "Skip Ads", "Skip >"):
        assert _detect_youtube_skip([_box(txt, 800, 600)], CHROME) == (800, 600), txt


def test_unrelated_text_does_not_fire():
    boxes = [_box("Add to playlist", 800, 600), _box("Subscribe", 700, 650)]
    assert _detect_youtube_skip(boxes, CHROME) is None


# ── Cookie banner (prefers REJECT) ──────────────────────────────────────────

def test_cookie_banner_prefers_reject():
    boxes = [
        _box("We use cookies to improve your experience", 500, 700),
        _box("Accept All", 400, 750),
        _box("Reject All", 600, 750),
    ]
    # Should return the REJECT button's center, not accept
    assert _detect_cookie_banner(boxes, CHROME) == (600, 750)


def test_cookie_banner_needs_trigger_phrase():
    # No "cookies" trigger present → don't click anything
    boxes = [_box("Accept All", 400, 750)]
    assert _detect_cookie_banner(boxes, CHROME) is None


def test_cookie_banner_falls_back_to_accept_when_no_reject():
    boxes = [
        _box("This site uses cookies", 500, 700),
        _box("Accept All", 500, 750),
    ]
    assert _detect_cookie_banner(boxes, CHROME) == (500, 750)


# ── Still watching ──────────────────────────────────────────────────────────

def test_still_watching_clicks_continue():
    boxes = [
        _box("Are you still watching?", 500, 400),
        _box("Continue", 500, 500),
    ]
    assert _detect_still_watching(boxes, CHROME) == (500, 500)


def test_still_watching_no_trigger_no_action():
    boxes = [_box("Continue", 500, 500)]
    assert _detect_still_watching(boxes, CHROME) is None


# ── Window probe ──────────────────────────────────────────────────────────────
# The detectors above are all fed a synthetic `win`, so they passed happily while
# the real probe returned the wrong rect and every pattern was dead in practice.
# Chrome publishes several layer-0 windows per browser window; picking the first
# one over 200x150 selected a 166px UI panel instead of the 920px page, so the
# watcher OCR'd a thin strip across the top of the screen and never saw a Skip Ad
# button at all.

def _win(app, w, h, title="", x=0, y=36, layer=0):
    return {"kCGWindowLayer": layer, "kCGWindowOwnerName": app, "kCGWindowName": title,
            "kCGWindowBounds": {"X": x, "Y": y, "Width": w, "Height": h}}


def _probe(monkeypatch, windows):
    import tools.screen_watcher as sw
    monkeypatch.setattr(sw.Quartz, "CGWindowListCopyWindowInfo",
                        lambda *a, **k: windows, raising=False)
    return sw._get_focused_window()


def test_window_probe_picks_the_real_chrome_window(monkeypatch):
    # Exactly what Chrome reported on the machine where ad-skip silently never fired.
    win = _probe(monkeypatch, [
        _win("Google Chrome", 1470, 41),
        _win("Google Chrome", 1470, 81),
        _win("Google Chrome", 1470, 166),                       # decoy panel, untitled
        _win("Google Chrome", 1470, 920, "Batman Gadgets - YouTube"),
    ])
    assert win is not None
    assert win.h == 920, "must pick the full window, not a chrome UI panel"
    assert win.title == "Batman Gadgets - YouTube"


def test_window_probe_prefers_titled_window_on_equal_area(monkeypatch):
    win = _probe(monkeypatch, [
        _win("Google Chrome", 1000, 800),
        _win("Google Chrome", 1000, 800, "Real Page"),
    ])
    assert win.title == "Real Page"


def test_window_probe_skips_jarvis_own_hud(monkeypatch):
    win = _probe(monkeypatch, [
        _win("Electron", 1600, 1000, "JARVIS"),
        _win("Google Chrome", 1200, 800, "YouTube"),
    ])
    assert win.app_name == "Google Chrome"


def test_window_probe_ignores_non_zero_layers(monkeypatch):
    win = _probe(monkeypatch, [
        _win("Dock", 1600, 1000, layer=20),
        _win("Google Chrome", 1200, 800, "YouTube"),
    ])
    assert win.app_name == "Google Chrome"


def test_window_probe_returns_none_when_nothing_qualifies(monkeypatch):
    assert _probe(monkeypatch, [_win("Google Chrome", 100, 100)]) is None


def test_skip_ad_accepts_real_world_confidence():
    # Apple Vision returned exactly 0.50 for a real "Skip ad >" button. The old
    # strict `> 0.5` gate dropped it, so ad skipping never fired even though the
    # button was found and correctly located.
    boxes = [_box("Skip Ad", 800, 600, conf=0.50)]
    assert _detect_youtube_skip(boxes, CHROME) == (800, 600)


def test_skip_ad_ignores_premium_upsell():
    # "Skip ads with YouTube Premium" renders in the same corner as the real button.
    # An unanchored startswith("skip ad") matched it, so the watcher would click the
    # upsell and navigate to the Premium purchase page mid-video.
    for txt in ("Skip ads with YouTube Premium", "Skip ads with Premium"):
        assert _detect_youtube_skip([_box(txt, 800, 600)], CHROME) is None, txt


def test_skip_ad_still_matches_chevron_variants():
    for txt in ("Skip", "Skip Ad", "Skip Ads", "Skip >", "Skip ad >", "Skip »"):
        assert _detect_youtube_skip([_box(txt, 800, 600)], CHROME) == (800, 600), txt
