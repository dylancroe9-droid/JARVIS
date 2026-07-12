"""
Voice profile — speaker verification for JARVIS.

Workflow:
  1. Enrollment: record Dylan talking for ~15s → extract 256D GE2E embedding → save
  2. Runtime: for every audio chunk that passes WebRTC VAD, extract embedding and
     compute cosine similarity against Dylan's stored embedding.  If similarity ≥
     SIMILARITY_THRESHOLD the audio is from Dylan and should be processed.  Below
     the threshold it is likely someone else and gets silently dropped.

If no profile exists the checker always returns True (no filtering) so JARVIS
works exactly as before until Dylan explicitly enrolls.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

import numpy as np

# ── Config ────────────────────────────────────────────────────────────────────

PROFILE_PATH       = Path.home() / ".jarvis_voice_profile.npy"
# Voice-as-primary, lip-as-backup architecture:
#   PASS_THRESHOLD (0.55) — below this, audio is rejected at VAD level
#   CONFIDENT_THRESHOLD (0.80) — when off-camera, voice match must be THIS high
#                                 to accept. Tightened from 0.68 — videos with
#                                 similar voices were sneaking through.
#   Between the two — marginal match. Lip gate decides (when face is visible).
SIMILARITY_THRESHOLD = 0.55
CONFIDENT_THRESHOLD  = 0.80
ENROLL_SECS        = 12        # seconds of speech to capture during enrollment
SAMPLE_RATE        = 16_000

# Emergency bypass — when voice profile keeps rejecting valid speech.
DISABLED = os.environ.get("JARVIS_DISABLE_VOICE_PROFILE", "").lower() in ("1", "true", "yes")

# ── Module-level encoder (loaded once, reused) ────────────────────────────────

_encoder      = None
_encoder_lock = threading.Lock()

def _get_encoder():
    global _encoder
    if _encoder is not None:
        return _encoder
    with _encoder_lock:
        if _encoder is not None:
            return _encoder
        try:
            from resemblyzer import VoiceEncoder
            _encoder = VoiceEncoder()
        except Exception as exc:
            print(f"[voice_profile] resemblyzer unavailable: {exc}")
            _encoder = None
    return _encoder


# ── Stored embedding ──────────────────────────────────────────────────────────

_profile_embedding: Optional[np.ndarray] = None
_profile_lock = threading.Lock()


def load_profile() -> bool:
    """Load voice profile from disk. Returns True if a profile was found."""
    global _profile_embedding
    if not PROFILE_PATH.exists():
        return False
    with _profile_lock:
        try:
            _profile_embedding = np.load(str(PROFILE_PATH))
            print(f"[voice_profile] Loaded voice profile ({_profile_embedding.shape[0]}D embedding)")
            return True
        except Exception as exc:
            print(f"[voice_profile] Failed to load profile: {exc}")
            _profile_embedding = None
            return False


def save_profile(embedding: np.ndarray) -> None:
    """Persist voice embedding to disk."""
    global _profile_embedding
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    np.save(str(PROFILE_PATH), embedding)
    with _profile_lock:
        _profile_embedding = embedding
    print(f"[voice_profile] Voice profile saved → {PROFILE_PATH}")


def has_profile() -> bool:
    return _profile_embedding is not None or PROFILE_PATH.exists()


def clear_profile() -> None:
    """Delete voice profile — JARVIS reverts to no-filter mode."""
    global _profile_embedding
    with _profile_lock:
        _profile_embedding = None
    if PROFILE_PATH.exists():
        PROFILE_PATH.unlink()
    print("[voice_profile] Voice profile cleared")


# ── Embedding extraction ──────────────────────────────────────────────────────

def embed(audio_np: np.ndarray) -> Optional[np.ndarray]:
    """Extract a 256D speaker embedding from raw int16 audio at 16kHz."""
    enc = _get_encoder()
    if enc is None:
        return None
    try:
        # resemblyzer needs float64 normalised to [-1, 1]
        wav = audio_np.astype(np.float64) / 32768.0
        if len(wav) < SAMPLE_RATE * 0.5:   # need at least 0.5s
            return None
        return enc.embed_utterance(wav)
    except Exception:
        return None


# ── Similarity check ──────────────────────────────────────────────────────────

# Track the most recent similarity score so the server can decide whether to
# use the lip gate as a backup. Updated every is_owner() call. 1.0 means
# "no profile / accepted with full confidence" so the server skips lip check.
_last_similarity: float = 1.0
_last_similarity_lock = threading.Lock()

def last_similarity() -> float:
    """Return the cosine-similarity score from the most recent is_owner() call."""
    with _last_similarity_lock:
        return _last_similarity

def is_owner(audio_np: np.ndarray) -> bool:
    """
    Returns True if this audio chunk sounds like the enrolled user, OR if no
    profile exists (pass-through mode — no filtering).

    Called once per audio chunk AFTER WebRTC VAD confirms speech.
    Adds ~20ms overhead on MacBook; negligible.

    Side effect: updates _last_similarity so the server can decide whether
    to consult the lip-gate backup. last_similarity() returns the score.
    """
    global _last_similarity

    if DISABLED:
        with _last_similarity_lock:
            _last_similarity = 1.0
        return True

    with _profile_lock:
        profile = _profile_embedding

    if profile is None:
        # Lazy-load from disk
        if PROFILE_PATH.exists():
            load_profile()
            with _profile_lock:
                profile = _profile_embedding

    if profile is None:
        with _last_similarity_lock:
            _last_similarity = 1.0
        return True

    emb = embed(audio_np)
    if emb is None:
        # Too short — pass through with full confidence so lip gate is skipped
        with _last_similarity_lock:
            _last_similarity = 1.0
        return True

    sim = float(np.dot(emb, profile) / (np.linalg.norm(emb) * np.linalg.norm(profile) + 1e-9))
    with _last_similarity_lock:
        _last_similarity = sim
    return sim >= SIMILARITY_THRESHOLD


# ── Enrollment ────────────────────────────────────────────────────────────────

def enroll_from_audio(audio_np: np.ndarray) -> bool:
    """
    Build a voice profile from a recording of Dylan speaking.
    audio_np: int16 array at 16kHz, should be 10-20 seconds of clean speech.
    Returns True on success.
    """
    enc = _get_encoder()
    if enc is None:
        print("[voice_profile] Can't enroll — resemblyzer not available")
        return False
    try:
        wav = audio_np.astype(np.float64) / 32768.0
        # Build embedding from full utterance
        embedding = enc.embed_utterance(wav)
        save_profile(embedding)
        return True
    except Exception as exc:
        print(f"[voice_profile] Enrollment failed: {exc}")
        return False
