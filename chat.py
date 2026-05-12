#!/usr/bin/env python3
"""JARVIS — terminal chat. Type OR speak 'Hey JARVIS'. Both work simultaneously."""

import os, sys, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import GROQ_API_KEY
if not GROQ_API_KEY:
    print("No GROQ_API_KEY in ~/JARVIS/.env")
    sys.exit(1)

from brain.jarvis import Jarvis
from rich.console import Console

console = Console()

# ── Speaker (optional — graceful degradation if audio broken) ─────────────────
try:
    from voice.speaker import Speaker
    speaker = Speaker()
except Exception as e:
    console.print(f"[yellow]TTS unavailable ({e}) — text-only mode.[/yellow]")
    speaker = None

# ── First-run onboarding ──────────────────────────────────────────────────────
from setup import needs_setup, run_setup
if needs_setup():
    run_setup(speaker=speaker)

# ── Core objects ──────────────────────────────────────────────────────────────
jarvis = Jarvis()
audio  = None
_lock  = threading.Lock()   # one response at a time


def process(text: str) -> None:
    with _lock:
        if audio:
            audio.suspend()   # don't pick up TTS output or our own thinking
        if speaker:
            speaker.stop()
            speaker.resume()
        console.print(f"\n[bold]You:[/bold] {text}")
        console.print("[cyan]JARVIS:[/cyan] ", end="")

        def _gen():
            for chunk in jarvis.chat(text):
                print(chunk, end="", flush=True)
                yield chunk

        if speaker:
            speaker.stream_speak(_gen())
        else:
            for _ in _gen():
                pass

        print("\n")
        if audio:
            # Stay hot — no wake word needed for 5s after JARVIS replies
            audio.start_conversing()


def on_transcription(kind: str, text: str) -> None:
    from voice.audio_engine import check_wake_word
    if kind == "wake":
        triggered, inline = check_wake_word(text)
        if not triggered:
            return
        if inline:
            threading.Thread(target=process, args=(inline,), daemon=True).start()
        else:
            audio.start_recording()
    elif kind == "command":
        if text.strip():
            threading.Thread(target=process, args=(text,), daemon=True).start()
        else:
            audio.start_detecting()


def on_barge_in() -> None:
    if speaker:
        speaker.stop()
    if audio:
        audio.start_recording()


# ── Start audio engine ────────────────────────────────────────────────────────
_mic_muted = "--mute" in sys.argv   # start muted with: python chat.py --mute

try:
    from voice.audio_engine import AudioEngine
    audio = AudioEngine(on_transcription=on_transcription, on_barge_in=on_barge_in)
    if _mic_muted:
        audio.suspend()
        console.print("[bold cyan]◈  JARVIS[/bold cyan] — mic off. Type 'unmute' to enable voice.\n")
    else:
        audio.start_detecting()
        console.print("[bold cyan]◈  JARVIS[/bold cyan] — type or say 'Hey JARVIS'. Ctrl-C to quit.\n")
    console.print("[dim]mute / unmute — toggle mic  |  reset setup  |  clear memory[/dim]\n")
except Exception as e:
    console.print(f"[yellow]Voice unavailable ({e}) — text only.[/yellow]")
    console.print("[bold cyan]◈  JARVIS[/bold cyan] — type a message, Enter to send. Ctrl-C to quit.\n")

# ── Text input loop (main thread) ─────────────────────────────────────────────
while True:
    try:
        text = input("You: ").strip()
        if not text:
            continue

        # Built-in meta-commands
        tl = text.lower()

        if tl == "mute":
            if audio:
                audio.suspend()
                _mic_muted = True
            console.print("[dim]Mic off — voice disabled. Type 'unmute' to re-enable.[/dim]")
            continue

        if tl == "unmute":
            if audio:
                audio.start_detecting()
                _mic_muted = False
            console.print("[dim]Mic on — listening for 'Hey JARVIS'.[/dim]")
            continue

        if tl in ("reset setup", "redo setup"):
            from setup import reset_setup
            reset_setup()
            run_setup(speaker=speaker)
            continue

        if tl in ("clear memory", "forget everything"):
            from brain.memory import clear_memory
            console.print(clear_memory())
            continue

        threading.Thread(target=process, args=(text,), daemon=True).start()

    except KeyboardInterrupt:
        print("\nGoodbye, Mr. Roe.")
        if speaker:
            speaker.stop()
        if audio:
            audio.close()
        break
    except EOFError:
        break
