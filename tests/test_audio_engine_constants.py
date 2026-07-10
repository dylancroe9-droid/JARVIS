"""
Tests for voice/audio_engine.py constants and the WebRTC VAD frame helper.

These verify the invariants that were broken in bug #4: WebRTC VAD frame
must be exactly one of the WebRTC-supported sizes for the chosen sample
rate, and it must fit inside one audio block.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voice import audio_engine as ae


# ── Constant sanity ───────────────────────────────────────────────────────────

class TestConstants:
    def test_sample_rate_is_16k(self):
        # WebRTC VAD only supports 8/16/32/48 kHz — we use 16k. If this
        # changes, the valid frame sizes change too (verified below).
        assert ae.SAMPLE_RATE == 16_000

    def test_block_size_is_a_power_of_two(self):
        # sounddevice and most audio backends are happiest with PoT blocks
        assert ae.BLOCK_SIZE & (ae.BLOCK_SIZE - 1) == 0

    def test_webrtc_frame_is_valid_size(self):
        # WebRTC accepts {10, 20, 30} ms frames. At 16 kHz that's:
        # 160 / 320 / 480 samples. Any other value silently raises.
        valid_at_16k = {160, 320, 480}
        assert ae.WEBRTC_VAD_FRAME_SAMPLES in valid_at_16k

    def test_webrtc_frame_fits_in_block(self):
        # The whole point: we must be able to slice one VAD frame out of
        # one audio block. If BLOCK_SIZE ever shrinks below the frame,
        # the slice returns short data and VAD raises.
        assert ae.WEBRTC_VAD_FRAME_SAMPLES <= ae.BLOCK_SIZE

    def test_frame_duration_is_meaningful(self):
        # 30 ms is the WebRTC sweet spot for accuracy. Sanity check we're
        # in the 10-30 ms range.
        ms = ae.WEBRTC_VAD_FRAME_SAMPLES * 1000 / ae.SAMPLE_RATE
        assert 10 <= ms <= 30


# ── Behavior of the bounds-checked slice ──────────────────────────────────────

class TestVADFrameExtraction:
    """
    Reproduce the slicing logic from _is_speech and assert it behaves well.
    We can't easily instantiate AudioEngine in a test without a mic, so we
    re-implement the slice and verify the invariants.
    """

    def _extract_frame(self, chunk: np.ndarray) -> np.ndarray:
        """Mirror of the bounds-checked slice inside _is_speech."""
        if len(chunk) < ae.WEBRTC_VAD_FRAME_SAMPLES:
            return None
        return np.ascontiguousarray(
            chunk[:ae.WEBRTC_VAD_FRAME_SAMPLES], dtype=np.int16,
        )

    def test_full_block_yields_full_frame(self):
        chunk = np.zeros(ae.BLOCK_SIZE, dtype=np.int16)
        frame = self._extract_frame(chunk)
        assert frame is not None
        assert len(frame) == ae.WEBRTC_VAD_FRAME_SAMPLES

    def test_short_chunk_returns_none(self):
        # E.g., during mic warm-up the first block can be short
        short = np.zeros(100, dtype=np.int16)
        frame = self._extract_frame(short)
        assert frame is None

    def test_exact_size_chunk_works(self):
        chunk = np.zeros(ae.WEBRTC_VAD_FRAME_SAMPLES, dtype=np.int16)
        frame = self._extract_frame(chunk)
        assert frame is not None
        assert len(frame) == ae.WEBRTC_VAD_FRAME_SAMPLES

    def test_frame_is_contiguous_int16_bytes(self):
        # WebRTC VAD wants raw int16 little-endian bytes. If the array is
        # a non-contiguous view (e.g., from numpy advanced indexing), the
        # tobytes() output is wrong.
        chunk = np.arange(ae.BLOCK_SIZE, dtype=np.int16)
        frame = self._extract_frame(chunk)
        assert frame.dtype == np.int16
        assert frame.flags['C_CONTIGUOUS']
        # 480 samples × 2 bytes/sample = 960 bytes
        assert len(frame.tobytes()) == ae.WEBRTC_VAD_FRAME_SAMPLES * 2

    def test_float_input_gets_converted(self):
        # The audio callback might give us float32 due to backend coercion
        float_chunk = np.zeros(ae.BLOCK_SIZE, dtype=np.float32)
        frame = self._extract_frame(float_chunk)
        assert frame is not None
        assert frame.dtype == np.int16


# ── WebRTC VAD library compatibility check ────────────────────────────────────

class TestWebRTCVADIntegration:
    """
    Verify the actual library accepts our frame size. Catches regressions
    where someone changes BLOCK_SIZE / WEBRTC_VAD_FRAME_SAMPLES without
    realizing the library only accepts specific sizes.
    """

    @pytest.fixture
    def vad(self):
        try:
            import webrtcvad
            return webrtcvad.Vad(ae.WEBRTC_AGGRESSIVENESS)
        except ImportError:
            pytest.skip("webrtcvad not installed")

    def test_vad_accepts_our_frame_size(self, vad):
        frame = np.zeros(ae.WEBRTC_VAD_FRAME_SAMPLES,
                         dtype=np.int16).tobytes()
        # Should not raise — silence at sample rate 16k with our frame size
        result = vad.is_speech(frame, ae.SAMPLE_RATE)
        assert result is False        # zeros are not speech

    def test_vad_rejects_wrong_frame_size(self, vad):
        # Verify the invariant — this is what the FIX prevents from
        # happening in production. If you pass the wrong size, the
        # library throws.
        wrong = np.zeros(100, dtype=np.int16).tobytes()
        with pytest.raises(Exception):
            vad.is_speech(wrong, ae.SAMPLE_RATE)
