#!/usr/bin/env python3
"""
JARVIS first-run onboarding.
Asks Dylan a series of questions, speaks each one, saves answers to memory.
Run automatically by chat.py on first launch, or manually: python setup.py
"""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from brain.memory import save_fact
from rich.console import Console

console = Console()

SETUP_FLAG = os.path.expanduser("~/JARVIS/.setup_done")

QUESTIONS = [
    ("school_year",
     "What year are you in — freshman, sophomore, junior, senior?"),
    ("subjects",
     "What subjects or courses are you taking right now?"),
    ("exam_schedule",
     "Any big exams or deadlines coming up in the next few months?"),
    ("wake_time",
     "What time do you usually wake up on a school day?"),
    ("fitness",
     "How do you like to stay active — gym, running, sports? And how often?"),
    ("music",
     "What kind of music do you listen to? Artists, genres, whatever."),
    ("friends",
     "Who are your closest friends? First names is fine."),
    ("hobbies",
     "Outside school and working out, what do you spend your time on?"),
    ("goals",
     "What's one big goal you're working toward right now?"),
    ("anything_else",
     "Last one — anything else you want me to always know about you?"),
]


def run_setup(speaker=None) -> None:
    """Run the onboarding interview. Speaker is optional for TTS."""
    console.print("\n[bold cyan]◈  JARVIS — First-time setup[/bold cyan]\n")
    console.print("[dim]I'm going to ask you a few quick questions so I actually know who I'm working with.")
    console.print("Skip any question by pressing Enter. This only runs once.[/dim]\n")

    def ask(text: str) -> str:
        """Speak + print question, return answer."""
        if speaker:
            try:
                speaker.stream_speak(iter([text]))
            except Exception:
                pass
        console.print(f"[cyan]JARVIS:[/cyan] {text}")
        try:
            answer = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            answer = ""
        return answer

    saved = []
    for key, question in QUESTIONS:
        answer = ask(question)
        if answer:
            label = key.replace("_", " ").title()
            fact = f"{label}: {answer}"
            save_fact(fact)
            saved.append(fact)
            console.print("[dim]  ✓ Saved[/dim]\n")
        else:
            console.print("[dim]  — Skipped[/dim]\n")

    # Mark setup complete
    open(SETUP_FLAG, "w").close()

    closing = (
        f"Got it. I've saved {len(saved)} things about you. "
        "I'll remember all of this going forward and use it when it matters."
    )
    if speaker:
        try:
            speaker.stream_speak(iter([closing]))
        except Exception:
            pass
    console.print(f"\n[cyan]JARVIS:[/cyan] {closing}\n")


def needs_setup() -> bool:
    """Return True if onboarding hasn't been run yet."""
    return not os.path.exists(SETUP_FLAG)


def reset_setup() -> None:
    """Force onboarding to run again next time."""
    if os.path.exists(SETUP_FLAG):
        os.remove(SETUP_FLAG)
    console.print("[dim]Setup reset. Onboarding will run on next launch.[/dim]")


if __name__ == "__main__":
    if "--reset" in sys.argv:
        reset_setup()
    else:
        try:
            from voice.speaker import Speaker
            speaker = Speaker()
        except Exception:
            speaker = None
        run_setup(speaker=speaker)
