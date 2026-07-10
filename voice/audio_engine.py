"""
Single-stream audio engine.

One sd.InputStream stays open the entire session and drives a state machine:

  DETECTING  — passive: buffer speech segments, send to STT for wake word
  RECORDING  — active:  record the command that follows the wake word
  BARGE_IN   — silent:  watch mic energy only; fire callback when user speaks
  IDLE       — paused:  stream open, audio discarded (used during processing)

Having one stream eliminates the double-stream conflicts that plagued the old design.
"""

from __future__ import annotations

import queue
import threading
from typing import Callable, Optional

import numpy as np
import sounddevice as sd
from rich.console import Console

console = Console()

# ── Tuning ───────────────────────────────────────────────────────────────────
SAMPLE_RATE  = 16_000
BLOCK_SIZE   = 512            # 32 ms per block — fast VAD / barge-in response

# WebRTC VAD requires EXACTLY one of {80, 160, 240, 320, 480, 640} samples
# per frame at 16kHz (corresponding to 10/15/20/30/40ms windows). Our
# BLOCK_SIZE (512) doesn't match any of those, so we use the largest VAD
# frame that fits inside one block — 30ms / 480 samples — and silently
# discard the trailing ~2ms. This used to be hardcoded as `chunk[:480]`
# inside _is_speech which broke if BLOCK_SIZE ever changed below 480 and
# silently fell back to RMS on any error. Now it's a named constant and
# the slicing is bounds-checked.
WEBRTC_VAD_FRAME_SAMPLES = 480     # 30 ms at 16 kHz — valid for WebRTC VAD
assert WEBRTC_VAD_FRAME_SAMPLES <= BLOCK_SIZE, (
    f"WebRTC frame ({WEBRTC_VAD_FRAME_SAMPLES}) must fit in BLOCK_SIZE ({BLOCK_SIZE})"
)

# WebRTC VAD replaces raw RMS as the primary speech detector.
# It's a ML model specifically trained to distinguish human speech from noise,
# music, fan hum, phone audio, etc. — far more accurate than RMS alone.
# RMS is still used as an energy floor (ignore near-silence) and for barge-in.
WEBRTC_AGGRESSIVENESS = 2     # 0=permissive … 3=aggressive noise rejection; 2 is the sweet spot
VAD_RMS_FLOOR = 80            # absolute silence floor — skip WebRTC below this (saves CPU)
VAD_RMS      = 210            # fallback RMS threshold if WebRTC unavailable
VAD_ONSET    = 1              # 1 speech block (~32ms) before flagging — was 2 (clipped first word)
SILENCE_SECS = 0.5            # wake detection: flush after 0.5s silence
SILENCE_BLKS = int(SILENCE_SECS * SAMPLE_RATE / BLOCK_SIZE)

CMD_SILENCE_SECS = 0.65       # 0.65s silence — fast response without cutting off normal speech
CMD_SILENCE_BLKS = int(CMD_SILENCE_SECS * SAMPLE_RATE / BLOCK_SIZE)

MAX_WAKE_SECS   = 7
MAX_WAKE_BLKS   = int(MAX_WAKE_SECS * SAMPLE_RATE / BLOCK_SIZE)
KEEP_BLKS       = int(0.8 * SAMPLE_RATE / BLOCK_SIZE)   # 0.8s pre-roll (was 0.4s — first word kept getting clipped)

MAX_CMD_SECS    = 12
MAX_CMD_BLKS    = int(MAX_CMD_SECS * SAMPLE_RATE / BLOCK_SIZE)
CMD_TIMEOUT_SECS = 7          # give up waiting for command after this
CMD_TIMEOUT_BLKS = int(CMD_TIMEOUT_SECS * SAMPLE_RATE / BLOCK_SIZE)

CONVERSE_TIMEOUT_SECS = 2     # seconds of silence before reverting to wake-word mode
CONVERSE_TIMEOUT_BLKS = int(CONVERSE_TIMEOUT_SECS * SAMPLE_RATE / BLOCK_SIZE)

BARGE_RMS_MIN  = 800          # floor: low enough that normal-volume speech triggers
BARGE_RMS_MAX  = 3500         # ceiling: high enough that ElevenLabs speaker bleed
                              # (~1500 RMS) doesn't pin the threshold at the cap
