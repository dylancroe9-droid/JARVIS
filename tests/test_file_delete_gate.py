"""
Tests for the manage_files delete safety gate — it must refuse home/root scope
outright and require confirmation before deleting, instead of the old behavior
of unlink()-ing every match immediately with no prompt.
"""

from pathlib import Path

import tools.file_tools as ft
import tools.permissions as perms


def test_delete_refuses_home_directory(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    victim = fake_home / "important.txt"
    victim.write_text("do not delete")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    out = ft.manage_files(action="delete", directory=str(fake_home), pattern="*")
    assert "Refusing" in out
    assert victim.exists()          # untouched


def test_delete_refuses_symlink_or_alias_to_home(monkeypatch, tmp_path):
    # A path that differs lexically from home but resolves to it (symlink, or a
    # case variant on a case-insensitive FS) must still be refused — the guard
    # compares filesystem identity (dev+inode), not the path string.
    real_home = tmp_path / "realhome"
    real_home.mkdir()
    (real_home / "keep.txt").write_text("keep")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: real_home))

    aliased = tmp_path / "home_link"      # different string, same inode as home
    aliased.symlink_to(real_home)
    out = ft.manage_files(action="delete", directory=str(aliased), pattern="*")
    assert "Refusing" in out
    assert (real_home / "keep.txt").exists()


def test_delete_cancelled_when_permission_denied(monkeypatch, tmp_path):
    sub = tmp_path / "junk"
    sub.mkdir()
    f = sub / "a.tmp"
    f.write_text("x")
    # Keep it out of the home-scope refusal
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "elsewhere"))
    monkeypatch.setattr(perms, "request", lambda msg: False)

    out = ft.manage_files(action="delete", directory=str(sub), pattern="*.tmp")
    assert "Cancelled" in out
    assert f.exists()               # not deleted — permission denied


def test_delete_proceeds_when_approved(monkeypatch, tmp_path):
    sub = tmp_path / "junk2"
    sub.mkdir()
    f = sub / "b.tmp"
    f.write_text("x")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "elsewhere"))
    monkeypatch.setattr(perms, "request", lambda msg: True)

    out = ft.manage_files(action="delete", directory=str(sub), pattern="*.tmp")
    assert "Deleted 1 file" in out
    assert not f.exists()           # actually deleted after approval


def test_list_action_never_deletes(monkeypatch, tmp_path):
    sub = tmp_path / "junk3"
    sub.mkdir()
    f = sub / "c.tmp"
    f.write_text("x")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "elsewhere"))
    out = ft.manage_files(action="list", directory=str(sub), pattern="*.tmp")
    assert "c.tmp" in out
    assert f.exists()
