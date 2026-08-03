"""
Core JARVIS brain.
Primary:  Claude (Anthropic) — when ANTHROPIC_API_KEY is set
Fallback: Groq              — when only GROQ_API_KEY is available
"""

from __future__ import annotations

import json
import os
import re
from collections import deque
from typing import Generator

from rich.console import Console

from datetime import datetime

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, GROQ_API_KEY, GROQ_MODEL, OLLAMA_URL, OLLAMA_MODEL, USER_NAME
from brain.personality import (
    get_system_prompt, get_fast_prompt, get_mid_prompt,
    get_stable_prompt, get_volatile_prompt, get_current_mode,
)
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


# Anthropic beta flag required for defer_loading + the tool-search server tool.
_TOOL_SEARCH_BETA = "advanced-tool-use-2025-11-20"

# Tools kept resident in the prompt on every turn.  Everything else is deferred and
# reached through tool search.  Keep this list SHORT and free of generic catch-alls:
# a visible do-anything tool (web_search) shadows the specialists, and Claude stops
# searching — measured 2/6 correct with web_search resident vs 7/6 without.
#
# take_screenshot MUST stay resident.  Deferring it breaks homework help outright:
# "what is the answer to this?" shares no words with "take_screenshot", so BM25
# cannot retrieve it — measured 1/6 deferred vs 6/6 resident, with no reflexive
# screenshots on plain conversation (6/6 clean).
# Tools resident on EVERY turn, in every mode. Keep this tiny — each one is billed
# on every single request.
#
# set_listening: a type-first user needs "mute the mic" instantly in any mode.
# create_practice_test / show_test_answers: "quiz me on X" is unambiguous and never
#   misfires, so — unlike take_screenshot — these are safe to keep always-on, which
#   lets JARVIS do everything Alex can (make a verified practice test on command)
#   from any mode, not only study mode. Deferred, Claude just typed questions into
#   chat instead of building a real test (measured 0 tool calls).
_BASE_TOOL_NAMES = {"play_music", "pause_music", "set_mode", "set_listening",
                    "create_practice_test", "show_test_answers"}

# Schoolwork tools resident ONLY in study mode.
#
# take_screenshot is deliberately tuned to treat a dangling "this"/"that" as a
# screen reference — right when Dylan is on a problem set, wrong the rest of the
# time (asking "what do I do?" while browsing must not silently screenshot his
# desktop). That is the one that leaks, so it stays mode-gated. The practice-test
# tools moved to BASE above because they never leak.
_STUDY_TOOL_NAMES = {"take_screenshot"}

_CORE_TOOL_NAMES = _BASE_TOOL_NAMES | _STUDY_TOOL_NAMES


def _resident_tools_for_mode(mode: str) -> set:
    """Which tools stay loaded, given the mode JARVIS is currently in."""
    if (mode or "normal").lower() == "study":
        return _BASE_TOOL_NAMES | _STUDY_TOOL_NAMES
    return _BASE_TOOL_NAMES

# Computed once and reused: the text must be byte-identical every turn or the tool
# array shifts and takes the cached system prefix down with it.
_TOOL_ADDENDA_CACHE: dict | None = None


def _tool_addenda() -> dict:
    global _TOOL_ADDENDA_CACHE
    if _TOOL_ADDENDA_CACHE is None:
        from brain.personality import get_tool_addenda
        try:
            _TOOL_ADDENDA_CACHE = get_tool_addenda()
        except Exception as exc:      # never let prompt slimming break tool use
            console.print(f"[yellow]tool addenda unavailable: {exc}[/yellow]")
            _TOOL_ADDENDA_CACHE = {}
    return _TOOL_ADDENDA_CACHE


# Running total for the process, so cost is observable instead of asserted.
_COST_TOTAL = {"usd": 0.0, "turns": 0}

# $ per MILLION tokens. cache read is 0.1x input; a 1h-TTL write is 2x.
_PRICES = {
    "claude-sonnet-5":  (3.00, 15.00),
    "claude-haiku-4-5": (1.00,  5.00),
    "claude-opus-4-8":  (15.00, 75.00),
}


def _log_cost(usage, model: str) -> None:
    """
    Print what the turn actually cost, straight off the wire.

    This exists because "it's cheaper now" is worthless as a claim you can't see.
    cache_read should be large and steady; cache_write should be near zero after
    the first turn. If write stays high, something is changing the prefix between
    turns and the caching is silently doing nothing.
    """
    try:
        i_rate, o_rate = _PRICES.get(model, (3.00, 15.00))
        billed = getattr(usage, "input_tokens", 0) or 0
        write  = getattr(usage, "cache_creation_input_tokens", 0) or 0
        read   = getattr(usage, "cache_read_input_tokens", 0) or 0
        out    = getattr(usage, "output_tokens", 0) or 0
        usd = (billed * i_rate + write * i_rate * 2.0 + read * i_rate * 0.1
               + out * o_rate) / 1e6
        _COST_TOTAL["usd"] += usd
        _COST_TOTAL["turns"] += 1
        # Escape the tag — rich parses a bare [cost] as markup and swallows it.
        console.print(
            f"[dim]\\[cost] in={billed:,} write={write:,} read={read:,} out={out:,} "
            f"→ ${usd:.4f}  (session ${_COST_TOTAL['usd']:.3f} over "
            f"{_COST_TOTAL['turns']} calls)[/dim]"
        )
    except Exception:
        pass          # cost telemetry must never break a reply


