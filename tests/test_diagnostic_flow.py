"""
Integration tests for tools/diagnostics.py — the auto-investigation pipeline.

Tests the *event-emitting contract* of run_full_diagnostic and verifies the
investigation stash gets populated and cleared correctly across runs. We use
a stripped-down CHECKS list via monkeypatch so the tests run fast (<1s) and
don't depend on real external state (network, Ollama, ElevenLabs).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import diagnostics as diag
from tools.diagnostics import (
    CHECKS,
    Check,
    _LAST_INVESTIGATIONS,
    get_last_investigations,
    run_full_diagnostic,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tiny_checks(monkeypatch):
    """
    Replace the full CHECKS list with a tiny test set so tests are fast
    and predictable. Includes one pass, one auto-fixable fail, and one
    unfixable fail to cover all status transitions.
    """
    test_set = [
        Check("test_pass", "Always pass", lambda: True,
              "would not happen", log_keywords=["never"]),
        Check("test_fixable", "Fixable fail", lambda: False,
              "fixable impact",
              fix=lambda: True,
              log_keywords=["fixable"]),
        Check("test_unfixable", "Unfixable fail", lambda: False,
              "unfixable impact",
              log_keywords=["unfixable"], source_files=["tests/fake.py"]),
    ]
    monkeypatch.setattr(diag, "CHECKS", test_set)
    # Also collapse the timing so tests run quickly
    monkeypatch.setattr(diag, "_MIN_PROBE_DURATION_SEC", 0.0)
    monkeypatch.setattr(diag, "_FIX_VISIBLE_PAUSE_SEC", 0.0)
    monkeypatch.setattr(diag, "_INVESTIGATE_VISIBLE_SEC", 0.0)
    yield test_set


@pytest.fixture(autouse=True)
def clean_stash():
    """Always start each test with an empty investigation stash."""
    _LAST_INVESTIGATIONS.clear()
    yield
    _LAST_INVESTIGATIONS.clear()


# ── Event-sequence tests ──────────────────────────────────────────────────────

class TestEventSequence:
    def test_emits_start_and_done(self, tiny_checks):
        events = list(run_full_diagnostic())
        assert events[0]["type"] == "diagnostic_start"
        assert events[0]["total"] == len(tiny_checks)
        assert events[-1]["type"] == "diagnostic_done"

    def test_step_count_matches_checks(self, tiny_checks):
        events = list(run_full_diagnostic())
        # 2 step events per check (running + final). Failed checks ALSO emit
        # a 'fixing' step if fixable. Let's count by status.
        running_events = [e for e in events
                          if e.get("type") == "diagnostic_step" and e.get("status") == "running"]
        # One "running" emit per check
        assert len(running_events) == len(tiny_checks)

    def test_pass_check_emits_pass(self, tiny_checks):
        events = list(run_full_diagnostic())
        # The first check is "always pass"
        final_pass = next(
            e for e in events
            if e.get("type") == "diagnostic_step"
            and e.get("name") == "test_pass"
            and e.get("status") != "running"
        )
        assert final_pass["status"] == "pass"

    def test_fix_attempt_emits_fixing_then_fixed(self, tiny_checks):
        events = list(run_full_diagnostic())
        fixable_events = [e for e in events
                          if e.get("type") == "diagnostic_step"
                          and e.get("name") == "test_fixable"]
        # Expect: running → fixing → fixed (3 emissions)
        statuses = [e["status"] for e in fixable_events]
        assert "running" in statuses
        assert "fixing" in statuses
        assert "fixed" in statuses

    def test_unfixable_emits_running_then_fail(self, tiny_checks):
        events = list(run_full_diagnostic())
        unfix_events = [e for e in events
                        if e.get("type") == "diagnostic_step"
                        and e.get("name") == "test_unfixable"]
        statuses = [e["status"] for e in unfix_events]
        assert statuses == ["running", "fail"]


# ── Stash behavior tests ──────────────────────────────────────────────────────

class TestInvestigationStash:
    def test_failure_triggers_investigation(self, tiny_checks):
        events = list(run_full_diagnostic())
        investigation_events = [e for e in events
                                if e.get("type", "").startswith("diagnostic_investigation")]
        # 1 failure (test_unfixable) → 2 events (start + done)
        # test_fixable got fixed, so no investigation
        assert len(investigation_events) == 2
        assert investigation_events[0]["type"] == "diagnostic_investigation_start"
        assert investigation_events[1]["type"] == "diagnostic_investigation_done"

    def test_fixed_checks_dont_get_investigated(self, tiny_checks):
        events = list(run_full_diagnostic())
        invest_for_fixed = [e for e in events
                            if e.get("type", "").startswith("diagnostic_investigation")
                            and e.get("name") == "test_fixable"]
        assert invest_for_fixed == [], "auto-fixed checks should not trigger investigation"

    def test_stash_populated_after_run(self, tiny_checks):
        list(run_full_diagnostic())
        stash = get_last_investigations()
        # Only test_unfixable failed and got investigated
        assert "test_unfixable" in stash
        assert "test_pass" not in stash
        assert "test_fixable" not in stash

    def test_stash_finding_has_required_fields(self, tiny_checks):
        list(run_full_diagnostic())
        stash = get_last_investigations()
        finding = stash["test_unfixable"]
        assert finding["check"] == "test_unfixable"
        assert finding["label"] == "Unfixable fail"
        assert "summary" in finding
        assert isinstance(finding["log_matches"], list)
        assert finding["source_files"] == ["tests/fake.py"]

    def test_stash_cleared_at_start_of_new_run(self, tiny_checks):
        # Seed the stash with stale data from a previous run
        _LAST_INVESTIGATIONS["stale_check"] = {
            "check": "stale_check", "label": "Stale",
            "log_matches": [], "source_files": [], "summary": "old",
        }
        list(run_full_diagnostic())
        # Stale entry must be gone
        stash = get_last_investigations()
        assert "stale_check" not in stash

    def test_stash_expires_after_ttl(self, tiny_checks, monkeypatch):
        # Run a diagnostic, confirm stash is populated, then jump time
        # forward past the TTL — get_last_investigations should return empty.
        list(run_full_diagnostic())
        assert "test_unfixable" in get_last_investigations()
        # Force the run timestamp into the deep past
        import tools.diagnostics as d
        d._LAST_INVESTIGATIONS_RUN_AT = d._LAST_INVESTIGATIONS_RUN_AT - (d._STASH_TTL_SEC + 60)
        # Stash dict still has the entry but the accessor should hide it
        assert get_last_investigations() == {}


# ── Done-event summary tests ──────────────────────────────────────────────────

class TestDoneSummary:
    def test_done_counts_correct(self, tiny_checks):
        events = list(run_full_diagnostic())
        done = events[-1]
        assert done["type"] == "diagnostic_done"
        assert done["passed"] == 1     # test_pass
        assert done["fixed"]  == 1     # test_fixable
        assert done["failed"] == 1     # test_unfixable

    def test_done_includes_full_results(self, tiny_checks):
        events = list(run_full_diagnostic())
        done = events[-1]
        assert "results" in done
        assert len(done["results"]) == len(tiny_checks)
        names = {r["name"] for r in done["results"]}
        assert names == {"test_pass", "test_fixable", "test_unfixable"}


# ── Fixability annotation tests ───────────────────────────────────────────────

class TestFixabilityFlag:
    def test_fixable_flag_true_when_fix_exists(self, tiny_checks):
        events = list(run_full_diagnostic())
        for e in events:
            if e.get("type") == "diagnostic_step" and e.get("name") == "test_fixable":
                assert e.get("fixable") is True
            if e.get("type") == "diagnostic_step" and e.get("name") == "test_unfixable":
                assert e.get("fixable") is False
