# Voice-loop latency profiling

JARVIS has a built-in profiler that measures each hop of the voice loop —
mic → STT → brain → TTS → playback — so you can see which step is slowest
without guessing.

## Turn it on

```bash
JARVIS_PROFILE=1 ./start.sh
# or:
JARVIS_PROFILE=1 .venv/bin/python server.py
```

When `JARVIS_PROFILE` is unset (the default), every profiler call is a
no-op — there's no overhead in production builds.

## What you'll see

After each spoken turn, the server prints a block to stdout:

```
[latency] turn: 'whats the weather'
  capture→stt       412.1 ms     ← Whisper transcription
  stt→brain          22.4 ms     ← thread + queue dispatch
  brain TTFT        680.3 ms     ← time to first LLM token
  brain total      2104.7 ms     ← full reply finished streaming
  tts synth         534.0 ms     ← first sentence synthesized
  tts→play          910.5 ms     ← first audio file out → afplay starts
  end-to-end       2034.9 ms     ← user stopped speaking → user hears reply
```

## How to read it

- **end-to-end** is what the user feels. Optimize this.
- **capture→stt** is Whisper. If this is >500ms on a Mac, you're probably
  on the `base` model — drop to `tiny` for 3-5× speedup with minor
  accuracy loss. Set `WHISPER_MODEL=tiny` in `.env`.
- **brain TTFT** is the LLM's time-to-first-token. If this is high:
  - Switch from Anthropic to Groq (Groq is ~3-5× faster TTFT for short replies).
  - Shorten the system prompt.
  - Check if memory.txt is bloating context.
- **tts synth** is edge-tts / ElevenLabs API call. ElevenLabs is high-quality
  but typically 400-1500ms per sentence; edge-tts is 200-600ms.
- **tts→play** > 1s usually means the first sentence was long. The pipelined
  player can't start until at least one sentence is fully synthesized.
  Shorter opening sentences from the brain → faster perceived latency.

## Adding new marks

If you instrument a new hop, add a `profiler.mark("<name>")` call where it
happens, then add a row to `_dump()` in `voice/latency.py` showing the gap
you care about.

## Common findings

In Dylan's local testing (Mac M-series, edge-tts, Whisper `base`):

| Hop          | Typical | Worst |
|--------------|---------|-------|
| capture→stt  | 300-600 | 1200  |
| brain TTFT   | 400-900 | 2500  |
| tts synth    | 300-700 | 1800  |
| **e2e**      | 1.4-2.5s| 5s+   |

Biggest wins so far:
1. `WHISPER_MODEL=tiny` if you don't need perfect accuracy → −300ms STT.
2. Trim system prompt → −200-400ms TTFT.
3. Make the brain start replies with a short ack ("Sure—") → user hears
   audio sooner even though `brain total` is unchanged.
