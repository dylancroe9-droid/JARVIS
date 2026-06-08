"""
Voice speaker — sentence-by-sentence streaming TTS.

Engine priority (first available wins):
  1. ElevenLabs  — most human-sounding, needs ELEVENLABS_API_KEY in .env
  2. edge-tts    — free Microsoft Neural voices, very good quality
  3. macOS say   — offline fallback, robotic but always works

Pipelining: sentence N+1 is synthesized while sentence N plays → zero gap.
Persistent event loop: single async loop reused across all synthesis calls
  so the WebSocket connection to Microsoft's TTS is kept alive between
  sentences instead of being torn down and rebuilt each time (~1-4s savings).

Stop / barge-in:
  call stop() from any thread to kill current speech immediately.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import queue
import re
import subprocess
import threading
from typing import Generator, Optional

from rich.console import Console

console = Console()

# Sentence-boundary split — only hard sentence endings.
# Em-dashes intentionally NOT split — they create natural in-sentence rhythm
# and splitting on them produces dozens of tiny chunks with audible gaps.
_SENTENCE_END = re.compile(
    r'(?<!\bMr)(?<!\bMs)(?<!\bMrs)(?<!\bDr)(?<!\bSt)(?<!\bvs)'
    r'(?<=[.!?])\s+'    # sentence-ending punctuation only
)

# Minimum chunk size before sending to TTS.
# Short sentences get bundled together to reduce TTS calls → fewer gaps.
_MIN_CHUNK = 90

_tmpfile_seq = itertools.count()


def _mktmp(ext: str = "mp3") -> str:
    return f"/tmp/jarvis_{os.getpid()}_{next(_tmpfile_seq)}.{ext}"


# ── Engine detection ──────────────────────────────────────────────────────────

def _find_eleven_client():
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))
        key = os.getenv("ELEVENLABS_API_KEY", "").strip()
        if not key:
            return None
        from elevenlabs.client import ElevenLabs
        return ElevenLabs(api_key=key)
    except Exception:
        return None


def _edge_tts_available() -> bool:
    try:
        import edge_tts  # noqa
        return True
    except ImportError:
        return False


def _find_say_voice() -> str:
    try:
        out = subprocess.check_output(["say", "-v", "?"], text=True, stderr=subprocess.DEVNULL)
        for candidate in ["Daniel (Premium)", "Daniel (Enhanced)", "Daniel"]:
            if candidate.lower() in out.lower():
                return candidate
    except Exception:
        pass
    return "Daniel"


# ── Engine selection ──────────────────────────────────────────────────────────

_eleven_client = _find_eleven_client()

_ELEVEN_VOICE      = os.getenv("ELEVEN_VOICE", "onwK4e9ZLuTAKqWW03F9")
_ELEVEN_MODEL      = "eleven_turbo_v2_5"   # turbo = fastest model, lowest latency
_ELEVEN_STABILITY  = 0.45
_ELEVEN_SIMILARITY = 0.80
_ELEVEN_STYLE      = 0.20                  # lower style = more consistent, less robotic
_ELEVEN_SAMPLERATE = 16_000                # pcm_16000 — lowest latency format
_ELEVEN_LATENCY    = 4                     # optimize_streaming_latency 0-4, 4=fastest

_USE_EDGE   = not _eleven_client and _edge_tts_available()
_EDGE_VOICE = "en-GB-RyanNeural"
_EDGE_RATE  = "+14%"   # slightly faster → shorter sentences → tighter gaps
_EDGE_PITCH = "-8Hz"   # slight depth — natural, not over-processed

# Whether to use ffmpeg to strip leading silence from each TTS audio chunk.
# Cuts the ~80–150ms dead air at the start of every edge-tts mp3.
_HAS_FFMPEG = bool(subprocess.run(
    ["which", "ffmpeg"], capture_output=True
).returncode == 0)

_SAY_VOICE = _find_say_voice()
_SAY_RATE  = 155

if _eleven_client:
    console.print("[dim]TTS: ElevenLabs (Daniel)[/dim]")
elif _USE_EDGE:
    console.print(f"[dim]TTS: edge-tts ({_EDGE_VOICE})[/dim]")
else:
    console.print(f"[dim]TTS: macOS say ({_SAY_VOICE})[/dim]")


# ── Speaker class ─────────────────────────────────────────────────────────────

class Speaker:

    def __init__(self) -> None:
        self._stop_flag = threading.Event()
        self._lock      = threading.Lock()
        self._play_proc: Optional[subprocess.Popen] = None

        # ── Persistent async event loop for edge-tts ──────────────────────────
        # Reusing one event loop keeps the WebSocket connection to Microsoft's
        # TTS server alive between sentences. Without this, every sentence pays
        # TCP + TLS + WebSocket handshake overhead (~1-4 s per sentence).
        self._synth_loop: Optional[asyncio.AbstractEventLoop] = None
        if _USE_EDGE:
            self._synth_loop = asyncio.new_event_loop()
            t = threading.Thread(
                target=self._synth_loop.run_forever,
                daemon=True,
                name="tts-event-loop",
            )
            t.start()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def stop(self) -> None:
        self._stop_flag.set()
        with self._lock:
            if self._play_proc and self._play_proc.poll() is None:
                try:
                    self._play_proc.terminate()
                except Exception:
                    pass

    def resume(self) -> None:
        self._stop_flag.clear()

    # ── Synthesis (network call, returns tmpfile path) ─────────────────────────

    def _synthesize_edge(self, text: str) -> Optional[str]:
        """Synthesize text with edge-tts → mp3 tmpfile. Returns path or None."""
        import edge_tts
        if self._stop_flag.is_set():
            return None
        path = _mktmp()
        try:
            async def _gen() -> None:
                c = edge_tts.Communicate(text, voice=_EDGE_VOICE,
                                         rate=_EDGE_RATE, pitch=_EDGE_PITCH)
                await c.save(path)

            if self._synth_loop and self._synth_loop.is_running():
                future = asyncio.run_coroutine_threadsafe(_gen(), self._synth_loop)
                future.result(timeout=20)
            else:
                asyncio.run(_gen())

            if self._stop_flag.is_set():
                try: os.unlink(path)
                except Exception: pass
                return None

            # ── Strip leading silence from the mp3 (edge-tts adds ~100ms dead air) ──
            # Fast fixed-seek: skip the first 85ms with -ss before -i and stream-copy
            # the rest. This is ~10x faster than the silenceremove adaptive filter
            # because it doesn't analyse the entire audio — just jumps to a frame boundary.
            if _HAS_FFMPEG:
                stripped = _mktmp()
                try:
                    result = subprocess.run(
                        [
                            "ffmpeg", "-y", "-loglevel", "error",
                            "-ss", "0.085",   # skip first 85ms (leading silence)
                            "-i", path,
                            "-c", "copy",     # stream copy — no re-encode, near-instant
                            stripped,
                        ],
                        capture_output=True, timeout=5
                    )
                    if result.returncode == 0 and os.path.getsize(stripped) > 512:
                        os.unlink(path)
                        return stripped
                    else:
                        try: os.unlink(stripped)
                        except Exception: pass
                except Exception:
                    try: os.unlink(stripped)
                    except Exception: pass

            return path
        except Exception as exc:
            console.print(f"[dim]edge-tts error: {exc}[/dim]")
            try: os.unlink(path)
            except Exception: pass
            return None

    def _synthesize_eleven(self, text: str) -> Optional[str]:
        """Synthesize via ElevenLabs → mp3 tmpfile. Returns path or None."""
        if self._stop_flag.is_set() or not _eleven_client:
            return None
        path = _mktmp()
        try:
            from elevenlabs import VoiceSettings
            audio_gen = _eleven_client.text_to_speech.convert(
                voice_id=_ELEVEN_VOICE,
                text=text,
                model_id=_ELEVEN_MODEL,
                output_format="mp3_44100_128",
                voice_settings=VoiceSettings(
                    stability=_ELEVEN_STABILITY,
                    similarity_boost=_ELEVEN_SIMILARITY,
                    style=_ELEVEN_STYLE,
                    use_speaker_boost=True,
                ),
            )
            with open(path, "wb") as f:
                for chunk in audio_gen:
                    if chunk:
                        f.write(chunk)
            return path if not self._stop_flag.is_set() else None
        except Exception as exc:
            console.print(f"[dim]ElevenLabs error: {exc}[/dim]")
            try:
                os.unlink(path)
            except Exception:
                pass
            return None

    # ── Playback ──────────────────────────────────────────────────────────────

    def _play_file(self, path: str) -> None:
        """Play audio file with afplay, then delete it."""
        if self._stop_flag.is_set():
            try:
                os.unlink(path)
            except Exception:
                pass
            return
        try:
            with self._lock:
                if not self._stop_flag.is_set():
                    self._play_proc = subprocess.Popen(
                        ["afplay", path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
            if self._play_proc:
                self._play_proc.wait()
            with self._lock:
                self._play_proc = None
        except Exception:
            pass
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass

    def _speak_say(self, text: str) -> None:
        if self._stop_flag.is_set():
            return
        enhanced = (
            text
            .replace(", ",  " [[slnc 90]] ")
            .replace(" — ", " [[slnc 130]] ")
            .replace(" - ",  " [[slnc 100]] ")
        )
        try:
            with self._lock:
                self._play_proc = subprocess.Popen(
                    ["say", "-v", _SAY_VOICE, "-r", str(_SAY_RATE), enhanced],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            self._play_proc.wait()
            with self._lock:
                self._play_proc = None
        except Exception as exc:
            console.print(f"[yellow]macOS say failed: {exc}[/yellow]")
            with self._lock:
                self._play_proc = None

    # ── Public speak (single piece, blocking) ─────────────────────────────────

    def speak(self, text: str) -> None:
        text = text.strip()
        if not text or self._stop_flag.is_set():
            return
        if _eleven_client:
            path = self._synthesize_eleven(text)
            if path:
                self._play_file(path)
                return
        elif _USE_EDGE:
            path = self._synthesize_edge(text)
            if path:
                self._play_file(path)
                return
        self._speak_say(text)

    # ── ElevenLabs PCM streaming ──────────────────────────────────────────────

    def _stream_speak_eleven(self, text_gen: Generator[str, None, None]) -> str:
        """
        True streaming TTS via ElevenLabs PCM output + sounddevice playback.

        Pipeline:
          Producer  → sentence_q  → Synthesizer (ElevenLabs PCM stream) → pcm_q → Player
          (tokens)                   starts playing as bytes arrive                (sounddevice)

        Key wins over edge-tts:
          • pcm_16000 format — raw bytes, no container, no file I/O
          • optimize_streaming_latency=4 — ElevenLabs prioritises first-byte speed
          • sounddevice OutputStream — plays chunks as they land, no afplay startup lag
          • Human voice quality instead of Microsoft TTS
        """
        import numpy as np
        import sounddevice as _sd
        from voice.latency import profiler
        from elevenlabs import VoiceSettings

        collected: list[str] = []
        sentence_q: queue.Queue = queue.Queue()
        pcm_q: queue.Queue      = queue.Queue(maxsize=4)

        # ── Producer: tokenise LLM stream → sentence chunks ──────────────────
        def producer() -> None:
            buf = ""; pending = ""
            for chunk in text_gen:
                if self._stop_flag.is_set(): break
                buf += chunk
                collected.append(chunk)
                while True:
                    m = _SENTENCE_END.search(buf)
                    if not m: break
                    sentence = buf[:m.start()+1].strip()
                    buf = buf[m.end():]
                    if not sentence: continue
                    candidate = (pending + " " + sentence).strip() if pending else sentence
                    if len(candidate) >= _MIN_CHUNK:
                        sentence_q.put(candidate); pending = ""
                    else:
                        pending = candidate
            tail = (pending + " " + buf).strip() if pending else buf.strip()
            if tail and not self._stop_flag.is_set():
                sentence_q.put(tail)
            sentence_q.put(None)

        # ── Synthesizer: sentence → ElevenLabs PCM bytes ─────────────────────
        _first_synth = [True]

        def synthesizer() -> None:
            while True:
                try:
                    sentence = sentence_q.get(timeout=0.5)
                except queue.Empty:
                    if self._stop_flag.is_set(): pcm_q.put(None); return
                    continue
                if sentence is None or self._stop_flag.is_set():
                    pcm_q.put(None); return
                if _first_synth[0]:
                    profiler.mark("tts_first_synth_start")
                try:
                    pcm_bytes = b""
                    for chunk in _eleven_client.text_to_speech.stream(
                        voice_id   = _ELEVEN_VOICE,
                        text       = sentence,
                        model_id   = _ELEVEN_MODEL,
                        output_format = f"pcm_{_ELEVEN_SAMPLERATE}",
                        optimize_streaming_latency = _ELEVEN_LATENCY,
                        voice_settings = VoiceSettings(
                            stability        = _ELEVEN_STABILITY,
                            similarity_boost = _ELEVEN_SIMILARITY,
                            style            = _ELEVEN_STYLE,
                            use_speaker_boost= True,
                        ),
                    ):
                        if self._stop_flag.is_set(): break
                        if chunk: pcm_bytes += chunk
                    if pcm_bytes and not self._stop_flag.is_set():
                        pcm_q.put(pcm_bytes)
                except Exception as exc:
                    console.print(f"[dim]ElevenLabs PCM error: {exc}[/dim]")
                    pcm_q.put(None); return
                if _first_synth[0]:
                    profiler.mark("tts_first_synth_end")
                    _first_synth[0] = False

        threading.Thread(target=producer,    daemon=True, name="tts-producer").start()
        threading.Thread(target=synthesizer, daemon=True, name="tts-synth").start()

        # ── Player: PCM bytes → sounddevice output ────────────────────────────
        _first_play = [True]
        while True:
            try:
                item = pcm_q.get(timeout=0.15)
            except queue.Empty:
                if self._stop_flag.is_set(): break
                continue
            if item is None or self._stop_flag.is_set(): break
            if _first_play[0]:
                profiler.mark("tts_first_play")
                _first_play[0] = False
            try:
                arr = np.frombuffer(item, dtype=np.int16)
                with self._lock:
                    if not self._stop_flag.is_set():
                        _sd.play(arr, samplerate=_ELEVEN_SAMPLERATE, blocking=False)
                # Poll until done or interrupted
                import time as _time
                while _sd.get_stream().active:
                    if self._stop_flag.is_set():
                        _sd.stop(); break
                    _time.sleep(0.015)
            except Exception as exc:
                console.print(f"[dim]ElevenLabs playback error: {exc}[/dim]")

        return "".join(collected)

    # ── Streaming (pipelined) — edge-tts / say fallback ───────────────────────

    def stream_speak(self, text_gen: Generator[str, None, None]) -> str:
        """
        Route to ElevenLabs PCM streaming if available, else edge-tts sentence pipeline.
        ElevenLabs path: human voice, PCM streaming, no files, starts playing faster.
        edge-tts path: free, good quality fallback.
        """
        self.resume()

        if _eleven_client:
            return self._stream_speak_eleven(text_gen)

        # ── edge-tts / say fallback ───────────────────────────────────────────
        sentence_q: queue.Queue = queue.Queue()
        audio_q: queue.Queue    = queue.Queue(maxsize=5)
        collected: list[str]    = []

        def producer() -> None:
            buf = ""; pending = ""
            for chunk in text_gen:
                if self._stop_flag.is_set(): break
                buf += chunk
                collected.append(chunk)
                while True:
                    m = _SENTENCE_END.search(buf)
                    if not m: break
                    sentence = buf[:m.start()+1].strip()
                    buf = buf[m.end():]
                    if not sentence: continue
                    candidate = (pending + " " + sentence).strip() if pending else sentence
                    if len(candidate) >= _MIN_CHUNK:
                        sentence_q.put(candidate); pending = ""
                    else:
                        pending = candidate
            tail = (pending + " " + buf).strip() if pending else buf.strip()
            if tail and not self._stop_flag.is_set():
                sentence_q.put(tail)
            sentence_q.put(None)

        from voice.latency import profiler
        _first_synth = [True]

        def synthesizer() -> None:
            while True:
                try:
                    sentence = sentence_q.get(timeout=0.5)
                except queue.Empty:
                    if self._stop_flag.is_set(): audio_q.put(None); return
                    continue
                if sentence is None or self._stop_flag.is_set():
                    audio_q.put(None); return
                if _first_synth[0]: profiler.mark("tts_first_synth_start")
                if _USE_EDGE:
                    path = self._synthesize_edge(sentence)
                    audio_q.put(("file", path) if path else ("say", sentence))
                else:
                    audio_q.put(("say", sentence))
                if _first_synth[0]:
                    profiler.mark("tts_first_synth_end")
                    _first_synth[0] = False

        threading.Thread(target=producer,    daemon=True, name="tts-producer").start()
        threading.Thread(target=synthesizer, daemon=True, name="tts-synth").start()

        _first_play = [True]
        while True:
            try:
                item = audio_q.get(timeout=0.1)
            except queue.Empty:
                if self._stop_flag.is_set(): break
                continue
            if item is None or self._stop_flag.is_set(): break
            if _first_play[0]:
                profiler.mark("tts_first_play")
                _first_play[0] = False
            kind, data = item
            if kind == "file": self._play_file(data)
            else:              self._speak_say(data)

        return "".join(collected)
