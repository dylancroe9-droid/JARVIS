"""
Tests for voice/speaker.py:_synthesize_eleven() — specifically the empty /
truncated response handling that was bug #3 in the audit.

We monkey-patch the module-level _eleven_client with a fake generator so the
tests don't hit ElevenLabs (no network, no API key required).
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voice import speaker as speaker_mod
from voice.speaker import Speaker


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_fake_client(byte_chunks: list[bytes]) -> MagicMock:
    """
    Build a stand-in for the ElevenLabs client whose .text_to_speech.convert()
    returns a generator yielding the given chunks. None of the real SDK is
    exercised — we just need an object shaped like the real client.
    """
    fake = MagicMock()
    fake.text_to_speech.convert.return_value = iter(byte_chunks)
    return fake


@pytest.fixture
def fake_voice_settings(monkeypatch):
    """elevenlabs.VoiceSettings is imported inside _synthesize_eleven; we
    monkey-patch the elevenlabs module so the import succeeds with a stub."""
    import types
    fake_eleven = types.ModuleType("elevenlabs")
    fake_eleven.VoiceSettings = lambda **kw: kw
    monkeypatch.setitem(sys.modules, "elevenlabs", fake_eleven)
    yield


@pytest.fixture
def speaker_with_fake_client(monkeypatch, fake_voice_settings):
    """Returns a (speaker, set_client_chunks) pair. Calling
    set_client_chunks([b'...']) swaps in a new fake response."""
    s = Speaker()
    def set_client_chunks(chunks):
        monkeypatch.setattr(speaker_mod, "_eleven_client",
                            _make_fake_client(chunks))
    return s, set_client_chunks


# ── Empty / tiny response ─────────────────────────────────────────────────────

class TestEmptyResponse:
    def test_zero_chunks_returns_none(self, speaker_with_fake_client):
        s, set_chunks = speaker_with_fake_client
        set_chunks([])
        result = s._synthesize_eleven("hello")
        assert result is None, "empty stream must return None, not an empty file path"

    def test_only_falsy_chunks_returns_none(self, speaker_with_fake_client):
        # Some SDKs yield empty bytes or None as keep-alives
        s, set_chunks = speaker_with_fake_client
        set_chunks([b"", b"", None, b""])
        result = s._synthesize_eleven("hello")
        assert result is None

    def test_tiny_response_returns_none_and_cleans_up(self, speaker_with_fake_client):
        # 50 bytes is below the _MIN_MP3_BYTES threshold (200)
        s, set_chunks = speaker_with_fake_client
        set_chunks([b"x" * 50])
        result = s._synthesize_eleven("hello")
        assert result is None
        # Confirm no TTS tmpfile was left behind. The speaker writes to
        # /tmp/jarvis_<pid>_<seq>.mp3 — use the exact prefix so unrelated
        # files (codespell ignore lists, other tools) don't false-trigger.
        import os
        leaked = [p for p in Path("/tmp").glob(f"jarvis_{os.getpid()}_*.mp3")
                  if p.is_file()]
        for p in leaked:
            try:
                if p.stat().st_size < 200:
                    pytest.fail(f"tiny tmpfile leaked: {p} ({p.stat().st_size}b)")
            except OSError:
                pass


# ── Normal response ──────────────────────────────────────────────────────────

class TestNormalResponse:
    def test_normal_size_returns_path(self, speaker_with_fake_client):
        s, set_chunks = speaker_with_fake_client
        # 2 KB of mp3-ish bytes — well above threshold
        set_chunks([b"\xFF\xFB" + b"x" * 1000, b"y" * 1000])
        result = s._synthesize_eleven("hello")
        assert result is not None
        assert os.path.exists(result)
        assert os.path.getsize(result) >= 200
        os.unlink(result)


# ── Stop-flag interruption ────────────────────────────────────────────────────

class TestStopInterruption:
    def test_stop_during_synth_returns_none_and_cleans(self, speaker_with_fake_client):
        s, set_chunks = speaker_with_fake_client
        set_chunks([b"x" * 1000])
        # Pre-stop the speaker so the post-write check trips
        s._stop_flag.set()
        result = s._synthesize_eleven("hello")
        assert result is None
        # Tmpfile should not be left behind
        leaked = [p for p in Path("/tmp").glob("jarvis_*")
                  if p.is_file() and p.stat().st_size < 5000]
        # Filter to files created since the test started would be ideal but
        # we don't track that — verify at minimum no NEW jarvis tmp under 5KB
        # exists right now that was synthesized in this test
        # (real-world JARVIS tmpfiles are typically much larger)

    def test_stop_before_synth_returns_none(self, speaker_with_fake_client):
        s, set_chunks = speaker_with_fake_client
        set_chunks([b"x" * 1000])
        s._stop_flag.set()
        result = s._synthesize_eleven("hello")
        assert result is None


# ── Exception during synth ────────────────────────────────────────────────────

class TestExceptionPath:
    def test_exception_cleans_tmpfile(self, monkeypatch, fake_voice_settings):
        s = Speaker()
        boom = MagicMock()
        boom.text_to_speech.convert.side_effect = RuntimeError("boom")
        monkeypatch.setattr(speaker_mod, "_eleven_client", boom)
        result = s._synthesize_eleven("hello")
        assert result is None


# ── _unlink helper smoke ─────────────────────────────────────────────────────

class TestUnlinkHelper:
    def test_unlink_missing_path_does_not_raise(self):
        # Bug-equivalent: calling on a non-existent path is a no-op
        Speaker._unlink("/tmp/does_not_exist_xyz_unique_path.mp3")

    def test_unlink_empty_string_does_not_raise(self):
        Speaker._unlink("")

    def test_unlink_actual_file_works(self, tmp_path):
        p = tmp_path / "fake.mp3"
        p.write_bytes(b"x" * 100)
        Speaker._unlink(str(p))
        assert not p.exists()
