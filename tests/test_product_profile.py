"""
Product profiles — one codebase, two shippable apps (JARVIS and Alex).

Alex is the homework product that gets sold. It must NOT expose the ambient
assistant's tools (music, movies, smart home, ad-skip): a student shouldn't trip
over them, and the schoolwork build shouldn't be pointable at anything off-task.
"""

import importlib
import os

import pytest


@pytest.fixture
def as_product(monkeypatch):
    def _set(key):
        monkeypatch.setenv("JARVIS_PRODUCT", key)
        import brain.product as prod
        importlib.reload(prod)
        return prod
    yield _set
    # restore default for anything importing product afterward
    monkeypatch.delenv("JARVIS_PRODUCT", raising=False)
    import brain.product as prod
    importlib.reload(prod)


class TestSelection:
    def test_default_is_jarvis(self, monkeypatch):
        monkeypatch.delenv("JARVIS_PRODUCT", raising=False)
        import brain.product as prod
        importlib.reload(prod)
        assert prod.current().key == "jarvis"
        assert prod.is_alex() is False

    def test_alex_selected_by_env(self, as_product):
        prod = as_product("alex")
        assert prod.current().name == "Alex"
        assert prod.is_alex() is True
        assert prod.current().study_only is True

    def test_unknown_product_falls_back_to_jarvis(self, as_product):
        prod = as_product("banana")
        assert prod.current().key == "jarvis"


class TestToolGating:
    def test_jarvis_allows_everything(self, as_product):
        prod = as_product("jarvis")
        for name in ("play_music", "watch_movie", "empty_trash", "take_screenshot"):
            assert prod.tool_is_allowed(name)

    def test_alex_hides_ambient_tools(self, as_product):
        prod = as_product("alex")
        for name in ("play_music", "watch_movie", "get_stock_price",
                     "empty_trash", "facetime_call", "set_dark_mode"):
            assert not prod.tool_is_allowed(name), f"{name} must be hidden from Alex"

    def test_alex_allows_schoolwork_tools(self, as_product):
        prod = as_product("alex")
        for name in ("take_screenshot", "create_practice_test", "show_test_answers",
                     "web_search", "calculate", "set_mode"):
            assert prod.tool_is_allowed(name), f"{name} must be available to Alex"

    def test_alex_never_hides_the_search_tool(self, as_product):
        # It's how the model reaches anything deferred — hiding it would strand
        # every non-resident tool.
        prod = as_product("alex")
        assert prod.tool_is_allowed("tool_search_tool_bm25")


class TestBrainIntegration:
    def test_alex_tool_set_is_small(self, as_product):
        as_product("alex")
        from brain.jarvis import _search_tools
        from brain.tools import TOOL_DEFINITIONS
        names = {t["name"] for t in _search_tools(TOOL_DEFINITIONS, "study") if t.get("name")}
        assert "play_music" not in names
        assert "take_screenshot" in names
        assert len(names) < 20, "Alex should expose a tight schoolwork set, not 120 tools"

    def test_alex_persona_is_not_jarvis(self, as_product):
        as_product("alex")
        from brain.personality import get_stable_prompt
        p = get_stable_prompt()
        assert p.startswith("You are Alex")
        assert "JARVIS" not in p
        assert "answer first" in p.lower()

    def test_jarvis_persona_unchanged(self, as_product):
        as_product("jarvis")
        from brain.personality import get_stable_prompt
        assert "JARVIS" in get_stable_prompt()
