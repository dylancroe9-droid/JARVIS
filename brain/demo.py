"""
Demo brain — runs without any API key.

Used when the user clicks "Try without an API key" in the setup wizard. The
goal is to give a non-developer a real feel for the product (voice in, voice
out, the HUD, the responses) before they sign up for Anthropic or Groq.

Implements the same surface area as Jarvis.chat(text) — a generator that
yields string chunks — so server.py can swap it in transparently.

Responses are intentionally short. We pattern-match on the user's text and
fall back to a "switch to a real key for full conversation" nudge.
"""

from __future__ import annotations

import datetime
import random
import re
from typing import Generator


_GREETINGS = (
    "hi", "hello", "hey", "yo", "sup", "hi jarvis", "hello jarvis",
    "hey jarvis", "good morning", "good evening", "good afternoon",
)

_CAPABILITIES = (
    "what can you do", "what do you do", "who are you", "what are you",
    "tell me about yourself", "introduce yourself", "your capabilities",
    "help",
)

_TIME_TRIGGERS = ("what time", "current time", "the time", "what's the time")
_DATE_TRIGGERS = ("what's the date", "what day", "today's date", "what is today")
_THANKS        = ("thanks", "thank you", "appreciate", "ty ")

_PITCH = (
    "I'm running in demo mode right now — I can speak and listen, but for "
    "real conversation, weather, calendar, screen vision, and the rest, "
    "you'll want to add an API key. Open Settings whenever you're ready."
)

_DEMO_PRELUDE = (
    "Here's a taste of what I sound like — full power needs an API key, "
    "which takes about a minute to set up."
)


def _is(text: str, triggers) -> bool:
    return any(t in text for t in triggers)


class DemoJarvis:
    """A drop-in stand-in for brain.jarvis.Jarvis that needs no API key."""

    def __init__(self) -> None:
        self._last_blueprint_subject: str | None = None
        self._first_turn = True

    # The real Jarvis exposes other methods (history, memory, etc) — none of
    # them are reached during the demo path because server.py only calls
    # .chat() when handling user input.

    def chat(self, text: str) -> Generator[str, None, None]:
        t = text.lower().strip()
        reply = self._reply_for(t)
        # Stream word-by-word so the renderer + TTS see a real generator.
        # Word granularity gives the streaming feel without being too jittery.
        words = re.findall(r"\S+\s*", reply)
        for w in words:
            yield w

    def _reply_for(self, t: str) -> str:
        # First turn: gentle intro
        if self._first_turn:
            self._first_turn = False
            return (
                "Demo mode active. " + _DEMO_PRELUDE + " "
                "Try saying 'what time is it' or 'who are you'."
            )

        if _is(t, _GREETINGS):
            return random.choice([
                "Hello. I'm running in demo mode — go easy on me.",
                "Hi there. Demo mode here, so my answers are short.",
                "Hey. Demo brain online.",
            ])

        if _is(t, _CAPABILITIES):
            return (
                "I'm JARVIS — an Iron-Man-style voice assistant for your Mac. "
                "Normally I run on Claude or Llama, watch your screen, read your "
                "calendar, write code, and answer in voice. " + _PITCH
            )

        if _is(t, _TIME_TRIGGERS):
            now = datetime.datetime.now().strftime("%-I:%M %p")
            return f"It's {now}."

        if _is(t, _DATE_TRIGGERS):
            today = datetime.datetime.now().strftime("%A, %B %-d")
            return f"Today is {today}."

        if _is(t, _THANKS):
            return "You're welcome."

        if "weather" in t:
            return (
                "I'd check the weather for you, but I need an API key for that. "
                + _PITCH
            )

        if "joke" in t:
            return random.choice([
                "I tried to write a joke about API keys, but it required authentication.",
                "Why did the assistant refuse to argue? It was running out of tokens.",
                "I'd tell you a UDP joke but you might not get it.",
            ])

        if any(w in t for w in ("code", "build", "write", "fix", "deploy")):
            return (
                "Coding agent, screen vision, and tool use are off in demo mode. "
                + _PITCH
            )

        # Fallback
        return random.choice([
            "I hear you, but demo mode keeps my answers shallow. " + _PITCH,
            "That's beyond what demo mode handles — but with a real key I'd dig in. "
            + _PITCH,
            "Noted. " + _PITCH,
        ])
