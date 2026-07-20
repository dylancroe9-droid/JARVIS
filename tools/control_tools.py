"""
Computer control — mouse, keyboard, scroll.

Clicks go through Quartz CGEvent, not pyautogui. pyautogui was never installed and
was not in requirements.txt, so every click_screen call returned "pyautogui not
installed" — meaning JARVIS could never actually click anything, including selecting
an answer on a test. Quartz ships with pyobjc, which is already a hard dependency,
and it is the same mechanism the screen watcher uses to auto-click Skip Ad, so it is
proven to work on this machine.

Requires macOS Accessibility permission (System Settings → Privacy & Security →
Accessibility) for whichever app launched JARVIS.
"""

import subprocess
import time

try:
    import Quartz
    _QUARTZ_OK = True
except ImportError:                     # pragma: no cover - pyobjc is a hard dep
    _QUARTZ_OK = False

_NO_QUARTZ = ("Quartz unavailable — reinstall deps: "
              "pip install pyobjc-framework-Quartz")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _gui():
    import pyautogui
    pyautogui.FAILSAFE = False
    return pyautogui


def _post(evt_type, x: float, y: float, button=None, clicks: int = 1) -> None:
    """Post one mouse event at (x, y), optionally tagged with a click count."""
    btn = Quartz.kCGMouseButtonLeft if button is None else button
    ev = Quartz.CGEventCreateMouseEvent(None, evt_type,
                                        Quartz.CGPointMake(float(x), float(y)), btn)
    if clicks > 1:
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, clicks)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


# ── Mouse ─────────────────────────────────────────────────────────────────────

def click_screen(x: int, y: int, button: str = "left", double: bool = False) -> str:
    """Click at screen coordinates (logical pixels)."""
    if not _QUARTZ_OK:
        return _NO_QUARTZ
    try:
        x, y = int(x), int(y)
        # Move first: some apps only register a click after the cursor has actually
        # arrived, and a hover often has to fire before the target is clickable.
        _post(Quartz.kCGEventMouseMoved, x, y)
        time.sleep(0.03)
        if button == "right":
            _post(Quartz.kCGEventRightMouseDown, x, y, Quartz.kCGMouseButtonRight)
            time.sleep(0.03)
            _post(Quartz.kCGEventRightMouseUp, x, y, Quartz.kCGMouseButtonRight)
            return f"Right-clicked ({x}, {y})."
        for n in (1, 2) if double else (1,):
            _post(Quartz.kCGEventLeftMouseDown, x, y, clicks=n)
            time.sleep(0.03)
            _post(Quartz.kCGEventLeftMouseUp, x, y, clicks=n)
            if double and n == 1:
                time.sleep(0.05)
        return f"{'Double-clicked' if double else 'Clicked'} ({x}, {y})."
    except Exception as exc:
        return f"Click failed: {exc}"


def move_mouse(x: int, y: int) -> str:
    """Move the mouse cursor without clicking."""
    if not _QUARTZ_OK:
        return _NO_QUARTZ
    try:
        _post(Quartz.kCGEventMouseMoved, int(x), int(y))
        return f"Mouse at ({x}, {y})."
    except Exception as exc:
        return f"Move failed: {exc}"


def scroll_screen(direction: str = "down", amount: int = 3) -> str:
    """
    Scroll the screen.
    direction: "up" or "down"
    amount: scroll clicks (3 is a moderate scroll, 10 is a lot)
    """
    if not _QUARTZ_OK:
        return _NO_QUARTZ
    try:
        # One event per click, matching a real wheel, so the target app animates
        # instead of jumping — a single large delta gets treated as a fling.
        step = 3 if direction == "up" else -3
        for _ in range(max(1, int(amount))):
            ev = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, step)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.02)
        return f"Scrolled {direction}."
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

        # Save current clipboard so we can restore it after the paste
        try:
            old = subprocess.check_output(["pbpaste"]).decode("utf-8", errors="replace")
        except Exception:
            old = ""

        # try/finally guarantees the clipboard restore runs even if pyautogui
        # raises (previous code lost the user's clipboard data on any hotkey
        # failure between the load and the restore).
        try:
            # Load new text into clipboard
            p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            p.communicate(text.encode("utf-8"))
            time.sleep(0.05)

            # Paste
            pg.hotkey("command", "v")
            time.sleep(0.15)
            return "Typed text."
        finally:
            # Restore old clipboard — best effort, never raises out of finally
            try:
                p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
                p.communicate(old.encode("utf-8"))
            except Exception:
                pass
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
    # AppleScript rather than pyautogui — pyautogui was never installed, so this
    # returned "pyautogui not installed" for every keypress. System Events is
    # already used elsewhere in the codebase and needs no extra dependency.
    parts = [k.strip().lower() for k in keys.split("+")]
    mods = {"cmd": "command down", "command": "command down", "ctrl": "control down",
            "control": "control down", "alt": "option down", "opt": "option down",
            "option": "option down", "shift": "shift down"}
    # Non-printing keys have to go through `key code`, not `keystroke`.
    codes = {"return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51,
             "backspace": 51, "escape": 53, "esc": 53, "left": 123, "right": 124,
             "down": 125, "up": 126, "home": 115, "end": 119,
             "pageup": 116, "pagedown": 121}
    used = [mods[p] for p in parts if p in mods]
    target = [p for p in parts if p not in mods]
    if not target:
        return f"No key to press in {keys!r}."
    key = target[-1]
    using = f" using {{{', '.join(used)}}}" if used else ""
    if key in codes:
        cmd = f"key code {codes[key]}{using}"
    elif len(key) == 1:
        cmd = f'keystroke "{key}"{using}'
    else:
        return f"Unknown key: {key!r}"
    try:
        r = subprocess.run(
            ["osascript", "-e", f'tell application "System Events" to {cmd}'],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return f"Key press failed: {(r.stderr or '').strip()[:120]}"
        return f"Pressed {keys}."
    except Exception as exc:
        return f"Key press failed: {exc}"
