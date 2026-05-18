"""
proactive.py — JARVIS proactive intelligence engine.

Runs as a background thread. Checks every 5 minutes for:
  1. Upcoming calendar events  → 15-minute heads-up toast
  2. Unread emails with invites → ask Dylan → add to calendar
  3. Memory-based goal reminders → gentle nudge toast

Email invite flow:
  detect invite keywords → extract details (Claude Haiku or regex)
  → confirm_request dialog ("Add to calendar?") → create_calendar_event
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

# ── Config ─────────────────────────────────────────────────────────────────────
POLL_INTERVAL        = 300    # seconds between checks (5 min)
REPEAT_COOLDOWN      = 3600   # don't re-alert same key within 1 hour
MEETING_WARN_MINUTES = 15     # warn this many minutes before a meeting
QUIET_START          = 23     # no alerts at/after 11pm
QUIET_END            = 8      # no alerts before 8am

# Keywords that suggest an email contains an event invitation
_INVITE_KW = (
    "invite", "invitation", "invited", "you're invited", "you are invited",
    "rsvp", "join us", "register", "registration", "webinar",
    "conference", "workshop", "seminar", "meeting request", "calendar invite",
    "attend", "attendance", "event on", "event at",
)


class ProactiveEngine:
    def __init__(self) -> None:
        self._alerted:       dict[str, datetime] = {}
        self._seen_emails:   set[str]             = set()
        self._stop           = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="proactive"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ── Main loop ──────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        # Let the server fully boot before first check
        self._stop.wait(45)
        while not self._stop.is_set():
            try:
                self._check_upcoming_meetings()
                self._check_email_invitations()
                self._check_memory_goals()
            except Exception:
                pass  # never crash the background thread
            self._stop.wait(POLL_INTERVAL)

    # ── Guards ─────────────────────────────────────────────────────────────────

    def _is_quiet(self) -> bool:
        h = datetime.now().hour
        if QUIET_START > QUIET_END:          # wraps midnight
            return h >= QUIET_START or h < QUIET_END
        return QUIET_START <= h < QUIET_END

    def _cooldown_ok(self, key: str) -> bool:
        last = self._alerted.get(key)
        return last is None or (datetime.now() - last).total_seconds() >= REPEAT_COOLDOWN

    def _mark(self, key: str) -> None:
        self._alerted[key] = datetime.now()

    # ── 1. Upcoming meetings ───────────────────────────────────────────────────

    def _check_upcoming_meetings(self) -> None:
        from tools.calendar_tool import get_calendar_events
        from server import broadcast

        raw = get_calendar_events(days=1)
        if not raw or any(k in raw.lower() for k in ("error", "not granted", "nothing", "no event")):
            return

        now       = datetime.now()
        warn_at   = now + timedelta(minutes=MEETING_WARN_MINUTES)

        for line in raw.splitlines():
            line = line.strip().lstrip("•").strip()
            if not line or "—" not in line:
                continue
            title_part, _, rest = line.partition("—")
            title    = title_part.strip()
            event_dt = self._parse_dt(rest.strip())
            if event_dt is None:
                continue
            diff = event_dt - now
            # Only alert if event is between now and MEETING_WARN_MINUTES from now
            if not (timedelta(0) <= diff <= timedelta(minutes=MEETING_WARN_MINUTES)):
                continue
            key = f"mtg:{title}:{event_dt.strftime('%Y%m%d%H%M')}"
            if not self._cooldown_ok(key) or self._is_quiet():
                continue
            self._mark(key)
            mins = max(0, int(diff.total_seconds() / 60))
            when = f"in {mins} minute{'s' if mins != 1 else ''}" if mins > 0 else "right now"
            broadcast({"type": "proactive", "text": f"📅 '{title}' starts {when}."})

    def _parse_dt(self, s: str) -> Optional[datetime]:
        """Parse a date string from calendar output (strips trailing calendar name)."""
        s = re.sub(r'\s*\([^)]+\)\s*$', '', s).strip()
        for fmt in (
            "%A, %B %d, %Y at %I:%M:%S %p",
            "%A, %B %d, %Y at %I:%M %p",
            "%B %d, %Y at %I:%M:%S %p",
            "%B %d, %Y at %I:%M %p",
        ):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    # ── 2. Email invitations ───────────────────────────────────────────────────

    def _check_email_invitations(self) -> None:
        from tools.email_tools import get_unread_emails
        from tools.calendar_tool import create_calendar_event
        from server import broadcast, request_permission

        raw = get_unread_emails(count=20)
        if not raw or any(k in raw.lower() for k in ("no unread", "error", "not granted", "not allowed")):
            return

        for block in raw.split("---"):
            block = block.strip()
            if not block:
                continue

            sender = subject = preview = ""
            for line in block.splitlines():
                if line.startswith("FROM:"):
                    sender  = line[5:].strip()
                elif line.startswith("SUBJECT:"):
                    subject = line[8:].strip()
                elif line.startswith("PREVIEW:"):
                    preview = line[8:].strip()

            if not subject:
                continue

            key = f"email:{subject[:80]}:{sender[:40]}"
            if key in self._seen_emails or not self._cooldown_ok(key):
                continue

            combined = (subject + " " + preview).lower()
            if not any(kw in combined for kw in _INVITE_KW):
                continue

            # Mark immediately so we don't re-process on next poll
            self._seen_emails.add(key)
            self._mark(key)

            if self._is_quiet():
                continue

            # Extract event details
            details = self._extract_event_details(subject, preview)
            if not details:
                continue

            title    = details.get("title") or subject[:60]
            date_str = details.get("date", "")
            time_str = details.get("time", "")
            when_str = f"{date_str} {time_str}".strip()

            # Ask Dylan via the confirm dialog
            msg_lines = [
                f"📧 From: {sender}",
                f'"{subject}"',
                "",
                "Looks like an event invitation."
                + (f" ({when_str})" if when_str else ""),
                "",
                "Add it to your calendar?",
            ]
            approved = request_permission(
                "\n".join(msg_lines),
                title="📅 CALENDAR INVITE",
                timeout=90.0,
            )

            if approved:
                result = create_calendar_event(
                    title=title,
                    start=when_str if when_str else "today",
                )
                broadcast({"type": "proactive", "text": f"✓ Added: {title}"})
                # Also echo into chat so it feels natural
                broadcast({"type": "chunk",    "text": result})
                broadcast({"type": "done",     "full_text": result})
            else:
                broadcast({"type": "proactive", "text": f"Skipped '{title}'."})

    def _extract_event_details(self, subject: str, preview: str) -> Optional[dict]:
        """Use Claude Haiku to extract event details; fall back to regex."""
        from config import ANTHROPIC_API_KEY
        if ANTHROPIC_API_KEY.startswith("sk-ant-"):
            try:
                return self._extract_claude(subject, preview)
            except Exception:
                pass
        return self._extract_regex(subject, preview)

    def _extract_claude(self, subject: str, preview: str) -> Optional[dict]:
        import anthropic, json as _j
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=120,
            system=(
                "Extract event details from this email snippet. "
                "Output ONLY valid JSON: {\"title\":\"...\",\"date\":\"...\",\"time\":\"...\"}. "
                "date = natural language (e.g. 'June 5' or 'tomorrow'). "
                "time = e.g. '7:00 PM' or ''. "
                "If this is NOT a real event invitation, output: null"
            ),
            messages=[{"role": "user", "content": f"SUBJECT: {subject}\nPREVIEW: {preview[:300]}"}],
        )
        text = resp.content[0].text.strip() if resp.content else ""
        if not text or text == "null":
            return None
        s, e = text.find("{"), text.rfind("}") + 1
        return _j.loads(text[s:e]) if s >= 0 and e > s else None

    def _extract_regex(self, subject: str, preview: str) -> Optional[dict]:
        combined = subject + " " + preview
        date_m = re.search(
            r'\b((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
            r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
            r'\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?'
            r'|\d{1,2}/\d{1,2}(?:/\d{2,4})?'
            r'|(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))',
            combined, re.IGNORECASE,
        )
        time_m = re.search(
            r'\b(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?|\d{1,2}\s*(?:AM|PM|am|pm))\b',
            combined, re.IGNORECASE,
        )
        return {
            "title": subject[:80],
            "date":  date_m.group(0) if date_m else "",
            "time":  time_m.group(0) if time_m else "",
        }

    # ── 3. Memory goal reminders ───────────────────────────────────────────────

    def _check_memory_goals(self) -> None:
        from brain.memory import _parse_facts, _strip_ts
        from server import broadcast

        if self._is_quiet():
            return

        facts = _parse_facts()
        if not facts:
            return

        now    = datetime.now()
        month  = now.strftime("%B").lower()
        year   = str(now.year)

        for fact in facts:
            text = _strip_ts(fact).lower()
            has_goal = any(w in text for w in (
                "goal", "deadline", "by ", "want to", "need to", "due", "target", "aim"
            ))
            has_time = any(t in text for t in (month, year, "this week", "this month"))
            if not (has_goal and has_time):
                continue
            key = f"goal:{text[:60]}"
            if not self._cooldown_ok(key):
                continue
            self._mark(key)
            broadcast({"type": "proactive", "text": f"🎯 {_strip_ts(fact)}"})
            break  # one goal reminder per cycle


# ── Module-level singleton ─────────────────────────────────────────────────────

_engine: Optional[ProactiveEngine] = None


def start() -> None:
    global _engine
    if _engine is None:
        _engine = ProactiveEngine()
        _engine.start()
        print("[proactive] engine started", flush=True)


def stop() -> None:
    global _engine
    if _engine:
        _engine.stop()
        _engine = None
