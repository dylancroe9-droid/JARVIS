"""
Speaker verification must actually run.

is_owner() existed but was called from nowhere, so last_similarity() sat at its
1.0 default forever and the server's lip gate read every sound in the room as a
perfect match for Dylan. A fragment off a video went in as a command and JARVIS
answered it on screen. An always-on mic in someone's bedroom must not fail OPEN
on identity.
"""

import numpy as np
import pytest

import voice.voice_profile as vp


@pytest.fixture(autouse=True)
def _reset():
    with vp._last_similarity_lock:
        vp._last_similarity = 1.0
        vp._last_similarity_verified = False
    yield


def test_similarity_is_not_fresh_before_any_check():
    # The placeholder must be distinguishable from a real measurement.
    assert vp.last_similarity() == 1.0
    assert vp.similarity_is_fresh() is False


def test_similarity_becomes_fresh_after_a_check(monkeypatch):
    monkeypatch.setattr(vp, "_profile_embedding", np.ones(256))
    monkeypatch.setattr(vp, "embed", lambda a: np.ones(256))
    vp.is_owner(np.zeros(16000, dtype=np.int16))
    assert vp.similarity_is_fresh() is True


def test_mismatched_speaker_is_rejected(monkeypatch):
    profile = np.zeros(256); profile[0] = 1.0
    other = np.zeros(256);   other[1] = 1.0          # orthogonal → similarity 0
    monkeypatch.setattr(vp, "_profile_embedding", profile)
    monkeypatch.setattr(vp, "embed", lambda a: other)
    assert vp.is_owner(np.zeros(16000, dtype=np.int16)) is False
    assert vp.last_similarity() < vp.SIMILARITY_THRESHOLD


def test_matching_speaker_is_accepted(monkeypatch):
    profile = np.zeros(256); profile[0] = 1.0
    monkeypatch.setattr(vp, "_profile_embedding", profile)
    monkeypatch.setattr(vp, "embed", lambda a: profile.copy())
    assert vp.is_owner(np.zeros(16000, dtype=np.int16)) is True


def test_no_profile_passes_through(monkeypatch):
    # Refusing to listen at all is a worse failure than an occasional stray pickup.
    monkeypatch.setattr(vp, "_profile_embedding", None)
    monkeypatch.setattr(vp, "PROFILE_PATH", vp.Path("/nonexistent/profile.npy"))
    assert vp.is_owner(np.zeros(16000, dtype=np.int16)) is True
    assert vp.similarity_is_fresh() is True


class TestFlushGate:
    """The audio flush must consult the speaker check before queuing for STT."""

    def test_flush_calls_the_speaker_check(self):
        import inspect
        from voice import audio_engine
        src = inspect.getsource(audio_engine.AudioEngine._flush)
        assert "_speaker_ok" in src, "flush must verify the speaker before STT"

    def test_speaker_ok_is_permissive_without_a_profile(self, monkeypatch):
        from voice.audio_engine import AudioEngine
        monkeypatch.setattr(vp, "has_profile", lambda: False)
        ok = AudioEngine._speaker_ok(object(), np.zeros(16000, dtype=np.int16))
        assert ok is True

    def test_speaker_ok_rejects_a_mismatch(self, monkeypatch):
        from voice.audio_engine import AudioEngine
        monkeypatch.setattr(vp, "has_profile", lambda: True)
        monkeypatch.setattr(vp, "is_owner", lambda a: False)
        ok = AudioEngine._speaker_ok(object(), np.zeros(16000, dtype=np.int16))
        assert ok is False

    def test_speaker_ok_does_not_go_deaf_on_error(self, monkeypatch):
        from voice.audio_engine import AudioEngine
        def boom(a): raise RuntimeError("encoder died")
        monkeypatch.setattr(vp, "has_profile", lambda: True)
        monkeypatch.setattr(vp, "is_owner", boom)
        ok = AudioEngine._speaker_ok(object(), np.zeros(16000, dtype=np.int16))
        assert ok is True, "a broken verifier must not make JARVIS deaf"