def _system_blocks(stable: str) -> list:
    """
    The character prompt as a single cached system block.

    Per-turn state does NOT go here.  The cache prefix runs tools → system →
    messages, so a volatile system block poisons everything after it: with the clock
    in block two, the conversation history could never cache and was re-written at 2x
    every turn (measured $38.52/user/mo vs $28.96 doing nothing).  Volatile state is
    appended to the final message instead — see _claude_messages.

    1-hour TTL, not the 5-minute default: JARVIS is spoken to in bursts, and a gap
    longer than the window turns every turn into a fresh write at 1.25x — measurably
    worse than not caching at all.
    """
    return [{"type": "text", "text": stable,
             "cache_control": {"type": "ephemeral", "ttl": "1h"}}]


def _search_tools(tools: list, mode: str = "") -> list:
    """
    Return `tools` rewritten for Anthropic tool search: core tools stay resident,
    the rest are marked defer_loading so their schemas are fetched only when Claude
    searches for them.  Sending all 118 schemas cost 19,175 tokens on EVERY turn;
    deferring costs ~1,900 — the same 118 tools remain callable.

    Restricted sets (forced show_overlay, etc.) are passed through untouched: they
    are already small, and deferring a tool we are about to force would strand it.

    No cache_control here — the API rejects deferred tools carrying a cache
    breakpoint.  The system block holds the breakpoint instead, which caches the
    tools+system prefix together anyway.
    """
    if not tools or len(tools) < 20:
        return tools
    from brain.product import tool_is_allowed
    resident = _resident_tools_for_mode(mode)
    addenda = _tool_addenda()
    out = []
    for t in tools:
        name = t.get("name")
        # Alex (study-only product) never sees music/movie/smart-home/etc tools at
        # all — not deferred, absent. A student can't trip over them, and the
        # schoolwork build can't be pointed at anything off-task.
        if not tool_is_allowed(name):
            continue
        extra = addenda.get(name)
        # Guidance lifted out of the system prompt rides on the tool it belongs to.
        t = {**t, "description": f"{t['description']}\n\n{extra}"} if extra else t
        out.append(t if name in resident else {**t, "defer_loading": True})
    # BM25 ranks by relevance and beat regex 7/8 vs 5/8 on real phrasings.
    out.append({"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"})
    return out


# ── Startup greeting pool ───────────────────────────────────────────────────────
# Fallback when Claude can't generate one live. Casual, varied, ONE flowing line
# each — never the old robotic "all systems operational" template.
_GREETING_POOL = [
    "Hey — what's on the agenda today?",
    "Back at it. What are we getting into?",
    "Alright, I'm up. What's the move?",
    "What are we feeling — work, or something fun?",
    "Ready when you are. Wanna knock something out, or just chill?",
    "Yo. What's the plan, sir?",
    "I'm here — watch something, get some work done, or just talk?",
    "What's good? What are we doing today?",
    "Up and running. Where do you wanna start?",
    "Alright, hit me — what's first?",
    "Wanna throw something on, or get to work?",
    "What are we building today?",
    "Let's do something. What's it gonna be?",
    "I'm around. What do you need?",
]

# A random angle is injected per greeting so Claude doesn't fall into the same
# structure every launch — forces genuine variety.
_GREETING_ANGLES = [
    "Ask what's on the agenda today.",
    "Suggest putting something on to watch.",
    "Nudge him toward getting some work done.",
    "Just a low-key hello — ask what he needs.",
    "Riff lightly on the time of day.",
    "Ask what he feels like building or working on.",
    "Be playful — tease him a little, warmly.",
    "Offer a couple of options: chill, work, or watch something.",
    "Ask how he's doing, genuinely, in one line.",
    "Just say you're here and ready — short and easy.",
]

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


# Tools the tiny 8b fallback tends to fire spuriously on garbled/ambiguous
# input (e.g. take_screenshot on "run a-", open_application on "system search").
# Each requires at least one matching keyword in the user's actual words, or
# the call is treated as a hallucination and dropped.
_SURPRISING_TOOL_KEYWORDS = {
    "open_application": ("open", "launch", "start", "fire up", "pull up",
                         "go to", "bring up"),
    "take_screenshot":  ("screenshot", "screen", "look at", "what's on",
                         "whats on", "see my", "capture", "what do you see"),
    "read_camera":      ("camera", "look at this", "what am i holding",
                         "what is this", "see this", "hold"),
}


def _tool_call_plausible(name: str, user_text: str) -> bool:
    """
    True if a tool call is plausible given the user's words. Only gates the
    'surprising' tools above — everything else is always allowed. Prevents the
    8b fallback from firing a screenshot / app-launch on input that never
    asked for one.
    """
    kws = _SURPRISING_TOOL_KEYWORDS.get(name)
    if not kws:
        return True
    low = (user_text or "").lower()
    return any(k in low for k in kws)


