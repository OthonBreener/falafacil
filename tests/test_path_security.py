"""Tests for shared write-permission security checks."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from falafacil import path_security
from falafacil.path_security import has_foreign_write


def _stat_with(mode: int, uid: int = 1000, gid: int = 1000) -> os.stat_result:
    return os.stat_result((mode, 1, 1, 1, uid, gid, 0, 0, 0, 0))


def test_world_writable_is_always_foreign(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(path_security, "_lookup_group_uids", lambda gid: frozenset({1000}))

    assert has_foreign_write(_stat_with(0o40777)) is True
    assert has_foreign_write(_stat_with(0o100666)) is True


def test_owner_only_write_is_not_foreign(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(gid: int) -> frozenset[int]:
        raise AssertionError("group lookup must not run when the group-write bit is clear")

    monkeypatch.setattr(path_security, "_lookup_group_uids", _fail)

    assert has_foreign_write(_stat_with(0o40755)) is False
    assert has_foreign_write(_stat_with(0o100644)) is False


def test_group_writable_private_group_is_not_foreign(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(path_security, "_lookup_group_uids", lambda gid: frozenset({1000}))

    assert has_foreign_write(_stat_with(0o40775)) is False
    assert has_foreign_write(_stat_with(0o100664)) is False


def test_group_writable_empty_group_is_not_foreign(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(path_security, "_lookup_group_uids", lambda gid: frozenset())

    assert has_foreign_write(_stat_with(0o40775)) is False


def test_group_writable_shared_group_is_foreign(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(path_security, "_lookup_group_uids", lambda gid: frozenset({1000, 4242}))

    assert has_foreign_write(_stat_with(0o40775)) is True
    assert has_foreign_write(_stat_with(0o100664)) is True


def test_group_writable_group_without_owner_is_foreign(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(path_security, "_lookup_group_uids", lambda gid: frozenset({4242}))

    assert has_foreign_write(_stat_with(0o40775)) is True


def test_unresolvable_group_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(path_security, "_lookup_group_uids", lambda gid: None)

    assert has_foreign_write(_stat_with(0o40775)) is True


def test_lookup_group_uids_returns_none_for_unknown_gid() -> None:
    assert path_security._lookup_group_uids(4_294_967_000) is None


def test_lookup_group_uids_includes_primary_group_members() -> None:
    uids = path_security._lookup_group_uids(os.getgid())

    assert uids is not None
    assert os.getuid() in uids


def test_real_private_group_tree_is_accepted(tmp_path: Path) -> None:
    """A 775/664 tree owned by the current user must not be flagged on a private group."""
    directory = tmp_path / "keg"
    directory.mkdir()
    directory.chmod(0o775)
    payload = directory / "marker.json"
    payload.write_text("{}", encoding="utf-8")
    payload.chmod(0o664)

    group_uids = path_security._lookup_group_uids(directory.stat().st_gid)
    if group_uids is None or group_uids - {os.getuid()}:
        pytest.skip("current primary group is shared with other users")

    assert has_foreign_write(directory.stat()) is False
    assert has_foreign_write(payload.stat()) is False
    assert stat.S_IMODE(directory.stat().st_mode) == 0o775
