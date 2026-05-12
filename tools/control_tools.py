"""
Computer control — mouse, keyboard, scroll.
Uses pyautogui (primary). Requires macOS Accessibility permission.

Install: pip install pyautogui
Grant:   System Settings → Privacy & Security → Accessibility → add Terminal
"""

import subprocess
import time


# ── Helpers ───────────────────────────────────────────────────────────────────

def _gui():
    import pyautogui
    pyautogui.FAILSAFE = False
    return pyautogui


# ── Mouse ─────────────────────────────────────────────────────────────────────

def click_screen(x: int, y: int, button: str = "left", double: bool = False) -> str:
    """Click at screen coordinates (logical pixels)."""
    try:
        pg = _gui()
        x, y = int(x), int(y)
        if double:
            pg.doubleClick(x, y)
            return f"Double-clicked ({x}, {y})."
        if button == "right":
            pg.rightClick(x, y)
            return f"Right-clicked ({x}, {y})."
        pg.click(x, y)
        return f"Clicked ({x}, {y})."
    except ImportError:
        return (
            "pyautogui not installed — run: pip install pyautogui  "
            "Then grant Accessibility in System Settings → Privacy & Security."
        )
    except Exception as exc:
        return f"Click failed: {exc}"


def move_mouse(x: int, y: int) -> str:
    """Move the mouse cursor without clicking."""
    try:
        pg = _gui()
        pg.moveTo(int(x), int(y), duration=0.15)
        return f"Mouse at ({x}, {y})."
    except ImportError:
        return "pyautogui not installed."
    except Exception as exc:
        return f"Move failed: {exc}"


def scroll_screen(direction: str = "down", amount: int = 3) -> str:
    """
    Scroll the screen.
    direction: "up" or "down"
    amount: scroll clicks (3 is a moderate scroll, 10 is a lot)
    """
    try:
        pg = _gui()
        clicks = amount if direction == "up" else -amount
        pg.scroll(clicks)
        return f"Scrolled {direction}."
    except ImportError:
        return "pyautogui not installed."
    except Exception as exc:
        return f"Scroll failed: {exc}"


# ── Keyboard ──────────────────────────────────────────────────────────────────

def type_text(text: str) -> str:
    """
    Type text at the current cursor position.
    Uses clipboard paste so all characters (including emoji, unicode) work.
    """
    try:
        import pyautogui as pg
        pg.FAILSAFE = False

        # Save current clipboard
        old = subprocess.check_output(["pbpaste"]).decode("utf-8", errors="replace")

        # Load new text into clipboard
        p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        p.communicate(text.encode("utf-8"))
        time.sleep(0.05)

        # Paste
        pg.hotkey("command", "v")
        time.sleep(0.15)

        # Restore old clipboard
        p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        p.communicate(old.encode("utf-8"))

        return "Typed text."
    except ImportError:
        # Fallback: AppleScript (ASCII only)
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(
            ["osascript", "-e",
             f'tell application "System Events" to keystroke "{escaped}"'],
            capture_output=True,
        )
        return "Typed text."
    except Exception as exc:
        return f"Type failed: {exc}"


def press_key(keys: str) -> str:
    """
    Press a key or key combination.
    Examples:
      "return"        → press Enter
      "escape"        → press Escape
      "cmd+c"         → copy
      "cmd+shift+t"   → reopen tab
      "space"         → space bar
      "tab"           → tab
    """
    try:
        pg = _gui()
        parts = [k.strip().lower() for k in keys.split("+")]

        key_map = {
            "cmd":       "command",
            "ctrl":      "ctrl",
            "alt":       "option",
            "opt":       "option",
            "shift":     "shift",
            "return":    "return",
            "enter":     "return",
            "esc":       "escape",
            "escape":    "escape",
            "space":     "space",
            "tab":       "tab",
            "delete":    "delete",
            "backspace": "backspace",
            "up":        "up",
            "down":      "down",
            "left":      "left",
            "right":     "right",
            "home":      "home",
            "end":       "end",
            "pageup":    "pageup",
            "pagedown":  "pagedown",
        }
        mapped = [key_map.get(k, k) for k in parts]

        if len(mapped) == 1:
            pg.press(mapped[0])
        else:
            pg.hotkey(*mapped)
        return f"Pressed {keys}."
    except ImportError:
        return "pyautogui not installed."
    except Exception as exc:
        return f"Key press failed: {exc}"
