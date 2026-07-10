"""
Tests for the unified mode manager (tools/mode_manager.py).

Covers:
  - classify_screen() priority + signal matching (watch > gaming > work > normal)
  - ModeManager.force / exit_forced routing and no-double-fire
  - sticky gaming (auto-entered gaming pins itself, doesn't flap)
  - callbacks fire OUTSIDE the internal lock (so blocking TTS can't stall polls)

These monkeypatch `_frontmost` / `classify_screen` so they run headless with no
GUI session — the real Quartz probe returns nothing in CI.
"""

import threading

import tools.mode_manager as mm


# ── classify_screen matrix ─────────────────────────────────────────────────────

def _patch_front(monkeypatch, app, title):
    monkeypatch.setattr(mm, "_frontmost", lambda: (app, title))


def test_classify_youtube_in_browser_is_watch(monkeypatch):
    _patch_front(monkeypatch, "Google Chrome", "Rick Astley - Never Gonna - YouTube")
    mode, _ = mm.classify_screen()
    assert mode == "watch"


def test_classify_netflix_title_is_watch(monkeypatch):
    _patch_front(monkeypatch, "Safari", "Netflix")
    assert mm.classify_screen()[0] == "watch"


def test_classify_native_video_app_is_watch(monkeypatch):
    _patch_front(monkeypatch, "VLC", "some_movie.mkv")
    assert mm.classify_screen()[0] == "watch"


def test_classify_quicktime_is_watch(monkeypatch):
    _patch_front(monkeypatch, "QuickTime Player", "clip.mov")
    assert mm.classify_screen()[0] == "watch"


def test_classify_steam_is_gaming(monkeypatch):
    _patch_front(monkeypatch, "Steam", "")
    assert mm.classify_screen()[0] == "gaming"


def test_classify_app_name_with_game_substring_is_gaming(monkeypatch):
    _patch_front(monkeypatch, "Some Cool Game", "")
    assert mm.classify_screen()[0] == "gaming"


def test_classify_vscode_is_work(monkeypatch):
    _patch_front(monkeypatch, "Code", "server.py — JARVIS")
    assert mm.classify_screen()[0] == "work"


def test_classify_terminal_is_work(monkeypatch):
    _patch_front(monkeypatch, "Terminal", "bash")
    assert mm.classify_screen()[0] == "work"


def test_classify_github_in_browser_is_work(monkeypatch):
    _patch_front(monkeypatch, "Google Chrome", "dylanroe/JARVIS - GitHub")
    assert mm.classify_screen()[0] == "work"


def test_classify_plain_browser_is_normal(monkeypatch):
    _patch_front(monkeypatch, "Google Chrome", "Google")
    assert mm.classify_screen()[0] == "normal"


def test_classify_finder_is_normal(monkeypatch):
    _patch_front(monkeypatch, "Finder", "Downloads")
    assert mm.classify_screen()[0] == "normal"


def test_classify_no_window_is_normal(monkeypatch):
    _patch_front(monkeypatch, "", "")
    assert mm.classify_screen()[0] == "normal"


def test_watch_beats_gaming_priority(monkeypatch):
    # A browser titled youtube should be watch even though nothing else matches
    _patch_front(monkeypatch, "Google Chrome", "youtube.com - game trailer")
    assert mm.classify_screen()[0] == "watch"


def test_own_hud_never_classifies(monkeypatch):
    # If _frontmost returns our own app it should already be filtered; simulate
    # by returning empty (which _frontmost does for Electron/JARVIS).
    _patch_front(monkeypatch, "", "")
    assert mm.classify_screen()[0] == "normal"


def test_classify_pure_function_is_side_effect_free():
    # classify() must not touch the system — same inputs, same output.
    assert mm.classify("Steam", "") == mm.classify("Steam", "")
    assert mm.classify("Steam", "")[0] == "gaming"
    assert mm.classify("", "")[0] == "normal"


# ── looks_like_watch_site (pull-up watch/work routing) ──────────────────────────

def test_looks_like_watch_site_streaming():
    for name in ("Netflix", "youtube.com", "Peacock", "Hulu", "Disney+",
                 "https://www.max.com", "Prime Video", "twitch.tv"):
        assert mm.looks_like_watch_site(name), name


