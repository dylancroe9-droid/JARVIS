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

VAD_RMS      = 380            # int16 RMS: above this = speech (raised to reduce ambient pickup)
SILENCE_SECS = 0.5            # wake detection: flush after 0.5s silence
SILENCE_BLKS = int(SILENCE_SECS * SAMPLE_RATE / BLOCK_SIZE)

CMD_SILENCE_SECS = 1.3        # command/conversation: wait 1.3s before flushing — long enough for mid-sentence pauses
CMD_SILENCE_BLKS = int(CMD_SILENCE_SECS * SAMPLE_RATE / BLOCK_SIZE)

MAX_WAKE_SECS   = 7
MAX_WAKE_BLKS   = int(MAX_WAKE_SECS * SAMPLE_RATE / BLOCK_SIZE)
KEEP_BLKS       = int(0.4 * SAMPLE_RATE / BLOCK_SIZE)   # 0.4 s pre-roll

MAX_CMD_SECS    = 12
MAX_CMD_BLKS    = int(MAX_CMD_SECS * SAMPLE_RATE / BLOCK_SIZE)
CMD_TIMEOUT_SECS = 7          # give up waiting for command after this
CMD_TIMEOUT_BLKS = int(CMD_TIMEOUT_SECS * SAMPLE_RATE / BLOCK_SIZE)

CONVERSE_TIMEOUT_SECS = 2     # seconds of silence before reverting to wake-word mode
CONVERSE_TIMEOUT_BLKS = int(CONVERSE_TIMEOUT_SECS * SAMPLE_RATE / BLOCK_SIZE)

BARGE_RMS    = 1000           # int16 RMS for barge-in — high enough that JARVIS's own TTS doesn't self-trigger
BARGE_FRAMES = 6              # consecutive loud blocks required (~192ms of sustained speech)

MIN_AUDIO    = int(0.35 * SAMPLE_RATE)   # skip STT if audio < 350 ms

def _load_wake_words() -> list[str]:
    """Comma-separated env override → fall back to defaults."""
    import os as _os
    raw = _os.getenv("JARVIS_WAKE_WORDS", "").strip()
    if raw:
        words = [w.strip().lower() for w in raw.split(",") if w.strip()]
        if words:
            return words
    return ["jarvis", "hey jarvis", "ok jarvis", "yo jarvis", "okay jarvis"]


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
    ) -> None:
        """
        on_transcription(kind, text):
            kind = "wake"    — full utterance that triggered wake word
                               caller should call check_wake_word(text)
            kind = "command" — the command text to execute

        on_barge_in():
            called when sustained loud speech is detected in BARGE_IN state
        """
        self._on_transcription = on_transcription
        self._on_barge_in      = on_barge_in

        self._state: str      = self.IDLE
        self._frames: list    = []
        self._speech_started  = False
        self._silence_count   = 0
        self._block_count     = 0
        self._loud_count      = 0

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
            console.print(f"[dim]  No wake word needed — JARVIS hears everything[/dim]")
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
        """Watch mic energy only — used while JARVIS is speaking."""
        self._reset()
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

        # ── DETECTING: accumulate speech, flush to STT on silence ────────────
        if state == self.DETECTING:
            self._block_count += 1
            self._frames.append(chunk)

            if rms > VAD_RMS:
                self._speech_started = True
                self._silence_count  = 0
            elif self._speech_started:
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

            if rms > VAD_RMS:
                self._speech_started = True
                self._silence_count  = 0
            elif self._speech_started:
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

            if rms > VAD_RMS:
                self._speech_started = True
                self._silence_count  = 0
            elif self._speech_started:
                self._silence_count += 1
                if self._silence_count >= CMD_SILENCE_BLKS:
                    self._flush("command")
                    self._state = self.CONVERSING   # always stay conversational

            # Reset block count periodically (no-speech idle) — but STAY in CONVERSING
            # Never revert to DETECTING: wake word is permanently disabled.
            if not self._speech_started and self._block_count > CONVERSE_TIMEOUT_BLKS:
                self._reset()
                self._state = self.CONVERSING   # stay here — always-on mode

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
            if rms > VAD_RMS:
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

        # ── BARGE_IN: energy threshold only ──────────────────────────────────
        elif state == self.BARGE_IN:
            if rms > BARGE_RMS:
                self._loud_count += 1
                if self._loud_count >= BARGE_FRAMES:
                    self._loud_count = 0
                    self._state      = self.DETECTING
                    # Fire callback in its own thread — never block the callback
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
                self._audio_q.put_nowait((kind, audio))
        self._reset()

    # ── STT thread ────────────────────────────────────────────────────────────

    def _load_whisper(self) -> None:
        """Load Whisper model in background so first transcription is fast."""
        try:
            import whisper as _whisper
            from config import WHISPER_MODEL
            model_name = WHISPER_MODEL if WHISPER_MODEL in ("tiny", "base", "small") else "base"
            self._whisper = _whisper.load_model(model_name)
            console.print(f"[green]✓ Whisper {model_name} loaded — high-accuracy STT active[/green]")
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
        Transcribe audio using Whisper (preferred) or Google STT (fallback).
        Whisper is dramatically more accurate for natural speech, names,
        subject-specific terms (AP Gov, AP Chemistry, etc.).
        """
        from voice.latency import profiler
        profiler.mark("stt_start")
        # ── Whisper path ──────────────────────────────────────────────────────
        if getattr(self, "_whisper", None) is not None:
            try:
                import whisper as _whisper
                # Whisper needs float32 normalised to [-1, 1] at 16kHz
                audio_f32 = audio_np.astype(np.float32) / 32768.0
                result = self._whisper.transcribe(
                    audio_f32,
                    language="en",
                    fp16=False,
                    condition_on_previous_text=False,
                )
                # Skip if Whisper is highly confident this isn't speech
                # (ambient noise, music, AC, etc.) — critical for always-on mode
                if result.get("no_speech_prob", 0) > 0.72:
                    profiler.mark("stt_end")
                    return ""
                text = result.get("text", "").strip()
                profiler.mark("stt_end")
                return text
            except Exception as exc:
                console.print(f"[dim]Whisper error: {exc} — falling back to Google[/dim]")

        # ── Google STT fallback ───────────────────────────────────────────────
        sr = self._sr
        r  = sr.Recognizer()
        audio_data = sr.AudioData(audio_np.tobytes(), SAMPLE_RATE, 2)
        try:
            return r.recognize_google(audio_data, language="en-US").strip()
        finally:
            profiler.mark("stt_end")

    def _stt_loop(self) -> None:
        # Load Whisper before entering the loop
        self._load_whisper()

        while True:
            item = self._audio_q.get()
            if item is None:
                break                       # poison pill → exit

            kind, audio_np = item
            try:
                text = self._transcribe(audio_np).lower()
            except self._sr.UnknownValueError:
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
