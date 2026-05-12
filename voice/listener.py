"""
Voice listener: records via sounddevice, transcribes via Google free STT.
Wake words: "jarvis", "hey jarvis", "ok jarvis", "yo jarvis"
"""

import subprocess
import threading
from typing import Optional, Tuple

import numpy as np
import sounddevice as sd
from rich.console import Console

console = Console()

SAMPLE_RATE   = 16000
BLOCK_SIZE    = 1024
SILENCE_RMS   = 250          # int16 RMS below this = silence (lowered for better sensitivity)
SILENCE_SECS  = 1.0          # seconds of silence to end phrase
MAX_SECS      = 10           # hard cap per listen

WAKE_WORDS = ["jarvis", "hey jarvis", "ok jarvis", "yo jarvis", "okay jarvis"]


class VoiceListener:
    def __init__(self) -> None:
        self._ready        = False
        self._sr           = None
        self._abort_event  = threading.Event()   # abort current recording
        threading.Thread(target=self._init, daemon=True).start()

    # ------------------------------------------------------------------ #
    # Init                                                                 #
    # ------------------------------------------------------------------ #

    def _init(self) -> None:
        try:
            import speech_recognition as sr
            self._sr = sr
            self._ready = True
            console.print("[dim]Voice ready.[/dim]")
        except ImportError:
            console.print("[yellow]SpeechRecognition not installed — run fix_and_run.sh[/yellow]")

    @property
    def ready(self) -> bool:
        return self._ready

    # ------------------------------------------------------------------ #
    # Recording                                                            #
    # ------------------------------------------------------------------ #

    def abort_recording(self) -> None:
        """Abort a currently-running listen_once (for barge-in)."""
        self._abort_event.set()

    def _record(self, max_seconds: int = MAX_SECS) -> Optional[np.ndarray]:
        """Record until silence detected. Returns int16 array or None."""
        self._abort_event.clear()
        frames      = []
        silence_blocks = 0
        speech_started = False
        silence_limit  = int(SILENCE_SECS * SAMPLE_RATE / BLOCK_SIZE)
        max_blocks     = int(max_seconds * SAMPLE_RATE / BLOCK_SIZE)
        done = threading.Event()

        def _cb(indata, _f, _t, _s):
            nonlocal silence_blocks, speech_started
            chunk = indata.copy().flatten()
            frames.append(chunk)
            rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))

            if rms > SILENCE_RMS:
                speech_started = True
                silence_blocks = 0
            elif speech_started:
                silence_blocks += 1
                if silence_blocks >= silence_limit:
                    done.set()

            if len(frames) >= max_blocks or self._abort_event.is_set():
                done.set()

        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1,
                dtype="int16", blocksize=BLOCK_SIZE, callback=_cb,
            ):
                done.wait(timeout=max_seconds + 2)
        except Exception as e:
            console.print(f"[yellow]Mic error: {e}[/yellow]")
            return None

        if self._abort_event.is_set():
            return None
        if not speech_started or len(frames) < 3:
            return None
        return np.concatenate(frames)

    # ------------------------------------------------------------------ #
    # Transcription                                                        #
    # ------------------------------------------------------------------ #

    def _transcribe(self, audio_int16: np.ndarray) -> Optional[str]:
        sr = self._sr
        r  = sr.Recognizer()
        audio_data = sr.AudioData(audio_int16.tobytes(), SAMPLE_RATE, 2)
        try:
            return r.recognize_google(audio_data, language="en-US").strip().lower()
        except sr.UnknownValueError:
            return None
        except Exception as e:
            console.print(f"[dim]STT: {e}[/dim]")
            return None

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def listen_once(self, max_seconds: int = MAX_SECS) -> Optional[str]:
        if not self._ready:
            return None
        audio = self._record(max_seconds=max_seconds)
        if audio is None:
            return None
        return self._transcribe(audio)

    def check_wake_word(self, text: str) -> Tuple[bool, str]:
        t = text.lower().strip()
        for wake in sorted(WAKE_WORDS, key=len, reverse=True):
            if t.startswith(wake):
                return True, t[len(wake):].strip()
            if wake in t:
                idx = t.index(wake)
                return True, t[idx + len(wake):].strip()
        return False, ""

    # ------------------------------------------------------------------ #
    # Energy-based barge-in monitor                                        #
    # ------------------------------------------------------------------ #

    def watch_for_speech(
        self,
        on_detect,
        threshold: int = 1000,
        hold_frames: int = 5,
        stop_flag: threading.Event = None,
    ) -> None:
        """
        Stream mic energy. When sustained loud speech detected, call on_detect().
        Runs until stop_flag is set. Use this for barge-in while JARVIS speaks.
        threshold: int16 RMS level that counts as speech (default 1000)
        hold_frames: how many consecutive loud frames needed before firing
        """
        loud_count = 0
        fired      = threading.Event()

        def _cb(indata, _f, _t, _s):
            nonlocal loud_count
            if fired.is_set():
                return
            rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
            if rms > threshold:
                loud_count += 1
                if loud_count >= hold_frames:
                    fired.set()
                    on_detect()
            else:
                loud_count = max(0, loud_count - 1)

        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1,
                dtype="int16", blocksize=BLOCK_SIZE, callback=_cb,
            ):
                while (stop_flag is None or not stop_flag.is_set()) and not fired.is_set():
                    import time; time.sleep(0.05)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Sounds                                                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def activation_sound() -> None:
        subprocess.Popen(
            ["afplay", "/System/Library/Sounds/Tink.aiff"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    @staticmethod
    def deactivation_sound() -> None:
        subprocess.Popen(
            ["afplay", "/System/Library/Sounds/Pop.aiff"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
