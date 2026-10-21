"""
Tests for the anti-fabrication filter in tools/deep_scan.py:_parse_ai_findings.

This is the core defense against "false reports to show something": every AI
finding must quote a source line that ACTUALLY EXISTS in the file. A finding
whose quoted code appears nowhere is a fabrication and must be dropped. These
tests feed crafted model-JSON (no LLM) and assert the filter's behavior.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.deep_scan import _parse_ai_findings


FILE_LINES = [
    "def divide(a, b):",              # 1
    "    return a / b",               # 2
    "",                               # 3
    "def run(cmd):",                  # 4
    "    subprocess.run(cmd, shell=True)",  # 5
    "    return True",                # 6
]


def _model_json(items):
    return json.dumps(items)


class TestFabricationDropped:
    def test_quote_not_in_file_is_dropped(self):
        # The quoted code does not exist anywhere in FILE_LINES → fabricated.
        raw = _model_json([{
            "line": 2, "code": "os.system(user_input)",
            "severity": "high", "category": "injection",
            "trigger": "any", "message": "shell injection",
        }])
        out = _parse_ai_findings(raw, "mod.py", 1, 6, FILE_LINES)
        assert out == [], "fabricated finding (quote absent) must be dropped"

    def test_real_quote_is_kept(self):
        raw = _model_json([{
            "line": 5, "code": "subprocess.run(cmd, shell=True)",
            "severity": "high", "category": "injection",
            "trigger": "cmd from user", "message": "shell injection via shell=True",
        }])
        out = _parse_ai_findings(raw, "mod.py", 1, 6, FILE_LINES)
        assert len(out) == 1
        assert out[0].line == 5

    def test_relocates_to_true_line_when_number_wrong(self):
        # Quote is real but the model put the wrong line number (99). The
        # filter should relocate to the actual line (5), not trust 99.
        raw = _model_json([{
            "line": 99, "code": "subprocess.run(cmd, shell=True)",
            "severity": "high", "category": "injection",
            "trigger": "cmd", "message": "injection",
        }])
        out = _parse_ai_findings(raw, "mod.py", 1, 6, FILE_LINES)
        assert len(out) == 1
        assert out[0].line == 5, "should relocate to the true line of the quote"


class TestMissingFields:
    def test_no_code_field_dropped(self):
        raw = _model_json([{
            "line": 2, "severity": "high", "category": "x",
            "message": "something", "trigger": "y",
        }])
        assert _parse_ai_findings(raw, "mod.py", 1, 6, FILE_LINES) == []

    def test_no_message_dropped(self):
        raw = _model_json([{
            "line": 5, "code": "subprocess.run(cmd, shell=True)",
            "severity": "high", "category": "x",
        }])
        assert _parse_ai_findings(raw, "mod.py", 1, 6, FILE_LINES) == []

    def test_too_short_quote_dropped(self):
        # A quote under the min length (e.g. "pass") is unverifiable → drop.
        raw = _model_json([{
            "line": 2, "code": "a/b",
            "severity": "low", "category": "x", "message": "div",
        }])
        assert _parse_ai_findings(raw, "mod.py", 1, 6, FILE_LINES) == []


class TestMalformedInput:
    def test_empty_string(self):
        assert _parse_ai_findings("", "mod.py", 1, 6, FILE_LINES) == []

    def test_not_json(self):
        assert _parse_ai_findings("the code looks fine to me",
                                  "mod.py", 1, 6, FILE_LINES) == []

    def test_json_object_not_array(self):
        assert _parse_ai_findings('{"line": 5}', "mod.py", 1, 6, FILE_LINES) == []

    def test_markdown_fenced_json_still_parses(self):
        raw = "```json\n" + _model_json([{
            "line": 5, "code": "subprocess.run(cmd, shell=True)",
            "severity": "high", "category": "x", "message": "injection",
        }]) + "\n```"
        out = _parse_ai_findings(raw, "mod.py", 1, 6, FILE_LINES)
        assert len(out) == 1

    def test_garbage_items_skipped_not_crashed(self):
        raw = json.dumps([
            "a string not a dict",
            42,
            {"line": 5, "code": "subprocess.run(cmd, shell=True)",
             "severity": "high", "category": "x", "message": "real one"},
        ])
        out = _parse_ai_findings(raw, "mod.py", 1, 6, FILE_LINES)
        assert len(out) == 1     # only the valid dict survives

    def test_whitespace_insensitive_match(self):
        # Model quotes with different spacing than the source — should still
        # match (we normalize whitespace before comparing).
        raw = _model_json([{
            "line": 5, "code": "subprocess.run( cmd , shell = True )",
            "severity": "high", "category": "x", "message": "injection",
        }])
        out = _parse_ai_findings(raw, "mod.py", 1, 6, FILE_LINES)
        assert len(out) == 1

    def test_gutter_prefix_in_quote_still_matches(self):
        # The model is SHOWN numbered lines ("5: subprocess.run(...)") and may
        # echo the "5: " gutter in its quote. The filter must strip it, not
        # drop the (real) finding as a fabrication. This was a silent
        # false-negative that could zero out the whole AI pass.
        raw = _model_json([{
            "line": 5, "code": "5: subprocess.run(cmd, shell=True)",
            "severity": "high", "category": "x", "message": "injection",
        }])
        out = _parse_ai_findings(raw, "mod.py", 1, 6, FILE_LINES)
        assert len(out) == 1, "gutter-prefixed quote must still match the source"
        assert out[0].line == 5
