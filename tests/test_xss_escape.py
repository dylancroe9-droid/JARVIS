"""
Tests for bug #5 — XSS in the diagnostic investigation panel hint.

We can't run JavaScript in pytest directly, but we can extract the
`_escapeHtml()` function from the source and reproduce it in Python to
verify the escape table is complete. Then we grep the source to confirm
every interpolation into innerHTML inside _diagAttachInvestigation goes
through the escape function (no raw `${field}` left in the hint).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

APP_JS = (Path(__file__).resolve().parent.parent
          / "jarvis-app" / "renderer" / "app.js").read_text()


# ── Source-level invariants ───────────────────────────────────────────────────

class TestNoRawInterpolationInHint:
    """The specific bug — `Ask "why did ${event.label} fail?"` was raw."""

    def test_event_label_in_hint_is_escaped(self):
        # Find the _diagAttachInvestigation function block
        m = re.search(
            r'function _diagAttachInvestigation\b.*?\n\}',
            APP_JS, re.DOTALL,
        )
        assert m, "could not locate _diagAttachInvestigation in app.js"
        block = m.group(0)
        # The hint line must use _escapeHtml(event.label), NOT bare event.label
        # Look for the hint and confirm it's wrapped
        hint_lines = [ln for ln in block.splitlines()
                      if "diag-investigate-hint" in ln]
        assert hint_lines, "hint line missing from function"
        for ln in hint_lines:
            assert "_escapeHtml(event.label)" in ln, \
                f"hint must escape event.label; got: {ln.strip()}"
            # No bare ${event.label} (without escape) in the hint
            assert "${event.label}" not in ln or "_escapeHtml(event.label)" in ln


# ── Escape function contract ──────────────────────────────────────────────────

def python_escape_html(s):
    """Pure-Python mirror of the _escapeHtml in app.js."""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


class TestEscapeCoverage:
    """Verify the table in _escapeHtml covers every dangerous character."""

    def test_escape_function_exists_in_source(self):
        # Verify both helpers exist (we have both _escapeHtml and escHtml)
        assert "function _escapeHtml" in APP_JS
        assert "function escHtml" in APP_JS

    def test_escape_table_in_source_matches_python_mirror(self):
        # Grab the _escapeHtml body and pick out the substitution table
        m = re.search(
            r"function _escapeHtml \(s\) \{[^}]+\{([^}]+)\}",
            APP_JS, re.DOTALL,
        )
        assert m, "could not locate _escapeHtml body"
        body = m.group(1)
        # All five HTML-sensitive characters must be in the table
        for ch, entity in [
            ("&", "&amp;"),
            ("<", "&lt;"),
            (">", "&gt;"),
            ('"', "&quot;"),
            ("'", "&#39;"),
        ]:
            assert entity in body, f"escape entity missing from table: {entity}"


# ── Attack payload verification ───────────────────────────────────────────────

@pytest.mark.parametrize("payload", [
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    "</div><script>fetch('/steal?c='+document.cookie)</script>",
    "javascript:void(0)",
    "<svg onload=alert(1)>",
    "&lt;already-escaped&gt;",                # idempotent? maybe not, but safe
    "normal label",
])
def test_payload_is_neutralized(payload):
    escaped = python_escape_html(payload)
    # After escape, the result must contain NO live HTML tags. A `<` MUST
    # be followed by an entity, not by a tag name.
    assert "<script" not in escaped
    assert "<img" not in escaped
    assert "<svg" not in escaped
    assert "<div" not in escaped
    # Quotes must not be live — `"><` is a classic XSS attempt
    assert '">' not in escaped


# ── Sweep — any other innerHTML interpolations missing escapes ────────────────

class TestNoUnescapedTemplateInDiagInvestigation:
    """
    Walk the body of _diagAttachInvestigation looking for any `${var}` in
    its template literals that's NOT inside _escapeHtml(...). If we find
    one, fail — that's a future XSS waiting to happen.
    """

    def test_all_interpolations_escaped_or_safe(self):
        m = re.search(
            r'function _diagAttachInvestigation\b.*?\n\}',
            APP_JS, re.DOTALL,
        )
        block = m.group(0)
        # Find all ${...} interpolations inside template literals
        interpolations = re.findall(r"\$\{([^}]+)\}", block)
        # An interpolation is OK if it's:
        #   - already passed through _escapeHtml(...)
        #   - a literal numeric count like logs.length
        #   - boolean / conditional
        SAFE_PATTERNS = (
            r"^_escapeHtml\(",                # already escaped
            r"^logs\.length$",
            r"^sources\.length$",
            r"\.length$",
            r"^logs\.map\b",                  # internal map that escapes per-item
            r"^sources\.map\b",
            r"^'[^']*'$",                     # literal string
            # HTML chunks built earlier in the same function — verified by
            # inspection to use _escapeHtml on every nested interpolation
            r"^logHtml$",
            r"^srcHtml$",
        )
        unsafe = []
        for expr in interpolations:
            expr_stripped = expr.strip()
            if any(re.search(p, expr_stripped) for p in SAFE_PATTERNS):
                continue
            unsafe.append(expr_stripped)
        assert not unsafe, (
            f"unescaped interpolations in _diagAttachInvestigation: {unsafe}. "
            "Wrap each in _escapeHtml() or add to SAFE_PATTERNS if you're "
            "sure the source is non-user-controlled."
        )
