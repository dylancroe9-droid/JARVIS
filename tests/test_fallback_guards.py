"""
Tests for the 8b-fallback plausibility guard in brain/jarvis.py.

The tiny 8b emergency model fires 'surprising' tools (screenshots, app
launches) on garbled or ambiguous input. _tool_call_plausible drops those
calls unless the user's words actually match. These cases include the exact
mis-fires seen in real logs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain.jarvis import _tool_call_plausible


class TestDropsHallucinations:
    @pytest.mark.parametrize("name,text", [
        ("take_screenshot", "run a"),               # real log: fired on "run a-"
        ("open_application", "deep system search"),  # real log: opened System Settings
        ("take_screenshot", "can you pick a video"),
        ("open_application", "what time is it"),
        ("read_camera", "play some music"),
    ])
    def test_implausible_calls_dropped(self, name, text):
        assert _tool_call_plausible(name, text) is False


class TestAllowsLegit:
    @pytest.mark.parametrize("name,text", [
        ("take_screenshot", "what is on my screen"),
        ("take_screenshot", "look at this screen"),
        ("open_application", "open youtube"),
        ("open_application", "launch spotify"),
        ("open_application", "pull up notes"),
        ("read_camera", "what am i holding"),
        ("read_camera", "look at this"),
    ])
    def test_plausible_calls_allowed(self, name, text):
        assert _tool_call_plausible(name, text) is True


class TestNonSurprisingAlwaysAllowed:
    @pytest.mark.parametrize("name", [
        "play_music", "get_weather", "set_timer", "add_reminder",
        "web_search", "quick_note", "remember_fact",
    ])
    def test_non_surprising_tools_never_gated(self, name):
        # These aren't in the surprising set, so they're always allowed
        # regardless of the input text.
        assert _tool_call_plausible(name, "literally anything") is True
        assert _tool_call_plausible(name, "") is True


class TestEdgeCases:
    def test_empty_user_text_drops_surprising(self):
        assert _tool_call_plausible("take_screenshot", "") is False

    def test_none_user_text_safe(self):
        assert _tool_call_plausible("take_screenshot", None) is False
        assert _tool_call_plausible("play_music", None) is True