BARGE_MEASURE  = 20           # blocks to sample ambient/speaker level (~640ms)
BARGE_FRAMES   = 15           # ~480ms — slightly longer than 380ms to avoid false trips
                              # from JARVIS's own voice variation during ElevenLabs playback
BARGE_MULT     = 2.5          # threshold = ambient × this. Was 2.2 — bumped for headroom.

MIN_AUDIO    = int(0.20 * SAMPLE_RATE)   # skip STT if audio < 200 ms (was 350 — was dropping short commands)

def _load_wake_words() -> list[str]:
    """Comma-separated env override → fall back to defaults.

    Includes common Whisper mistranscriptions of "JARVIS" so the wake-word
    detector still fires even when STT mangles the name. Empirically observed
    on MacBook Air built-in mic with Whisper small:
      "joris", "jervis", "arvis", "yarvis", "javris", "jorvis", "jarvis"
    Plus common compound mistakes:
      "hate arvis" (heard "hey jarvis"), "he jarvis" (heard "hey jarvis"),
      "hey joris", "ok arvis", etc.
    """
    import os as _os
    raw = _os.getenv("JARVIS_WAKE_WORDS", "").strip()
    if raw:
        words = [w.strip().lower() for w in raw.split(",") if w.strip()]
        if words:
            return words
    return [
        # Canonical
        "jarvis", "hey jarvis", "ok jarvis", "yo jarvis", "okay jarvis",
        # Whisper STT mistranscriptions of "jarvis"
        "joris", "jervis", "jorvis", "yarvis", "javris",
        # Whisper mishears "hey" as "hate" / "he" / "hi"
        "hate jarvis", "hate arvis", "he jarvis", "he arvis", "hi jarvis",
        "hey joris", "hey jervis", "hey arvis", "hey yarvis",
        "ok arvis", "okay arvis",
        # Bare "arvis" (sometimes the "j" gets clipped)
        "arvis",
    ]


WAKE_WORDS = _load_wake_words()


def _pick_input_device(devices) -> int | None:
    """
    Pick the best input device for wake-word listening.

    Priority order:
    1. AirPods / Bluetooth headset — when in ears, only picks up Dylan's voice.
       Critical in class/public so ambient speech doesn't trigger JARVIS.
    2. Built-in MacBook mic — reliable fallback when no headset connected.
    3. System default.
    """
    # Priority 1: AirPods or any Bluetooth/wireless headset
    airpod_hints = [
        "airpods", "airpod", "beats", "powerbeats",
        "bluetooth", "wireless", "headset", "headphones",
        "earpods", "bose", "sony", "jabra", "sennheiser",
    ]
    for i, d in enumerate(devices):
        name = d.get("name", "").lower()
        if d.get("max_input_channels", 0) > 0:
            if any(h in name for h in airpod_hints):
                return i

    # Priority 2: built-in MacBook mic
    builtins = ["macbook air microphone", "macbook pro microphone",
                "built-in microphone", "built-in input"]
    for i, d in enumerate(devices):
        name = d.get("name", "").lower()
        if d.get("max_input_channels", 0) > 0:
            if any(b in name for b in builtins):
                return i

    # Priority 3: any internal device
    for i, d in enumerate(devices):
        name = d.get("name", "").lower()
        if d.get("max_input_channels", 0) > 0:
            if "internal" in name or "built" in name:
                return i

    return None


