"""
Study mode — schoolwork stays inside schoolwork.

take_screenshot is deliberately tuned to read a dangling "this"/"that" as a screen
reference. That is right when Dylan is on a problem set and wrong the rest of the
time: asking "what do I do?" while browsing should not make JARVIS screenshot his
desktop. Gating the schoolwork tools on study mode is what keeps the homework
product from bleeding into the ambient assistant.
"""

import pytest

from brain.jarvis import (_BASE_TOOL_NAMES, _STUDY_TOOL_NAMES,
                          _resident_tools_for_mode, _search_tools)
from brain.tools import TOOL_DEFINITIONS
from tools.mode_manager import _STICKY_MODES, classify


def _resident(mode):
    return {t["name"] for t in _search_tools(TOOL_DEFINITIONS, mode)
            if t.get("name") and not t.get("defer_loading")}


class TestToolGating:
    def test_schoolwork_tools_are_absent_in_normal_mode(self):
        res = _resident("normal")
        for name in _STUDY_TOOL_NAMES:
            assert name not in res, f"{name} must not be loaded outside study mode"

    def test_schoolwork_tools_are_present_in_study_mode(self):
        res = _resident("study")
        for name in _STUDY_TOOL_NAMES:
            assert name in res, f"{name} must be loaded in study mode"

    def test_screenshot_is_the_one_that_matters(self):
        # This is the specific leak: a casual "what do I do?" on the desktop.
        assert "take_screenshot" in _resident("study")
        assert "take_screenshot" not in _resident("normal")
        assert "take_screenshot" not in _resident("watch")

    def test_base_tools_survive_every_mode(self):
        for mode in ("normal", "study", "watch", "gaming", "work"):
            assert _BASE_TOOL_NAMES <= _resident(mode)

    def test_unknown_mode_is_treated_as_normal(self):
        assert _resident_tools_for_mode("nonsense") == _BASE_TOOL_NAMES
        assert _resident_tools_for_mode("") == _BASE_TOOL_NAMES

    def test_tool_array_is_stable_within_a_mode(self):
        # The tool array is the head of the cache prefix; it must not wobble
        # between turns spent in the same mode.
        assert _search_tools(TOOL_DEFINITIONS, "study") == _search_tools(TOOL_DEFINITIONS, "study")
        assert _search_tools(TOOL_DEFINITIONS, "normal") == _search_tools(TOOL_DEFINITIONS, "normal")

    def test_resident_set_stays_small_in_both_modes(self):
        # Every resident tool is billed on every turn.
        assert len(_resident("normal")) <= 6
        assert len(_resident("study")) <= 9


class TestModeBehaviour:
    def test_study_is_sticky(self):
        # Studying means switching windows constantly — notes, browser, PDF. A
        # non-sticky pin would drop out the moment he opened anything.
        assert "study" in _STICKY_MODES

    def test_auto_detection_never_enters_study(self):
        # Homework is something Dylan declares, not something guessed from a
        # window title. Auto-entering work mode is what made modes feel random.
        for app, title in [("Code", "homework.py"), ("Google Chrome", "Canvas"),
                           ("Preview", "problem set.pdf"), ("Notion", "AP Bio notes")]:
            assert classify(app, title)[0] != "study"


class TestSetModeTool:
    def _tool(self):
        return next(t for t in TOOL_DEFINITIONS if t["name"] == "set_mode")

    def test_study_is_an_accepted_mode(self):
        assert "study" in self._tool()["input_schema"]["properties"]["mode"]["enum"]

    def test_description_explains_the_gating(self):
        d = self._tool()["description"].lower()
        assert "study" in d
        assert "only mode" in d
