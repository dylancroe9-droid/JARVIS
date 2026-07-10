"""
Unit tests for tools/self_inspect.py — the read-only window JARVIS has into
his own code. Coverage focuses on the safety boundaries (path escapes,
blocked files, blocked dirs) and on graceful degradation when things are
missing (no log file, no history file, malformed history).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.self_inspect import (
    JARVIS_ROOT,
    _BLOCKED_DIRS,
    _safe_path,
    grep_jarvis,
    list_jarvis_dir,
    read_jarvis_file,
    read_jarvis_history,
    read_jarvis_log,
)


# ── _safe_path ────────────────────────────────────────────────────────────────

class TestSafePath:
    def test_resolves_relative_under_root(self):
        p = _safe_path("server.py")
        assert p is not None
        assert p == JARVIS_ROOT / "server.py"

    def test_resolves_nested(self):
        p = _safe_path("tools/diagnostics.py")
        assert p is not None
        assert p.parent.name == "tools"

    def test_rejects_path_escape(self):
        assert _safe_path("../../etc/passwd") is None
        assert _safe_path("/etc/passwd") is None

    def test_rejects_dotenv(self):
        assert _safe_path(".env") is None
        assert _safe_path(".env.local") is None

    def test_rejects_secrets_by_suffix(self):
        assert _safe_path("foo.pem") is None
        assert _safe_path("bar.key") is None

    def test_rejects_binary_suffixes(self):
        assert _safe_path("compiled.pyc") is None
        assert _safe_path("lib.so") is None

    def test_blocks_blocked_dirs(self):
        # _BLOCKED_DIRS should always include these
        for d in ("node_modules", "__pycache__", ".venv", ".git"):
            assert d in _BLOCKED_DIRS

    def test_empty_path_rejected(self):
        assert _safe_path("") is None


# ── read_jarvis_file ──────────────────────────────────────────────────────────

class TestReadFile:
    def test_reads_existing_source_file(self):
        out = read_jarvis_file("server.py")
        assert not out.startswith("__error__")
        assert "# server.py" in out
        # Verify size header is present
        assert "bytes)" in out.split("\n", 1)[0]

    def test_reads_memory_txt(self):
        # memory.txt is intentionally readable for self-inspection
        out = read_jarvis_file("memory.txt")
        # Either readable or doesn't exist — both acceptable
        assert out.startswith("# memory.txt") or "does not exist" in out

    def test_blocks_dotenv(self):
        out = read_jarvis_file(".env")
        assert out.startswith("__error__")
        assert "blocked" in out or "outside" in out

    def test_rejects_path_escape(self):
        out = read_jarvis_file("../../etc/passwd")
        assert out.startswith("__error__")

    def test_clean_error_on_missing(self):
        out = read_jarvis_file("definitely_not_a_real_file.xyz")
        assert out.startswith("__error__")
        assert "does not exist" in out

    def test_clean_error_on_directory(self):
        out = read_jarvis_file("tools")
        assert out.startswith("__error__")
        assert "directory" in out

    def test_size_cap_truncates(self):
        # Force a tiny cap and confirm truncation message is appended
        out = read_jarvis_file("server.py", max_bytes=512)
        assert "[truncated at" in out


# ── grep_jarvis ───────────────────────────────────────────────────────────────

class TestGrep:
    def test_finds_known_symbol(self):
        out = grep_jarvis(r"_MUSIC_SKIP_WORDS", "brain/*.py")
        assert "Found" in out
        assert "brain/jarvis.py" in out

    def test_no_matches_returns_clear_message(self):
        # Pattern uses a non-printable control char that cannot appear in source.
        out = grep_jarvis("\x00impossible_pattern_\x01", "**/*.py")
        assert "No matches" in out

    def test_bad_regex(self):
        out = grep_jarvis(r"[unclosed")
        assert out.startswith("__error__")
        assert "bad regex" in out

    def test_empty_pattern_rejected(self):
        out = grep_jarvis("")
        assert out.startswith("__error__")

    def test_respects_max_results(self):
        # Use a very common pattern but cap at 3
        out = grep_jarvis(r"def ", "**/*.py", max_results=3)
        if "Found" in out:
            # Count actual result lines (after the header)
            result_lines = [l for l in out.splitlines()[1:] if l.strip()]
            assert len(result_lines) <= 3


# ── list_jarvis_dir ───────────────────────────────────────────────────────────

class TestListDir:
    def test_lists_root(self):
        out = list_jarvis_dir(".")
        assert "server.py" in out
        assert "brain/" in out

    def test_lists_subdirectory(self):
        out = list_jarvis_dir("tools")
        assert "diagnostics.py" in out

    def test_does_not_show_venv(self):
        out = list_jarvis_dir(".")
        assert ".venv/" not in out
        assert "node_modules" not in out

    def test_clean_error_on_missing(self):
        out = list_jarvis_dir("not_a_real_directory")
        assert out.startswith("__error__")

    def test_clean_error_on_file(self):
        out = list_jarvis_dir("server.py")
        assert out.startswith("__error__")


# ── read_jarvis_log ───────────────────────────────────────────────────────────

class TestReadLog:
    def test_returns_clean_error_when_no_log(self, monkeypatch, tmp_path):
        # Force every candidate path to a non-existent location
        from tools import self_inspect as si
        fake = [tmp_path / f"fake_{i}.log" for i in range(3)]
        monkeypatch.setattr(si, "_LOG_PATH_CANDIDATES", fake)
        out = read_jarvis_log()
        assert out.startswith("__error__")
        assert "no log file" in out.lower()

    def test_reads_existing_log(self, monkeypatch, tmp_path):
        from tools import self_inspect as si
        log_path = tmp_path / "fake.log"
        log_path.write_text("line one\nline two\nline three\n")
        monkeypatch.setattr(si, "_LOG_PATH_CANDIDATES", [log_path])
        out = read_jarvis_log(lines=10)
        assert "line one" in out
        assert "line two" in out
        assert "line three" in out

    def test_tail_caps_at_max(self, monkeypatch, tmp_path):
        from tools import self_inspect as si
        log_path = tmp_path / "big.log"
        log_path.write_text("\n".join(f"line {i}" for i in range(2000)) + "\n")
        monkeypatch.setattr(si, "_LOG_PATH_CANDIDATES", [log_path])
        # Ask for way more than the cap (500) — should be silently clamped
        out = read_jarvis_log(lines=10_000)
        assert "line 1999" in out
        # Should NOT contain the very first line because we tailed
        assert "line 0\n" not in out

    def test_handles_huge_log_without_oom(self, monkeypatch, tmp_path):
        # Write a >5 MB log; tail should still return only the last lines
        # without loading the whole file.
        from tools import self_inspect as si
        log_path = tmp_path / "huge.log"
        with log_path.open("w") as f:
            for i in range(120_000):
                f.write(f"log line number {i} with some padding text\n")
        assert log_path.stat().st_size > 4 * 1024 * 1024
        monkeypatch.setattr(si, "_LOG_PATH_CANDIDATES", [log_path])
        out = read_jarvis_log(lines=50)
        # We tailed only 2 MB so the very early lines shouldn't appear
        assert "log line number 0 with" not in out
        # But late lines should
        assert "log line number 119999" in out
        # And the size note should mention the 2 MB tail
        assert "tailed last 2 MB" in out


# ── read_jarvis_history ───────────────────────────────────────────────────────

class TestReadHistory:
    def test_clean_error_when_missing(self, monkeypatch, tmp_path):
        from tools import self_inspect as si
        fake_home = tmp_path / "nohome"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        out = read_jarvis_history()
        assert out.startswith("__error__")

    def test_clean_error_on_malformed(self, monkeypatch, tmp_path):
        from tools import self_inspect as si
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        (tmp_path / ".jarvis_history.json").write_text("not valid json{")
        out = read_jarvis_history()
        assert out.startswith("__error__")

    def test_clean_error_on_wrong_type(self, monkeypatch, tmp_path):
        from tools import self_inspect as si
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        (tmp_path / ".jarvis_history.json").write_text('{"not": "a list"}')
        out = read_jarvis_history()
        assert out.startswith("__error__")
        assert "unexpected" in out.lower()

    def test_returns_recent_turns(self, monkeypatch, tmp_path):
        from tools import self_inspect as si
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        history = [
            {"role": "user", "content": f"turn {i}"} for i in range(30)
        ]
        (tmp_path / ".jarvis_history.json").write_text(json.dumps(history))
        out = read_jarvis_history(max_turns=5)
        # Should contain the LAST 5 turns
        assert "turn 29" in out
        assert "turn 25" in out
        assert "turn 24" not in out

    def test_handles_complex_content(self, monkeypatch, tmp_path):
        from tools import self_inspect as si
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        history = [
            {"role": "assistant", "content": [{"text": "complex"}, {"tool": "x"}]},
            {"role": "user", "content": "simple"},
        ]
        (tmp_path / ".jarvis_history.json").write_text(json.dumps(history))
        out = read_jarvis_history(max_turns=10)
        assert "simple" in out
        # Doesn't crash on list content
        assert not out.startswith("__error__")
