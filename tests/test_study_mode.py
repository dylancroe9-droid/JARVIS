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

    def test_screenshot_is_available_in_every_mode(self):
        # take_screenshot is resident everywhere. Gating it to study mode broke the
        # core feature — "look at my screen and do this" outside study mode found
        # the tool not even loaded, so JARVIS couldn't see the screen at all. WHEN
        # it fires is controlled by the tool description (dangling reference = look;
        # casual chat = don't), not by which mode you're in.
        for mode in ("normal", "study", "watch", "work"):
            assert "take_screenshot" in _resident(mode), mode

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
        # Every resident tool is billed on every turn. normal = 6 base tools +
        # the search tool; study adds take_screenshot. Practice-test tools are
        # in base now (they never leak, and it lets JARVIS do everything Alex can).
        assert len(_resident("normal")) <= 8
        assert len(_resident("study")) <= 9

    def test_practice_tests_available_in_every_mode(self):
        # "quiz me" must work from any mode — it's unambiguous and never misfires,
        # so it isn't gated like take_screenshot. This is what lets JARVIS match
        # Alex's practice-test capability everywhere.
        for mode in ("normal", "study", "watch", "gaming", "work"):
            res = _resident(mode)
            assert "create_practice_test" in res, mode
            assert "show_test_answers" in res, mode


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
