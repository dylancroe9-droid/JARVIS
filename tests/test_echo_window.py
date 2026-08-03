"""
Echo suppression must not permanently deafen JARVIS to short commands.

_is_echo dropped ANY sub-3-word utterance whenever _last_reply was non-empty — and
_last_reply is set at the startup greeting and effectively never cleared, so
"open Spotify" / "go back" / "stop" / "next" were dropped as echo forever. The
short-utterance rule is only meant for the split-second of tail bleed right after
JARVIS actually speaks; it's now bounded to a ~2s window after real playback.
"""

import time

import pytest

server = pytest.importorskip("server")


@pytest.fixture(autouse=True)
def _reset():
    server._last_reply = "Evening, sir. What's on your mind?"
    server._last_tts_active_at = 0.0
    # ensure no afplay proc is seen as active
    try:
        server.speaker._play_proc = None
    except Exception:
        pass
    yield


def test_short_command_accepted_long_after_speaking():
    assert server._is_echo("open spotify") is False
    assert server._is_echo("go back") is False
    assert server._is_echo("next") is False


def test_short_utterance_dropped_right_after_playback():
    server._last_tts_active_at = time.time()
    assert server._is_echo("you") is True


def test_short_utterance_window_expires():
    server._last_tts_active_at = time.time() - 3.0   # spoke 3s ago
    assert server._is_echo("stop") is False


def test_heavy_overlap_still_flagged_as_echo():
    server._last_reply = "the weather is seventy degrees and clear today"
    assert server._is_echo("the weather is seventy degrees and clear") is True