def _has_music_word(text: str, words) -> bool:
    """
    Match music keywords against text:
      - Single-word entries match on word boundary ("skip" ≠ "skipping")
      - Multi-word phrases match as substring ("next song" → in text)
    Prevents disasters like 'skipping ads' triggering next_track.
    """
    import re
    for w in words:
        if " " in w:
            if w in text:
                return True
        elif re.search(r"\b" + re.escape(w) + r"\b", text):
            return True
    return False

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
    
    
    
    # Pull up / bring up
    "pull up the ", "pull up a ", "pull up an ",
    
    
    # Display / render / visualize
    
    "render the ", "render a ", "render an ",
    "visualize the ", "visualize a ",
    
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
    
    "let me see the ", "let me see a ", "let me see an ",
    "want to see the ", "want to see a ", "want to see an ",
    "i'd like to see ", "i would like to see ",
    
    # Can you / could you
    
    
    "can you pull up", "can you bring up",
    "can you display", "can you render",
    # NOTE — deliberately absent: "what's the ", "what's a ", "look at the ",
    # "take a look at ", "let me look at ", "what does the ", "show me what ".
    # These match ordinary English, and a match here FORCES the tool array down to
    # show_overlay alone, so JARVIS physically cannot look at the screen or use any
    # other tool. "what's the answer to this?" drew a Bugatti blueprint; "take a
    # look at this problem" — a screen request — did the same. A specific noun below
    # ("the bugatti", "the f-22") already carries the real signal, so "show me the
    # Bugatti" still triggers without dragging every question in English along.
    # Hologram / 3D
    "hologram of", "hologram for",
    "3d model of", "3d model for",
    "3d of", "3d view of",
    "show me the 3d", "render the 3d",
    "model of the ", "model of a ",
    # Anatomy / science
    "anatomy of", "diagram of", "structure of",
    "the solar system", "the milky way", "planet earth",
    "the human dna", "double helix",
    "what does a human ", "what does the human ",
    
    # "put it on screen", "throw it up"
    
    # Build / design mode activators (mode-word triggers — conversational phrases
    # like "build me a X" are now routed to 3D build canvas in server.py first)
    "build mode", "design mode", "architect mode",
    
    
    # Specific objects people naturally say
    
    
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

# Natural acknowledgment phrases spoken BEFORE a tool runs.
# Gives the user immediate audio feedback so JARVIS doesn't go silent for 1-3s.
# Keys are tool names; None = no acknowledgment (instant / canned tools).
_TOOL_ACKS: dict[str, str | None] = {
    "get_weather":            "One sec.",
    "get_forecast":           "Let me pull that forecast.",
    "web_search":             "Let me look that up.",
    "web_search_results":     "Searching now.",
    "get_calendar_events":    "Checking your calendar.",
    "read_calendar_visually":  "Pulling up your calendar.",
    "create_calendar_event":  "Adding that to your schedule.",
    "get_unread_emails":      "Checking your inbox.",
    "get_recent_emails":      "Pulling up your emails.",
    "send_email":             "Sending that.",
    "get_news":               "Grabbing the latest.",
    "get_daily_briefing":     "Pulling up your briefing.",
    "deep_research":          "Let me dig into that.",
    "add_reminder":           "Setting that reminder.",
    "get_reminders":          "Checking your reminders.",
    "run_command":            "On it.",
    "read_file":              "Reading that file.",
    "write_file":             "Writing that.",
    "edit_file":              "Making that edit.",
    "youtube_search":         "Searching YouTube.",
    "open_application":       "Opening that.",
    "get_battery_status":     "Checking battery.",
    "translate_text":         "Translating that.",
    "define_word":            "Looking that up.",
    "get_wikipedia_summary":  "Let me look that up.",
    "generate_image":         "Generating that image.",
    "send_imessage":          "Sending that message.",
    "get_location":           "Checking your location.",
    "convert_units":          "Crunching that.",
    "get_weather_forecast":   "Let me check.",
    # Tools that already have near-instant results or canned replies → stay silent
    "remember_fact":          None,
    "quick_note":             None,
    "set_timer":              None,
    "take_screenshot":        None,
    "play_music":             None,
    "pause_music":            None,
    "next_track":             None,
    "show_overlay":           None,
    "ar_build":               None,
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
    # Picking an answer on a multiple-choice test. "select it" is the natural way to
    # ask for this and matched nothing here, so it fell through to a plain spoken
    # reply and the bubble never got clicked.
    "select it", "select this", "select that", "select the answer", "select the right",
    "select b", "select c", "select d", "select a",
    "pick it", "pick that", "pick the answer", "pick the right",
    "choose it", "choose that", "choose the answer", "choose the right",
    "mark it", "mark that", "bubble it", "bubble in",
    "answer and select", "answer it and select", "select it for me", "pick it for me",
    "choose it for me", "click it", "click it for me", "click the answer",
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
    "pull it up",  "bring it up", "pull that up",
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
    # llama-4-scout removed — outputs words without spaces (tokenizer bug)
    # llama3-groq-8b-8192-tool-use-preview removed — DECOMMISSIONED 2025
    "llama-3.1-8b-instant",
]


