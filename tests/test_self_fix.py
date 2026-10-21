"""
Safety tests for tools/self_fix.py — the module that EDITS source files.

This is the highest-risk code in the project: a bug here can corrupt code.
These tests exercise the apply-and-revert machinery directly (no LLM) by
pointing the fixer at a throwaway file in a tmp dir and stubbing the
test-runner, so we can assert the safety guarantees deterministically:

  - a patch that fails the test-gate is REVERTED (file back to original)
  - a patch that breaks syntax is REVERTED
  - an old_string that doesn't exist is rejected (no write)
  - an ambiguous old_string (>1 match) is rejected (no write)
  - a good patch is applied and the file matches the patched content
  - a .bak backup is created
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.self_fix as sf


ORIGINAL = '''\
def add(a, b):
    return a - b


def greet(name):
    return "hi " + name
'''


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """
    Point self_fix at a tmp dir with one throwaway file, and stub the
    test-runner + post-fix review so we control pass/fail deterministically.
    Yields (helpers). Restores nothing — tmp_path is discarded by pytest.
    """
    monkeypatch.setattr(sf, "JARVIS_ROOT", tmp_path)
    target = tmp_path / "mod.py"
    target.write_text(ORIGINAL)

    # Default: tests pass, review approves. Individual tests override these.
    monkeypatch.setattr(sf, "_run_tests", lambda: (True, "ok"))
    monkeypatch.setattr(sf, "_post_fix_review", lambda *a, **k: (True, "ok"))

    return {
        "root": tmp_path,
        "target": target,
        "rel": "mod.py",
        "monkeypatch": monkeypatch,
    }


class TestGoodPatch:
    def test_applies_and_persists(self, sandbox):
        ok, detail = sf.apply_fix_safely(
            "mod.py", "return a - b", "return a + b")
        assert ok, detail
        assert "return a + b" in sandbox["target"].read_text()

    def test_creates_backup(self, sandbox):
        sf.apply_fix_safely("mod.py", "return a - b", "return a + b")
        bak = sandbox["target"].with_suffix(".py.bak")
        assert bak.exists()
        # backup holds the ORIGINAL, unpatched content
        assert "return a - b" in bak.read_text()


class TestTestGateRevert:
    def test_failing_tests_revert_the_file(self, sandbox):
        sandbox["monkeypatch"].setattr(sf, "_run_tests",
                                       lambda: (False, "1 failed"))
        ok, detail = sf.apply_fix_safely(
            "mod.py", "return a - b", "return a + b")
        assert not ok
        assert "revert" in detail.lower()
        # File must be back to the ORIGINAL
        assert sandbox["target"].read_text() == ORIGINAL

    def test_post_review_rejection_reverts(self, sandbox):
        sandbox["monkeypatch"].setattr(sf, "_post_fix_review",
                                       lambda *a, **k: (False, "introduces new bug"))
        ok, detail = sf.apply_fix_safely(
            "mod.py", "return a - b", "return a + b")
        assert not ok
        assert sandbox["target"].read_text() == ORIGINAL


class TestSyntaxGuard:
    def test_syntax_breaking_patch_reverts(self, sandbox):
        # Replace a valid line with one that breaks Python syntax
        ok, detail = sf.apply_fix_safely(
            "mod.py", "return a - b", "return a + (b")   # unbalanced paren
        assert not ok
        assert "syntax" in detail.lower()
        assert sandbox["target"].read_text() == ORIGINAL


class TestBadOldString:
    def test_missing_old_string_rejected_no_write(self, sandbox):
        ok, detail = sf.apply_fix_safely(
            "mod.py", "this text is not in the file", "whatever")
        assert not ok
        assert "not found" in detail.lower()
        # File untouched, no backup written
        assert sandbox["target"].read_text() == ORIGINAL
        assert not sandbox["target"].with_suffix(".py.bak").exists()

    def test_ambiguous_old_string_rejected(self, sandbox):
        # "return " appears in both functions → ambiguous → must refuse
        ok, detail = sf.apply_fix_safely("mod.py", "return ", "yield ")
        assert not ok
        assert "ambiguous" in detail.lower()
        assert sandbox["target"].read_text() == ORIGINAL


class TestMissingFile:
    def test_nonexistent_file_rejected(self, sandbox):
        ok, detail = sf.apply_fix_safely(
            "does_not_exist.py", "a", "b")
        assert not ok
        assert "not found" in detail.lower()


class TestAtomicWrite:
    def test_write_failure_leaves_original_intact(self, sandbox, monkeypatch):
        # Simulate a disk-full / IO error DURING the patched write. With the
        # atomic write (temp + os.replace), the real file must be untouched —
        # never truncated — even though the write failed.
        import tools.self_fix as sfmod
        real_write = Path.write_text

        def boom_write(self, *a, **k):
            # Fail only when writing the .tmp file (the patched content)
            if self.suffix == ".tmp":
                raise OSError("No space left on device")
            return real_write(self, *a, **k)

        monkeypatch.setattr(Path, "write_text", boom_write)
        ok, detail = sf.apply_fix_safely(
            "mod.py", "return a - b", "return a + b")
        assert not ok
        # The crux: the original file is fully intact, NOT truncated/corrupted
        assert sandbox["target"].read_text() == ORIGINAL, \
            "atomic write failed to protect the original on write error"
        # No stray .tmp left behind
        assert not sandbox["target"].with_suffix(".py.tmp").exists()


class TestRunTestsThrows:
    def test_runner_exception_does_not_leave_file_patched(self, sandbox):
        # If _run_tests raises instead of returning, the file must NOT be
        # left in the patched state. (Guards against a runner that throws.)
        def boom():
            raise RuntimeError("pytest blew up")
        sandbox["monkeypatch"].setattr(sf, "_run_tests", boom)
        try:
            ok, detail = sf.apply_fix_safely(
                "mod.py", "return a - b", "return a + b")
        except Exception:
            ok = None  # if it propagates, that itself is a finding
        # Whether it raised or returned False, the file must be restored.
        assert sandbox["target"].read_text() == ORIGINAL, \
            "file left patched after test-runner exception"
