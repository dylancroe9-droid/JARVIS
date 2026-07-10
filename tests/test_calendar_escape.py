"""
Tests for the AppleScript escape helper in tools/calendar_tool.py.

These verify that no input can break out of the double-quoted AppleScript
string literal — which was a real injection vulnerability (calendar names,
event titles, and notes all went straight into the script unescaped).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.calendar_tool import _esc_as


class TestEscapeBasics:
    def test_empty_string(self):
        assert _esc_as("") == ""

    def test_none_safe(self):
        assert _esc_as(None) == ""

    def test_plain_text_unchanged(self):
        assert _esc_as("Lunch with Sam") == "Lunch with Sam"


class TestQuoteEscape:
    def test_double_quote_escaped(self):
        # The hostile case: a quote that would break out of the AppleScript
        # string and let the rest of the input become new statements.
        out = _esc_as('Say "hello"')
        assert out == r'Say \"hello\"'

    def test_no_orphan_quotes_anywhere(self):
        out = _esc_as('one " two " three')
        # Every literal " must be preceded by a backslash escape
        assert '"' not in out.replace('\\"', '')

    def test_backslash_escaped_first(self):
        # Backslash must be doubled BEFORE quote substitution so we don't
        # turn \ into \\ via "\" → "\\" while also processing a real "
        out = _esc_as(r'path\to\file')
        assert out == r'path\\to\\file'

    def test_quote_after_backslash(self):
        # The trickiest: literal `\"` should become `\\\"` (backslash itself
        # gets doubled, then the quote gets escaped)
        out = _esc_as(r'a\"b')
        assert out == r'a\\\"b'


class TestNewlineFlattening:
    def test_newline_becomes_space(self):
        # Newlines previously let the input start new AppleScript statements
        out = _esc_as("first\nsecond")
        assert "\n" not in out
        assert "first second" == out

    def test_crlf_becomes_space(self):
        out = _esc_as("a\r\nb")
        assert "\r" not in out and "\n" not in out

    def test_tab_becomes_space(self):
        out = _esc_as("a\tb")
        assert "\t" not in out


class TestRealAttackPayloads:
    """Verify the specific payloads we used to reproduce the bug."""

    @pytest.mark.parametrize("payload", [
        'Meeting" with secret\nset description to "PWNED',
        'Work" \nbeep\ntell app "Finder" to quit',
        'normal title',
        '"',
        '"""',
        '\\',
        '\\"',
        '\n',
        'mix\\of\\"things\nand\nmore',
    ])
    def test_payload_cannot_break_string(self, payload):
        escaped = _esc_as(payload)
        # The escaped string, when wrapped in double-quotes, must be a SINGLE
        # AppleScript string literal — no unescaped quotes, no newlines.
        assert "\n" not in escaped
        assert "\r" not in escaped
        # Every unescaped " has been backslash-escaped
        # Count literal " characters not preceded by \
        unescaped_quotes = re.findall(r'(?<!\\)"', escaped)
        assert unescaped_quotes == [], \
            f"escaped string still has unescaped quotes: {escaped!r}"

    def test_full_attack_demonstration(self):
        """
        Build the same `set summary of newEvent to "X"` line that the
        function builds, with a hostile X. Confirm we get a single, valid
        AppleScript literal — no syntactic break-out.
        """
        attack = 'Meeting" with newline\nset description of newEvent to "PWNED'
        line = f'set summary of newEvent to "{_esc_as(attack)}"'
        # Should contain exactly two literal quote characters (the surrounding
        # pair); every other quote is now backslash-escaped
        unescaped = re.findall(r'(?<!\\)"', line)
        assert len(unescaped) == 2, \
            f"expected exactly the wrapping quotes; got {len(unescaped)}: {line!r}"
        assert "\n" not in line


class TestCreateEventStillCallable:
    """Smoke check — the function still imports and accepts a normal call."""

    def test_function_still_imports(self):
        from tools.calendar_tool import create_calendar_event
        assert callable(create_calendar_event)

    def test_empty_title_short_circuits(self):
        from tools.calendar_tool import create_calendar_event
        result = create_calendar_event(title="", start="tomorrow 3pm")
        assert "need an event name" in result
