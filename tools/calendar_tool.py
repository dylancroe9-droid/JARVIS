"""
Reads and creates macOS Calendar events via AppleScript.
Requires Calendar access: System Settings > Privacy & Security > Calendars
Grant access to Terminal (for chat.py) or the Electron app (for JARVIS app).
"""

import subprocess


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
    """
    Get upcoming calendar events via Swift EventKit — correctly handles recurring events.
    AppleScript only returns master events (with old start dates), so weekly recurring
    events like guitar lessons never showed up. Swift EventKit returns actual occurrences.
    """
    days = max(1, min(days, 30))
    try:
        return _get_events_swift(days)
    except Exception:
        return _get_events_applescript(days)


def _get_events_swift(days: int) -> str:
    """Run a Swift snippet that uses EventKit to get calendar event occurrences."""
    import tempfile, os as _os
    swift_code = f"""
import EventKit
import Foundation

let store = EKEventStore()
let sema  = DispatchSemaphore(value: 0)
var ok    = false
store.requestFullAccessToEvents {{ granted, _ in ok = granted; sema.signal() }}
sema.wait()
guard ok else {{ print("NO_ACCESS"); exit(0) }}

let cal   = Calendar.current
let today = cal.startOfDay(for: Date())
let end   = cal.date(byAdding: .day, value: {days}, to: today)!
let pred  = store.predicateForEvents(withStart: today, end: end, calendars: nil)
let evts  = store.events(matching: pred).sorted {{ $0.startDate < $1.startDate }}

if evts.isEmpty {{ print("NONE"); exit(0) }}
let fmt = DateFormatter()
fmt.dateFormat = "EEE MMM d 'at' h:mm a"
for e in evts {{
    let cn = e.calendar.title
    let skip = ["siri","holiday","birthday"]
    if skip.contains(where: {{ cn.lowercased().contains($0) }}) {{ continue }}
    print("\\(e.title ?? "No title")|\\(fmt.string(from: e.startDate))|\\(cn)")
}}
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.swift', delete=False) as f:
        f.write(swift_code)
        tmp = f.name
    try:
        r = subprocess.run(
            ['swift', tmp],
            capture_output=True, text=True, timeout=15
        )
    finally:
        try: _os.unlink(tmp)
        except Exception: pass

    if r.returncode != 0 or 'NO_ACCESS' in r.stdout:
        raise RuntimeError("Swift/EventKit unavailable")

    out = r.stdout.strip()
    if not out or out == 'NONE':
        return f"Nothing on the calendar for the next {days} day{'s' if days != 1 else ''}."

    rows = []
    for line in out.splitlines():
        parts = line.split('|')
        if len(parts) >= 3:
            rows.append(f"  • {parts[0]} — {parts[1]} ({parts[2]})")

    if not rows:
        return f"Nothing on the calendar for the next {days} day{'s' if days != 1 else ''}."

    label = f"Next {days} day{'s' if days != 1 else ''}:"
    return label + "\n" + "\n".join(rows)


def _get_events_applescript(days: int) -> str:
    """Fallback: AppleScript query (misses recurring event occurrences)."""
    _ensure_calendar_running()
    script = f"""
tell application "Calendar"
    set theDate to current date
    set time of theDate to 0
    set endDate to theDate + ({days} * days)
    set output to ""
    repeat with aCal in calendars
        set calName to name of aCal
        if calName does not contain "Siri" and calName does not contain "Suggestions" then
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


