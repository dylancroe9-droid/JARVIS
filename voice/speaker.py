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
# Each TTS call = one afplay invocation with ~100-150ms startup. Bigger chunks =
# fewer audible pauses between sentences.
_MIN_CHUNK = 200

# Abbreviations that ElevenLabs treats as sentence ends because of the period —
# convert them so TTS doesn't pause mid-phrase.
# "Mr. Roe" specifically → "sir" — sounds more natural in spoken delivery
# than "Mister Roe" (which still has a tiny pause between words). Matches
# Paul Bettany's movie-JARVIS register.
_TTS_PREPROCESS = [
    (re.compile(r"\bMr\.?\s+Roe\b", re.IGNORECASE), "sir"),  # specific case first
    (re.compile(r"\bMr\.\s+"),   "Mister "),
    (re.compile(r"\bMrs\.\s+"),  "Missus "),
    (re.compile(r"\bMs\.\s+"),   "Miss "),
    (re.compile(r"\bDr\.\s+"),   "Doctor "),
    (re.compile(r"\bSt\.\s+"),   "Saint "),
    (re.compile(r"\bvs\.\s+"),   "versus "),
    (re.compile(r"\bProf\.\s+"), "Professor "),
    (re.compile(r"\be\.g\.,?\s+"), "for example, "),
    (re.compile(r"\bi\.e\.,?\s+"), "that is, "),
]

def _preprocess_for_tts(text: str) -> str:
    """Expand honorifics and abbreviations so TTS doesn't add awkward pauses."""
    for pat, repl in _TTS_PREPROCESS:
        text = pat.sub(repl, text)
    return text

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

