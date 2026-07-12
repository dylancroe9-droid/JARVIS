"""
Tests for the screen_watcher auto-action detectors (YouTube Skip Ad, cookie
banners, "still watching"). These are pure functions of (OCR boxes, window), so
they run headless — no Vision/Quartz needed. Locks in the ad-skip behavior and
its conservative gates (right-lower position, browser-only, confidence floor).
"""

from tools.screen_watcher import (
    OCRBox, FocusedWindow,
    _detect_youtube_skip, _detect_cookie_banner, _detect_still_watching,
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
