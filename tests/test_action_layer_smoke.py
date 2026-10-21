"""
Import + no-pyautogui smoke test for the action layer.

Every past regression (mic gate, click_screen, ad-skip, type_text, homework_loop)
lived in the modules that take real actions on the Mac — and the suite was
structurally blind to them because they were never imported in a test. The whole
class of "it imports pyautogui, which was never installed, so it silently no-ops"
would have been caught by one test that just imports these and greps for pyautogui.
"""

import ast
import importlib
from pathlib import Path

import pytest

ACTION_MODULES = [
    "tools.control_tools",
    "tools.homework_loop",
    "tools.screen_watcher",
    "tools.practice_test",
    "tools.mode_manager",
]

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("mod", ACTION_MODULES)
def test_action_module_imports(mod):
    # A bare import must not explode — this alone catches missing-dep and syntax
    # regressions in the layer where they always happen.
    importlib.import_module(mod)


@pytest.mark.parametrize("mod", ACTION_MODULES)
def test_no_pyautogui_dependency(mod):
    # pyautogui is not installed and not in requirements. Any module that imports
    # it will silently no-op its real action (that was the click_screen, type_text,
    # scroll, keypress, and homework_loop bug). Forbid the dependency outright.
    path = ROOT / (mod.replace(".", "/") + ".py")
    src = path.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name != "pyautogui" for a in node.names), \
                f"{mod} imports pyautogui, which is not installed — it will no-op"
        if isinstance(node, ast.ImportFrom):
            assert node.module != "pyautogui", \
                f"{mod} imports from pyautogui, which is not installed"


def test_control_tools_expose_the_real_functions():
    import tools.control_tools as ct
    for fn in ("click_screen", "type_text", "scroll_screen", "press_key", "move_mouse"):
        assert callable(getattr(ct, fn, None)), f"control_tools.{fn} missing"
