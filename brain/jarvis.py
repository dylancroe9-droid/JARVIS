"""
Core JARVIS brain.
Primary:  Claude (Anthropic) — when ANTHROPIC_API_KEY is set
Fallback: Groq              — when only GROQ_API_KEY is available
"""

from __future__ import annotations

import json
import re
from collections import deque
from typing import Generator

from rich.console import Console

from datetime import datetime

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, GROQ_API_KEY, GROQ_MODEL
from brain.personality import get_system_prompt, get_fast_prompt, get_mid_prompt
from brain.tools import TOOL_DEFINITIONS, get_openai_tools, get_core_tools, get_8b_tools

console = Console()

# Regex to strip raw function-call XML that some quantized models emit as text
# e.g. <function=quick_note{"text":"..."}</function>
_FUNC_CALL_TAG_RE = re.compile(r'<function=\w+\{.*?\}</function>', re.DOTALL)


def _clean_text(text: str) -> str:
    """Strip raw function-call XML tags before displaying to the user."""
    cleaned = _FUNC_CALL_TAG_RE.sub('', text).strip()
    return cleaned


# Only use Claude if the key looks like a real Anthropic key — guards against
# placeholder values like "YOUR_KEY_HERE" causing failed API calls on every request.
_USE_CLAUDE = ANTHROPIC_API_KEY.startswith("sk-ant-")

# ── Music intent keywords ──────────────────────────────────────────────────────
_MUSIC_PLAY_WORDS  = frozenset({"play", "music", "song", "track", "spotify",
                                 "apple music", "playlist", "album", "artist",
                                 "shuffle", "queue"})
_MUSIC_PAUSE_WORDS = frozenset({"pause", "stop the music", "pause the music",
                                 "pause it", "stop it", "stop playing"})
# Note: bare "next" is intentionally excluded — it matches "next line", "next question", etc.
_MUSIC_SKIP_WORDS  = frozenset({"skip", "next song", "next track", "skip this",
                                 "skip it", "next one"})
_MUSIC_PREV_WORDS  = frozenset({"previous song", "previous track", "last song",
                                 "last track", "last one", "go back", "previous"})
_MUSIC_WORDS = _MUSIC_PLAY_WORDS | _MUSIC_PAUSE_WORDS | _MUSIC_SKIP_WORDS | _MUSIC_PREV_WORDS

# Overlay trigger phrases — force show_overlay tool when detected.
# Phrase match: ANY of these substrings in the user message → force overlay.
_OVERLAY_FORCE_PHRASES = frozenset({
    # Timeline variants
    "timeline of", "timeline for", "timeline about", "timeline on",
    "show me a timeline", "show timeline", "give me a timeline",
    "a timeline", "the timeline",
    # Visual / diagram
    "visual of", "visual on", "show me a visual", "show visual", "give me a visual",
    "diagram of", "diagram for", "show me a diagram", "give me a diagram",
    # Concept maps / flashcards
    "concept map", "concepts of", "concepts for", "concept of",
    "flashcards", "flash cards", "flashcard of", "flashcard for",
    # Mind maps
    "mindmap", "mind map",
    # Overlay
    "show overlay", "overlay of",
    "visual breakdown", "breakdown of",
})

# Single-word triggers: if "timeline" / "flashcard" / "diagram" appears anywhere
# in a message longer than 5 words, force the overlay.
# Catches: "AP world history timeline", "give me an APUSH flashcard set", etc.
_OVERLAY_SINGLE_WORDS = frozenset({"timeline", "flashcard", "flashcards", "mindmap"})
_OVERLAY_VISUAL_WORDS = frozenset({"visual", "diagram", "concept map"})

# Blueprint triggers — "show me the Bugatti", "show me a Ferrari", "pull up the F-150"
# These all mean: display the object as a blueprint overlay.
# Designed to be CONVERSATIONAL — user should not need magic words.
_BLUEPRINT_TRIGGER_PHRASES = frozenset({
    # Direct show commands
    "show me the ", "show me a ", "show me an ",
    "show the ", "show a ", "show an ",
    "show it", "show that",
    # Pull up / bring up
    "pull up the ", "pull up a ", "pull up an ",
    "pull it up", "pull that up",
    "bring up the ", "bring up a ", "bring it up", "bring that up",
    # Display / render / visualize
    "display the ", "display a ", "display an ",
    "render the ", "render a ", "render an ",
    "visualize the ", "visualize a ",
    "put up a ", "put up the ",
    # Blueprint / specs explicit — "blueprint the/a/an/of/for/on X"
    "blueprint the ", "blueprint a ", "blueprint an ",
    "blueprint of", "blueprint for", "blueprint on",
    "specs of", "specs for", "specs on", "spec sheet",
    "technical specs", "technical data",
    "make me a blueprint", "build me a blueprint", "draw me a blueprint",
    "give me a blueprint", "give me the blueprint",
    "create a blueprint", "generate a blueprint",
    # Want to see
    "i want to see the ", "i want to see a ", "i want to see an ",
    "i wanna see the ", "i wanna see a ", "i wanna see it",
    "let me see the ", "let me see a ", "let me see an ",
    "want to see the ", "want to see a ", "want to see an ",
    "i'd like to see ", "i would like to see ",
    "show me what ", "let me look at ",
    # Can you / could you
    "can you show me the ", "can you show me a ", "can you show me an ",
    "can you show me what ",
    "can you pull up", "can you bring up",
    "can you display", "can you render",
    # Look at
    "look at the ", "look at a ", "look at an ",
    "take a look at ", "let's look at ",
    # What does X look like
    "what does the ", "what does a ", "what does an ",
    "what do the ", "what do a ",
    "what's the ", "what's a ",     # "what's the bugatti chiron" in context
    # Hologram / 3D
    "hologram of", "hologram for",
    "3d model of", "3d model for",
    "3d of", "3d view of",
    "show me the 3d", "render the 3d",
    "model of the ", "model of a ",
    # Anatomy / science
    "anatomy of", "diagram of", "structure of",
    "what does a human ", "what does the human ",
    "show me how ", "show me what a ",
    # "put it on screen", "throw it up"
    "put it on", "throw it up", "project the ", "project a ",
    # Build / design mode activators (mode-word triggers — conversational phrases
    # like "build me a X" are now routed to 3D build canvas in server.py first)
    "build mode", "design mode", "architect mode",
    "design a ", "design the ",
    "architect a ", "draft a ", "sketch a ",
    # Specific objects people naturally say
    "i'm talking about the ", "i'm thinking about the ",
    "you know the ", "you know that ",
    # Brand / object shortcuts — vehicles
    "the bugatti", "the ferrari", "the lamborghini", "the porsche",
    "the falcon 9", "the starship", "the f-22", "the f-35",
    # Brand / object shortcuts — tech
    "the iphone", "the macbook", "the ipad", "the vision pro",
    "the airpods", "the ps5", "the xbox", "the nintendo switch",
    # Common everyday objects
    "the eiffel tower", "the empire state", "the burj khalifa",
    "the pyramids", "the colosseum", "the space needle",
    # Nature / science
    "the solar system", "the milky way", "planet earth", "the moon",
    "the heart", "the brain", "the lungs", "the spine", "the dna",
    "human heart", "human brain", "human lungs", "human skull", "human body",
    "the human heart", "the human brain", "the human lungs", "the human skull",
    "a human heart", "a human brain",
    "dna helix", "double helix", "the atom", "an atom",
    # Animals
    "a lion", "a tiger", "a wolf", "a bear", "an eagle",
    "a shark", "a whale", "a dolphin", "a horse", "a dragon",
    # In-progress / collaborative design phrases (show blueprint alongside)
    "i want to design", "i'm designing",
    "help me design", "help me create",
})

# Words that cancel the blueprint trigger — simple factual questions
_BLUEPRINT_CANCEL_WORDS = frozenset({
    "time", "weather", "temperature", "news", "score", "date", "today", "tomorrow",
    "yourself", "your name", "how to ",
    "when is ", "who invented", "how fast is ", "how much does",
})

# Tools that get a canned one-liner — no follow-up LLM call needed
_MUSIC_CANNED_TOOLS = {
    "pause_music":          "Paused.",
    "next_track":           "Next.",
    "previous_track":       "Going back.",
    "set_volume":           "Done.",
    "mute_volume":          "Muted.",
    "remove_from_playlist": "Removed.",
}

# Action tools where the tool result IS the response — skip the follow-up LLM call.
# This eliminates ~1-2s of latency after web/youtube/open actions.
# NOTE: deep_research, web_search_results, get_wikipedia_summary are NOT here —
# we want Claude to synthesize those results into an overlay or answer, not just dump them.
_FIRE_AND_DONE_TOOLS = {
    "web_search",
    "youtube_search",
    "open_url",
    "open_application",
    "generate_image",
    "show_overlay",
    # Timers & productivity — tool result IS the confirmation message
    "set_timer",
    "set_pomodoro",
    "add_reminder",
    "quick_note",
    "remember_fact",
    "create_calendar_event",
    "set_volume",
    "mute_volume",
    # Read-only queries whose result is the answer — avoids extra LLM loop on fallback models
    "get_reminders",
    "get_calendar_events",
    "get_battery_status",
    "get_uptime",
    "get_weather",
    "get_forecast",
    "get_location",
    "get_daily_briefing",
    "get_unread_emails",
    "get_recent_emails",
    "get_now_playing",
    "list_playlists",
    "define_word",
    "get_wikipedia_summary",
    "translate_text",
    "convert_units",
}

