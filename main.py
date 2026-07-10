#!/usr/bin/env python3
"""
JARVIS — Just A Rather Very Intelligent System
Run with: python main.py         (voice mode)
          python main.py --text  (text-only, no mic/speaker needed)
"""

import sys
import os
import signal
from typing import Optional

# ── ensure the JARVIS directory is on the path ──────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

from rich.console import Console
from rich.panel import Panel

from config import USER_NAME, ANTHROPIC_API_KEY, GROQ_API_KEY, ANTHROPIC_MODEL, GROQ_MODEL

console = Console()

TEXT_MODE = "--text" in sys.argv or "-t" in sys.argv


# --------------------------------------------------------------------------- #
# Startup banner                                                               #
# --------------------------------------------------------------------------- #

def print_banner() -> None:
    console.print(
        Panel.fit(
            "[bold cyan]J . A . R . V . I . S[/bold cyan]\n"
            "[dim]Just A Rather Very Intelligent System[/dim]",
            border_style="cyan",
            padding=(1, 4),
        )
    )
    mode = "[yellow]text mode[/yellow]" if TEXT_MODE else "[green]voice mode[/green]"
    active_model = ANTHROPIC_MODEL if ANTHROPIC_API_KEY.startswith("sk-ant-") else GROQ_MODEL
    console.print(f"  Mode: {mode}   |   Model: {active_model}")
    console.print("  Type [bold]exit[/bold] or press [bold]Ctrl-C[/bold] to shut down.\n")


# --------------------------------------------------------------------------- #
# Main loop                                                                    #
# --------------------------------------------------------------------------- #

def main() -> None:
    print_banner()

    # ── Validate API key early ───────────────────────────────────────────────
    if not ANTHROPIC_API_KEY and not GROQ_API_KEY:
        console.print(
            "[red bold]No API key found.[/red bold]\n"
            "Copy [dim].env.example[/dim] → [dim].env[/dim] and add your Anthropic or Groq key, then try again."
        )
        sys.exit(1)

    # ── Import heavy modules after banner so startup feels snappy ────────────
    from brain.jarvis import Jarvis
    from voice.speaker import Speaker

    jarvis = Jarvis()
    speaker = Speaker() if not TEXT_MODE else None

    # ── Load voice listener (pre-warm Whisper in background) ─────────────────
    listener = None
    if not TEXT_MODE:
        from voice.listener import VoiceListener
        from config import WHISPER_MODEL
        listener = VoiceListener(model_name=WHISPER_MODEL)
        listener.preload()   # loads model in background thread

    # ── Opening greeting ──────────────────────────────────────────────────────
    console.print("[dim]Initialising…[/dim]")
    _speak_response(
        jarvis.chat(
            "Greet me concisely. One or two sentences max. "
            "Mention today's date and that you're ready."
        ),
        speaker,
        prefix="[cyan]JARVIS:[/cyan] ",
    )
    console.print()

    # ── Conversation loop ─────────────────────────────────────────────────────
    interrupted = False

    def _handle_sigint(sig, frame):
        nonlocal interrupted
        if interrupted:
            console.print(f"\n[dim]Shutting down. Goodbye, {USER_NAME}.[/dim]")
            sys.exit(0)
        interrupted = True
        console.print(
            "\n[dim]Interrupted. Press Ctrl-C again to exit, or just keep talking.[/dim]"
        )
        if speaker:
            speaker.stop()

    signal.signal(signal.SIGINT, _handle_sigint)

    while True:
        interrupted = False

        try:
            user_input = _get_input(listener)
        except EOFError:
            break

        if user_input is None:
            console.print("[dim]Didn't catch that — try again.[/dim]\n")
            continue

        if not user_input.strip():
            continue

        lower = user_input.lower().strip()
        if lower in {"exit", "quit", "bye", "goodbye", "shut down", "shutdown"}:
            farewell = ""
            for chunk in jarvis.chat("Say goodbye briefly, one sentence."):
                farewell += chunk
            console.print(f"[cyan]JARVIS:[/cyan] {farewell}")
            if speaker:
                speaker.speak(farewell)
            break

        if lower in {"reset", "clear memory", "forget everything"}:
            jarvis.reset()
            msg = "Memory cleared. Fresh start, Mr. Roe."
            console.print(f"[cyan]JARVIS:[/cyan] {msg}")
            if speaker:
                speaker.speak(msg)
            console.print()
            continue

        # ── Show what the user said (voice mode only) ─────────────────────
        if not TEXT_MODE:
            console.print(f"[yellow]You:[/yellow] {user_input}\n")

        # ── Stream JARVIS response ────────────────────────────────────────
        _speak_response(
            jarvis.chat(user_input),
            speaker,
            prefix="[cyan]JARVIS:[/cyan] ",
        )
        console.print()


# --------------------------------------------------------------------------- #
# Input helpers                                                                #
# --------------------------------------------------------------------------- #

def _get_input(listener) -> "Optional[str]":
    """Get user input from mic (voice mode) or stdin (text mode)."""
    if TEXT_MODE or listener is None:
        try:
            console.print("[bold yellow]You:[/bold yellow] ", end="", highlight=False)
            return input()
        except (EOFError, KeyboardInterrupt):
            raise EOFError

    # Voice mode
    console.print(
        "[bold yellow]▶  Press Enter to speak[/bold yellow] "
        "[dim](or type your message):[/dim] ",
        end="",
    )

    # Allow typed input too — check if user typed something before Enter
    import termios
    import tty

    # Non-blocking stdin peek
    try:
        old_settings = termios.tcgetattr(sys.stdin)
        tty.setraw(sys.stdin.fileno())
        chars = []
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\n", "\r"):
                break
            if ch in ("\x03",):   # Ctrl-C
                raise KeyboardInterrupt
            if ch in ("\x7f", "\x08"):  # backspace
                if chars:
                    chars.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
            else:
                chars.append(ch)
                sys.stdout.write(ch)
                sys.stdout.flush()
        sys.stdout.write("\n")
        sys.stdout.flush()
    except Exception:
        # Fallback if terminal manipulation fails (e.g. piped input)
        termios = None
        line = input()
        return line.strip() if line.strip() else None
    finally:
        try:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        except Exception:
            pass

    typed = "".join(chars).strip()
    if typed:
        return typed

    # No typed text → record voice
    console.print()
    text = listener.listen()
    return text


# --------------------------------------------------------------------------- #
# Output helpers                                                               #
# --------------------------------------------------------------------------- #

def _speak_response(gen, speaker, prefix: str = "") -> None:
    """
    Stream text chunks from `gen`, printing and speaking simultaneously.
    Prints the prefix once, then appends chunks in-place.
    """
    console.print(prefix, end="", highlight=False)

    if speaker:
        # stream_speak handles printing via the generator wrapper
        speaker.stream_speak(_printing_gen(gen))
        # The generator already printed chunks; add a trailing newline
    else:
        # Text mode: just print chunks
        for chunk in gen:
            console.print(chunk, end="", highlight=False)


def _printing_gen(gen):
    """Wrap a generator to print each chunk as it arrives."""
    for chunk in gen:
        console.print(chunk, end="", highlight=False)
        yield chunk


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    main()
