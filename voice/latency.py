"""
Voice-loop latency profiler.

Gated on the JARVIS_PROFILE env var — when unset, all calls are no-ops, so this
adds zero overhead in production.

Usage:
    from voice.latency import profiler

    profiler.start_turn("Whatever the user said")
    profiler.mark("stt_start")
    # … STT runs …
    profiler.mark("stt_end")
    profiler.mark("brain_first_chunk")
    profiler.mark("brain_done")
    profiler.mark("tts_first_synth_start")
    profiler.mark("tts_first_synth_end")
    profiler.mark("tts_first_play")
    profiler.end_turn()        # prints summary

Hops the summary reports (in ms, wall-clock):
    capture→stt:   STT processing (Whisper)
    stt→brain:     dispatch to brain.chat (queue + thread hop)
    brain TTFT:    first chunk from the LLM (this is what the user feels)
    brain total:   full reply finished streaming
    tts TTFT:      first synthesized audio file ready
    tts→play:      first audio actually playing through speakers
    end-to-end:    user finished speaking → user hears reply

The profiler tracks the *current* turn — calls to mark() outside of a turn
are silently dropped. Threading is not a concern because the server.py
voice loop serializes input under _lock.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

ENABLED = os.environ.get("JARVIS_PROFILE", "").lower() in ("1", "true", "yes")


@dataclass
class _Turn:
    label: str
    started: float = field(default_factory=time.monotonic)
    marks: dict[str, float] = field(default_factory=dict)


class _Profiler:
    def __init__(self) -> None:
        self._turn: Optional[_Turn] = None
        self._lock = threading.Lock()

    def start_turn(self, label: str) -> None:
        if not ENABLED:
            return
        with self._lock:
            self._turn = _Turn(label=label[:60])

    def mark(self, name: str) -> None:
        if not ENABLED:
            return
        with self._lock:
            if self._turn is None:
                return
            # First occurrence wins — in pipelined TTS, the *first* sentence
            # is what determines perceived latency.
            self._turn.marks.setdefault(name, time.monotonic())

    def end_turn(self) -> None:
        if not ENABLED:
            return
        with self._lock:
            t = self._turn
            self._turn = None
        if t is None:
            return
        self._dump(t)

    def _dump(self, t: _Turn) -> None:
        m = t.marks

        def gap(a: str, b: str) -> Optional[float]:
            if a not in m or b not in m:
                return None
            return (m[b] - m[a]) * 1000.0

        rows: list[tuple[str, Optional[float]]] = [
            ("capture→stt",  gap("stt_start", "stt_end")),
            ("stt→brain",    gap("stt_end", "brain_start")),
            ("brain TTFT",   gap("brain_start", "brain_first_chunk")),
            ("brain total",  gap("brain_start", "brain_done")),
            ("tts synth",    gap("tts_first_synth_start", "tts_first_synth_end")),
            ("tts→play",     gap("brain_first_chunk", "tts_first_play")),
            ("end-to-end",   gap("stt_start", "tts_first_play")),
        ]

        print(f"\n[latency] turn: {t.label!r}")
        for name, ms in rows:
            if ms is None:
                print(f"  {name:<14} —")
            else:
                print(f"  {name:<14} {ms:>8.1f} ms")
        print()


profiler = _Profiler()