# Computer agent: when these phrases appear, put JARVIS in autonomous screen-control mode.
# More rounds, forced take_screenshot first, no babying.
_COMPUTER_AGENT_PHRASES = frozenset({
    # Direct commands
    "do my homework", "do my work", "do this for me", "do it for me",
    "fill this out", "fill it out", "fill in the answers", "fill out this",
    "answer this for me", "answer these for me", "answer the questions",
    "type this for me", "type the answer", "type it for me",
    "take over", "take over my screen", "control my screen",
    "handle this", "handle it", "do it",
    "help me with this assignment", "help me with this form",
    "write the answers", "write this for me",
    "submit this", "submit it for me",
    "click that", "click the button", "click on",
    "scroll down for me", "scroll the page",
    "copy and paste", "select all", "highlight",
    # Natural "stepping away" phrases — "I'll be right back, finish this"
    "be right back", "brb", "i'll be back", "i will be back",
    "finish this for me", "finish it for me", "can you finish",
    "take care of this", "take care of it",
    "handle the rest", "handle the rest for me",
    "complete this for me", "complete it for me",
    "while i'm gone", "while im gone", "while i step away",
    "you got this", "you handle it", "you take over",
    "i'm stepping out", "im stepping out", "i need to step away",
    "just finish it", "just do it", "just handle it",
    # AP Classroom / College Board
    "ap classroom", "progress check", "collegeboard", "college board",
    "do progress check", "do the progress check", "do my progress check",
    "do unit", "finish unit", "complete unit",
    "ap world", "ap gov", "ap chem", "ap history",
    "ap government", "ap chemistry",
    "myap", "my ap",
})

# Research-first overlay: when these topics appear with overlay intent,
# Claude must call get_wikipedia_summary or deep_research BEFORE show_overlay.
_RESEARCH_TOPIC_WORDS = frozenset({
    "history", "historical", "war", "battle", "revolution", "president",
    "century", "era", "civilization", "empire", "ancient", "medieval",
    "world war", "civil war", "cold war", "dynasty", "kingdom",
    "biography", "chronology", "discovery", "scientific", "evolution",
    "ap world", "apush", "ap history", "blueprint", "specs", "specification",
    "car", "plane", "aircraft", "vehicle", "engine", "architecture",
    "inventor", "invention", "founded", "established", "created",
    "events of", "history of", "rise of", "fall of", "causes of",
})

def _extract_blueprint_subject(msg: str) -> str:
    """Strip the trigger phrase from a user message to get the subject.
    e.g. 'show me the bugatti chiron' → 'bugatti chiron'
         'give me a blueprint for the dodge challenger' → 'dodge challenger'
         'show me planet earth' → 'planet earth'   (phrase IS the subject)
    """
    # Sort by length descending so longer (more specific) phrases match first
    best_subject = ""
    best_phrase_len = 0

    for phrase in sorted(_BLUEPRINT_TRIGGER_PHRASES, key=len, reverse=True):
        if phrase not in msg:
            continue
        idx = msg.index(phrase)
        after = msg[idx + len(phrase):].strip()
        # Strip leading articles/prepositions: "for the", "of a", "the", "a", "an"
        for leading in ("for the ", "for a ", "for an ", "of the ", "of a ", "of an ",
                        "the ", "a ", "an "):
            if after.startswith(leading):
                after = after[len(leading):].strip()
                break
        # Strip trailing filler (loop: keep stripping until no match)
        _trailing_words = (
            " please", " now", " for me", " thanks", " ok", " okay",
            " to me", " look like", " looks like", " look", " looks",
            " blueprint", " blueprints", " specs", " specifications",
            " 3d", " hologram", " diagram", " model",
        )
        for _ in range(4):  # up to 4 trailing words stripped
            stripped = False
            for trailing in _trailing_words:
                if after.endswith(trailing):
                    after = after[: -len(trailing)].strip()
                    stripped = True
                    break
            if not stripped:
                break
        after = after.strip(" .,?!")

        if len(after) >= 2:
            # Prefer the match whose trigger phrase is longer (more specific)
            if len(phrase) > best_phrase_len:
                best_subject = after
                best_phrase_len = len(phrase)
            continue

        # after is empty — the phrase itself may BE the subject
        # (e.g. "the heart", "planet earth", "a lion", "human heart")
        # Use what comes BEFORE the trigger if that's where context sits,
        # OR use the trigger phrase directly (stripped of articles).
        phrase_as_subj = phrase.strip()
        for leading in ("the ", "a ", "an "):
            if phrase_as_subj.startswith(leading):
                phrase_as_subj = phrase_as_subj[len(leading):].strip()
                break
        # Also strip trailing space from fixed-suffix triggers like "show me the "
        phrase_as_subj = phrase_as_subj.rstrip()
        if len(phrase_as_subj) >= 2 and len(phrase) > best_phrase_len:
            best_subject = phrase_as_subj
            best_phrase_len = len(phrase)

    return best_subject


# Context-only trigger words — if the user says one of these SHORT phrases AND
# the conversation recently mentioned a vehicle/object, treat it as a blueprint request.
_BLUEPRINT_CONTEXT_TRIGGERS = frozenset({
    "pull it up", "show it", "show that", "bring it up", "pull that up",
    "yes", "yeah", "yep", "do it", "go ahead",
    "show me", "let me see it", "let me see that",
    "make it 3d", "make it a hologram", "render it", "visualize it",
    "what does it look like", "show me what it looks like",
    "i want to see it", "i wanna see it",
    "put it up", "display it",
})

_RATE_LIMIT_MSG = (
    "My processing queue is briefly overloaded, Mr. Roe. Give it a moment and try again."
)

_BAD_REQUEST_MSG = (
    "My API access has been restricted. "
    "Replace your key in ~/JARVIS/.env — either ANTHROPIC_API_KEY "
    "(console.anthropic.com) or GROQ_API_KEY (console.groq.com)."
)

_FALLBACK_MODELS = [
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "llama3-groq-8b-8192-tool-use-preview",
    "llama-3.1-8b-instant",
]