def test_looks_like_watch_site_non_streaming():
    for name in ("GitHub", "Gmail", "google.com", "Amazon", "Wikipedia"):
        assert not mm.looks_like_watch_site(name), name


def test_looks_like_watch_site_empty():
    assert not mm.looks_like_watch_site("")
    assert not mm.looks_like_watch_site(None)


# ── ModeManager behavior ────────────────────────────────────────────────────────

def _mk_manager():
    events = []
    mgr = mm.ModeManager(on_mode_change=lambda mode, reason: events.append((mode, reason)),
                         poll_sec=999)
    return mgr, events


def _front(monkeypatch, app, title=""):
    """Point both _frontmost() and classify_screen() at a fixed window."""
    monkeypatch.setattr(mm, "_frontmost", lambda: (app, title))


def test_force_fires_callback():
    mgr, events = _mk_manager()
    mgr.force("gaming")
    assert events == [("gaming", "forced by voice")]
    assert mgr.mode == "gaming"


def test_force_same_mode_twice_only_fires_once():
    mgr, events = _mk_manager()
    mgr.force("gaming")
    mgr.force("gaming")
    assert events == [("gaming", "forced by voice")]


def test_force_work_overrides_active_watch(monkeypatch):
    # Bug #4: pulling up a work site while watching must actually switch to work
    # (un-muting notifications), not just speak the work line. At the manager
    # level, force("work") from watch must transition.
    _front(monkeypatch, "Safari", "Netflix")
    mgr, events = _mk_manager()
    mgr.force("watch", reason="pull-up")
    events.clear()
    mgr.force("work")
    assert events == [("work", "forced by voice")]
    assert mgr.mode == "work"


def test_forced_work_holds_while_same_app(monkeypatch):
    # Pull-up forces work on a site that classifies as "normal" (e.g. a news
    # article). While that same app stays frontmost, the pin keeps us in work.
    _front(monkeypatch, "Google Chrome", "CNN - Breaking News")
    mgr, events = _mk_manager()
    mgr.force("work")
    assert mgr.mode == "work"
    events.clear()
    mgr._poll_once()               # same app → pin holds despite classify=normal
    assert events == []
    assert mgr.mode == "work"


def test_forced_work_releases_on_app_change(monkeypatch):
    _front(monkeypatch, "Google Chrome", "CNN - Breaking News")
    mgr, events = _mk_manager()
    mgr.force("work")
    events.clear()
    # User switches to Slack (classifies normal) → work pin releases.
    _front(monkeypatch, "Slack", "")
    mgr._poll_once()
    assert events == [("normal", "Slack")]
    assert mgr.mode == "normal"
    assert mgr._forced is None


def test_forced_gaming_holds_across_app_change(monkeypatch):
    # Gaming is sticky — alt-tabbing to a browser must NOT drop gaming mode.
    _front(monkeypatch, "Steam", "")
    mgr, events = _mk_manager()
    mgr.force("gaming")
    events.clear()
    _front(monkeypatch, "Google Chrome", "some wiki")
    mgr._poll_once()
    assert events == []
    assert mgr.mode == "gaming"


def test_exit_forced_goes_to_normal(monkeypatch):
    mgr, events = _mk_manager()
    _front(monkeypatch, "Steam", "")
    mgr.force("gaming")
    events.clear()
    _front(monkeypatch, "Finder", "Downloads")
    mgr.exit_forced()
    assert events == [("normal", "exited by voice")]
    assert mgr.mode == "normal"


def test_exit_gaming_while_game_frontmost_stays_normal_no_flap(monkeypatch):
    # The core anti-flap guarantee: "exit gaming mode" while the game is STILL
    # frontmost must leave gaming and NOT flip back on the next poll.
    mgr, events = _mk_manager()
    _front(monkeypatch, "Steam", "")
    mgr.force("gaming")
    events.clear()
    mgr.exit_forced()                       # game still frontmost
    assert mgr.mode == "normal"
    assert events == [("normal", "exited by voice")]
    # Next poll, same app → must stay normal (pin holds), NOT re-enter gaming.
    events.clear()
    mgr._poll_once()
    assert events == []
    assert mgr.mode == "normal"
    # But if they alt-tab away and back to the game, gaming re-detects (intended).
    _front(monkeypatch, "Finder", "")
    mgr._poll_once()                        # app changed → pin releases → normal
    assert mgr.mode == "normal"
    _front(monkeypatch, "Steam", "")
    mgr._poll_once()                        # back to the game → gaming again
    assert mgr.mode == "gaming"


