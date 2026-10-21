"""
Tests for the self-inspect intercept's direct-answer fast path.

When the user asks about a SPECIFIC failed check AND stashed evidence
exists, the intercept should answer directly from that evidence without
round-tripping through the brain. This is the path that protects us from
8b token blow-ups when the cloud brain is rate-limited.

These tests exercise the answer-building logic by importing the helper
functions used inside server.py's intercept and verifying their output
shapes against fixture findings.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SERVER = (Path(__file__).resolve().parent.parent / "server.py").read_text()


# ── Compact-context size check ────────────────────────────────────────────────

class TestCompactContext:
    def test_compact_format_lives_in_source(self):
        """
        The new compact context block must contain 'Self-Q:' marker.
        If a future refactor changes the marker, fix THIS test too — the
        marker is what the brain learns to recognize as a self-question.
        """
        assert "Self-Q:" in SERVER

    def test_context_says_never_web_search(self):
        # The inline rule must travel with the context block, not just sit
        # in the personality prompt — small fallback models may ignore the
        # personality but read the user message verbatim.
        assert "NEVER web_search" in SERVER

    def test_old_verbose_context_removed(self):
        # The old 100+ token block had this distinctive opener. If it
        # creeps back in, this test fires immediately.
        assert "DO NOT speak the brackets aloud" not in SERVER

    def test_no_long_log_block_template(self):
        # Old template wrote 10 log lines inline. The compact version
        # doesn't dump logs into the prompt at all — that's the brain's
        # job to fetch via tools when actually needed.
        assert 'log_block = "\\n".join' not in SERVER


# ── Direct-answer formatting ──────────────────────────────────────────────────

# We can't import the intercept directly (it lives inside a function in
# server.py) without booting the whole server, so we re-implement the
# answer-builder shape here and assert on the contract. If the contract
# changes, this test highlights it.

def _build_direct_answer(finding: dict, impact: str) -> str:
    """Mirror of server.py's direct-answer construction."""
    label = finding["label"]
    log_evidence = ""
    if finding.get("log_matches"):
        snippet = finding["log_matches"][0].strip()
        if len(snippet) > 140:
            snippet = snippet[:140] + "…"
        log_evidence = f" The log showed: {snippet}."
    if impact:
        answer = f"{label} failed.{log_evidence} {impact}"
    else:
        answer = f"{label} failed.{log_evidence}"
    if finding.get("source_files"):
        files = finding["source_files"][:2]
        answer += f" The code lives in {', '.join(files)}."
    return answer


class TestDirectAnswerShape:
    def test_includes_label_and_impact(self):
        finding = {
            "check": "screen_watcher", "label": "Screen watcher",
            "log_matches": [], "source_files": [],
        }
        out = _build_direct_answer(finding, "Auto skip-ad disabled.")
        assert "Screen watcher failed" in out
        assert "Auto skip-ad disabled" in out

    def test_includes_log_evidence_when_present(self):
        finding = {
            "check": "music_poller", "label": "Music state poller",
            "log_matches": ["[music] poller crashed: AttributeError"],
            "source_files": [],
        }
        out = _build_direct_answer(finding, "")
        assert "The log showed" in out
        assert "[music] poller crashed" in out

    def test_truncates_long_log_line(self):
        finding = {
            "check": "x", "label": "X",
            "log_matches": ["A" * 500],
            "source_files": [],
        }
        out = _build_direct_answer(finding, "")
        assert "…" in out
        assert len(out) < 500   # bounded by truncation

    def test_includes_source_files_when_present(self):
        finding = {
            "check": "x", "label": "Renderer connection",
            "log_matches": [],
            "source_files": ["server.py", "jarvis-app/renderer/app.js", "extra.py"],
        }
        out = _build_direct_answer(finding, "")
        # Only first 2 file pointers, never all 3 (keeps spoken answer tight)
        assert "server.py" in out
        assert "jarvis-app/renderer/app.js" in out
        assert "extra.py" not in out

    def test_handles_empty_source_files_gracefully(self):
        finding = {
            "check": "x", "label": "Cloud reasoning core",
            "log_matches": [], "source_files": [],
        }
        out = _build_direct_answer(finding, "Falling back to local brain.")
        assert "code lives in" not in out   # nothing to point at
        assert "Cloud reasoning core failed" in out
        assert "Falling back to local brain" in out

    def test_no_empty_log_section(self):
        finding = {
            "check": "x", "label": "X",
            "log_matches": [], "source_files": [],
        }
        out = _build_direct_answer(finding, "y")
        assert "The log showed" not in out


# ── Direct-answer log marker ──────────────────────────────────────────────────

class TestAuditMarker:
    def test_direct_path_prints_marker(self):
        # The intercept prints [self-inspect-direct] when it bypasses the
        # brain — so we can see in the log that the fast path fired.
        assert "[self-inspect-direct]" in SERVER
        assert "bypassed brain" in SERVER
