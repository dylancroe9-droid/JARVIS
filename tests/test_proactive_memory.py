"""
Tests for proactive.py — specifically bug #18, the unbounded growth of
the _alerted dict and _seen_emails set.

We don't run the full proactive loop (it'd need calendar / email APIs).
Instead we test the purge logic in isolation.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# We can't `from proactive import ProactiveEngine` cleanly because
# proactive.py imports from server, which has side effects. Read the file
# and exec the class body in a controlled namespace instead — gives us
# the methods under test without booting JARVIS.
PROACTIVE_PATH = Path(__file__).resolve().parent.parent / "proactive.py"


@pytest.fixture(scope="module")
def engine_class():
    """Pull ProactiveEngine out of proactive.py in isolation."""
    # The simplest way: import the module, but mock out the server import
    import importlib
    import types
    fake_server = types.ModuleType("server")
    fake_server.broadcast = lambda *a, **k: None
    sys.modules["server"] = fake_server
    # Need calendar_tool stub
    fake_cal = types.ModuleType("tools.calendar_tool")
    fake_cal.get_calendar_events = lambda **k: []
    sys.modules["tools.calendar_tool"] = fake_cal
    try:
        import proactive
        importlib.reload(proactive)
        yield proactive.ProactiveEngine
    finally:
        sys.modules.pop("server", None)
        sys.modules.pop("tools.calendar_tool", None)


class TestPurgeStaleAlerts:
    def test_recent_entries_kept(self, engine_class):
        e = engine_class()
        now = datetime.now()
        e._alerted = {
            "meeting:a": now - timedelta(minutes=5),
            "meeting:b": now - timedelta(hours=1),
            "meeting:c": now,
        }
        e._purge_stale_alerts()
        assert "meeting:a" in e._alerted
        assert "meeting:b" in e._alerted
        assert "meeting:c" in e._alerted

    def test_stale_entries_purged(self, engine_class):
        e = engine_class()
        now = datetime.now()
        e._alerted = {
            "old:1": now - timedelta(days=2),
            "old:2": now - timedelta(hours=25),
            "fresh": now - timedelta(minutes=10),
        }
        e._purge_stale_alerts()
        assert "old:1" not in e._alerted
        assert "old:2" not in e._alerted
        assert "fresh" in e._alerted

    def test_empty_dict_no_error(self, engine_class):
        e = engine_class()
        e._alerted = {}
        e._purge_stale_alerts()      # must not raise
        assert e._alerted == {}

    def test_boundary_just_inside_ttl(self, engine_class):
        e = engine_class()
        now = datetime.now()
        e._alerted = {
            "borderline": now - timedelta(seconds=engine_class._ALERTED_TTL_SEC - 60),
        }
        e._purge_stale_alerts()
        assert "borderline" in e._alerted

    def test_boundary_just_outside_ttl(self, engine_class):
        e = engine_class()
        now = datetime.now()
        e._alerted = {
            "expired": now - timedelta(seconds=engine_class._ALERTED_TTL_SEC + 60),
        }
        e._purge_stale_alerts()
        assert "expired" not in e._alerted


class TestSeenEmailsCap:
    def test_below_cap_unchanged(self, engine_class):
        e = engine_class()
        e._seen_emails = {f"email{i}" for i in range(50)}
        before = set(e._seen_emails)
        e._purge_stale_alerts()
        assert e._seen_emails == before

    def test_over_cap_gets_trimmed(self, engine_class):
        e = engine_class()
        e._seen_emails = {f"email{i}" for i in range(5000)}
        e._purge_stale_alerts()
        # Cap is currently 2000 threshold → trims to 1000
        assert len(e._seen_emails) <= 2000
        assert len(e._seen_emails) >= 1000

    def test_exactly_at_cap_unchanged(self, engine_class):
        e = engine_class()
        e._seen_emails = {f"email{i}" for i in range(2000)}
        e._purge_stale_alerts()
        # 2000 is NOT > 2000 — should not trim
        assert len(e._seen_emails) == 2000


class TestPurgeUnderConcurrentMutation:
    """Defensive: purge takes a snapshot of items before iterating."""

    def test_purge_works_on_large_dict(self, engine_class):
        e = engine_class()
        now = datetime.now()
        # 5000 stale entries + 500 fresh
        for i in range(5000):
            e._alerted[f"stale:{i}"] = now - timedelta(days=2)
        for i in range(500):
            e._alerted[f"fresh:{i}"] = now - timedelta(minutes=1)
        before = len(e._alerted)
        assert before == 5500
        e._purge_stale_alerts()
        assert len(e._alerted) == 500
        assert all(k.startswith("fresh:") for k in e._alerted)