def _esc_as(s: str) -> str:
    """
    Escape a Python string for safe embedding inside an AppleScript
    double-quoted literal. Handles:
      - backslash (must be escaped FIRST so we don't double-escape the
        backslashes we add for quote escaping)
      - double-quote
      - carriage return / newline / tab — turn into spaces so they can't
        break out of the string into new AppleScript statements
    """
    if s is None:
        return ""
    return (
        s.replace("\\", "\\\\")
         .replace('"', '\\"')
         .replace("\r", " ")
         .replace("\n", " ")
         .replace("\t", " ")
    )


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

    # All free-form fields get escaped before reaching AppleScript. Prior
    # to this, calendar / title / start / end / notes were f-string
    # interpolated raw — any " or newline would break out and inject new
    # AppleScript statements (verified reproducible).
    e_title    = _esc_as(title)
    e_start    = _esc_as(start)
    e_end      = _esc_as(end)
    e_calendar = _esc_as(calendar)
    e_notes    = _esc_as(notes)

    # Pick the target calendar. If the user named one, try to match it by
    # name — but wrapped so a non-matching name falls back to the first
    # writable calendar instead of throwing and failing the whole creation
    # (previously `calTarget` was computed then never used, AND an unmatched
    # name aborted the event). Now the name actually takes effect when it
    # matches, and degrades gracefully when it doesn't.
    if calendar:
        cal_lookup = (
            'set targetCal to missing value\n'
            f'    try\n'
            f'        set targetCal to (first calendar whose name contains "{e_calendar}" and writable is true)\n'
            f'    end try\n'
            f'    if targetCal is missing value then set targetCal to (first calendar whose writable is true)\n'
        )
    else:
        cal_lookup = 'set targetCal to (first calendar whose writable is true)\n'
    end_clause  = (
        f'set end date of newEvent to date "{e_end}"\n'
        if end else ""
    )
    notes_clause = (
        f'set description of newEvent to "{e_notes}"\n'
        if notes else ""
    )

    _ensure_calendar_running()
    script = f"""
tell application "Calendar"
    {cal_lookup}
    set newEvent to make new event at end of events of targetCal
    set summary of newEvent to "{e_title}"
    set start date of newEvent to date "{e_start}"
    {end_clause}{notes_clause}reload calendars
end tell
return "Created: {e_title}"
"""
    ok, out = _run_cal(script)
    if not ok:
        return _PERMISSION_MSG if _permission_error(out) else f"Couldn't create event: {out}"
    return f"Added to calendar: {title}"


# ── Visual calendar reader (screenshot + vision) ─────────────────────────────
#
# EventKit / AppleScript ONLY sees calendars Dylan has explicitly subscribed to
# in Apple Calendar.app. If his events live in Google Calendar / Outlook /
# school portals that he hasn't synced to Apple Calendar, EventKit returns
# "nothing scheduled" — wrong. The screenshot approach sees whatever Calendar.app
# is actually showing him, regardless of source.

def _vision_read_image(image_path: str, question: str, view_key: str) -> str:
    """Send an image path to the Groq vision model and return its description."""
    import os
    import base64
    import subprocess
    # Downsize to keep upload reasonable
    try:
        subprocess.run(["sips", "-Z", "1600", image_path], capture_output=True, timeout=5)
    except Exception:
        pass
    try:
        with open(image_path, "rb") as f:
            image_b64 = base64.standard_b64encode(f.read()).decode("utf-8")
    except Exception as exc:
        return f"Could not read calendar screenshot: {exc}"

    if question.strip():
        q = question.strip()
    else:
        range_label = {"1": "today", "2": "this week", "3": "this month"}[view_key]
        q = (
            f"You are looking at the Apple Calendar app showing {range_label}. "
            f"Scan ALL visible days and list EVERY scheduled event you can see. "
            f"For each event give: day of week, date, time (if shown), and event title. "
            f"Group events by day chronologically. Skip days with nothing. "
            f"If you cannot see any events at all, say exactly: 'No events visible on the calendar.' "
            f"NEVER invent events. Only report what is actually visible. "
            f"Skip US Holidays / Birthdays / banners — only real scheduled events."
        )

    from openai import OpenAI
    from config import GROQ_API_KEY
    # Explicit timeout — without it the SDK default is ~600s, which would
    # hang the user-facing "read my calendar" path on a stalled request.
    client = OpenAI(api_key=GROQ_API_KEY,
                    base_url="https://api.groq.com/openai/v1", timeout=25)
    try:
        response = client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                    {"type": "text", "text": q},
                ],
            }],
            max_tokens=1024,
        )
        return response.choices[0].message.content or "No response from vision model."
    except Exception as exc:
        return f"Calendar vision read failed: {exc}"
    finally:
        try:
            os.remove(image_path)
        except Exception:
            pass


