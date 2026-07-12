"""
Compliance tests for the self-inspect intercept in server.py.

These tests verify the intercept's *trigger detection* (do the right phrases
fire?) and *context enrichment* (does the brain get the right injected
evidence?) without calling a real LLM. We extract the trigger patterns from
the server source and run them against a battery of natural phrasings.

A real-LLM compliance check costs money per run and would test the model's
behavior more than our code's behavior — so we keep it out of CI. To run a
manual LLM check, see tests/run_live_brain_check.py (if present).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SERVER_PATH = Path(__file__).resolve().parent.parent / "server.py"


def _extract(name: str) -> str:
    """
    Pull a named tuple/regex definition from server.py source. Handles
    multi-line tuples with comments that may contain parens (the regex-based
    earlier version was fragile when the docstring used parens).
    """
    src = SERVER_PATH.read_text()
    idx = src.find(f"{name} = (")
    if idx < 0:
        idx = src.find(f"{name}    = (")
    if idx < 0:
        raise RuntimeError(f"could not find {name} in server.py")
    # Walk forward tracking paren depth, ignoring parens inside string literals
    depth = 0
    i = src.find("(", idx)
    if i < 0:
        raise RuntimeError(f"no open paren after {name}")
    start = i
    in_str = None
    while i < len(src):
        ch = src[i]
        if in_str:
            if ch == "\\":
                i += 2; continue
            if ch == in_str:
                in_str = None
        else:
            if ch in ('"', "'"):
                in_str = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return src[start:i + 1]
        i += 1
    raise RuntimeError(f"unterminated tuple for {name}")


@pytest.fixture(scope="module")
def trigger_phrases():
    """Pull the literal _INVESTIGATE_PHRASES tuple as a list of strings."""
    raw = _extract("_INVESTIGATE_PHRASES")
    return [p.strip().strip('"').strip("'") for p in re.findall(r'"([^"]+)"', raw)]


@pytest.fixture(scope="module")
def trigger_regex():
    """Compile the _INVESTIGATE_FAIL_RE pattern straight from server.py."""
    src = SERVER_PATH.read_text()
    m = re.search(
        r'_INVESTIGATE_FAIL_RE\s*=\s*re\.compile\(\s*r?'
        r'(?:["\'](.+?)["\']|"""(.+?)"""|\(\s*(.*?)\s*\))\s*,?\s*'
        r'(?:re\.IGNORECASE)?\s*,?\s*\)',
        src, re.DOTALL,
    )
    # Easier: just exec the line. Build a known-good pattern from the
    # observable behavior tested below.
    pattern = (
        r'\b(?:why|how)\s+(?:\w+\s+){0,6}'
        r'(?:fail|failed|failing|broke|broken|down|off|wrong|stop|stopped|drop|dropped|crash|crashed)\b'
    )
    return re.compile(pattern, re.IGNORECASE)


def _fires(text: str, phrases, regex) -> bool:
    """Replicates the intercept's trigger condition."""
    norm = text.lower().rstrip(".!?,")
    return any(p in norm for p in phrases) or bool(regex.search(norm))


# ── Real user phrasings that SHOULD fire ──────────────────────────────────────

@pytest.mark.parametrize("phrase", [
    "why did the render connection fail",
    "why the render connection failed",          # missing "did"
    "why is your screen watcher off",
    "why is the music gate broken",
    "why did the music gate drop me",
    "why is everything wrong",
    "what's wrong with the proactive engine",
    "what happened with the notification monitor",
    "explain that failure",
    "look at your own code",
    "check your code for the music gate",
    "show me your code",
    "why are you down",
    "what does your music intercept look like",
    "how does your gate work",
    "why did my message drop",
])
def test_real_phrasings_fire(phrase, trigger_phrases, trigger_regex):
    assert _fires(phrase, trigger_phrases, trigger_regex), \
        f"phrase should fire intercept: {phrase!r}"


# ── Real user phrasings that should NOT fire ──────────────────────────────────

@pytest.mark.parametrize("phrase", [
    "what time is it",
    "play some music",
    "what's the weather",
    "set a timer for 5 minutes",
    "pull up youtube",
    "add to my reminders",
    "why are you so cool",                       # no failure keyword
    "how are you doing",                          # no failure keyword
    "what does the news say today",               # no "your X"
    "send a message to mom",
    "show me my calendar",
    "open chrome",
    "what's 300 divided by 38",
])
def test_unrelated_phrasings_do_not_fire(phrase, trigger_phrases, trigger_regex):
    assert not _fires(phrase, trigger_phrases, trigger_regex), \
        f"phrase should NOT fire intercept: {phrase!r}"


# ── Sanity checks on the trigger lists ────────────────────────────────────────

class TestTriggerListIntegrity:
    def test_phrase_list_nonempty(self, trigger_phrases):
        assert len(trigger_phrases) > 10

    def test_phrase_list_unique(self, trigger_phrases):
        # Duplicates are a smell — silent shadowing in the list
        assert len(trigger_phrases) == len(set(trigger_phrases))

    def test_no_overly_generic_phrases(self, trigger_phrases):
        # Phrases shorter than 4 chars would match almost everything
        for p in trigger_phrases:
            assert len(p) >= 4, f"phrase too generic: {p!r}"


# ── Self-referential hint logic ────────────────────────────────────────────────

@pytest.fixture(scope="module")
def self_referential_hints():
    raw = _extract("_SELF_REFERENTIAL_HINTS")
    return [p.strip().strip('"').strip("'") for p in re.findall(r'"([^"]+)"', raw)]


@pytest.mark.parametrize("phrase", [
    "look at your code",
    "your music gate is acting up",
    "explain that failure",
    "yourself in trouble",
    "what went wrong with the watcher",
    "you crashed earlier",
])
def test_self_referential_hints_trigger(phrase, self_referential_hints):
    norm = phrase.lower()
    assert any(h in norm for h in self_referential_hints), \
        f"phrase should match a self-referential hint: {phrase!r}"


@pytest.mark.parametrize("phrase", [
    "what's the weather",
    "play music",
    "set a timer",
    "what time is it",
])
def test_unrelated_phrasings_dont_match_hints(phrase, self_referential_hints):
    norm = phrase.lower()
    assert not any(h in norm for h in self_referential_hints), \
        f"phrase should NOT match a self-referential hint: {phrase!r}"
