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


class TestHardMute:
    """
    Type-first / mic-off. suspend() only flipped state to IDLE, leaving the capture
    callback fully running — so 'muted' room audio was still transcribed and could
    fire commands (this is what let it act on the TV). mute_mic() is a real hard
    mute: the callback drops every block. Typed input is unaffected.
    """

    def _engine(self):
        import numpy as np, queue
        from voice.audio_engine import AudioEngine
        e = AudioEngine.__new__(AudioEngine)
        e._closed = False; e._muted = False; e._state = AudioEngine.DETECTING
        e._frames = []; e._speech_started = False; e._silence_count = 0
        e._block_count = 0; e._loud_count = 0; e._onset_count = 0
        e._audio_q = queue.Queue()
        return e

    def test_muted_callback_drops_audio(self):
        import numpy as np
        e = self._engine()
        e.mute_mic()
        blk = (np.random.randn(480) * 3000).astype("int16").reshape(-1, 1)
        e._callback_inner(blk, 480, None, None)
        assert e._frames == [], "muted mic must buffer no audio"
        assert e._audio_q.qsize() == 0, "muted mic must queue nothing for STT"

    def test_unmute_restores_listening(self):
        from voice.audio_engine import AudioEngine
        e = self._engine()
        e.mute_mic()
        assert e.muted is True
        e.unmute_mic()
        assert e.muted is False
        assert e._state == AudioEngine.DETECTING

    def test_engine_registers_for_tool_access(self):
        # set_listening reaches the mic via get_engine(); if registration breaks,
        # the tool silently can't toggle anything.
        import voice.audio_engine as ae
        assert hasattr(ae, "get_engine")

    def test_set_listening_is_resident(self):
        # Deferred, Claude confabulated a false "I can't mute the mic" instead of
        # calling it. Mic control must be always-loaded.
        from brain.jarvis import _BASE_TOOL_NAMES
        assert "set_listening" in _BASE_TOOL_NAMES