class Jarvis:
    def __init__(self) -> None:
        self._executor = None
        self.messages: list[dict] = []
        self._recent_replies: deque = deque(maxlen=5)
        self._last_blueprint_subject: str = ""  # context for follow-up requests like "pull it up"

        # Primary: Claude
        if _USE_CLAUDE:
            import anthropic
            self._claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            self.model = ANTHROPIC_MODEL
            console.print(f"[dim]Brain: Claude {ANTHROPIC_MODEL}[/dim]")

        # Groq: primary fallback or only brain
        if GROQ_API_KEY:
            from openai import OpenAI
            self.client = OpenAI(
                api_key=GROQ_API_KEY,
                base_url="https://api.groq.com/openai/v1",
            )
            if not _USE_CLAUDE:
                self.model = GROQ_MODEL
                console.print(f"[dim]Brain: Groq {GROQ_MODEL}[/dim]")
        elif not _USE_CLAUDE:
            raise RuntimeError(
                "No API key found.\n"
                "Add ANTHROPIC_API_KEY or GROQ_API_KEY to ~/JARVIS/.env"
            )

    @property
    def tools_executor(self):
        if self._executor is None:
            from brain._executor import ToolsExecutor
            self._executor = ToolsExecutor()
        return self._executor

    # ── Public API ─────────────────────────────────────────────────────────────

    def chat(self, user_input: str) -> Generator[str, None, None]:
        self.messages.append({"role": "user", "content": user_input})
        if len(self.messages) > 60:
            self.messages = self.messages[-60:]

        if _USE_CLAUDE:
            # Silently extract memorable facts in background — no await, never blocks
            self._auto_remember(user_input)
            yield from self._claude_chat()
        else:
            _lower_input = user_input.lower()
            # [STUDY] and [STUDY MODE] prompts are pure generation — skip tools entirely
            # to avoid the LLM calling run_command or other tools mid-generation.
            if _lower_input.startswith("[study]") or _lower_input.startswith("[study mode]"):
                yield from self._plain_stream(model=GROQ_MODEL)
            elif self._is_fast_query(user_input):
                yield from self._plain_stream(model="meta-llama/llama-4-scout-17b-16e-instruct")
            else:
                yield from self._stream_with_tools()

    def reset(self) -> None:
        self.messages.clear()

    def _get_recent_replies_note(self) -> str:
        if not self._recent_replies:
            return ""
        return (
            "\n[INTERNAL — variety guard: Do NOT start your response the same way as any of these recent replies: "
            + " | ".join(list(self._recent_replies)[-3:])
            + "]"
        )

    def _get_proactive_context(self) -> str:
        """
        Generate a brief proactive context note that gets appended to the system prompt.
        Checks time of day, upcoming calendar events, and recent memory to surface
        relevant information JARVIS should be aware of — even if not asked.
        This is what makes JARVIS feel like he's paying attention.
        """
        notes: list[str] = []
        now = datetime.now()
        hour = now.hour

        # Time-sensitive awareness
        if hour >= 23 or hour < 1:
            notes.append(
                "It is past midnight. Note this once if relevant — no lecture."
            )
        elif hour >= 22:
            notes.append(
                "It's late evening. If there's anything time-sensitive tomorrow, "
                "surface it naturally — once."
            )
        elif hour < 7:
            notes.append("It's very early. Acknowledge the discipline quietly if it fits.")

        # Upcoming calendar check — non-blocking, best effort
        try:
            from tools.calendar_tool import get_calendar_events
            cal = get_calendar_events(days=1)
            if cal and "no events" not in cal.lower() and "unavailable" not in cal.lower():
                notes.append(f"TODAY'S CALENDAR (proactive context): {cal[:300]}")
        except Exception:
            pass

        if not notes:
            return ""

        return (
            "\n\n[PROACTIVE CONTEXT — use naturally, don't read aloud directly]\n"
            + "\n".join(f"• {n}" for n in notes)
        )

    # ── Claude path ────────────────────────────────────────────────────────────

    @staticmethod
    def _token_budget(user_input: str) -> int:
        """
        Estimate how many tokens the response deserves.
        Commands get a tight budget (fast). Real conversations and complex questions
        get genuine room — JARVIS should never get cut off mid-thought.
        """
        t = user_input.lower().strip()
        words = t.split()

        # Hard commands — keep it punchy
        command_verbs = {"play", "pause", "stop", "skip", "open", "launch",
                         "close", "set", "timer", "remind", "mute", "unmute",
                         "screenshot", "search", "weather", "time", "date"}
        if len(words) <= 5 and any(t.startswith(v) for v in command_verbs):
            return 300

        # Short conversational — still needs room to be genuinely useful
        if len(words) <= 8:
            return 800

        # Medium questions — give a real answer
        if len(words) <= 20:
            return 1800

        # Long / complex / creative — full room to think and respond properly
        explain_words = {"explain", "how", "why", "what", "write", "create",
                         "help", "describe", "analyze", "compare", "plan",
                         "story", "poem", "code", "debug", "fix", "review",
                         "teach", "think", "opinion", "thoughts", "believe",
                         "difference", "better", "worse", "should", "would"}
        if any(w in t for w in explain_words) or len(words) > 20:
            return 4096

        return 1800

    def _claude_chat(self) -> Generator[str, None, None]:
        """
        Claude tool loop:
          1. Non-streaming call — detect tool use in response.content
          2. Execute tools, add results as user turn
          3. Stream the final conversational response
        Up to 4 rounds. Falls back to Groq if Claude errors out.
        """
        system = (
            get_system_prompt()
            + self._get_proactive_context()
            + self._get_recent_replies_note()
        )

        # Budget based on how complex the question is
        last_user = next(
            (m["content"] for m in reversed(self.messages) if m.get("role") == "user"
             and isinstance(m.get("content"), str)), ""
        ).lower()
        budget = self._token_budget(last_user)

        # Determine if this is an overlay request and whether it needs research first.
        _overlay_triggered = (
            any(phrase in last_user for phrase in _OVERLAY_FORCE_PHRASES)
            or (any(w in last_user for w in _OVERLAY_SINGLE_WORDS) and len(last_user.split()) > 5)
            or (any(w in last_user for w in _OVERLAY_VISUAL_WORDS)
                and any(w in last_user for w in {"show", "give", "make", "create", "draw", "build"}))
        )

        # Blueprint trigger: "show me the Bugatti", "pull up the F-150", etc.
        # These are NOT caught by _OVERLAY_FORCE_PHRASES but MUST force show_overlay.
        _blueprint_triggered = (
            any(phrase in last_user for phrase in _BLUEPRINT_TRIGGER_PHRASES)
            and not any(cancel in last_user for cancel in _BLUEPRINT_CANCEL_WORDS)
            and len(last_user.split()) >= 4
        )
        _overlay_triggered = _overlay_triggered or _blueprint_triggered

        # Research-first: knowledge-heavy topics need Wikipedia/deep_research before overlay.
        # Don't force show_overlay immediately — let Claude research then build the overlay.
        _needs_research = _overlay_triggered and any(
            w in last_user for w in _RESEARCH_TOPIC_WORDS
        )

        # Blueprints NEVER need research — Claude knows car/plane/rocket specs cold from
        # training data. Forcing research adds latency and often gives worse results since
        # Wikipedia specs are less detailed than what Claude already knows.
        # Blueprint path: force show_overlay directly with overlay_type='blueprint'.
        if _blueprint_triggered:
            _needs_research = False

        # Computer agent mode — autonomous screen control
        _computer_agent = any(phrase in last_user for phrase in _COMPUTER_AGENT_PHRASES)

        # Detect large sequential tasks (many questions / steps) — need high round budget
        _heavy_task = _computer_agent and any(
            w in last_user for w in (
                "question", "questions", "50", "40", "30", "quiz",
                "form", "assignment", "homework", "worksheet",
                # AP Classroom progress checks are always multi-question sequential tasks
                "progress check", "ap classroom", "ap world", "ap gov",
                "ap chem", "ap history", "ap government", "ap chemistry",
                "collegeboard", "college board", "myap", "unit",
            )
        )

        # Token budget per round: overlays / computer-agent need the full window.
        # Regular responses use the full budget so JARVIS is never cut off mid-thought.
        _loop_max_tokens = 4096 if (_overlay_triggered or _computer_agent) else min(budget, 2048)
        # Computer agent needs many rounds: screenshot → act → verify → repeat
        # Heavy tasks (50-question homework, long forms) need even more
        _MAX_ROUNDS = 80 if _heavy_task else (30 if _computer_agent else 6)

        if _overlay_triggered and not _needs_research:
            # Force show_overlay directly — Claude knows specs cold for well-known objects
            _overlay_tool_choice = {"type": "tool", "name": "show_overlay"}
            _active_tools = [t for t in TOOL_DEFINITIONS if t["name"] == "show_overlay"]
            if _blueprint_triggered:
                system = (
                    system
                    + "\n\n[BLUEPRINT MODE] Call show_overlay NOW with overlay_type='blueprint'. "
                    "Use real technical specs from your knowledge — exact engine figures, dimensions, "
                    "weight, top speed, production numbers, price. 7 items minimum. "
                    "model_type MUST match the subject (supercar/muscle_car/suv/pickup/plane/rocket/etc)."
                )
        elif _needs_research:
            # Knowledge-heavy: add a system note telling Claude to research first
            _overlay_tool_choice = {"type": "auto"}
            _active_tools = TOOL_DEFINITIONS
            _topic = " ".join(last_user.split()[:12])
            system = (
                system
                + f"\n\n[RESEARCH OVERLAY REQUIRED] The user wants a visual overlay about: '{_topic}'. "
                "You MUST: 1) First call get_wikipedia_summary with a specific query to get accurate data. "
                "2) Then call show_overlay using ONLY the real facts from step 1 — specific dates, real names, "
                "accurate events. Do NOT use vague summaries. Do NOT skip the research step. "
                "For blueprints: search '[specific model] specifications' then show actual specs as items."
            )
        elif _computer_agent:
            # Computer agent: force take_screenshot first, then let it act freely
            _overlay_tool_choice = {"type": "tool", "name": "take_screenshot"}
            _active_tools = TOOL_DEFINITIONS
            system = (
                system
                + "\n\n[COMPUTER AGENT MODE] Dylan wants you to DO THE WORK on his screen. "
                "FIRST call take_screenshot(for_control=True) to see the screen and get pixel coordinates. "
                "Then execute the task step by step: click fields, type answers, press keys, scroll as needed. "
                "Work autonomously — no asking for permission, no explaining. Just do it. "
                "After completing, take a final screenshot to verify and report 'Done.' in one line."
            )
        else:
            _overlay_tool_choice = {"type": "auto"}
            _active_tools = TOOL_DEFINITIONS

        for _round in range(_MAX_ROUNDS):
            try:
                response = self._claude.messages.create(
                    model=self.model,
                    system=system,
                    messages=self._claude_messages(),
                    tools=_active_tools,
                    tool_choice=_overlay_tool_choice,
                    max_tokens=_loop_max_tokens,
                )
                # After first round, stop forcing (let Claude continue naturally)
                _overlay_tool_choice = {"type": "auto"}
                _active_tools        = TOOL_DEFINITIONS
            except Exception as exc:
                console.print(f"[yellow]Claude error ({type(exc).__name__}): {exc}[/yellow]")
                if GROQ_API_KEY:
                    console.print("[dim]Falling back to Groq[/dim]")
                    yield from self._stream_with_tools()
                else:
                    msg = f"Connectivity issue — {type(exc).__name__}."
                    self._append_assistant_text(msg)
                    yield msg
                return

            # After first round, release the force — let Claude proceed naturally
            _overlay_tool_choice = {"type": "auto"}
            _active_tools = TOOL_DEFINITIONS

            tool_uses = [b for b in response.content if b.type == "tool_use"]

            # ── No tool calls → done ──────────────────────────────────────────
            if not tool_uses:
                text_parts = [b.text for b in response.content
                               if hasattr(b, "text") and b.text]
                text = "".join(text_parts)
                if text:
                    self._append_assistant_text(text)
                    self._recent_replies.append(text[:60])
                    yield text
                else:
                    yield from self._claude_final_stream(system, max_tokens=budget)
                return

            # ── Has tool calls ────────────────────────────────────────────────
            # Add assistant turn (raw blocks — SDK reserialises them correctly)
            self.messages.append({"role": "assistant", "content": response.content})

            # Execute every tool in this round
            tool_results = []
            executed: set[str] = set()
            for tu in tool_uses:
                executed.add(tu.name)
                console.print(f"[dim]  ⚙  {tu.name}({_summarise(dict(tu.input))})[/dim]")
                try:
                    result = self.tools_executor.execute(tu.name, dict(tu.input))
                except Exception as exc:
                    result = f"Tool error: {exc}"
                content = (
                    str(result) if not isinstance(result, list)
                    else "\n".join(
                        b.get("text", "")
                        for b in result
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                )
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": tu.id,
                    "content":     content,
                })

            # Add tool results as user turn
            self.messages.append({"role": "user", "content": tool_results})

            # Canned one-liners for simple controls
            if executed.issubset(_MUSIC_CANNED_TOOLS):
                reply = _MUSIC_CANNED_TOOLS.get(next(iter(executed)), "Done.")
                self._append_assistant_text(reply)
                self._recent_replies.append(reply[:60])
                yield reply
                return

            # Fire-and-done: action tools that open things / search / generate images.
            # The tool result itself is the confirmation — no second LLM call needed.
            # This cuts ~1-2s of latency from every web/youtube/open command.
            if executed.issubset(_FIRE_AND_DONE_TOOLS):
                best = next(
                    (tr["content"] for tr in tool_results
                     if tr.get("content") and not tr["content"].startswith("[Tool error")),
                    "Done.",
                )
                reply = best.split("\n")[0][:200]
                self._append_assistant_text(reply)
                self._recent_replies.append(reply[:60])
                yield reply
                return

            # Music play → short streaming comment (what's now playing)
            if executed & {"play_music", "play_playlist", "list_playlists",
                            "add_to_playlist", "create_playlist"}:
                yield from self._claude_final_stream(system, max_tokens=120)
                return

            # ── Overlay research pipeline fix ────────────────────────────────
            # If we just ran a research tool (Wikipedia / web) for an overlay
            # request, DO NOT drop into the text-only stream — Claude still
            # needs to call show_overlay with the research data. Loop back so
            # the next round gives Claude a tools-enabled API call.
            _research_tools = {
                "get_wikipedia_summary", "deep_research",
                "web_search_results", "web_search",
            }
            if _overlay_triggered and executed & _research_tools:
                # Force show_overlay in the next round — Claude has the data
                _overlay_tool_choice = {"type": "tool", "name": "show_overlay"}
                _active_tools = [t for t in TOOL_DEFINITIONS if t["name"] == "show_overlay"]
                continue   # back to top of for loop

            # Computer agent: keep looping — don't stop after screenshot or a click
            # release the forced first-tool after round 0 so Claude can sequence freely
            if _computer_agent:
                _overlay_tool_choice = {"type": "auto"}
                continue   # keep the agent loop going

            # Everything else: stream with the full budget
            yield from self._claude_final_stream(system, max_tokens=budget)
            return

        # Hit max rounds without finishing — force a clean response
        yield from self._claude_final_stream(system, max_tokens=budget)

    def _auto_remember(self, user_input: str) -> None:
        """
        Silently extract and save memorable facts from what Dylan just said.
        Runs in the background — never blocks the response.
        Looks for personal facts: preferences, goals, relationships, habits, events.
        """
        import threading
        def _extract():
            try:
                from brain.memory import save_fact
                check = self._claude.messages.create(
                    model=self.model,
                    system=(
                        "Extract memorable personal facts from this message. "
                        "Output ONLY a JSON array of short, specific fact strings about the user (Dylan Roe). "
                        "Examples: [\"Dylan's revenue goal is $10k\", \"Dylan has AP Chemistry on Thursdays\", "
                        "\"Dylan's gym playlist is called 'gym'\"] "
                        "Rules: "
                        "1. Facts must be about Dylan specifically, not general knowledge. "
                        "2. Only include things worth remembering for future conversations. "
                        "3. Be specific — 'Dylan likes rap' is weak, 'Dylan's favourite genre is trap rap' is useful. "
                        "4. Include goals, deadlines, preferences, relationships, habits, events. "
                        "5. If nothing memorable, output: []"
                    ),
                    messages=[{"role": "user", "content": user_input}],
                    max_tokens=250,
                )
                import json as _json
                text = check.content[0].text.strip() if check.content else "[]"
                # Find the JSON array in the response
                start = text.find("[")
                end   = text.rfind("]") + 1
                if start >= 0 and end > start:
                    facts = _json.loads(text[start:end])
                    for fact in facts:
                        if isinstance(fact, str) and len(fact) > 5:
                            save_fact(fact)
            except Exception:
                pass  # Never let memory extraction break the main flow
        threading.Thread(target=_extract, daemon=True, name="auto-memory").start()

    def _claude_final_stream(
        self, system: str, max_tokens: int = 1024
    ) -> Generator[str, None, None]:
        """Streaming response from Claude — no tools, pure text."""
        try:
            full: list[str] = []
            with self._claude.messages.stream(
                model=self.model,
                system=system,
                messages=self._claude_messages(),
                max_tokens=max_tokens,
            ) as stream:
                for chunk in stream.text_stream:
                    full.append(chunk)
                    yield chunk
            text = "".join(full)
            self._append_assistant_text(text)
            if text:
                self._recent_replies.append(text[:60])
        except Exception as exc:
            console.print(f"[yellow]Claude stream error: {exc}[/yellow]")
            err = f"Stream error — {type(exc).__name__}."
            self._append_assistant_text(err)
            yield err

    def _claude_messages(self) -> list[dict]:
        """
        Convert self.messages to Claude's required format.
        Handles both plain string content and block lists (tool use / tool results).

        Slim overlay tool results to prevent context overflow.  A full show_overlay
        call can contain 20+ spec items (1-3 KB each) that the model doesn't need
        to re-read in subsequent turns — only the confirmation string matters.
        """
        _MAX_TOOL_RESULT_CHARS = 300   # keep only the first N chars of tool results

        out = []
        for m in self.messages:
            content = m["content"]
            role    = m["role"]
            if isinstance(content, list):
                # Block list — slim any tool_result blocks that are large
                slimmed = []
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_result"
                        and isinstance(block.get("content"), str)
                        and len(block["content"]) > _MAX_TOOL_RESULT_CHARS
                    ):
                        block = dict(block,
                                     content=block["content"][:_MAX_TOOL_RESULT_CHARS]
                                             + "…[trimmed]")
                    slimmed.append(block)
                out.append({"role": role, "content": slimmed})
            else:
                out.append({"role": role, "content": str(content) if content else ""})
        return out

    def _append_assistant_text(self, text: str) -> None:
        self.messages.append({"role": "assistant", "content": text})

    # ── Groq path (original — preserved as fallback) ───────────────────────────

    @staticmethod
    def _is_fast_query(text: str) -> bool:
        t = text.lower().strip()
        words = t.split()
        if len(words) > 14:
            return False
        tool_hints = {
            "screen", "open", "play", "pause", "stop", "search", "find",
            "research", "study", "exam", "calendar", "remind", "timer",
            "weather", "file", "read", "write", "create", "run", "command",
            "music", "song", "volume", "note", "clipboard", "click", "type",
            "screenshot", "look", "this", "see", "show", "tabs", "chrome",
            "youtube", "google", "website", "url", "calculate",
            "spotify", "discord", "slack", "notion", "vscode", "cursor",
            "safari", "firefox", "arc", "finder", "terminal", "figma",
            "xcode", "zoom", "messages", "mail", "notes", "reminders",
            "launch", "start", "close", "quit", "hide", "switch", "focus",
            "skip", "next", "previous", "mute", "unmute", "restart",
            "download", "upload", "copy", "paste",
            "translate", "definition", "define", "convert", "news", "headlines",
            "do not disturb", "dnd", "flip", "random",
            "playlist", "add to", "remind", "pomodoro",
            # AR / visual
            "visual", "timeline", "diagram", "overlay", "concept", "flashcard",
            "holding", "bottle", "camera", "identify", "what is this",
            # Blueprint / hologram / 3D
            "blueprint", "hologram", "3d", "specs", "technical", "anatomy",
            "anatomy of", "model of", "render", "display", "pull up", "bring up",
            "show me", "show the", "show a",
        }
        return not any(hint in t for hint in tool_hints)

    @staticmethod
    def _slim_history(msgs: list[dict], max_chars: int = 400) -> list[dict]:
        """Return a copy of msgs with long content abbreviated.

        Tool messages (role='tool') from get_news / search_web / show_overlay can
        be thousands of tokens.  We keep the first `max_chars` characters so
        the model knows the tool ran without burning the entire quota.

        Also slim assistant messages that carry large tool_call arguments — e.g.
        show_overlay with 20+ spec items.  The model doesn't need to re-read the
        full item list; knowing the call was made is enough.
        """
        _TC_ARG_LIMIT = 120  # characters to keep from tool_call arguments
        out = []
        for m in msgs:
            if m.get("role") == "tool":
                content = m.get("content", "")
                if len(content) > max_chars:
                    m = dict(m, content=content[:max_chars] + "…[truncated]")
            elif m.get("role") == "assistant" and m.get("tool_calls"):
                # Slim large tool-call argument blobs
                slimmed_tcs = []
                changed = False
                for tc in m["tool_calls"]:
                    args_str = tc.get("function", {}).get("arguments", "")
                    if len(args_str) > _TC_ARG_LIMIT:
                        tc = dict(tc, function=dict(tc["function"],
                                  arguments=args_str[:_TC_ARG_LIMIT] + "…"))
                        changed = True
                    slimmed_tcs.append(tc)
                if changed:
                    m = dict(m, tool_calls=slimmed_tcs)
            out.append(m)
        return out

    def _make_stream(self, model: str, with_tools: bool = True, emergency: bool = False):
        """Build and return a streaming chat completion.

        emergency=True → ultra-stripped context (last 2 msgs only, fast prompt) for
        last-resort 8b calls when the normal 8b payload still hits 413.
        """
        from openai import OpenAI  # noqa: already imported at class init
        fast_model = "llama-3.1-8b-instant"
        is_primary = (model == GROQ_MODEL)
        is_fast    = (model == fast_model)

        # Detect long-form generation requests (study/flashcard/practice) that need more tokens
        _last_msg_lower = next(
            (m.get("content","").lower() for m in reversed(self.messages) if m.get("role") == "user"), ""
        )
        _is_study_gen = _last_msg_lower.startswith("[study]")

        if emergency:
            # Absolute minimum — just the current user turn.  Avoids any 413.
            system, history, max_tok = get_fast_prompt(), self._slim_history(self.messages[-2:]), 256
        elif is_primary:
            # Use the condensed prompt for Groq — the full prompt is ~3000 tokens
            # and burns through the free-tier token-per-minute quota before the
            # response even starts. Condensed = ~600 tokens = 5x more headroom.
            system, history, max_tok = get_mid_prompt(), self._slim_history(self.messages[-12:]), 1024
        elif is_fast:
            system, history, max_tok = get_fast_prompt(), self._slim_history(self.messages[-6:]), 512
        else:
            system, history, max_tok = get_mid_prompt(), self._slim_history(self.messages[-10:]), 768

        # Study generation needs more tokens regardless of model tier
        if _is_study_gen:
            max_tok = max(max_tok, 2048)

        all_messages = [{"role": "system", "content": system}] + history

        note = self._get_recent_replies_note()
        if note:
            all_messages[0]["content"] += note

        kwargs = dict(
            model=model,
            messages=all_messages,
            stream=True,
            max_tokens=max_tok,
            temperature=0.7,
        )
        if with_tools:
            if is_primary:
                kwargs["tools"] = get_openai_tools()
            elif is_fast:
                kwargs["tools"] = get_8b_tools()   # compact set — avoids 413 payload errors
            else:
                kwargs["tools"] = get_core_tools()  # scout gets the fuller set
            last_user = next(
                (m["content"] for m in reversed(history) if m.get("role") == "user"), ""
            ).lower()
            # Overlay requests → force show_overlay with only that tool.
            # Small models ignore tool_choice when many tools are present,
            # so we narrow the list to just show_overlay for this call.
            _overlay_triggered = (
                any(phrase in last_user for phrase in _OVERLAY_FORCE_PHRASES)
                or (any(w in last_user for w in _OVERLAY_SINGLE_WORDS) and len(last_user.split()) > 5)
                or (any(w in last_user for w in _OVERLAY_VISUAL_WORDS)
                    and any(w in last_user for w in {"show", "give", "make", "create", "draw", "build"}))
            )
            # Blueprint trigger: "show me the Bugatti", "pull up the F-22", etc.
            # Also fires on context-only follow-ups ("pull it up") if we remember a subject.
            _bp_phrase_match = (
                any(phrase in last_user for phrase in _BLUEPRINT_TRIGGER_PHRASES)
                and not any(cancel in last_user for cancel in _BLUEPRINT_CANCEL_WORDS)
                and len(last_user.split()) >= 3
            )
            _bp_context_match = (
                any(t in last_user for t in _BLUEPRINT_CONTEXT_TRIGGERS)
                and not any(cancel in last_user for cancel in _BLUEPRINT_CANCEL_WORDS)
                and bool(self._last_blueprint_subject)
                and len(last_user.split()) <= 5
            )
            _blueprint_triggered = _bp_phrase_match or _bp_context_match

            if _blueprint_triggered:
                # Extract and remember the subject for future context follow-ups
                extracted = _extract_blueprint_subject(last_user)
                if extracted:
                    self._last_blueprint_subject = extracted
                _subject = self._last_blueprint_subject or "the subject"

                _overlay_triggered = True
                # Build object-type-aware blueprint instructions
                _subj_lower = _subject.lower()
                _is_vehicle = any(w in _subj_lower for w in (
                    "car","bugatti","ferrari","lambo","porsche","mclaren","koenigsegg","mustang",
                    "maserati","pagani","bmw","mercedes","tesla","corvette","camaro","challenger",
                    "plane","rocket","helicopter","motorcycle","ship","jet","spacecraft","falcon"))
                _is_tech = any(w in _subj_lower for w in (
                    "iphone","phone","macbook","laptop","ipad","tablet","watch","airpods",
                    "android","samsung","pixel","computer","tv","drone","camera","gaming","ps5","xbox"))
                _is_anatomy = any(w in _subj_lower for w in (
                    "heart","brain","lung","skull","spine","dna","atom","cell","organ","blood"))
                _is_space = any(w in _subj_lower for w in (
                    "planet","moon","solar","galaxy","star","black hole","mars","saturn","jupiter","earth"))
                _is_animal = any(w in _subj_lower for w in (
                    "lion","tiger","wolf","shark","whale","eagle","dinosaur","horse","elephant","gorilla","dragon"))

                if _is_vehicle:
                    _bp_sections = (
                        "ENGINE (type, displacement, cylinders, power, torque, compression, redline), "
                        "PERFORMANCE (0-60 mph, 0-100 kph, top speed, quarter mile, lateral G, braking 60-0), "
                        "DRIVETRAIN (transmission, drive type, differential, gear ratios), "
                        "DIMENSIONS (length, width, height, wheelbase, track width F/R), "
                        "WEIGHT (curb weight, weight distribution, chassis material), "
                        "SUSPENSION (front type, rear type, spring rate, dampers), "
                        "AERO (downforce, drag coefficient, active aero elements), "
                        "PRODUCTION (year range, units built, base MSRP, country of origin)."
                    )
                elif _is_tech:
                    _bp_sections = (
                        "CHIP (processor name, cores, clock speed, GPU, neural engine), "
                        "DISPLAY (size, resolution, refresh rate, technology, brightness), "
                        "CAMERA (main sensor MP, aperture, zoom, video, front camera), "
                        "BATTERY (capacity mAh, charging speed, wireless charging, battery life), "
                        "CONNECTIVITY (5G/WiFi/Bluetooth versions, ports, NFC), "
                        "STORAGE (options, RAM, base storage), "
                        "BUILD (dimensions, weight, material, IP rating, colors), "
                        "PRICE (launch price, release date, variants)."
                    )
                elif _is_anatomy:
                    _bp_sections = (
                        "TYPE (organ/molecule/structure type, system it belongs to), "
                        "DIMENSIONS (size, length, width, volume, weight), "
                        "COMPOSITION (primary materials, tissues, elements, compounds), "
                        "FUNCTION (primary function, how it works, key processes), "
                        "STRUCTURE (main sub-parts, chambers, regions, layers), "
                        "PHYSIOLOGY (how it operates, cycles, rates, pressures, flows), "
                        "PATHOLOGY (common conditions, diseases, failure modes), "
                        "FACTS (interesting biology facts, evolutionary context, medical significance)."
                    )
                elif _is_space:
                    _bp_sections = (
                        "TYPE (classification, stellar or planetary type, composition), "
                        "DIMENSIONS (diameter, mass, volume, surface area), "
                        "GRAVITY (surface gravity vs Earth, escape velocity), "
                        "ORBIT (orbital period, distance from sun/center, orbital speed), "
                        "ATMOSPHERE (composition, pressure, temperature range), "
                        "MOONS (number of moons, notable satellites), "
                        "SURFACE (terrain, surface features, geology), "
                        "EXPLORATION (missions sent, first landing/flyby, current missions)."
                    )
                elif _is_animal:
                    _bp_sections = (
                        "CLASS (scientific classification, family, genus, species), "
                        "SIZE (length, height, wingspan/spread, weight range), "
                        "SPEED (top speed, sprint vs endurance, swimming/flying speed), "
                        "HABITAT (native regions, biomes, range, migration), "
                        "DIET (prey, hunting method, daily consumption, foraging), "
                        "PHYSIOLOGY (heart rate, body temperature, key adaptations), "
                        "BEHAVIOR (social structure, reproduction, lifespan, communication), "
                        "CONSERVATION (status, population, main threats, protected areas)."
                    )
                else:
                    _bp_sections = (
                        "OVERVIEW (what it is, category, origin/discovery), "
                        "DIMENSIONS (size, scale, key measurements), "
                        "COMPOSITION (materials, components, structure), "
                        "FUNCTION (purpose, how it works, key capabilities), "
                        "PERFORMANCE (speed, power, output, key metrics), "
                        "HISTORY (origin, creator/inventor, year, evolution), "
                        "VARIANTS (models, versions, configurations), "
                        "FACTS (interesting details, records, notable uses)."
                    )

                all_messages[0]["content"] += (
                    f"\n\n[BLUEPRINT MODE] Generate an ULTRA-DETAILED blueprint overlay for: {_subject}. "
                    "You MUST call show_overlay with overlay_type='blueprint'. "
                    f"Generate 20-24 items in these sections: {_bp_sections} "
                    "label=short spec name ALL CAPS (e.g. 'CHIP', 'DISPLACEMENT', 'WEIGHT'). "
                    "title=exact value with units (e.g. 'A18 Pro', '7,993 cc', '1,481 kg'). "
                    "detail=2-3 sentences of deep technical context spoken when clicked. "
                    "amber=key performance figure, red=extreme/record stat, purple=price/production, cyan=everything else. "
                    "model_type MUST exactly match the subject type from the allowed list. "
                    "DO NOT output any text — ONLY call show_overlay."
                )
                max_tok = 4096

            if _overlay_triggered:
                overlay_tool = [t for t in get_openai_tools()
                                if t["function"]["name"] == "show_overlay"]
                kwargs["tools"] = overlay_tool
                kwargs["tool_choice"] = {"type": "function",
                                         "function": {"name": "show_overlay"}}
                kwargs["max_tokens"] = max_tok
            elif any(w in last_user for w in _MUSIC_PAUSE_WORDS):
                kwargs["tool_choice"] = {"type": "function", "function": {"name": "pause_music"}}
            elif any(w in last_user for w in _MUSIC_SKIP_WORDS):
                kwargs["tool_choice"] = {"type": "function", "function": {"name": "next_track"}}
            elif any(w in last_user for w in _MUSIC_PREV_WORDS):
                kwargs["tool_choice"] = {"type": "function", "function": {"name": "previous_track"}}
            elif any(w in last_user for w in _MUSIC_PLAY_WORDS):
                kwargs["tool_choice"] = {"type": "function", "function": {"name": "play_music"}}
            elif any(w in last_user for w in {
                "timer", "set a timer", "set timer", "start a timer", "start timer",
                "countdown", "count down", "set an alarm", "set alarm",
            }):
                # Force set_timer tool — models prefer run_command otherwise
                timer_tool = [t for t in get_openai_tools()
                              if t["function"]["name"] == "set_timer"]
                if timer_tool:
                    kwargs["tools"] = timer_tool
                    kwargs["tool_choice"] = {"type": "function", "function": {"name": "set_timer"}}
            else:
                kwargs["tool_choice"] = "auto"

        return self.client.chat.completions.create(**kwargs)

    def _stream_with_tools(self) -> Generator[str, None, None]:
        from openai import RateLimitError, BadRequestError, APIStatusError

        # Compute blueprint intent ONCE here so recovery logic can reference it
        _last_user = next(
            (m["content"] for m in reversed(self.messages) if m.get("role") == "user"), ""
        ).lower()
        _bp_phrase = (
            any(phrase in _last_user for phrase in _BLUEPRINT_TRIGGER_PHRASES)
            and not any(cancel in _last_user for cancel in _BLUEPRINT_CANCEL_WORDS)
            and len(_last_user.split()) >= 3
        )
        _bp_context = (
            any(t in _last_user for t in _BLUEPRINT_CONTEXT_TRIGGERS)
            and not any(cancel in _last_user for cancel in _BLUEPRINT_CANCEL_WORDS)
            and bool(self._last_blueprint_subject)
            and len(_last_user.split()) <= 5
        )
        _wants_blueprint = _bp_phrase or _bp_context

        _tool_rounds = 0
        _MAX_TOOL_ROUNDS = 4
        while _tool_rounds < _MAX_TOOL_ROUNDS:
            stream = None
            models = [GROQ_MODEL] + _FALLBACK_MODELS
            for model in models:
                try:
                    stream = self._make_stream(model)
                    if model != GROQ_MODEL:
                        console.print(f"[dim]Falling back to {model}[/dim]")
                    break
                except RateLimitError:
                    console.print(f"[yellow]Rate limit on {model}[/yellow]")
                    if model != models[-1]:
                        continue
                    yield _RATE_LIMIT_MSG
                    self.messages.append({"role": "assistant", "content": _RATE_LIMIT_MSG})
                    self._recent_replies.append(_RATE_LIMIT_MSG[:80])
                    return
                except BadRequestError as exc:
                    err_str = str(exc)
                    if "model_decommissioned" in err_str or "model_not_active" in err_str:
                        continue
                    console.print(f"[red]API error: {exc}[/red]")
                    yield _BAD_REQUEST_MSG
                    self.messages.append({"role": "assistant", "content": _BAD_REQUEST_MSG})
                    return
                except APIStatusError as exc:
                    if exc.status_code == 413:
                        console.print(f"[yellow]Request too large for {model}[/yellow]")
                        if model == models[-1]:
                            # Last resort: emergency mode — 2 messages, tiny prompt.
                            try:
                                stream = self._make_stream(model, emergency=True)
                                console.print("[dim]Emergency slim context — 2 msgs[/dim]")
                                break
                            except Exception:
                                yield from self._plain_stream(overloaded=True)
                                return
                        continue
                    raise

            if stream is None:
                return

            text_chunks:    list[str]       = []
            tool_calls_raw: dict[int, dict] = {}
            finish_reason                   = None

            try:
                for chunk in stream:
                    choice = chunk.choices[0]
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason
                    delta = choice.delta
                    if delta.content:
                        text_chunks.append(delta.content)
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            i = tc.index
                            if i not in tool_calls_raw:
                                tool_calls_raw[i] = {"id": "", "name": "", "arguments": ""}
                            if tc.id:
                                tool_calls_raw[i]["id"] = tc.id
                            if tc.function:
                                if tc.function.name:
                                    tool_calls_raw[i]["name"] += tc.function.name
                                if tc.function.arguments:
                                    tool_calls_raw[i]["arguments"] += tc.function.arguments
            except Exception as exc:
                err = str(exc)
                if ("Failed to call a function" in err or "failed_generation" in err
                        or "tool call validation failed" in err or "not in request.tools" in err):
                    console.print("[dim]Tool call JSON failed — retrying with minimal tools[/dim]")
                    yield from self._minimal_tool_retry()
                    return
                console.print(f"[yellow]Stream error: {exc}[/yellow]")
                yield f" (error: {exc})"
                break

            full_text = "".join(text_chunks)

            # Groq sometimes streams tool calls but sends finish_reason="stop" — treat
            # any response that has tool_calls data as a tool call regardless of finish_reason.
            if tool_calls_raw and finish_reason != "tool_calls":
                console.print(f"[dim]Groq finish_reason={finish_reason!r} but tool_calls present — treating as tool_calls[/dim]")
                finish_reason = "tool_calls"

            if finish_reason != "tool_calls":
                # Blueprint recovery: we forced tool_choice but Groq returned plain text.
                # Try a direct non-streaming retry with a clean focused prompt.
                if _wants_blueprint and _tool_rounds == 0:
                    recovered = False
                    for chunk in self._blueprint_recovery(_last_user):
                        yield chunk
                        recovered = True
                    if recovered:
                        break

                # Strip any raw <function=...> XML that quantized models emit as text
                clean_chunks = [_clean_text(c) for c in text_chunks]
                clean_full   = "".join(clean_chunks)
                self.messages.append({"role": "assistant", "content": clean_full or full_text})
                if clean_full:
                    self._recent_replies.append(clean_full.strip().lower()[:80])
                yield from (c for c in clean_chunks if c)
                break

            _tool_rounds += 1
            tool_calls_formatted = [
                {
                    "id":   tool_calls_raw[i]["id"],
                    "type": "function",
                    "function": {
                        "name":      tool_calls_raw[i]["name"],
                        "arguments": tool_calls_raw[i]["arguments"],
                    },
                }
                for i in sorted(tool_calls_raw.keys())
            ]

            self.messages.append({
                "role":       "assistant",
                "content":    full_text or None,
                "tool_calls": tool_calls_formatted,
            })

            for tc in tool_calls_formatted:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}

                console.print(f"[dim]  ⚙  {name}({_summarise(args)})[/dim]")
                result = self.tools_executor.execute(name, args)

                if isinstance(result, list):
                    content = "\n".join(
                        b.get("text", "")
                        for b in result
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                else:
                    content = str(result)

                # Cap tool results to avoid bloating context window
                if len(content) > 800:
                    content = content[:800] + "…[truncated]"

                self.messages.append({
                    "role":         "tool",
                    "tool_call_id": tc["id"],
                    "content":      content,
                })

            executed = {tc["function"]["name"] for tc in tool_calls_formatted}
            if executed.issubset(_MUSIC_CANNED_TOOLS):
                reply = _MUSIC_CANNED_TOOLS.get(list(executed)[0], "Done.")
                self.messages.append({"role": "assistant", "content": reply})
                self._recent_replies.append(reply[:80])
                yield reply
                break
            if executed.issubset(_FIRE_AND_DONE_TOOLS):
                last_result = next(
                    (m["content"] for m in reversed(self.messages)
                     if m.get("role") == "tool" and m.get("content")),
                    "Done.",
                )
                reply = str(last_result).split("\n")[0][:200]
                self.messages.append({"role": "assistant", "content": reply})
                self._recent_replies.append(reply[:80])
                yield reply
                break
            if executed & {"play_music", "play_playlist", "list_playlists",
                            "add_to_playlist", "create_playlist"}:
                yield from self._plain_stream()
                break

    def _blueprint_recovery(self, last_user: str) -> Generator[str, None, None]:
        """
        Fallback when forced tool_choice still returned plain text.
        Makes a short non-streaming call with a focused single-tool prompt.
        Yields the blueprint result string if successful, nothing on failure.
        """
        subject = self._last_blueprint_subject or _extract_blueprint_subject(last_user) or last_user.strip()
        if not subject or len(subject) < 2:
            return

        console.print(f"[dim]Blueprint recovery for: {subject!r}[/dim]")
        overlay_tool = [t for t in get_openai_tools() if t["function"]["name"] == "show_overlay"]

        # Determine object category for section guidance
        _sl = subject.lower()
        _rec_is_vehicle = any(w in _sl for w in (
            "car","bugatti","ferrari","lambo","porsche","mclaren","koenigsegg","mustang",
            "maserati","pagani","bmw","mercedes","tesla","corvette","camaro","challenger",
            "plane","rocket","helicopter","motorcycle","ship","jet","spacecraft","falcon"))
        _rec_is_tech = any(w in _sl for w in (
            "iphone","phone","macbook","laptop","ipad","tablet","watch","airpods",
            "android","samsung","pixel","computer","tv","drone","camera","gaming","ps5","xbox"))
        _rec_is_anatomy = any(w in _sl for w in (
            "heart","brain","lung","skull","spine","dna","atom","cell","organ"))
        _rec_is_space = any(w in _sl for w in (
            "planet","moon","solar","galaxy","star","black hole","mars","saturn","jupiter","earth"))
        _rec_is_animal = any(w in _sl for w in (
            "lion","tiger","wolf","shark","whale","eagle","dinosaur","horse","elephant","gorilla"))

        if _rec_is_vehicle:
            _rec_sections = (
                "ENGINE (type, displacement, cylinders, power, torque, redline), "
                "PERFORMANCE (0-60 mph, top speed, quarter mile, lateral G), "
                "DRIVETRAIN (transmission, drive type, differential), "
                "DIMENSIONS (length, width, height, wheelbase), "
                "WEIGHT (curb weight, weight distribution, chassis), "
                "AERO (downforce, drag coefficient), "
                "PRODUCTION (year, units built, MSRP, origin)."
            )
            _rec_model_hint = "supercar/muscle_car/car/suv/pickup/plane/rocket/etc"
        elif _rec_is_tech:
            _rec_sections = (
                "CHIP (processor, cores, GPU, neural engine), "
                "DISPLAY (size, resolution, refresh rate, tech), "
                "CAMERA (main MP, aperture, video, front), "
                "BATTERY (capacity, charging speed, life), "
                "CONNECTIVITY (5G/WiFi/BT, ports), "
                "STORAGE (RAM, storage options), "
                "BUILD (dimensions, weight, IP rating), "
                "PRICE (launch price, release date)."
            )
            _rec_model_hint = "phone/tablet/laptop/computer/watch/headphones/tv/gaming/drone/robot"
        elif _rec_is_anatomy:
            _rec_sections = (
                "TYPE (organ type, body system), "
                "DIMENSIONS (size, weight, volume), "
                "COMPOSITION (tissues, materials, structure), "
                "FUNCTION (primary function, key processes), "
                "STRUCTURE (main parts, chambers, regions), "
                "PHYSIOLOGY (rates, pressures, cycles), "
                "PATHOLOGY (common conditions/diseases), "
                "FACTS (biology facts, medical significance)."
            )
            _rec_model_hint = "heart/brain/skull/lungs/dna/atom"
        elif _rec_is_space:
            _rec_sections = (
                "TYPE (classification, composition), "
                "DIMENSIONS (diameter, mass), "
                "GRAVITY (surface gravity, escape velocity), "
                "ORBIT (orbital period, distance from sun), "
                "ATMOSPHERE (composition, temperature), "
                "MOONS (count, notable satellites), "
                "SURFACE (terrain, geology), "
                "EXPLORATION (missions, discoveries)."
            )
            _rec_model_hint = "planet/moon/solar_system/galaxy/black_hole"
        elif _rec_is_animal:
            _rec_sections = (
                "CLASS (species, family, genus), "
                "SIZE (length, height, weight), "
                "SPEED (top speed, hunting speed), "
                "HABITAT (range, biomes), "
                "DIET (prey, hunting method), "
                "PHYSIOLOGY (key adaptations, senses), "
                "BEHAVIOR (social structure, reproduction), "
                "CONSERVATION (status, population, threats)."
            )
            _rec_model_hint = "lion/tiger/wolf/shark/whale/eagle/dinosaur/horse/elephant/gorilla"
        else:
            _rec_sections = (
                "OVERVIEW (what it is, origin), DIMENSIONS (size, scale), "
                "COMPOSITION (materials, structure), FUNCTION (purpose, how it works), "
                "PERFORMANCE (key metrics, capabilities), HISTORY (creator, year), "
                "VARIANTS (versions, models), FACTS (records, notable details)."
            )
            _rec_model_hint = "object/default/building/weapon/shoe/tree"

        recovery_messages = [
            {"role": "system", "content": (
                f"You are a technical database. Generate an ULTRA-DETAILED blueprint overlay for: {subject}. "
                "You MUST call show_overlay with overlay_type='blueprint'. "
                "Generate 20-24 items organized into sections using the category field. "
                f"Use these sections: {_rec_sections} "
                "label=short spec name ALL CAPS. title=exact value with units. "
                "detail=2-3 technical sentences spoken aloud when clicked. "
                "amber=key perf stat, red=extreme/record, purple=price/production, cyan=everything else. "
                f"model_type MUST be one of: {_rec_model_hint}. "
                "DO NOT output any text. ONLY call the show_overlay tool."
            )},
            {"role": "user", "content": f"Ultra-detailed blueprint for {subject}"},
        ]

        from openai import RateLimitError
        # Try models in order — prefer tool-use-optimised models for recovery
        _recovery_models = [
            GROQ_MODEL,
            "llama3-groq-8b-8192-tool-use-preview",  # Groq fine-tuned for tools
            "meta-llama/llama-4-scout-17b-16e-instruct",
            "llama-3.1-8b-instant",
        ]
        for _rm in _recovery_models:
            try:
                console.print(f"[dim]Blueprint recovery with {_rm}…[/dim]")
                response = self.client.chat.completions.create(
                    model=_rm,
                    messages=recovery_messages,
                    tools=overlay_tool,
                    tool_choice={"type": "function", "function": {"name": "show_overlay"}},
                    max_tokens=4096,
                    temperature=0.2,
                    stream=False,
                )
                msg = response.choices[0].message
                tool_calls = getattr(msg, "tool_calls", None) or []
                if not tool_calls:
                    console.print(f"[yellow]Blueprint recovery ({_rm}): no tool calls — trying next[/yellow]")
                    continue

                for tc in tool_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}
                    console.print(f"[dim]  ⚙  show_overlay (recovery/{_rm}) ({_summarise(args)})[/dim]")
                    result = self.tools_executor.execute("show_overlay", args)
                    reply = str(result).split("\n")[0][:200]
                    self.messages.append({"role": "assistant", "content": reply})
                    self._recent_replies.append(reply[:80])
                    yield reply
                return  # success — stop trying models
            except RateLimitError:
                console.print(f"[yellow]Blueprint recovery ({_rm}): rate limited[/yellow]")
                continue
            except Exception as exc:
                console.print(f"[yellow]Blueprint recovery ({_rm}) failed: {exc}[/yellow]")
                continue
        console.print("[yellow]Blueprint recovery: all models exhausted[/yellow]")

    def _minimal_tool_retry(self) -> Generator[str, None, None]:
        from openai import RateLimitError
        from brain.tools import get_minimal_tools
        fast_model = "llama-3.1-8b-instant"
        system = get_fast_prompt()
        # Use only the last 6 messages — the 8B model has a very tight TPM limit
        messages = [{"role": "system", "content": system}] + self.messages[-6:]
        try:
            last_u = next(
                (m["content"] for m in reversed(self.messages) if m.get("role") == "user"), ""
            ).lower()
            if any(w in last_u for w in _MUSIC_PAUSE_WORDS):
                _forced = {"type": "function", "function": {"name": "pause_music"}}
            elif any(w in last_u for w in _MUSIC_SKIP_WORDS):
                _forced = {"type": "function", "function": {"name": "next_track"}}
            elif any(w in last_u for w in _MUSIC_PREV_WORDS):
                _forced = {"type": "function", "function": {"name": "previous_track"}}
            elif any(w in last_u for w in _MUSIC_PLAY_WORDS):
                _forced = {"type": "function", "function": {"name": "play_music"}}
            elif any(w in last_u for w in ("remind me", "set a reminder", "add a reminder", "reminder")):
                _forced = {"type": "function", "function": {"name": "add_reminder"}}
            elif any(w in last_u for w in ("quick note", "note:", "jot this", "save a note", "write this down")):
                _forced = {"type": "function", "function": {"name": "quick_note"}}
            elif any(w in last_u for w in ("set a timer", "set timer", "timer for", "start a timer")):
                _forced = {"type": "function", "function": {"name": "set_timer"}}
            elif any(w in last_u for w in ("my reminders", "list reminders", "what are my reminders", "show reminders")):
                _forced = {"type": "function", "function": {"name": "get_reminders"}}
            else:
                _forced = "auto"
            stream = self.client.chat.completions.create(
                model=fast_model, messages=messages, stream=True,
                max_tokens=512, temperature=0.7,
                tools=get_minimal_tools(), tool_choice=_forced,
            )
            text_chunks:    list[str]       = []
            tool_calls_raw: dict[int, dict] = {}
            finish_reason                   = None
            for chunk in stream:
                choice = chunk.choices[0]
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                delta = choice.delta
                if delta.content:
                    text_chunks.append(delta.content)
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        i = tc.index
                        if i not in tool_calls_raw:
                            tool_calls_raw[i] = {"id": "", "name": "", "arguments": ""}
                        if tc.id: tool_calls_raw[i]["id"] = tc.id
                        if tc.function:
                            if tc.function.name:      tool_calls_raw[i]["name"]      += tc.function.name
                            if tc.function.arguments: tool_calls_raw[i]["arguments"] += tc.function.arguments

            full_text = "".join(text_chunks)
            if finish_reason != "tool_calls":
                clean_chunks = [_clean_text(c) for c in text_chunks]
                clean_full   = "".join(clean_chunks)
                self.messages.append({"role": "assistant", "content": clean_full or full_text})
                yield from (c for c in clean_chunks if c)
                return

            tool_calls_formatted = [
                {"id": tool_calls_raw[i]["id"], "type": "function",
                 "function": {"name": tool_calls_raw[i]["name"],
                              "arguments": tool_calls_raw[i]["arguments"]}}
                for i in sorted(tool_calls_raw.keys())
            ]
            self.messages.append({
                "role": "assistant", "content": full_text or None,
                "tool_calls": tool_calls_formatted,
            })
            for tc in tool_calls_formatted:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}
                console.print(f"[dim]  ⚙  {name}({_summarise(args)})[/dim]")
                result = self.tools_executor.execute(name, args)
                content = str(result) if not isinstance(result, list) else \
                    "\n".join(b.get("text", "") for b in result
                              if isinstance(b, dict) and b.get("type") == "text")
                if len(content) > 800:
                    content = content[:800] + "…[truncated]"
                self.messages.append({"role": "tool", "tool_call_id": tc["id"], "content": content})

            executed = {tc["function"]["name"] for tc in tool_calls_formatted}
            if executed.issubset(_MUSIC_CANNED_TOOLS):
                reply = _MUSIC_CANNED_TOOLS.get(list(executed)[0], "Done.")
                self.messages.append({"role": "assistant", "content": reply})
                yield reply
                return
            # Fire-and-done: tool result IS the reply — skip extra LLM call to avoid
            # rate-limiting loops where 8b calls another tool instead of answering.
            if executed.issubset(_FIRE_AND_DONE_TOOLS):
                last_tool_msg = next(
                    (m["content"] for m in reversed(self.messages) if m.get("role") == "tool"),
                    "Done."
                )
                reply = last_tool_msg[:400]  # cap at 400 chars for TTS
                self.messages.append({"role": "assistant", "content": reply})
                self._recent_replies.append(reply[:80])
                yield reply
                return
            if executed & {"play_music", "play_playlist", "list_playlists",
                            "add_to_playlist", "create_playlist"}:
                yield from self._plain_stream(fast_model)
                return
            yield from self._plain_stream(fast_model)

        except Exception as exc:
            console.print(f"[yellow]Minimal retry failed: {exc}[/yellow]")
            last_user = next(
                (m["content"] for m in reversed(self.messages) if m.get("role") == "user"), ""
            ).lower()
            if any(w in last_user for w in _MUSIC_PAUSE_WORDS):
                self.tools_executor.execute("pause_music", {})
                reply = "Paused."
            elif any(w in last_user for w in _MUSIC_SKIP_WORDS):
                self.tools_executor.execute("next_track", {})
                reply = "Next."
            elif any(w in last_user for w in _MUSIC_PREV_WORDS):
                self.tools_executor.execute("previous_track", {})
                reply = "Going back."
            elif any(w in last_user for w in _MUSIC_PLAY_WORDS):
                self.tools_executor.execute("play_music", {})
                reply = "Playing."
            else:
                yield from self._plain_stream(fast_model)
                return
            self.messages.append({"role": "assistant", "content": reply})
            yield reply

    def _plain_stream(self, model: str | None = None, overloaded: bool = False) -> Generator[str, None, None]:
        from openai import RateLimitError, BadRequestError, APIStatusError
        from brain.personality import get_fast_prompt

        # When tool execution failed (context too large, all fallbacks exhausted),
        # skip the LLM entirely — it will hallucinate tool execution without tools.
        if overloaded:
            _msg = "My processing queue is briefly overloaded, Mr. Roe. Try that again in a moment."
            self.messages.append({"role": "assistant", "content": _msg})
            yield _msg
            return

        target = model or GROQ_MODEL
        seen = set()
        candidates = []
        for m in [target] + _FALLBACK_MODELS:
            if m not in seen:
                seen.add(m)
                candidates.append(m)

        system = get_fast_prompt()

        for m in candidates:
            try:
                history = self._slim_history(self.messages[-4:])
                stream = self.client.chat.completions.create(
                    model=m,
                    messages=[{"role": "system", "content": system}] + history,
                    stream=True,
                    max_tokens=256,
                    temperature=0.7,
                )
                chunks = []
                for chunk in stream:
                    delta = chunk.choices[0].delta
                    if delta.content:
                        chunks.append(delta.content)
                        yield delta.content
                content = "".join(chunks)
                self.messages.append({"role": "assistant", "content": content})
                if content:
                    self._recent_replies.append(content.strip().lower()[:80])
                return
            except RateLimitError:
                console.print(f"[yellow]Rate limit on {m} (plain)[/yellow]")
                continue
            except APIStatusError as exc:
                if exc.status_code == 413:
                    console.print(f"[yellow]Request too large for {m} (plain)[/yellow]")
                    continue
                console.print(f"[yellow]Plain stream API error {exc.status_code}: {exc}[/yellow]")
                continue
            except BadRequestError as exc:
                err = str(exc)
                if "model_decommissioned" in err or "model_not_active" in err:
                    continue
                console.print(f"[red]Bad request (plain): {exc}[/red]")
                continue
            except Exception as exc:
                console.print(f"[yellow]Plain stream error: {exc}[/yellow]")
                continue

        # All models exhausted — give an honest canned response
        _fallback_msg = "My processing queue is briefly overloaded, Mr. Roe. Try that again in a moment."
        self.messages.append({"role": "assistant", "content": _fallback_msg})
        yield _fallback_msg


def _summarise(args: dict) -> str:
    if not args:
        return ""
    parts = []
    for k, v in list(args.items())[:3]:
        vs = str(v)
        if len(vs) > 40:
            vs = vs[:37] + "…"
        parts.append(f"{k}={vs!r}")
    return ", ".join(parts)
