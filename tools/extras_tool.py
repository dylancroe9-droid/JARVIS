"""
Extra tools: timers, reminders, notes, math, clipboard, notifications.
"""
import ast
import subprocess
import threading
import time as _time


# ── Timers ───────────────────────────────────────────────────────────────────

_active_timers: dict[str, threading.Thread] = {}


def set_timer(minutes: float, label: str = "Timer") -> str:
    """Set a timer that fires a macOS notification when done."""
    secs = minutes * 60

    def _ring():
        _time.sleep(secs)
        _notify(f"{label} — time's up!", "JARVIS Timer")
        try:
            from tools.system_controls import show_notification
            show_notification("⏰ Timer Done", f"{label} — {minutes} min", sound=True)
        except Exception:
            pass

    t = threading.Thread(target=_ring, daemon=True)
    t.start()
    _active_timers[label] = t

    mins_str = f"{int(minutes)} minute{'s' if minutes != 1 else ''}"
    if minutes < 1:
        mins_str = f"{int(minutes * 60)} seconds"
    return f"Timer set: {label} in {mins_str}. I'll notify you."


def set_pomodoro(work_minutes: int = 25, break_minutes: int = 5) -> str:
    """Classic Pomodoro: work block then break block, with notifications."""
    def _run():
        _time.sleep(work_minutes * 60)
        _notify(f"Work block done! Take a {break_minutes}-minute break.", "JARVIS — Pomodoro")
        _time.sleep(break_minutes * 60)
        _notify("Break over — back to it, Mr. Roe.", "JARVIS — Pomodoro")

    threading.Thread(target=_run, daemon=True).start()
    return (
        f"Pomodoro started: {work_minutes} min work, "
        f"then {break_minutes} min break. Head down."
    )