_ELEVEN_VOICE      = os.getenv("ELEVEN_VOICE", "JBFqnCBsd6RMkjVDRZzb")   # default: George (British, warm captivating storyteller — closest to movie JARVIS)
_ELEVEN_MODEL      = "eleven_turbo_v2_5"   # turbo = fastest model, lowest latency
# Tuned for calm, even delivery (was "aggressive" — too much punch/emphasis):
# - stability ↑↑ to 0.78 = much more uniform pitch, no sharp emphasis
# - similarity ↓ to 0.70 = looser to training reference = softer delivery
# - style ↓ to 0.00      = zero performative inflection, flat-calm
_ELEVEN_STABILITY  = 0.78
_ELEVEN_SIMILARITY = 0.70
_ELEVEN_STYLE      = 0.00
_ELEVEN_SAMPLERATE = 22_050                # pcm_22050 — better quality than 16k, still low-latency
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
    console.print(f"[dim]TTS: ElevenLabs (voice={_ELEVEN_VOICE[:8]}…)[/dim]")
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
        text = _preprocess_for_tts(text)
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
                self._unlink(path)
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
                        self._unlink(path)
                        return stripped
                    # ffmpeg "succeeded" but produced garbage (e.g. truncated
                    # input) — drop the stripped file, keep the original.
                    self._unlink(stripped)
                except Exception:
                    self._unlink(stripped)

            # One last stop-flag check — if the user interrupted mid-ffmpeg
            # we don't want to return the path and have the caller queue it.
            if self._stop_flag.is_set():
                self._unlink(path)
                return None
            return path
        except Exception as exc:
            console.print(f"[dim]edge-tts error: {exc}[/dim]")
            self._unlink(path)
            return None

    def _synthesize_eleven(self, text: str) -> Optional[str]:
        """
        Synthesize via ElevenLabs → mp3 tmpfile. Returns path on success or
        None if the synthesis failed, was interrupted, or returned empty
        audio. Caller can fall back to another TTS path on None.

        Why the size check: ElevenLabs occasionally returns an OK status
        with zero bytes (or only a few bytes of header) under quota / rate
        limit / model glitch. Returning the path of an effectively-empty
        file makes the caller queue silence; the downstream player would
        then silently bail. Validating here surfaces the failure cleanly
        and ensures every tmpfile we create is either valid OR deleted.
        """
        if self._stop_flag.is_set() or not _eleven_client:
            return None
        text = _preprocess_for_tts(text)
        path = _mktmp()
        # MP3 with a real audio frame is at least a few hundred bytes; a
        # 200-byte threshold catches "we got just a header" / "we got
        # nothing" without false-rejecting genuinely short utterances.
        _MIN_MP3_BYTES = 200
        bytes_written = 0
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
                        bytes_written += len(chunk)
            # If we were stopped mid-synth, throw away whatever we got
            if self._stop_flag.is_set():
                self._unlink(path)
                return None
            # Empty / near-empty MP3 — surface as failure so the caller
            # can fall back to edge-tts or macOS say, instead of queueing
            # a tmpfile the player will silently discard.
            try:
                actual_size = os.path.getsize(path)
            except OSError:
                actual_size = bytes_written
            if actual_size < _MIN_MP3_BYTES:
                console.print(
                    f"[yellow]ElevenLabs returned {actual_size}b "
                    f"— treating as failure, falling back[/yellow]"
                )
                self._unlink(path)
                return None
            return path
        except Exception as exc:
            console.print(f"[dim]ElevenLabs error: {exc}[/dim]")
            self._unlink(path)
            return None

    @staticmethod
    def _unlink(path: str) -> None:
        """Best-effort tmpfile cleanup — never raises."""
        try:
            if path and os.path.exists(path):
                os.unlink(path)
        except Exception:
            pass

    # ── Playback ──────────────────────────────────────────────────────────────

    def _play_file(self, path: str) -> None:
        """Play audio file with afplay, then delete it."""
        if self._stop_flag.is_set():
            try:
                os.unlink(path)
            except Exception:
                pass
            return
        # Local reference to the Popen — avoids a race with speaker.stop()
        # which sets self._play_proc = None while .communicate() is blocking
        # and then `.returncode` blows up on NoneType.
        proc = None
        try:
            file_size = os.path.getsize(path) if os.path.exists(path) else 0
            if file_size < 200:
                console.print(f"[yellow]play: MP3 too small ({file_size}b) — synthesis failed?[/yellow]")
                return
            with self._lock:
                if not self._stop_flag.is_set():
                    proc = subprocess.Popen(
                        ["afplay", path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                    )
                    self._play_proc = proc
            if proc is None:
                return
            try:
                _, stderr_bytes = proc.communicate()
            except Exception:
                stderr_bytes = b""
            rc = proc.returncode
            # rc == -15 = SIGTERM (we killed it — barge-in, etc) — not an error
            if rc not in (0, None, -15) or (stderr_bytes and b"error" in stderr_bytes.lower()):
                err = (stderr_bytes or b"").decode(errors="ignore").strip()
                console.print(f"[yellow]afplay rc={rc} ({file_size}b): {err}[/yellow]")
            with self._lock:
                if self._play_proc is proc:
                    self._play_proc = None
        except Exception as exc:
            console.print(f"[yellow]play exception: {type(exc).__name__}: {exc}[/yellow]")
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
        # Local Popen reference — mirrors the fix in _play_file. Without
        # this, stop() can set self._play_proc = None while we're blocked
        # in .wait() below, and then `self._play_proc.wait()` blows up on
        # NoneType. By holding our own reference, .wait() always has a
        # valid target even if stop() terminates the process out from
        # under us.
        proc = None
        try:
            with self._lock:
                proc = subprocess.Popen(
                    ["say", "-v", _SAY_VOICE, "-r", str(_SAY_RATE), enhanced],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._play_proc = proc
            try:
                proc.wait()
            except Exception as wait_exc:
                # `say` was killed or process file vanished — surface but
                # don't crash the TTS pipeline.
                console.print(
                    f"[dim]say wait interrupted: {wait_exc}[/dim]"
                )
        except Exception as exc:
            console.print(f"[yellow]macOS say failed: {exc}[/yellow]")
        finally:
            # Only clear if we're still the active process; another caller
            # may have replaced self._play_proc with a fresh Popen already.
            with self._lock:
                if self._play_proc is proc:
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

    # ── ElevenLabs streaming via MP3 + afplay ─────────────────────────────────

    def _stream_speak_eleven(self, text_gen: Generator[str, None, None]) -> str:
        """
        Streaming TTS via ElevenLabs MP3 synthesis + macOS afplay playback.

        Pipeline (same as the edge-tts path):
          Producer  → sentence_q  → Synthesizer (ElevenLabs MP3) → audio_q → Player (afplay)
          (tokens)                                                            sentence by sentence

        We use afplay instead of sounddevice PCM streaming because:
          • afplay respects macOS system volume (sounddevice bypasses it)
          • MP3 + afplay is rock-solid; PCM through sounddevice had
            silent-failure modes that were hard to debug
          • Latency cost is small (~80-150ms per sentence) and masked by pipelining
        """
        from voice.latency import profiler

        collected: list[str] = []
        sentence_q: queue.Queue = queue.Queue()
        audio_q: queue.Queue    = queue.Queue(maxsize=5)

        # ── Producer: tokenise LLM stream → sentence chunks ──────────────────
        # First sentence flushes EAGERLY — perceived latency = time to first audio.
        # Subsequent sentences bundle (>= _MIN_CHUNK) so inter-sentence gaps are
        # masked by playback of the previous sentence.
        def producer() -> None:
            buf = ""; pending = ""
            first_emit = True
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
                    min_chunk = 1 if first_emit else _MIN_CHUNK
                    if len(candidate) >= min_chunk:
                        sentence_q.put(candidate); pending = ""
                        first_emit = False
                    else:
                        pending = candidate
            tail = (pending + " " + buf).strip() if pending else buf.strip()
            if tail and not self._stop_flag.is_set():
                sentence_q.put(tail)
            sentence_q.put(None)

        # ── Synthesizer: sentence → ElevenLabs MP3 file path ─────────────────
        _first_synth = [True]

        def _safe_put(item) -> bool:
            """
            Put onto the bounded audio_q WITHOUT blocking forever. On barge-in
            the player stops draining; a plain blocking put() on a full queue
            would wedge this synthesizer thread permanently. Returns False if we
            gave up because playback is stopping (caller then unlinks the file
            so it isn't leaked).
            """
            while not self._stop_flag.is_set():
                try:
                    audio_q.put(item, timeout=0.2)
                    return True
                except queue.Full:
                    continue
            return False

        def synthesizer() -> None:
            while True:
                try:
                    sentence = sentence_q.get(timeout=0.5)
                except queue.Empty:
                    if self._stop_flag.is_set(): _safe_put(None); return
                    continue
                if sentence is None or self._stop_flag.is_set():
                    _safe_put(None); return
                if _first_synth[0]:
                    profiler.mark("tts_first_synth_start")
                path = self._synthesize_eleven(sentence)
                if path:
                    if not _safe_put(path):
                        self._unlink(path)   # stopping — don't leak the mp3
                        return
                else:
                    console.print("[dim]ElevenLabs returned no audio for sentence — skipping[/dim]")
                if _first_synth[0]:
                    profiler.mark("tts_first_synth_end")
                    _first_synth[0] = False

        threading.Thread(target=producer,    daemon=True, name="tts-producer").start()
        threading.Thread(target=synthesizer, daemon=True, name="tts-synth").start()

        # ── Player: MP3 paths → afplay ────────────────────────────────────────
        _first_play = [True]
        while True:
            try:
                item = audio_q.get(timeout=0.15)
            except queue.Empty:
                if self._stop_flag.is_set(): break
                continue
            if item is None or self._stop_flag.is_set(): break
            if _first_play[0]:
                profiler.mark("tts_first_play")
                _first_play[0] = False
            self._play_file(item)   # afplay + auto-delete

        # Drain any synthesized-but-unplayed mp3s still queued (barge-in breaks
        # the loop above while files may remain) so they don't leak in /tmp.
        while True:
            try:
                leftover = audio_q.get_nowait()
            except queue.Empty:
                break
            if leftover:
                self._unlink(leftover)

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

        # First sentence flushes eagerly; subsequent sentences bundle (see _stream_speak_eleven).
        def producer() -> None:
            buf = ""; pending = ""
            first_emit = True
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
                    min_chunk = 1 if first_emit else _MIN_CHUNK
                    if len(candidate) >= min_chunk:
                        sentence_q.put(candidate); pending = ""
                        first_emit = False
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