class Jarvis:
    # Conversation history is persisted here so JARVIS remembers across restarts.
    # Cap stored history at 200 messages; in-memory cap stays at 40 (last 40
    # are sent to the LLM for context).
    HISTORY_PATH = os.path.join(os.path.expanduser("~"), ".jarvis_history.json")
    HISTORY_MAX  = 200

    def __init__(self) -> None:
        # Eager construction — lazy init was racy (two threads could each
        # create a ToolsExecutor instance, doubling tool-call accounting).
        # The executor is cheap; build it up front.
        from brain._executor import ToolsExecutor
        self._executor = ToolsExecutor()
        self.messages: list[dict] = []
        self._recent_replies: deque = deque(maxlen=5)
        self._last_blueprint_subject: str = ""  # context for follow-up requests like "pull it up"
        # Load saved conversation from previous sessions
        self._load_history()

        # ── Local Ollama brain (runs on YOUR hardware) ────────────────────────
        self._local: "OpenAI | None" = None
        try:
            import httpx as _hx
            r = _hx.get(f"{OLLAMA_URL}/api/tags", timeout=2.0)
            if r.status_code == 200:
                from openai import OpenAI as _OAI
                self._local = _OAI(base_url=f"{OLLAMA_URL}/v1", api_key="ollama")
                console.print(f"[green]Local brain: Ollama {OLLAMA_MODEL} ✓[/green]")
            else:
                console.print("[dim]Ollama not running — using cloud only[/dim]")
        except Exception:
            console.print("[dim]Ollama not found — using cloud only[/dim]")

        # ── Primary: Claude ────────────────────────────────────────────────────
        if _USE_CLAUDE:
            import anthropic
            self._claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            self.model = ANTHROPIC_MODEL
            console.print(f"[dim]Cloud brain: Claude {ANTHROPIC_MODEL}[/dim]")

        # ── Groq: cloud fallback for tool-heavy / complex tasks ────────────────
        if GROQ_API_KEY:
            from openai import OpenAI
            self.client = OpenAI(
                api_key=GROQ_API_KEY,
                base_url="https://api.groq.com/openai/v1",
            )
            if not _USE_CLAUDE:
                self.model = GROQ_MODEL
                console.print(f"[dim]Cloud brain: Groq {GROQ_MODEL}[/dim]")
        elif not _USE_CLAUDE:
            raise RuntimeError(
                "No API key found.\n"
                "Add ANTHROPIC_API_KEY or GROQ_API_KEY to ~/JARVIS/.env"
            )

    @property
    def tools_executor(self):
        # Built eagerly in __init__ to avoid a TOCTOU on lazy init.
        return self._executor

    # ── Public API ─────────────────────────────────────────────────────────────

    def chat(self, user_input: str) -> Generator[str, None, None]:
        # ── Alex credits gate (study-only product) ──────────────────────────────
        # Handled before anything else so redeeming, checking a balance, and the
        # empty-tank case never call the model or touch history. JARVIS is never
        # metered — this whole block is is_alex()-guarded. See brain/credits.py.
        from brain.product import is_alex
        if is_alex():
            from brain import credits
            _stripped = (user_input or "").strip()
            _low = _stripped.lower()
            if _low.startswith("redeem "):
                _ok, _msg, _bal = credits.redeem(_stripped.split(None, 1)[1])
                yield _msg
                return
            if _low in ("credits", "balance", "credits left", "check credits",
                        "how many credits", "how many credits do i have",
                        "how many credits do i have left"):
                yield f"You've got {credits.balance()} credits left."
                return
            if not credits.has_credit():
                yield credits.out_of_credits_message()
                return

        # Strip injected context prefixes before storing in history —
        # [WORK MODE — SCREEN: ...] blobs compound fast and blow up the context window.
        import re as _re
        _clean_input = _re.sub(
            r'^\[(?:WORK MODE|GAMING MODE)[^\]]*\]\s*(?:Use this[^.]*\.)?\s*(?:User said:\s*)?',
            '', user_input
        ).strip() or user_input
        # Reset the per-turn self-inspect counter — see _executor.py for cap logic
        try:
            self.tools_executor.reset_self_inspect_counter()
        except AttributeError:
            pass
        self.messages.append({"role": "user", "content": _clean_input})
        # Trim in chunks, not one message per turn.  Dropping the oldest message on
        # every turn shifts the start of the conversation every turn, and prompt
        # caching matches on a *prefix* — so a sliding window meant history could
        # never cache and was re-sent at full price forever.  Letting it run to 56
        # and then cutting back to 40 keeps the prefix byte-stable for ~16 turns,
        # which is long enough to actually get cache hits.  Same ceiling on context.
        if len(self.messages) > 56:
            self.messages = self.messages[-40:]

        if _USE_CLAUDE:
            # Silently extract memorable facts in background — no await, never blocks
            self._auto_remember(user_input)
            if is_alex():
                # Meter the turn on its ACTUAL measured cost, converted to credits.
                # Runs once the consumer finishes streaming this turn's chunks.
                _start_usd = _COST_TOTAL["usd"]
                yield from self._claude_chat()
                from brain import credits
                credits.charge_usd(_COST_TOTAL["usd"] - _start_usd, reason="turn")
            else:
                yield from self._claude_chat()
        else:
            _lower_input = user_input.lower()
            # [STUDY] prompts — skip tools entirely
            if _lower_input.startswith("[study]") or _lower_input.startswith("[study mode]"):
                yield from self._plain_stream(model=GROQ_MODEL)
            elif self._is_fast_query(user_input):
                # Simple conversational — still use 70b but with compressed context
                # 8b-instant outputs words-without-spaces on short prompts (broken)
                yield from self._plain_stream(model=GROQ_MODEL)
            else:
                yield from self._stream_with_tools()

    def startup_greeting(self, tod: str, issues=None) -> str:
        """
        A fresh, casual, single-line greeting for when Dylan opens the app —
        written by Claude so it's different every launch (no canned template, no
        robotic 'all systems operational', no clock announcement).  Still honest:
        if something is actually broken it works that in.  Falls back to a varied
        pool if Claude is unavailable.
        """
        import random
        issues = issues or []
        # Alex greets as a study partner, not JARVIS. The greeting pool lives in
        # brain/alex.py so Alex's content isn't buried inside the JARVIS brain.
        from brain.product import is_alex
        if is_alex():
            from brain.alex import STARTUP_GREETINGS
            return random.choice(STARTUP_GREETINGS)
        if _USE_CLAUDE and getattr(self, "_claude", None):
            try:
                angle = random.choice(_GREETING_ANGLES)
                sys = (
                    "You are JARVIS. Dylan just opened the app — greet him with ONE short, "
                    "casual, natural spoken line in your voice: warm, easy, a little playful. "
                    f"It's {tod.lower()}. THIS TIME take this angle: {angle} "
                    "Vary the opener — do NOT always start with 'Evening' or his name. Use 'sir' "
                    "only once in a while. NEVER announce the clock time, NEVER say 'all systems "
                    "operational' or 'welcome back'. One flowing sentence, no lists. "
                    "Reply with ONLY the line, no quotes."
                )
                if issues:
                    sys += f" Also work in, casually, that there's a problem: {issues[0]}."
                r = self._claude.messages.create(
                    model=self.model,
                    thinking={"type": "disabled"},
                    max_tokens=90,
                    system=sys,
                    messages=[{"role": "user", "content": "(the app just opened)"}],
                )
                line = "".join(
                    b.text for b in r.content if getattr(b, "type", "") == "text"
                ).strip().strip('"').strip()
                if line:
                    return line
            except Exception as exc:
                console.print(f"[dim]greeting gen failed: {exc}[/dim]")
        # Fallback: varied casual pool
        line = random.choice(_GREETING_POOL)
        if issues:
            line = line.rstrip(".?! ") + f" — though heads up, {issues[0]}."
        return line

    def reset(self) -> None:
        self.messages.clear()
        # Clear persisted history too — fresh start
        try:
            if os.path.exists(self.HISTORY_PATH):
                os.unlink(self.HISTORY_PATH)
        except Exception:
            pass

    def _load_history(self) -> None:
        """Restore conversation messages from disk on startup."""
        try:
            if not os.path.exists(self.HISTORY_PATH):
                return
            import json as _json
            with open(self.HISTORY_PATH, "r", encoding="utf-8") as f:
                data = _json.load(f)
            if isinstance(data, list):
                # Only keep messages whose content is a plain string — skip
                # any old tool-use/result blocks that may have stale references.
                cleaned = []
                for m in data[-self.HISTORY_MAX:]:
                    if not isinstance(m, dict):
                        continue
                    if m.get("role") not in ("user", "assistant"):
                        continue
                    if not isinstance(m.get("content"), str):
                        continue
                    cleaned.append(m)
                # Trim to recent in-memory cap for active context
                self.messages = cleaned[-40:]
                if cleaned:
                    console.print(
                        f"[dim]Conversation memory: loaded {len(cleaned)} prior messages ({len(self.messages)} active in context)[/dim]"
                    )
        except Exception as exc:
            console.print(f"[dim]Could not load history: {exc}[/dim]")

    def _save_history(self) -> None:
        """Persist conversation messages to disk. Called after each turn."""
        try:
            import json as _json
            # Only persist plain text messages — tool-result blocks reference
            # mid-turn state that won't make sense after restart.
            persistable = [
                m for m in self.messages
                if isinstance(m, dict) and isinstance(m.get("content"), str)
                and m.get("role") in ("user", "assistant")
            ]
            # Cap at HISTORY_MAX to avoid file bloat
            persistable = persistable[-self.HISTORY_MAX:]
            tmp = self.HISTORY_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(persistable, f, ensure_ascii=False)
            os.replace(tmp, self.HISTORY_PATH)
        except Exception:
            pass

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
        # CRITICAL: only inject real calendar data. Never invent events from memory.
        try:
            from tools.calendar_tool import get_calendar_events
            cal = get_calendar_events(days=7)
            if cal and "nothing" not in cal.lower() and "unavailable" not in cal.lower():
                notes.append(
                    f"TODAY'S REAL CALENDAR DATA (from device): {cal[:400]}\n"
                    f"  ⚠ NEVER invent or guess calendar events. "
                    f"Only report what is listed above. If asked about schedule, read this exactly."
                )
            else:
                notes.append(
                    "TODAY'S CALENDAR: Nothing scheduled today (verified from device). "
                    "Do NOT invent events."
                )
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

        # Short conversational — needs room for a real answer
        if len(words) <= 8:
            return 500

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
        # `system` is the VOLATILE half only — the character half is held apart in
        # _stable so it stays byte-identical and caches.  Every mode note below
        # appends to `system`, which is correct: those are per-turn instructions.
        _stable = get_stable_prompt()
        system = (
            get_volatile_prompt()
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
            # Tool set is deliberately identical on every turn.  The cache prefix is
            # tools → system, so a tool array that changes shape between turns also
            # invalidates the cached system prompt.  The old keyword vision gate did
            # exactly that; Sonnet 5 declines to screenshot plain conversation on its
            # own (measured 6/6 clean), so the gate is no longer earning its cost.
            _active_tools = TOOL_DEFINITIONS

        for _round in range(_MAX_ROUNDS):
            _streamed_text = False
            try:
                # STREAM the turn so a conversational reply starts speaking within a
                # beat instead of after the whole thought is generated (the old
                # non-streaming create() made JARVIS wait for the full answer, then
                # dump it all at once — that was the "crazy slow" lag).  Text deltas
                # are yielded live to TTS; tool_use blocks are collected and handled
                # after via get_final_message().  A pure command emits only a tool_use
                # (no text), so nothing is spoken before the action.
                # thinking disabled = no pre-answer pause (Sonnet 5 defaults to adaptive).
                with self._claude.beta.messages.stream(
                    model=self.model,
                    thinking={"type": "disabled"},
                    system=_system_blocks(_stable),
                    messages=self._claude_messages(volatile=system),
                    tools=_search_tools(_active_tools, get_current_mode()),
                    tool_choice=_overlay_tool_choice,
                    max_tokens=_loop_max_tokens,
                    betas=[_TOOL_SEARCH_BETA],
                ) as stream:
                    for event in stream:
                        if (event.type == "content_block_delta"
                                and getattr(event.delta, "type", "") == "text_delta"
                                and event.delta.text):
                            _streamed_text = True
                            yield event.delta.text
                    response = stream.get_final_message()
                _log_cost(response.usage, self.model)
                # After first round, stop forcing (let Claude continue naturally)
                _overlay_tool_choice = {"type": "auto"}
                _active_tools        = TOOL_DEFINITIONS
            except Exception as exc:
                # If we already streamed part of the answer, don't fall back and
                # repeat it — just end cleanly.
                if _streamed_text:
                    console.print(f"[yellow]Claude stream cut off: {exc}[/yellow]")
                    return
                console.print(f"[yellow]Claude error ({type(exc).__name__}): {exc}[/yellow]")
                exc_str = str(exc).lower()
                # Auth / billing errors — tell the user exactly what's wrong
                if any(k in exc_str for k in ("authentication", "invalid api key", "api key")):
                    msg = (
                        "Your Anthropic API key looks invalid. "
                        "Check ANTHROPIC_API_KEY in ~/JARVIS/.env — "
                        "grab a fresh one at console.anthropic.com."
                    )
                    self._append_assistant_text(msg)
                    yield msg
                    return
                if any(k in exc_str for k in ("credit", "quota", "billing", "insufficient", "payment")):
                    msg = (
                        "Your Anthropic account is out of credits. "
                        "Top up at console.anthropic.com/billing — "
                        "or add a GROQ_API_KEY to .env as a free fallback."
                    )
                    self._append_assistant_text(msg)
                    yield msg
                    return
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
                    if not _streamed_text:      # already spoken live during the stream
                        yield text
                else:
                    yield from self._claude_final_stream(_system_blocks(_stable), system, max_tokens=budget)
                return

            # ── Has tool calls ────────────────────────────────────────────────
            # Add assistant turn (raw blocks — SDK reserialises them correctly)
            self.messages.append({"role": "assistant", "content": response.content})

            # Execute every tool in this round
            tool_results = []
            executed: set[str] = set()

            # Speak before the first tool runs
            first_ack = _TOOL_ACKS.get(tool_uses[0].name, "")
            if first_ack and not _streamed_text:
                yield first_ack + " "

            for tu in tool_uses:
                executed.add(tu.name)
                console.print(f"[dim]  ⚙  {tu.name}({_summarise(dict(tu.input))})[/dim]")
                try:
                    result = self.tools_executor.execute(tu.name, dict(tu.input))
                except Exception as exc:
                    result = f"Tool error: {exc}"
                # If a tool returned image blocks (e.g. take_screenshot on the Claude
                # brain), forward them straight into the tool_result so Claude SEES the
                # screen with its own vision — critical for reading math/diagrams. Else
                # collapse to text as before.
                if isinstance(result, list) and any(
                        isinstance(b, dict) and b.get("type") == "image" for b in result):
                    content = result
                else:
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
                yield from self._claude_final_stream(_system_blocks(_stable), system, max_tokens=120)
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
            yield from self._claude_final_stream(_system_blocks(_stable), system, max_tokens=budget)
            return

        # Hit max rounds without finishing — force a clean response
        yield from self._claude_final_stream(_system_blocks(_stable), system, max_tokens=budget)

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
                    thinking={"type": "disabled"},   # cheap background extraction — no thinking
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
        self, system: list, volatile: str = "", max_tokens: int = 1024
    ) -> Generator[str, None, None]:
        """
        Streaming text-only response from Claude.

        The tool array is still sent, with tool_choice="none" to forbid new calls.
        It cannot be omitted: history may hold a tool_use for a DEFERRED tool, and
        replaying that without the definitions fails with "Tool reference 'X' not
        found in available tools".  tool_choice="none" keeps this a pure text turn.
        """
        try:
            full: list[str] = []
            with self._claude.beta.messages.stream(
                model=self.model,
                thinking={"type": "disabled"},   # keep voice replies instant (see _claude_chat)
                system=system,
                messages=self._claude_messages(volatile=volatile),
                tools=_search_tools(TOOL_DEFINITIONS, get_current_mode()),
                tool_choice={"type": "none"},
                max_tokens=max_tokens,
                betas=[_TOOL_SEARCH_BETA],
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

    def _claude_messages(self, volatile: str = "") -> list[dict]:
        """
        Convert self.messages to Claude's required format.
        Handles both plain string content and block lists (tool use / tool results).

        `volatile` (clock, mode, status, per-turn mode notes) is appended as a trailing
        block on the final message — the last position in the whole request — so that
        everything before it can cache.  It is deliberately NOT persisted into
        self.messages: the cache breakpoint sits on the message's own content, so the
        prefix ends before the volatile block and still matches next turn, once this
        message is history and the state text is gone.

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

        # Boundary guard — the single biggest daily-chat hazard.
        #
        # The history trim (self.messages[-40:]) is blind, so it can leave the window
        # starting on an assistant message, or on a user message whose content is an
        # orphan tool_result (its parent tool_use fell out of the window). Claude 400s
        # on either ("first message must use the user role" / "tool_result without
        # tool_use"), and because the trim keeps landing there, EVERY following turn
        # 400s too — silently dropping Dylan to the weaker Groq model, or losing the
        # turn. It fires in plain conversation with no tools involved. Drop leading
        # messages until the window begins on a real user turn.
        def _is_orphan_tool_result(msg) -> bool:
            c = msg.get("content")
            return (isinstance(c, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in c))

        while out and (out[0]["role"] != "user" or _is_orphan_tool_result(out[0])):
            out.pop(0)

        # Cache the conversation too, not just the prompt.  History runs ~3.8k tokens
        # and was billed in full every turn — more than the character prompt costs once
        # that one is cached.  The breakpoint goes on the message's real content and
        # the volatile state block is added AFTER it, so the cached prefix stops short
        # of the state text.  Next turn this message is history without that block, and
        # the prefix still matches — which is what lets the conversation read back at
        # 10% instead of being rewritten.
        if out:
            last    = out[-1]
            content = last["content"]
            if isinstance(content, str):
                content = [{"type": "text", "text": content}] if content else []
            content = list(content)
            if content and isinstance(content[-1], dict):
                content[-1] = {**content[-1],
                               "cache_control": {"type": "ephemeral", "ttl": "1h"}}
            if volatile:
                # Worded hard as background.  Sitting in the user turn, this is the
                # most-attended position in the request, and a plainly-labelled state
                # dump reads as something to remark on — JARVIS started opening every
                # reply with the clock ("11:25, sir...") even though the character
                # prompt forbids it.  The reference data has to be here for caching;
                # the instruction not to perform it has to be here too.
                content.append({
                    "type": "text",
                    "text": (
                        f"[Background state. {USER_NAME} did not say this and cannot see it. "
                        f"Reference only — do NOT mention the time, mode, or status unless "
                        f"he actually asks, or it genuinely changes your answer. Never open "
                        f"a reply with the clock.]\n{volatile}"
                    ),
                })
            if content:
                out[-1] = {**last, "content": content}
        return out

    def _append_assistant_text(self, text: str) -> None:
        self.messages.append({"role": "assistant", "content": text})
        # Persist conversation after each assistant turn so we survive restarts.
        self._save_history()

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
            # Calendar / schedule — must call the tool, never hallucinate
            "schedule", "agenda", "appointment", "meeting", "today", "event",
            "what do i have", "what's today", "what time", "when is",
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
        # Drop leading ORPHAN tool messages. A tail slice (self.messages[-N:])
        # can begin partway through a tool exchange — on a role:"tool" message
        # whose parent assistant(tool_calls) fell just outside the window. Groq
        # then 400s ("tool message must be preceded by the assistant tool_calls
        # message"), which _stream_with_tools mislabels as "API access
        # restricted" and drops the user's turn. Trimming leading tool messages
        # makes any window start on a valid boundary (user / system / an
        # assistant whose tool_calls ARE followed by their results in-window).
        start = 0
        while start < len(msgs) and msgs[start].get("role") == "tool":
            start += 1
        msgs = msgs[start:]

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

    def _local_stream(self) -> Generator[str, None, None]:
        """
        Stream a pure-conversation response from local Ollama.
        No tool calls — fast query path only.
        Falls back automatically if self._local is None (caller checks before calling).
        """
        from brain.personality import get_fast_prompt
        system  = get_fast_prompt() + self._get_recent_replies_note()
        history = self._slim_history(self.messages[-8:], max_chars=500)

        stream = self._local.chat.completions.create(
            model=OLLAMA_MODEL,
            messages=[{"role": "system", "content": system}] + history,
            stream=True,
            temperature=0.7,
            max_tokens=400,
        )
        full: list[str] = []
        for chunk in stream:
            delta = chunk.choices[0].delta.content or ""
            if delta:
                full.append(delta)
                yield delta

        text = "".join(full).strip()
        if text:
            self.messages.append({"role": "assistant", "content": text})
            self._recent_replies.append(text[:80])

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
            # Absolute minimum — Groq 8b free tier is 6000 TPM. The fast prompt
            # is ~2400 tokens; with tools + history we exceeded 6k on a 'project
            # iron' turn. Hard-code a TINY system prompt for emergency retries.
            system  = (
                "You are JARVIS, Dylan's AI. Helpful, capable, calm authority. "
                "British style. Short replies. Say 'sir' occasionally. "
                "Never invent capabilities or facts. If unsure, say so."
            )
            history = self._slim_history(self.messages[-2:], max_chars=120)
            max_tok = 256
        elif is_primary:
            # Use the condensed prompt for Groq — the full prompt is ~3000 tokens
            # and burns through the free-tier token-per-minute quota before the
            # response even starts. Condensed = ~600 tokens = 5x more headroom.
            # Keep only last 6 messages (down from 12) to stay well under context limit.
            system, history, max_tok = get_mid_prompt(), self._slim_history(self.messages[-6:], max_chars=280), 900
        elif is_fast:
            # 8b-instant: slim context = less to process = faster first token
            system, history, max_tok = get_fast_prompt(), self._slim_history(self.messages[-4:], max_chars=150), 400
        else:
            system, history, max_tok = get_mid_prompt(), self._slim_history(self.messages[-10:]), 768

        # Study generation needs more tokens regardless of model tier
        if _is_study_gen:
            max_tok = max(max_tok, 2048)

        all_messages = [{"role": "system", "content": system}] + history

        note = self._get_recent_replies_note()
        if note:
            all_messages[0]["content"] += note

        # Temperature 0.9 — higher than the default 0.7 to push variety in
        # conversational/humor responses. 0.7 was producing same-y replies to
        # the same prompts across sessions; 0.9 keeps the model on-voice while
        # giving it room to riff differently each time.
        kwargs = dict(
            model=model,
            messages=all_messages,
            stream=True,
            max_tokens=max_tok,
            temperature=0.9,
        )
        if with_tools:
            if is_primary:
                # Groq free tier is 6-12k TOKENS-PER-MINUTE. The full 106-tool
                # schema is ~11k tokens and the fuller core set ~8.5k — a single
                # such request blows the entire minute's budget, which is what
                # produced the 47-second "Request too large" cascades. Use the
                # compact ~17-tool set (~1.5k tokens) so the primary request
                # fits with room to spare. The long-tail tools are reached
                # through the hard-coded intercepts (overlays, AR, calendar,
                # music, deep scan, etc.), not this general path.
                kwargs["tools"] = get_8b_tools()
            elif is_fast:
                kwargs["tools"] = get_8b_tools()   # compact set — avoids 413 payload errors
            else:
                kwargs["tools"] = get_8b_tools()  # keep every Groq path lean
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
            elif _has_music_word(last_user, _MUSIC_PAUSE_WORDS):
                kwargs["tool_choice"] = {"type": "function", "function": {"name": "pause_music"}}
            elif _has_music_word(last_user, _MUSIC_SKIP_WORDS):
                kwargs["tool_choice"] = {"type": "function", "function": {"name": "next_track"}}
            elif _has_music_word(last_user, _MUSIC_PREV_WORDS):
                kwargs["tool_choice"] = {"type": "function", "function": {"name": "previous_track"}}
            elif _has_music_word(last_user, _MUSIC_PLAY_WORDS):
                kwargs["tool_choice"] = {"type": "function", "function": {"name": "play_music"}}
            elif any(w in last_user for w in {
                "set a timer", "set timer", "start a timer", "start timer",
                "timer for", "set a countdown", "set countdown",
                "countdown for", "set an alarm", "set alarm", "alarm for",
            }):
                # Force set_timer tool — models prefer run_command otherwise.
                # Bare "timer" intentionally dropped — Whisper often mis-hears
                # math expressions ("300 over 38") with words containing "timer".
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

            # ── Acknowledgment: speak before the tool runs so JARVIS isn't silent ──
            first_name = tool_calls_formatted[0]["function"]["name"]
            ack = _TOOL_ACKS.get(first_name, "")  # "" = not in dict = no ack
            if ack:
                yield ack + " "

            for tc in tool_calls_formatted:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}

                # A fallback model (8b) is hallucination-prone: drop 'surprising'
                # tool calls (screenshot / app-launch / camera) whose trigger
                # words never appeared in what the user said. The primary 70b is
                # trustworthy, so gate only when we actually fell back — the same
                # protection _minimal_tool_retry has, which the main loop lacked.
                if model != GROQ_MODEL and not _tool_call_plausible(name, _last_user):
                    console.print(f"[yellow]  ⚠ dropped implausible {name}() "
                                  f"from fallback model[/yellow]")
                    self.messages.append({
                        "role": "tool", "tool_call_id": tc["id"],
                        "content": "(skipped — not what the user asked for)",
                    })
                    continue

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
        else:
            # Loop fell through _MAX_TOOL_ROUNDS with no terminal answer — every
            # round kept calling non-finishing tools (read_file, grep_code, …).
            # Force a final plain (tool-free) response so the turn isn't silently
            # dropped AND self.messages doesn't end on a dangling tool message
            # (which would orphan the next turn's history window). Mirrors what
            # the Claude tool loop does after its own round budget.
            yield from self._plain_stream()

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
        # Try models in order. llama3-groq-8b-tool-use-preview and llama-4-scout
        # both removed — first decommissioned, second has tokenizer bugs.
        _recovery_models = [
            GROQ_MODEL,
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
        from brain.tools import get_minimal_tools
        from brain.personality import get_tiny_prompt
        fast_model = "llama-3.1-8b-instant"
        # Tiny prompt (~120 tok) not get_fast_prompt (~3000 tok): the emergency
        # path must stay under the 8b's 6,000 TPM limit or it 413s and the user
        # gets nothing. Slim history too.
        system = get_tiny_prompt()
        messages = [{"role": "system", "content": system}] + self._slim_history(
            self.messages[-4:], max_chars=140)
        try:
            last_u = next(
                (m["content"] for m in reversed(self.messages) if m.get("role") == "user"), ""
            ).lower()
            if _has_music_word(last_u, _MUSIC_PAUSE_WORDS):
                _forced = {"type": "function", "function": {"name": "pause_music"}}
            elif _has_music_word(last_u, _MUSIC_SKIP_WORDS):
                _forced = {"type": "function", "function": {"name": "next_track"}}
            elif _has_music_word(last_u, _MUSIC_PREV_WORDS):
                _forced = {"type": "function", "function": {"name": "previous_track"}}
            elif _has_music_word(last_u, _MUSIC_PLAY_WORDS):
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
            # Last thing the user actually said — used to sanity-check the
            # fallback model's tool choices.
            _last_user_text = next(
                (m["content"] for m in reversed(self.messages)
                 if m.get("role") == "user" and isinstance(m.get("content"), str)),
                "",
            )
            for tc in tool_calls_formatted:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}
                # Drop hallucinated 'surprising' tool calls — the 8b fallback
                # fires screenshots / app-launches on input that never asked.
                if not _tool_call_plausible(name, _last_user_text):
                    console.print(f"[yellow]  ⚠ dropped implausible {name}() — "
                                  f"no matching keyword in {_last_user_text[:40]!r}[/yellow]")
                    self.messages.append({
                        "role": "tool", "tool_call_id": tc["id"],
                        "content": "(skipped — not what the user asked for)",
                    })
                    continue
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
            if _has_music_word(last_user, _MUSIC_PAUSE_WORDS):
                self.tools_executor.execute("pause_music", {})
                reply = "Paused."
            elif _has_music_word(last_user, _MUSIC_SKIP_WORDS):
                self.tools_executor.execute("next_track", {})
                reply = "Next."
            elif _has_music_word(last_user, _MUSIC_PREV_WORDS):
                self.tools_executor.execute("previous_track", {})
                reply = "Going back."
            elif _has_music_word(last_user, _MUSIC_PLAY_WORDS):
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
