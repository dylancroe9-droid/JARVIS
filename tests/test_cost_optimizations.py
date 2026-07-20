"""
Guards on the prompt-caching + tool-search setup that took cost from $206 to $15
per user per month.

Every rule here is subtle and silently reversible: nothing crashes if the cache
stops hitting, the bill just quietly goes back up 10x. These tests fail loudly
instead.
"""

import re

import pytest

from brain.jarvis import (
    _CORE_TOOL_NAMES,
    _search_tools,
    _system_blocks,
)
from brain.personality import (
    get_stable_prompt,
    get_system_prompt,
    get_tool_addenda,
    get_volatile_prompt,
)
from brain.tools import TOOL_DEFINITIONS


class TestToolSearch:
    def test_non_core_tools_are_deferred(self):
        out = _search_tools(TOOL_DEFINITIONS)
        deferred = [t for t in out if t.get("defer_loading")]
        # 118 tools cost 19,175 tokens/turn resident; deferring costs ~1,900.
        assert len(deferred) > 100

    def test_core_tools_stay_resident(self):
        out = _search_tools(TOOL_DEFINITIONS)
        for t in out:
            if t.get("name") in _CORE_TOOL_NAMES:
                assert not t.get("defer_loading"), f"{t['name']} must stay resident"

    def test_take_screenshot_is_resident(self):
        # Deferring this breaks homework help: "what is the answer to this?" shares
        # no words with "take_screenshot", so BM25 cannot retrieve it. Measured
        # 1/6 deferred vs 6/6 resident.
        assert "take_screenshot" in _CORE_TOOL_NAMES

    def test_no_generic_search_tool_resident(self):
        # A resident do-anything tool shadows the specialists and Claude stops
        # searching entirely — measured 2/6 correct with web_search resident.
        assert "web_search" not in _CORE_TOOL_NAMES

    def test_search_tool_is_appended(self):
        out = _search_tools(TOOL_DEFINITIONS)
        assert any(t.get("type", "").startswith("tool_search_tool") for t in out)

    def test_deferred_tools_carry_no_cache_control(self):
        # The API rejects defer_loading + cache_control on the same tool.
        for t in _search_tools(TOOL_DEFINITIONS):
            if t.get("defer_loading"):
                assert "cache_control" not in t

    def test_small_tool_sets_pass_through(self):
        # Forced sets (e.g. show_overlay only) must not be deferred out from under
        # the tool_choice that is about to force them.
        one = [t for t in TOOL_DEFINITIONS if t["name"] == "show_overlay"]
        assert _search_tools(one) == one

    def test_tool_array_is_byte_stable(self):
        # The cache prefix is tools -> system, so a tool array that changes shape
        # between turns also invalidates the cached system prompt.
        assert _search_tools(TOOL_DEFINITIONS) == _search_tools(TOOL_DEFINITIONS)


class TestStablePromptIsCacheable:
    def test_no_clock_in_stable_prompt(self):
        # CURRENT TIME carries the minute. In the cached block it expired the whole
        # prefix every 60 seconds, so the cache never hit in normal use.
        assert not re.search(r"\d{1,2}:\d{2}\s*(AM|PM)", get_stable_prompt())

    def test_no_live_state_in_stable_prompt(self):
        stable = get_stable_prompt()
        assert "CURRENT TIME" not in stable
        assert "YOUR CURRENT STATE" not in stable

    def test_stable_prompt_is_byte_identical(self):
        assert get_stable_prompt() == get_stable_prompt()

    def test_volatile_prompt_carries_the_clock(self):
        assert "CURRENT TIME" in get_volatile_prompt()

    def test_full_prompt_still_contains_both_halves(self):
        full = get_system_prompt()
        assert "CURRENT TIME" in full
        assert get_stable_prompt()[:60] in full


class TestToolManualsMovedToTools:
    def test_manuals_are_lifted_out_of_the_prompt(self):
        # ~14k tokens of single-tool how-to guides used to be billed every turn.
        assert "═══ VISUAL OVERLAYS" not in get_stable_prompt()

    def test_manuals_are_attached_to_their_tool(self):
        addenda = get_tool_addenda()
        assert "show_overlay" in addenda
        assert "blueprint" in addenda["show_overlay"].lower()

    def test_addenda_reach_the_tool_description(self):
        out = _search_tools(TOOL_DEFINITIONS)
        overlay = next(t for t in out if t.get("name") == "show_overlay")
        assert "blueprint" in overlay["description"].lower()

    def test_addenda_are_stable(self):
        assert get_tool_addenda() == get_tool_addenda()


class TestHomeworkGateSurvivedTheTrim:
    """
    The keyword vision gate (_mentions_screen) is gone — the tool description does
    this job now, and does it better (7/7 vs a hardcoded phrase list). These guard
    the wording that makes it work.
    """

    def _desc(self):
        return next(t for t in _search_tools(TOOL_DEFINITIONS)
                    if t.get("name") == "take_screenshot")["description"]

    def test_dangling_references_are_screen_references(self):
        # "what do I do?" is Dylan's actual homework phrasing. Without this it reads
        # as a request for advice and JARVIS answers without ever looking.
        d = self._desc().lower()
        assert "what do i do" in d
        assert "dangling reference" in d

    def test_homework_guidance_rides_on_the_tool(self):
        assert "HOMEWORK / MATH HELP" in self._desc()

    def test_still_told_not_to_screenshot_plain_conversation(self):
        assert "do not screenshot for ordinary conversation" in self._desc().lower()


class TestSystemBlocks:
    def test_stable_block_carries_the_breakpoint(self):
        blocks = _system_blocks("character")
        assert blocks[0]["cache_control"]["type"] == "ephemeral"

    def test_uses_one_hour_ttl(self):
        # The 5-minute default expires between spoken exchanges, and an expired
        # write costs 1.25x — worse than not caching at all.
        assert _system_blocks("character")[0]["cache_control"]["ttl"] == "1h"

    def test_no_volatile_block_in_system(self):
        # The cache prefix is tools -> system -> messages, so a volatile system
        # block poisons the history cache behind it. State goes on the last message.
        assert len(_system_blocks("character")) == 1


class TestPracticeTestToolsAreResident:
    """
    Deferred tools only get used if Claude thinks to SEARCH for them. "Quiz me on
    X" is answerable from knowledge, so with these deferred he typed questions
    into chat instead of generating a real test — measured 0 tool calls, meaning
    no verified answer keys, no page, no scoring. Same failure mode as
    take_screenshot.
    """

    def test_create_practice_test_is_resident(self):
        assert "create_practice_test" in _CORE_TOOL_NAMES

    def test_show_test_answers_is_resident(self):
        assert "show_test_answers" in _CORE_TOOL_NAMES

    def test_resident_set_stays_small(self):
        # Every resident tool is billed on every single turn. This is the budget.
        assert len(_CORE_TOOL_NAMES) <= 8