class AudioEngine:
    """
    State-machine audio engine over a single sounddevice input stream.
    All public methods are thread-safe.
    """

    DETECTING  = "detecting"
    RECORDING  = "recording"
    CONVERSING = "conversing"
    BARGE_IN   = "barge_in"
    IDLE       = "idle"
    STUDY      = "study"

    def __init__(
        self,
        on_transcription: Callable[[str, str], None],
        on_barge_in:      Callable[[], None],
        on_speech_end:    Optional[Callable[[], None]] = None,
    ) -> None:
        """
        on_transcription(kind, text):
            kind = "wake"    — full utterance that triggered wake word
            kind = "command" — the command text to execute
        on_barge_in():
            called when sustained loud speech detected in BARGE_IN state
        on_speech_end():
            called the moment speech ends and audio is queued for STT —
            used to play an instant acknowledgment sound before Whisper runs
        """
        self._on_transcription = on_transcription
        self._on_barge_in      = on_barge_in
        self._on_speech_end    = on_speech_end

        self._state: str      = self.IDLE
        self._frames: list    = []
        self._speech_started  = False
        self._silence_count   = 0
        self._block_count     = 0
        self._loud_count      = 0
        self._onset_count     = 0   # consecutive speech blocks — must hit VAD_ONSET before speech flagged

        # WebRTC VAD — loaded once, reused every block
        self._webrtc_vad = None
        try:
            import webrtcvad as _wvad
            self._webrtc_vad = _wvad.Vad(WEBRTC_AGGRESSIVENESS)
        except Exception:
            pass  # falls back to RMS-only

        # Voice profile — load on init, used to verify speaker is Dylan
        try:
            from voice.voice_profile import load_profile
            load_profile()
        except Exception:
            pass

        # Barge-in dynamic threshold
        self._barge_measure   = 0      # frames sampled so far
        self._barge_rms_sum   = 0.0    # running sum during measurement
        self._barge_threshold = BARGE_RMS_MIN  # computed after measurement

        self._audio_q: queue.Queue = queue.Queue()
        self._sr       = None
        self._stream: Optional[sd.InputStream] = None
        self._ready    = False
        self._closed   = False

        threading.Thread(target=self._init, daemon=True, name="audio-init").start()

    # ── Initialisation ────────────────────────────────────────────────────────

    def _init(self) -> None:
        try:
            import speech_recognition as sr
            self._sr = sr
        except ImportError as exc:
            console.print(f"[yellow]speech_recognition not installed — voice input disabled: {exc}[/yellow]")
            return

        try:
            devices = sd.query_devices()
            input_devices = [d for d in devices if d.get("max_input_channels", 0) > 0]
            if not input_devices:
                console.print("[yellow]No audio input devices found — running in text-only mode.[/yellow]")
                return
        except Exception as exc:
            console.print(f"[yellow]Could not query audio devices: {exc} — running in text-only mode.[/yellow]")
            return

        # Prefer built-in Mac mic — always available and doesn't depend on
        # whether AirPods/headphones are in ears or nearby.
        # Fall back to system default only if no built-in mic found.
        preferred_device = _pick_input_device(devices)
        if preferred_device is not None:
            console.print(f"[dim]Using mic: {devices[preferred_device]['name']}[/dim]")

        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=BLOCK_SIZE,
                callback=self._callback,
                device=preferred_device,   # None = system default if no built-in found
            )
        except Exception as exc:
            console.print(f"[yellow]Mic unavailable or busy: {exc} — running in text-only mode.[/yellow]")
            return

        try:
            self._stream.start()
        except Exception as exc:
            console.print(f"[yellow]Could not start audio stream: {exc} — running in text-only mode.[/yellow]")
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
            return

        threading.Thread(target=self._stt_loop, daemon=True, name="stt").start()
        self._ready = True
        try:
            default_in = sd.query_devices(kind='input')
            console.print(f"[green]✓ Voice ready — always-on | mic: {default_in['name']}[/green]")
            console.print("[dim]  No wake word needed — JARVIS hears everything[/dim]")
        except Exception:
            console.print("[green]✓ Voice ready — always-on[/green]")

    @property
    def ready(self) -> bool:
        return self._ready

    # ── State control (call from any thread) ──────────────────────────────────

    def _reset(self) -> None:
        self._frames          = []
        self._speech_started  = False
        self._silence_count   = 0
        self._block_count     = 0
        self._loud_count      = 0
        self._onset_count     = 0

    def _is_speech(self, chunk: np.ndarray, rms: float) -> bool:
        """
        Returns True if this audio block contains human speech FROM the enrolled user.

        Pipeline:
          1. Energy floor — reject absolute silence instantly
          2. WebRTC VAD — ML model rejects noise/music/fan hum/phone bleed
          3. Voice profile check — compares speaker embedding to Dylan's voiceprint
             (only runs when a profile is enrolled; skips if none saved)
        """
        if rms < VAD_RMS_FLOOR:
            return False

        # Step 1: WebRTC VAD — is this speech at all?
        # Frame must be EXACTLY WEBRTC_VAD_FRAME_SAMPLES (480) samples and
        # contiguous int16 PCM. If the audio block is too short (mic warm-up,
        # stream restart, partial final block), fall back to RMS rather than
        # silently letting WebRTC throw a misleading exception.
        is_speech = False
        if self._webrtc_vad is not None and len(chunk) >= WEBRTC_VAD_FRAME_SAMPLES:
            try:
                frame_int16 = np.ascontiguousarray(
                    chunk[:WEBRTC_VAD_FRAME_SAMPLES], dtype=np.int16
                )
                is_speech = self._webrtc_vad.is_speech(
                    frame_int16.tobytes(), SAMPLE_RATE
                )
            except Exception as exc:
                # Genuine errors (not size-related) should be visible — they
                # almost always mean the VAD library is in a bad state. We
                # still fall back to RMS so the audio engine keeps working.
                if not isinstance(exc, ValueError):
                    print(f"[vad] unexpected error — {type(exc).__name__}: {exc}",
                          flush=True)
                is_speech = rms > VAD_RMS
        else:
            is_speech = rms > VAD_RMS

        if not is_speech:
            return False

        # Step 2: Voice profile check — is this speech from Dylan?
        # Runs only if a profile is enrolled. Adds ~20ms but only reaches here
        # after WebRTC already confirmed speech.
        # SKIPPED during music playback — when music + voice mix together in
        # the mic, the voice embedding shifts and the profile check often
        # fails on Dylan's real speech. The wake-word check at the server
        # layer still filters out non-command audio (lyrics get dropped there).
        try:
            from server import _music_is_playing, _system_audio_playing
            if _music_is_playing or _system_audio_playing:
                return True
        except Exception:
            pass
        try:
            from voice.voice_profile import is_owner
            return is_owner(chunk)
        except Exception:
            return True   # profile check failed → accept (don't drop real commands)

    def start_detecting(self) -> None:
        """Passive wake word scanning."""
        self._reset()
        self._state = self.DETECTING

    def start_recording(self) -> None:
        """Record the command that follows the wake word."""
        self._reset()
        self._state = self.RECORDING

    def start_conversing(self) -> None:
        """
        Conversation mode — no wake word needed.
        Any speech is treated as a command immediately.
        After CONVERSE_TIMEOUT_SECS of silence, reverts to DETECTING.
        """
        self._reset()
        self._state = self.CONVERSING

    def start_barge_in(self) -> None:
        """Watch mic energy only — used while JARVIS is speaking.
        First BARGE_MEASURE frames are used to sample the ambient/speaker-bleed
        level so the threshold auto-adjusts above it."""
        self._reset()
        self._barge_measure   = 0
        self._barge_rms_sum   = 0.0
        self._barge_threshold = BARGE_RMS_MIN
        self._state = self.BARGE_IN

    def start_study(self) -> None:
        """Study mode — continuous listen, transcripts go to study buffer, not JARVIS."""
        self._reset()
        self._state = self.STUDY

    def suspend(self) -> None:
        """Keep stream open but ignore all audio."""
        self._state = self.IDLE

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._state  = self.IDLE
        self._audio_q.put(None)          # poison pill for STT thread
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass

    # ── Audio callback (PortAudio thread — must not block) ────────────────────

    def _callback(self, indata, _frames, _time, _status) -> None:
        try:
            self._callback_inner(indata, _frames, _time, _status)
        except Exception as exc:
            # Audio callbacks must NEVER raise — log and continue
            console.print(f"[yellow]Audio callback error (suppressed): {exc}[/yellow]")

    def _callback_inner(self, indata, _frames, _time, _status) -> None:
        if self._closed:
            return

        chunk = indata.copy().flatten()
        rms   = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
        state = self._state          # single CPython read — no lock needed

        speech = self._is_speech(chunk, rms)   # WebRTC VAD (or RMS fallback)

        # ── DETECTING: accumulate speech, flush to STT on silence ────────────
        if state == self.DETECTING:
            self._block_count += 1
            self._frames.append(chunk)

            if speech:
                self._onset_count += 1
                if self._onset_count >= VAD_ONSET:
                    self._speech_started = True
                self._silence_count = 0
            else:
                self._onset_count = max(0, self._onset_count - 1)
                if self._speech_started:
                    self._silence_count += 1
                    if self._silence_count >= SILENCE_BLKS:
                        self._flush("wake")

            # Rolling pre-roll: keep last 0.4 s when silent (for context)
            if not self._speech_started and len(self._frames) > MAX_WAKE_BLKS:
                self._frames      = self._frames[-KEEP_BLKS:]
                self._block_count = KEEP_BLKS

            # Hard cap: very long continuous speech
            if self._speech_started and self._block_count > MAX_WAKE_BLKS:
                self._flush("wake")

        # ── RECORDING: capture command after wake word ───────────────────────
        elif state == self.RECORDING:
            self._block_count += 1
            self._frames.append(chunk)

            if speech:
                self._onset_count += 1
                if self._onset_count >= VAD_ONSET:
                    self._speech_started = True
                self._silence_count = 0
            else:
                self._onset_count = max(0, self._onset_count - 1)
                if self._speech_started:
                    self._silence_count += 1
                    if self._silence_count >= CMD_SILENCE_BLKS:
                        self._flush("command")
                        self._state = self.DETECTING

            # No speech heard — give up after CMD_TIMEOUT_SECS
            if not self._speech_started and self._block_count > CMD_TIMEOUT_BLKS:
                self._reset()
                self._state = self.DETECTING

            # Hard cap
            if self._block_count > MAX_CMD_BLKS:
                if self._speech_started:
                    self._flush("command")
                else:
                    self._reset()
                self._state = self.DETECTING

        # ── CONVERSING: always-on — respond to anything, never revert ───────────
        elif state == self.CONVERSING:
            self._block_count += 1
            self._frames.append(chunk)

            if speech:
                self._onset_count += 1
                if self._onset_count >= VAD_ONSET:
                    self._speech_started = True
                self._silence_count = 0
            else:
                self._onset_count = max(0, self._onset_count - 1)
                if self._speech_started:
                    self._silence_count += 1
                    if self._silence_count >= CMD_SILENCE_BLKS:
                        self._flush("command")
                        self._state = self.CONVERSING

            # Reset block count periodically (no-speech idle) — stay in CONVERSING
            if not self._speech_started and self._block_count > CONVERSE_TIMEOUT_BLKS:
                self._reset()
                self._state = self.CONVERSING

            # Hard cap
            if self._block_count > MAX_CMD_BLKS:
                if self._speech_started:
                    self._flush("command")
                self._reset()
                self._state = self.CONVERSING

        # ── STUDY: continuous listen — transcripts to study buffer ───────────
        elif state == self.STUDY:
            self._block_count += 1
            self._frames.append(chunk)
            if speech:
                self._speech_started = True
                self._silence_count = 0
            elif self._speech_started:
                self._silence_count += 1
                if self._silence_count >= CMD_SILENCE_BLKS:
                    self._flush("study")
                    # stay in STUDY
            if self._block_count > MAX_CMD_BLKS:
                if self._speech_started:
                    self._flush("study")
                else:
                    self._reset()
                self._state = self.STUDY

        # ── BARGE_IN: dynamic threshold ───────────────────────────────────────
        elif state == self.BARGE_IN:
            # Phase 1 — measure ambient/speaker-bleed level for BARGE_MEASURE frames
            if self._barge_measure < BARGE_MEASURE:
                self._barge_rms_sum += rms
                self._barge_measure += 1
                if self._barge_measure == BARGE_MEASURE:
                    avg = self._barge_rms_sum / BARGE_MEASURE
                    # Threshold = 2× the measured ambient (speaker bleed), clamped
                    self._barge_threshold = min(BARGE_RMS_MAX, max(BARGE_RMS_MIN, avg * BARGE_MULT))
                    console.print(f"[dim]barge-in threshold set: {self._barge_threshold:.0f} (ambient {avg:.0f})[/dim]")
                return  # don't fire during measurement window

            # Phase 2 — detect sustained voice above threshold.
            # Loud audio counts toward barge-in trigger ONLY if it sounds like
            # Dylan's voice (per voice_profile). This stops JARVIS's own audio
            # bleed from triggering self-interruption.
            if rms > self._barge_threshold:
                is_dylan = True
                try:
                    from voice.voice_profile import is_owner
                    is_dylan = is_owner(chunk)
                except Exception:
                    pass
                if is_dylan:
                    self._loud_count += 1
                    if self._loud_count >= BARGE_FRAMES:
                        self._loud_count = 0
                        self._state      = self.DETECTING
                        threading.Thread(
                            target=self._on_barge_in,
                            daemon=True,
                            name="barge-in-cb",
                        ).start()
            else:
                self._loud_count = max(0, self._loud_count - 1)

    def _flush(self, kind: str) -> None:
        """Send buffered audio to STT queue and reset buffers."""
        if self._frames and self._speech_started:
            audio = np.concatenate(self._frames)
            if len(audio) >= MIN_AUDIO:
                # Fire ack callback immediately — before Whisper even starts.
                # User hears acknowledgment sound ~instant, not after the 1-2s LLM delay.
                if self._on_speech_end and kind == "command":
                    threading.Thread(
                        target=self._on_speech_end, daemon=True, name="ack"
                    ).start()
                self._audio_q.put_nowait((kind, audio))
        self._reset()

    # ── STT thread ────────────────────────────────────────────────────────────

    def _load_whisper(self) -> None:
        """Load Whisper models. Two-tier: tiny for short audio (<2s), base for longer."""
        try:
            import whisper as _whisper
            from config import WHISPER_MODEL
            model_name = WHISPER_MODEL if WHISPER_MODEL in (
                "tiny", "base", "small", "medium", "large", "large-v3", "turbo"
            ) else "base"
            self._whisper      = _whisper.load_model(model_name)   # primary (base)
            # Load the tiny fast-path model in its OWN try — if only tiny fails,
            # we must NOT discard the base model we just loaded (that would drop
            # Whisper entirely and force Google STT). _transcribe already falls
            # back to base when _whisper_tiny is None.
            try:
                self._whisper_tiny = _whisper.load_model("tiny")   # fast path for short clips
                console.print(f"[green]✓ Whisper {model_name} + tiny loaded — two-tier STT active[/green]")
            except Exception as _texc:
                self._whisper_tiny = None
                console.print(f"[yellow]Whisper tiny unavailable ({type(_texc).__name__}) — "
                              f"using {model_name} for all clips[/yellow]")
        except Exception as exc:
            # Whisper failures fall into a few buckets; give the user something
            # actionable instead of just dumping the traceback.
            import shutil as _sh
            if not _sh.which("ffmpeg"):
                console.print(
                    "[yellow]⚠ ffmpeg not found on PATH — Whisper transcription will fail.[/yellow]\n"
                    "[yellow]  Install with `brew install ffmpeg`, then relaunch JARVIS.[/yellow]"
                )
                print("[setup] ffmpeg missing — install with `brew install ffmpeg`, then relaunch.")
            else:
                console.print(f"[dim]Whisper unavailable ({exc}) — using Google STT[/dim]")
                print(f"[setup] Whisper failed to load: {type(exc).__name__}: {exc}")
            self._whisper = None

    def _transcribe(self, audio_np: np.ndarray) -> str:
        """
        Two-tier Whisper transcription:
          - Short audio (≤ 2s): use tiny model (~150ms) — fast path for quick commands
          - Longer audio (> 2s): use base model (~400ms) — more accurate for sentences
        Falls back to Google STT if both Whisper models are unavailable.
        """
        from voice.latency import profiler
        profiler.mark("stt_start")

        # ── Whisper path ──────────────────────────────────────────────────────
        if getattr(self, "_whisper", None) is not None:
            try:
                import whisper as _whisper
                audio_f32 = audio_np.astype(np.float32) / 32768.0
                # Pick model based on clip length
                is_short  = len(audio_np) <= SAMPLE_RATE * 2   # ≤ 2 seconds
                model     = getattr(self, "_whisper_tiny", None) if is_short else None
                if model is None:
                    model = self._whisper
                # NO initial_prompt — even "Hey JARVIS, this is Dylan" caused
                # Whisper to hallucinate that exact phrase from silence/noise.
                # Wake-word fuzzy matcher catches transcription variants instead.
                result = model.transcribe(
                    audio_f32,
                    language="en",
                    fp16=False,
                    condition_on_previous_text=False,
                )
                # Drop transcriptions Whisper isn't confident were real speech.
                # LOOSENED to 0.75 — Dylan was having to yell at 0.60.
                if result.get("no_speech_prob", 0) > 0.75:
                    profiler.mark("stt_end")
                    return ""
                # Whisper YouTube-training-data ghost phrases. These are the
                # most common phantom outputs when fed silence/music/noise.
                # Match by SUBSTRING so "thanks for watching, please" also gets caught.
                _whisper_ghost_substrings = (
                    "thanks for watching", "thank you for watching",
                    "subscribe to", "like and subscribe", "please subscribe",
                    "see you next time", "see you in the next video",
                    "don't forget to subscribe", "smash that like button",
                    "see you in the next one",
                )
                _text_stripped = result.get("text", "").strip().lower()
                if any(g in _text_stripped for g in _whisper_ghost_substrings):
                    profiler.mark("stt_end")
                    return ""
                # Single-word phantoms (exact match only — don't drop legit "you")
                _whisper_ghost_exact = {
                    "you.", "you", "thank you.", "thank you",
                    "bye.", "bye", "goodbye.", "goodbye", "okay.", "ok.",
                    ".", "..", "...", "♪", "♪♪",
                }
                if _text_stripped in _whisper_ghost_exact:
                    profiler.mark("stt_end")
                    return ""
                # Drop only the WORST low-confidence guesses (was -1.0, too strict)
                segs = result.get("segments", [])
                if segs:
                    avg_logprob = sum(s.get("avg_logprob", 0) for s in segs) / len(segs)
                    if avg_logprob < -1.5:
                        profiler.mark("stt_end")
                        return ""
                text = result.get("text", "").strip()
                profiler.mark("stt_end")
                return text
            except Exception as exc:
                console.print(f"[dim]Whisper error: {exc} — falling back to Google[/dim]")

        # ── Google STT fallback ───────────────────────────────────────────────
        sr = self._sr
        if sr is None:
            # speech_recognition wasn't importable at startup and Whisper
            # also failed — nothing left to fall back to. Return empty
            # instead of crashing the loop with an AttributeError.
            profiler.mark("stt_end")
            return ""
        r  = sr.Recognizer()
        # Frame width (bytes/sample) must match the actual dtype — earlier
        # code hardcoded 2 (int16). If the audio buffer is ever float32 or
        # int32, that constant becomes a lie and Google interprets the
        # bytes at the wrong sample boundary, producing gibberish or
        # silent failures. Derive from the array itself.
        if audio_np.dtype != np.int16:
            audio_np = audio_np.astype(np.int16)
        frame_width = audio_np.dtype.itemsize       # 2 for int16, future-proof
        audio_data = sr.AudioData(audio_np.tobytes(), SAMPLE_RATE, frame_width)
        try:
            return r.recognize_google(audio_data, language="en-US").strip()
        finally:
            profiler.mark("stt_end")

    def _stt_loop(self) -> None:
        # Load Whisper before entering the loop
        self._load_whisper()

        # Resolve `UnknownValueError` once up-front rather than evaluating
        # `self._sr.UnknownValueError` on every except. If speech_recognition
        # failed to import, self._sr is None and Python would raise
        # AttributeError when parsing the except clause — crashing the STT
        # thread on the very first iteration. Use a sentinel class that
        # never gets raised in that case so the loop still works.
        class _NeverRaised(Exception):
            """Stand-in when speech_recognition is unavailable."""
        unknown_value_error = (
            getattr(self._sr, "UnknownValueError", _NeverRaised)
            if self._sr is not None else _NeverRaised
        )

        while True:
            item = self._audio_q.get()
            if item is None:
                break                       # poison pill → exit

            kind, audio_np = item
            try:
                text = self._transcribe(audio_np).lower()
            except unknown_value_error:
                continue
            except Exception as exc:
                console.print(f"[dim]STT error: {exc}[/dim]")
                continue

            if text:
                console.print(f"[dim]Heard [{kind}]: {text!r}[/dim]")

                if kind == "study":
                    try:
                        from tools.study_tool import get_session
                        session = get_session()
                        # Check for trigger phrases — route those to normal processing
                        triggers = [
                            "summarize", "what did they say", "what did he say",
                            "what did she say", "stop study", "stop studying",
                            "pause study", "hey jarvis", "jarvis",
                        ]
                        if any(t in text.lower() for t in triggers):
                            threading.Thread(
                                target=self._on_transcription,
                                args=("command", text),
                                daemon=True,
                                name="study-trigger",
                            ).start()
                        else:
                            session.add_chunk(text)
                    except Exception as exc:
                        console.print(f"[dim]Study buffer error: {exc}[/dim]")
                    continue  # don't fall through to normal on_transcription

                threading.Thread(
                    target=self._on_transcription,
                    args=(kind, text),
                    daemon=True,
                    name="transcription-cb",
                ).start()


# ── Wake word helper ──────────────────────────────────────────────────────────

def check_wake_word(text: str):
    """
    Returns (triggered: bool, remainder: str).
    remainder is whatever came after the wake word — may be the command itself.
    """
    t = text.lower().strip()
    for wake in sorted(WAKE_WORDS, key=len, reverse=True):
        if t.startswith(wake):
            return True, t[len(wake):].strip()
        if wake in t:
            idx = t.index(wake)
            return True, t[idx + len(wake):].strip()
    return False, ""