def test_autodetected_gaming_is_sticky(monkeypatch):
    mgr, events = _mk_manager()
    _front(monkeypatch, "Steam", "")
    mgr._poll_once()
    assert events == [("gaming", "Steam")]
    assert mgr._forced == "gaming"     # pinned itself
    # Now the screen changes to a browser — sticky gaming must NOT switch away.
    events.clear()
    _front(monkeypatch, "Google Chrome", "wiki")
    mgr._poll_once()
    assert events == []
    assert mgr.mode == "gaming"


def test_autodetected_watch_is_not_sticky(monkeypatch):
    mgr, events = _mk_manager()
    _front(monkeypatch, "Safari", "Netflix")
    mgr._poll_once()
    assert mgr.mode == "watch"
    assert mgr._forced is None         # watch tracks the screen, not pinned
    # Screen changes → watch gives way to the new context.
    _front(monkeypatch, "Finder", "Downloads")
    mgr._poll_once()
    assert mgr.mode == "normal"


def test_work_tracks_screen(monkeypatch):
    mgr, events = _mk_manager()
    _front(monkeypatch, "Code", "server.py")
    mgr._poll_once()
    assert mgr.mode == "work"
    assert mgr._forced is None
    _front(monkeypatch, "Safari", "YouTube")
    mgr._poll_once()
    assert mgr.mode == "watch"


def test_set_auto_off_returns_to_normal_and_pauses(monkeypatch):
    mgr, events = _mk_manager()
    _front(monkeypatch, "Steam", "")
    mgr.force("gaming")
    events.clear()
    mgr.set_auto(False)
    assert mgr.mode == "normal"
    assert events == [("normal", "auto mode off")]
    assert mgr.paused is True
    # While paused, even a game on screen must NOT switch modes.
    events.clear()
    _front(monkeypatch, "Steam", "")
    mgr._poll_once()
    assert events == []
    assert mgr.mode == "normal"


def test_set_auto_off_when_already_normal_is_silent(monkeypatch):
    mgr, events = _mk_manager()
    mgr.set_auto(False)
    assert mgr.paused is True
    assert events == []          # no spurious normal→normal fire


def test_set_auto_on_resumes_detection(monkeypatch):
    mgr, events = _mk_manager()
    mgr.set_auto(False)
    _front(monkeypatch, "Code", "server.py")
    mgr.set_auto(True)
    assert mgr.paused is False
    mgr._poll_once()
    assert mgr.mode == "work"


def test_fire_skips_superseded_transition():
    # If the manager has already settled on a newer mode, a stale _fire (e.g.
    # from an auto-poll that raced a voice force) must be dropped so the badge
    # and spoken line don't disagree with the applied mode.
    mgr, events = _mk_manager()
    mgr._mode = "gaming"                       # manager settled on gaming
    mgr._fire("watch", "stale", "normal")      # stale watch transition arrives
    assert events == []                        # dropped, not applied


def test_fire_runs_matching_transition():
    mgr, events = _mk_manager()
    mgr._mode = "watch"
    mgr._fire("watch", "fresh", "normal")
    assert events == [("watch", "fresh")]


def test_callback_runs_outside_lock():
    # The callback tries to acquire the manager's lock; if force() held it while
    # calling back, this would deadlock. It must not.
    mgr, _ = _mk_manager()

    def cb(mode, reason):
        # Acquiring the lock here proves it isn't held during the callback.
        got = mgr._lock.acquire(timeout=2)
        assert got, "lock was held during callback — would stall the poll thread"
        mgr._lock.release()

    mgr.on_mode_change = cb
    done = threading.Event()

    def run():
        mgr.force("gaming")
        done.set()

    t = threading.Thread(target=run)
    t.start()
    t.join(timeout=5)
    assert done.is_set(), "force() deadlocked or hung"