def _notify(message: str, title: str = "JARVIS") -> None:
    try:
        subprocess.run(
            [
                "osascript", "-e",
                f'display notification "{message}" with title "{title}" sound name "Glass"',
            ],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass  # Notifications are best-effort — never crash


# ── Reminders ────────────────────────────────────────────────────────────────

def _run_applescript(script: str, timeout: int = 15) -> tuple[bool, str]:
    """Run AppleScript via temp file + 'sh -c osascript' to inherit TCC permissions."""
    import tempfile, os, shlex
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.applescript', delete=False) as f:
            f.write(script)
            tmp = f.name
        try:
            r = subprocess.run(
                ["sh", "-c", f"osascript {shlex.quote(tmp)}"],
                capture_output=True, text=True, timeout=timeout,
            )
        finally:
            os.unlink(tmp)
        if r.returncode != 0:
            return False, r.stderr.strip()
        return True, r.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except Exception as exc:
        return False, str(exc)


def add_reminder(text: str, notes: str = "") -> str:
    """Add a reminder to macOS Reminders, with file fallback."""
    import os
    note_line = f'set body of r to "{_esc(notes)}"' if notes else ""
    script = f'''tell application "Reminders"
    set r to make new reminder at end of default list
    set name of r to "{_esc(text)}"
    {note_line}
end tell'''
    ok, err = _run_applescript(script, timeout=8)
    if not ok:
        # Fallback: append to the same desktop notes file
        try:
            path = os.path.expanduser("~/Desktop/JARVIS_notes.txt")
            with open(path, "a") as f:
                from datetime import datetime
                f.write(f"\n[{datetime.now().strftime('%b %d %I:%M %p')}] REMINDER: {text}\n")
            return f"Reminder saved to Desktop/JARVIS_notes.txt: {text}"
        except OSError:
            pass
        if "not authorized" in err.lower() or "access" in err.lower():
            return "Reminders access denied. Grant access in System Settings > Privacy & Security > Reminders."
        return f"Couldn't add reminder: {err}"
    return f"Reminder added: {text}"


def get_reminders() -> str:
    """List incomplete reminders from macOS Reminders."""
    script = '''tell application "Reminders"
    set output to ""
    repeat with r in (every reminder of default list whose completed is false)
        set output to output & "• " & name of r & "\\n"
    end repeat
    return output
end tell'''
    ok, out = _run_applescript(script)
    if not ok:
        if "not authorized" in out.lower() or "access" in out.lower():
            return "Reminders access denied. Grant access in System Settings > Privacy & Security > Reminders."
        return f"Couldn't fetch reminders: {out}"
    return out if out else "No pending reminders."


# ── Notes ────────────────────────────────────────────────────────────────────

def quick_note(text: str, title: str = "JARVIS Note") -> str:
    """Append a quick note to Apple Notes."""
    import tempfile, os, shlex
    script = f'''tell application "Notes"
    tell account "iCloud"
        make new note at folder "Notes" with properties {{name:"{_esc(title)}", body:"{_esc(text)}"}}
    end tell
end tell'''
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.applescript', delete=False) as f:
            f.write(script)
            tmp = f.name
        try:
            result = subprocess.run(
                ["sh", "-c", f"osascript {shlex.quote(tmp)}"],
                capture_output=True, text=True, timeout=15,
            )
        finally:
            os.unlink(tmp)
    except subprocess.TimeoutExpired:
        result = None
    except Exception:
        result = None

    if result is None or result.returncode != 0:
        # Fallback: save to a plain text file on the Desktop
        try:
            path = os.path.expanduser("~/Desktop/JARVIS_notes.txt")
            with open(path, "a") as f:
                from datetime import datetime
                f.write(f"\n[{datetime.now().strftime('%b %d %I:%M %p')}] {title}: {text}\n")
            return f"Note saved to Desktop/JARVIS_notes.txt: {title}"
        except OSError as exc:
            return f"Couldn't save note: {exc}"
    return f"Note saved to Apple Notes: {title}"


# ── Math ─────────────────────────────────────────────────────────────────────

def calculate(expression: str) -> str:
    """Safely evaluate a math expression."""
    # Allow only safe math nodes
    _SAFE = (
        ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Num,
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod,
        ast.FloorDiv, ast.USub, ast.UAdd,
    )
    try:
        tree = ast.parse(expression.strip(), mode="eval")
        for node in ast.walk(tree):
            if not isinstance(node, _SAFE):
                return "I can only evaluate pure math expressions."
        result = eval(compile(tree, "<string>", "eval"))  # noqa: S307
        # Format nicely
        if isinstance(result, float) and result == int(result):
            result = int(result)
        return f"{expression} = {result}"
    except Exception:
        return "I couldn't evaluate that expression — make sure it's valid math (e.g. '2 + 2', 'sqrt(16)', '5 ** 3')."


# ── Clipboard ────────────────────────────────────────────────────────────────

def get_clipboard() -> str:
    """Read the current clipboard contents."""
    try:
        r = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
        text = r.stdout.strip()
        return text[:3000] if text else "(Clipboard is empty.)"
    except Exception as exc:
        return f"Couldn't read clipboard: {exc}"


def set_clipboard(text: str) -> str:
    """Write text to the clipboard."""
    try:
        subprocess.run(["pbcopy"], input=text, text=True, timeout=5)
        preview = text[:60] + ("…" if len(text) > 60 else "")
        return f"Copied to clipboard: {preview}"
    except Exception as exc:
        return f"Couldn't write to clipboard: {exc}"


# ── System info ──────────────────────────────────────────────────────────────

def get_battery_status() -> str:
    """Get MacBook battery level and charging status."""
    try:
        result = subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True, text=True, timeout=5
        )
        output = result.stdout
        import re
        match = re.search(r"(\d+)%", output)
        charging = "charging" in output.lower() or "ac power" in output.lower()
        if match:
            pct = match.group(1)
            status = "charging" if charging else "on battery"
            return f"Battery: {pct}% ({status})"
        return "Battery info unavailable."
    except Exception as exc:
        return f"Couldn't get battery status: {exc}"


def get_uptime() -> str:
    """Get system uptime."""
    try:
        result = subprocess.run(["uptime"], capture_output=True, text=True, timeout=5)
        return result.stdout.strip()
    except Exception:
        return "Uptime unavailable."


# ── Helpers ──────────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    """Escape a string for AppleScript double-quoted strings."""
    return s.replace("\\", "\\\\").replace('"', '\\"')