def _get_calendar_window_id() -> "int | None":
    """
    Return Calendar.app's main window ID, or None if we can't find it.
    Used to screencapture ONLY the Calendar window, bypassing the JARVIS
    Electron HUD that sits always-on-top.
    """
    # Quartz CGWindowList — most reliable, doesn't need Accessibility perm.
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
        )
        windows = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly, kCGNullWindowID
        )
        # Pick the largest Calendar window (skips menu-bar fragments).
        candidates = [
            w for w in windows
            if w.get("kCGWindowOwnerName") == "Calendar"
            and w.get("kCGWindowLayer", 0) == 0
            and w.get("kCGWindowBounds", {}).get("Width", 0) > 400
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda w: w["kCGWindowBounds"]["Width"] * w["kCGWindowBounds"]["Height"],
            reverse=True,
        )
        return int(candidates[0].get("kCGWindowNumber") or 0) or None
    except Exception:
        return None


def read_calendar_visually(question: str = "", view: str = "month") -> str:
    """
    Open Apple Calendar, screenshot JUST the Calendar window (not the
    JARVIS HUD on top of it), and use vision to read events.

    view: "month" (default — shows 5 weeks at once, best for "what's coming up")
          "week"  (just current week — more detail per event)
          "day"   (just today)
    """
    import subprocess
    import time
    import os

    # 1. Open Calendar.app and bring it to the front
    try:
        subprocess.run(["open", "-a", "Calendar"], capture_output=True, timeout=4)
    except Exception as exc:
        return f"Couldn't open Calendar app: {exc}"

    # 2. Switch to the requested view via cmd+1 (day), cmd+2 (week), cmd+3 (month)
    view_key = {"day": "1", "week": "2", "month": "3"}.get(view.lower(), "3")
    try:
        subprocess.run(
            ["osascript", "-e",
             f'tell application "System Events" to keystroke "{view_key}" using {{command down}}'],
            capture_output=True, timeout=3,
        )
    except Exception:
        pass

    # 3. Give it a moment to render (month view is heavier than week)
    time.sleep(1.5)

    # 4. Capture JUST Calendar's window — bypasses the always-on-top JARVIS HUD.
    cal_window_id = _get_calendar_window_id()
    SCREENSHOT_PATH = "/tmp/jarvis_calendar.png"
    if cal_window_id:
        try:
            r = subprocess.run(
                ["screencapture", "-x", "-l", str(cal_window_id), "-t", "png",
                 SCREENSHOT_PATH],
                capture_output=True, timeout=5,
            )
            if r.returncode == 0 and os.path.getsize(SCREENSHOT_PATH) > 20_000:
                return _vision_read_image(SCREENSHOT_PATH, question, view_key)
        except Exception:
            pass
        # Window capture failed or produced a too-small image and we're about
        # to fall through — clean up the temp PNG so it doesn't linger.
        try:
            if os.path.exists(SCREENSHOT_PATH):
                os.unlink(SCREENSHOT_PATH)
        except Exception:
            pass

    # Fallback: full-screen capture via the generic take_screenshot path.
    # (Will include the JARVIS HUD, but better than nothing.)
    from tools.screen_tool import take_screenshot
    if question.strip():
        q = question.strip()
    else:
        range_label = {"day": "today", "week": "this week", "month": "this month"}[
            "day" if view_key == "1" else "week" if view_key == "2" else "month"
        ]
        q = (
            f"You are looking at the Apple Calendar app showing {range_label}. "
            f"Scan ALL visible days and list EVERY event you can see, even if the text is small. "
            f"For each event give: day of week, date, time (if shown), and event title. "
            f"Group events by day chronologically. "
            f"If a day has no events, skip it (don't list empty days). "
            f"If you cannot see any events at all, say exactly: 'No events visible on the calendar.' "
            f"NEVER invent events or guess from holidays/labels. Only report what is actually visible. "
            f"Skip US Holidays / Birthdays / decorative banners — only real scheduled events."
        )
    result = take_screenshot(question=q)
    return result
