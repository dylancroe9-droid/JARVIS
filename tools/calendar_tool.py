"""
Reads and creates macOS Calendar events via AppleScript.
Requires Calendar access: System Settings > Privacy & Security > Calendars
Grant access to Terminal (for chat.py) or the Electron app (for JARVIS app).
"""

import subprocess
from datetime import datetime


def _run_cal(script: str, timeout: int = 30) -> tuple[bool, str]:
    """Run AppleScript against Calendar. Returns (success, output_or_error).
    Uses 'sh -c osascript' so the shell inherits Terminal's TCC Calendar access.
    """
    import shlex
    try:
        # Write script to a temp file to avoid shell-quoting issues
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.applescript',
                                         delete=False) as f:
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


def _permission_error(err: str) -> bool:
    err_l = err.lower()
    return any(k in err_l for k in ["not authorized", "1743", "not allowed",
                                     "user canceled", "access denied", "access"])


_PERMISSION_MSG = (
    "Calendar access not granted.\n"
    "Fix: System Settings → Privacy & Security → Calendars → "
    "enable Terminal (or JARVIS if using the app).\n"
    "Then restart JARVIS."
)


def _ensure_calendar_running() -> None:
    """Launch Calendar.app silently in the background if it isn't already running."""
    import time
    running = subprocess.run(
        ["pgrep", "-x", "Calendar"], capture_output=True
    ).returncode == 0
    if not running:
        subprocess.Popen(["open", "-a", "Calendar"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        time.sleep(2.0)   # give it time to initialise


def get_calendar_events(days: int = 7) -> str:
    """Get upcoming calendar events without opening Calendar.app in the foreground."""
    days = max(1, min(days, 30))
    _ensure_calendar_running()

    # NOTE: no 'activate' — query Calendar silently in the background
    script = f"""
tell application "Calendar"
    set theDate to current date
    set endDate to theDate + ({days} * days)
    set output to ""
    repeat with aCal in calendars
        set calName to name of aCal
        if calName does not contain "Siri" then
            try
                set evts to (every event of aCal whose start date >= theDate and start date < endDate)
                repeat with evt in evts
                    set evtTitle to summary of evt
                    set evtStart to start date of evt as string
                    set output to output & evtTitle & "|" & evtStart & "|" & calName & "\\n"
                end repeat
            end try
        end if
    end repeat
    return output
end tell
"""
    ok, out = _run_cal(script)
    if not ok:
        return _PERMISSION_MSG if _permission_error(out) else f"Calendar error: {out}"

    if not out:
        return f"Nothing on the calendar for the next {days} day{'s' if days != 1 else ''}."

    lines  = [l.strip() for l in out.splitlines() if l.strip()]
    events = []
    for line in lines:
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            events.append((parts[0], parts[1], parts[2]))

    if not events:
        return f"No events in the next {days} days."

    label = f"Next {days} day{'s' if days != 1 else ''}:"
    rows  = [f"  • {title} — {start} ({cal})" for title, start, cal in events]
    return label + "\n" + "\n".join(rows)


def create_calendar_event(
    title: str,
    start: str,
    end: str = "",
    calendar: str = "",
    notes: str = "",
    **kwargs,  # absorb any hallucinated fields from smaller models
) -> str:
    """
    Create a Calendar event.
    start/end: natural language like 'today at 3pm', 'tomorrow 9am'.
    """
    if not title or not title.strip():
        return "Couldn't create event — I need an event name. Try again: 'add [event] to calendar for [time]'."
    if not start or not start.strip():
        return "Couldn't create event — I need a start time. Try again: 'add [event] to calendar for [time]'."
    cal_clause = f'set calendar of newEvent to calTarget\n' if calendar else ""
    cal_lookup  = (
        f'set calTarget to first calendar whose name contains "{calendar}"\n'
        if calendar else ""
    )
    end_clause  = (
        f'set end date of newEvent to date "{end}"\n'
        if end else ""
    )
    notes_clause = (
        f'set description of newEvent to "{notes.replace(chr(34), chr(39))}"\n'
        if notes else ""
    )

    _ensure_calendar_running()
    script = f"""
tell application "Calendar"
    {cal_lookup}
    set targetCal to (first calendar whose writable is true)
    set newEvent to make new event at end of events of targetCal
    set summary of newEvent to "{title.replace('"', "'")}"
    set start date of newEvent to date "{start}"
    {end_clause}{notes_clause}reload calendars
end tell
return "Created: {title}"
"""
    ok, out = _run_cal(script)
    if not ok:
        return _PERMISSION_MSG if _permission_error(out) else f"Couldn't create event: {out}"
    return f"Added to calendar: {title}"
