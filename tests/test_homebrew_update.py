"""Tests for Homebrew update detection and marker loading foundation."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from PySide6.QtCore import QObject, QProcess, Signal
from PySide6.QtWidgets import QApplication

from falafacil import path_security
from falafacil.homebrew_update import (
    GENERIC_FAILURE_MESSAGE,
    HOMEBREW_CHANNEL,
    HOMEBREW_FORMULA,
    HOMEBREW_SCHEMA_VERSION,
    KILL_GRACE_MS,
    MAX_OUTDATED_STDOUT_BYTES,
    OUTDATED_TIMEOUT_MS,
    PROBE_TIMEOUT_MS,
    READY_TO_RESTART_MESSAGE,
    STATUS_CHECKING,
    STATUS_UPDATING,
    STATUS_UPGRADING,
    STATUS_VERIFYING,
    TIMEOUT_MESSAGE,
    UPDATE_TIMEOUT_MS,
    UPGRADE_TIMEOUT_MS,
    UP_TO_DATE_MESSAGE,
    HomebrewInstallation,
    HomebrewUpdateController,
    HomebrewUpdateError,
    detect_homebrew_installation,
    load_homebrew_marker,
)


def _create_valid_homebrew_tree(
    root: Path,
    version: str = "0.2.0",
) -> tuple[Path, Path, Path]:
    """Create a realistic Homebrew prefix, Cellar keg, opt symlink and marker.

    Returns:
        (prefix_path, cellar_executable, marker_file)
    """
    prefix = root / "homebrew"
    bin_dir = prefix / "bin"
    opt_dir = prefix / "opt" / "falafacil"
    cellar_keg = prefix / "Cellar" / "falafacil" / version
    libexec = cellar_keg / "libexec"
    opt_bin_dir = cellar_keg / "bin"

    for d in (root, prefix, bin_dir, opt_dir.parent, cellar_keg.parent.parent, cellar_keg.parent, cellar_keg, libexec, opt_bin_dir):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o755)

    brew_bin = bin_dir / "brew"
    brew_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    brew_bin.chmod(0o755)

    cellar_exec = libexec / "falafacil"
    cellar_exec.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cellar_exec.chmod(0o755)

    opt_dir.symlink_to(Path("..") / "Cellar" / "falafacil" / version)

    launch_symlink = bin_dir / "falafacil"
    launch_symlink.symlink_to(Path("..") / "opt" / "falafacil" / "libexec" / "falafacil")

    cellar_bin_exec = opt_bin_dir / "falafacil"
    cellar_bin_exec.symlink_to(Path("..") / "libexec" / "falafacil")
    marker_file = libexec / "falafacil-homebrew.json"
    payload = {
        "schema": HOMEBREW_SCHEMA_VERSION,
        "channel": HOMEBREW_CHANNEL,
        "formula": HOMEBREW_FORMULA,
        "version": version,
        "homebrew_prefix": str(prefix),
        "brew_path": str(brew_bin),
        "launch_path": str(prefix / "opt" / "falafacil" / "bin" / "falafacil"),
        "marker_path": str(prefix / "opt" / "falafacil" / "libexec" / "falafacil-homebrew.json"),
    }
    marker_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    marker_file.chmod(0o644)

    return prefix, cellar_exec, marker_file


def _force_shared_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a group shared with another account, which must stay rejected."""
    owner_uid = os.getuid()
    monkeypatch.setattr(
        path_security,
        "_lookup_group_uids",
        lambda gid: frozenset({owner_uid, owner_uid + 4242}),
    )


def _force_private_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a private per-user group, the Ubuntu/Homebrew default."""
    owner_uid = os.getuid()
    monkeypatch.setattr(path_security, "_lookup_group_uids", lambda gid: frozenset({owner_uid}))


def _relax_tree_to_umask_002(prefix: Path, version: str) -> None:
    """Apply the permissions Homebrew really creates under umask 002."""
    for directory in (
        prefix,
        prefix / "bin",
        prefix / "opt",
        prefix / "Cellar",
        prefix / "Cellar" / "falafacil",
        prefix / "Cellar" / "falafacil" / version,
        prefix / "Cellar" / "falafacil" / version / "libexec",
        prefix / "Cellar" / "falafacil" / version / "bin",
    ):
        directory.chmod(0o775)
    (prefix / "bin" / "brew").chmod(0o775)
    (prefix / "Cellar" / "falafacil" / version / "libexec" / "falafacil").chmod(0o775)
    (prefix / "Cellar" / "falafacil" / version / "libexec" / "falafacil-homebrew.json").chmod(0o664)


def test_load_homebrew_marker_accepts_umask_002_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Homebrew prefix created under umask 002 loads when the group is private."""
    prefix, _cellar_exec, marker_file = _create_valid_homebrew_tree(tmp_path, version="0.2.0")
    _relax_tree_to_umask_002(prefix, "0.2.0")
    _force_private_group(monkeypatch)

    installation = load_homebrew_marker(marker_file, expected_version="0.2.0")

    assert installation.version == "0.2.0"
    assert installation.homebrew_prefix == prefix


def test_load_homebrew_marker_fails_on_umask_002_tree_with_shared_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix, _cellar_exec, marker_file = _create_valid_homebrew_tree(tmp_path, version="0.2.0")
    _relax_tree_to_umask_002(prefix, "0.2.0")
    _force_shared_group(monkeypatch)

    with pytest.raises(HomebrewUpdateError, match="permissões de escrita inseguras para grupo/outros"):
        load_homebrew_marker(marker_file, expected_version="0.2.0")


def test_load_homebrew_marker_valid_tree(tmp_path: Path) -> None:
    prefix, _cellar_exec, marker_file = _create_valid_homebrew_tree(tmp_path, version="0.2.0")

    installation = load_homebrew_marker(marker_file, expected_version="0.2.0")
    assert isinstance(installation, HomebrewInstallation)
    assert installation.version == "0.2.0"
    assert installation.formula == HOMEBREW_FORMULA
    assert installation.homebrew_prefix == prefix
    assert installation.brew_path == prefix / "bin" / "brew"
    assert installation.launch_path == prefix / "opt" / "falafacil" / "bin" / "falafacil"
    assert (
        installation.marker_path
        == prefix / "opt" / "falafacil" / "libexec" / "falafacil-homebrew.json"
    )


def test_load_homebrew_marker_version_divergence_fails(tmp_path: Path) -> None:
    _prefix, _cellar_exec, marker_file = _create_valid_homebrew_tree(tmp_path, version="0.2.0")

    with pytest.raises(HomebrewUpdateError, match="diverge da versão esperada"):
        load_homebrew_marker(marker_file, expected_version="0.3.0")


def test_load_homebrew_marker_missing_file_fails(tmp_path: Path) -> None:
    non_existent = tmp_path / "does-not-exist.json"
    with pytest.raises(HomebrewUpdateError, match="Não foi possível resolver o caminho"):
        load_homebrew_marker(non_existent)


def test_load_homebrew_marker_relative_path_fails() -> None:
    relative = Path("relative/path/marker.json")
    with pytest.raises(HomebrewUpdateError, match="deve ser absoluto"):
        load_homebrew_marker(relative)


def test_load_homebrew_marker_oversized_file_fails(tmp_path: Path) -> None:
    _prefix, _cellar_exec, marker_file = _create_valid_homebrew_tree(tmp_path)
    marker_file.write_text(" " * 70000, encoding="utf-8")

    with pytest.raises(HomebrewUpdateError, match="excede o tamanho máximo permitido"):
        load_homebrew_marker(marker_file)


def test_load_homebrew_marker_malformed_json_fails(tmp_path: Path) -> None:
    _prefix, _cellar_exec, marker_file = _create_valid_homebrew_tree(tmp_path)
    marker_file.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(HomebrewUpdateError, match="Falha ao ler/analisar marker JSON"):
        load_homebrew_marker(marker_file)


def test_load_homebrew_marker_non_dict_json_fails(tmp_path: Path) -> None:
    _prefix, _cellar_exec, marker_file = _create_valid_homebrew_tree(tmp_path)
    marker_file.write_text("[\"item1\", \"item2\"]", encoding="utf-8")

    with pytest.raises(HomebrewUpdateError, match="deve ser um objeto JSON"):
        load_homebrew_marker(marker_file)


@pytest.mark.parametrize(
    "mutator, match_pattern",
    [
        (lambda p: p.pop("schema"), "Chaves do marker inválidas"),
        (lambda p: p.update({"extra_key": "extra"}), "Chaves do marker inválidas"),
        (lambda p: p.update({"schema": 2}), "Schema do marker inválido"),
        (lambda p: p.update({"schema": True}), "Schema do marker inválido"),
        (lambda p: p.update({"schema": "1"}), "Schema do marker inválido"),
        (lambda p: p.update({"channel": "cask"}), "Canal do marker inválido"),
        (lambda p: p.update({"formula": "other/tap/formula"}), "Fórmula do marker inválida"),
        (lambda p: p.update({"version": "v0.2.0"}), "Versão SemVer inválida"),
        (lambda p: p.update({"version": "0.2"}), "Versão SemVer inválida"),
        (lambda p: p.update({"version": "0.2.0-rc1"}), "Versão SemVer inválida"),
        (lambda p: p.update({"version": "0.2.0\n"}), "Versão SemVer inválida"),
        (lambda p: p.update({"version": "0.2.0\r\n"}), "Versão SemVer inválida"),
        (lambda p: p.update({"version": "0.2.0 "}), "Versão SemVer inválida"),
        (lambda p: p.update({"version": " 0.2.0"}), "Versão SemVer inválida"),
        (lambda p: p.update({"version": "0.2.0.1"}), "Versão SemVer inválida"),
        (lambda p: p.update({"homebrew_prefix": "relative/path"}), "não é absoluto"),
        (
            lambda p: p.update({"brew_path": "/some/other/bin/brew"}),
            "não corresponde ao esperado",
        ),
        (
            lambda p: p.update({"launch_path": "/some/other/opt/falafacil/bin/falafacil"}),
            "não corresponde ao esperado",
        ),
        (
            lambda p: p.update({"marker_path": "/some/other/opt/falafacil/libexec/falafacil-homebrew.json"}),
            "não corresponde ao esperado",
        ),
    ],
    ids=[
        "missing_key",
        "extra_key",
        "wrong_schema_number",
        "schema_bool_true",
        "schema_string",
        "wrong_channel",
        "wrong_formula",
        "semver_with_v_prefix",
        "semver_incomplete",
        "semver_prerelease",
        "semver_trailing_newline",
        "semver_trailing_crlf",
        "semver_trailing_space",
        "semver_leading_space",
        "semver_four_parts",
        "relative_prefix",
        "mismatched_brew_path",
        "mismatched_launch_path",
        "mismatched_marker_path",
    ],
)
def test_load_homebrew_marker_payload_schema_validations(
    tmp_path: Path,
    mutator: Any,
    match_pattern: str,
) -> None:
    _prefix, _cellar_exec, marker_file = _create_valid_homebrew_tree(tmp_path)
    payload = json.loads(marker_file.read_text(encoding="utf-8"))
    mutator(payload)
    marker_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(HomebrewUpdateError, match=match_pattern):
        load_homebrew_marker(marker_file)


def test_load_homebrew_marker_fails_on_shared_group_writable_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prefix, _cellar_exec, marker_file = _create_valid_homebrew_tree(tmp_path)
    marker_file.chmod(0o664)
    _force_shared_group(monkeypatch)

    with pytest.raises(HomebrewUpdateError, match="permissões de escrita inseguras"):
        load_homebrew_marker(marker_file)


def test_load_homebrew_marker_accepts_private_group_writable_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prefix, _cellar_exec, marker_file = _create_valid_homebrew_tree(tmp_path)
    marker_file.chmod(0o664)
    _force_private_group(monkeypatch)

    assert load_homebrew_marker(marker_file).version == "0.2.0"


def test_load_homebrew_marker_fails_on_non_executable_brew(tmp_path: Path) -> None:
    prefix, _cellar_exec, marker_file = _create_valid_homebrew_tree(tmp_path)
    brew_bin = prefix / "bin" / "brew"
    brew_bin.chmod(0o644)

    with pytest.raises(HomebrewUpdateError, match="não possui permissão de execução"):
        load_homebrew_marker(marker_file)


def test_load_homebrew_marker_fails_when_launch_path_escapes_prefix(tmp_path: Path) -> None:
    prefix, _cellar_exec, marker_file = _create_valid_homebrew_tree(tmp_path)
    # Redirect launch symlink outside prefix
    outside_bin = tmp_path / "outside_bin"
    outside_bin.mkdir()
    outside_exec = outside_bin / "outside_exec"
    outside_exec.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    outside_exec.chmod(0o755)

    cellar_bin_exec = prefix / "Cellar" / "falafacil" / "0.2.0" / "bin" / "falafacil"
    cellar_bin_exec.unlink()
    cellar_bin_exec.symlink_to(outside_exec)

    with pytest.raises(HomebrewUpdateError, match="escapa do prefixo"):
        load_homebrew_marker(marker_file)

def test_load_homebrew_marker_rejects_newline_semver_without_expected_version(
    tmp_path: Path,
) -> None:
    _prefix, _cellar_exec, marker_file = _create_valid_homebrew_tree(tmp_path)
    payload = json.loads(marker_file.read_text(encoding="utf-8"))
    payload["version"] = "0.2.0\n"
    marker_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(HomebrewUpdateError, match="Versão SemVer inválida"):
        load_homebrew_marker(marker_file, expected_version=None)


@pytest.mark.parametrize(
    "target_dir_fn",
    [
        lambda prefix, version: prefix,
        lambda prefix, version: prefix / "bin",
        lambda prefix, version: prefix / "opt",
        lambda prefix, version: prefix / "Cellar",
        lambda prefix, version: prefix / "Cellar" / "falafacil",
        lambda prefix, version: prefix / "Cellar" / "falafacil" / version,
        lambda prefix, version: prefix / "Cellar" / "falafacil" / version / "libexec",
    ],
    ids=["prefix", "bin_dir", "opt_dir", "cellar_dir", "formula_dir", "keg_dir", "libexec_dir"],
)
def test_load_homebrew_marker_fails_on_world_writable_intermediate_directory(
    tmp_path: Path,
    target_dir_fn: Any,
) -> None:
    prefix, _cellar_exec, marker_file = _create_valid_homebrew_tree(tmp_path, version="0.2.0")
    target = target_dir_fn(prefix, "0.2.0")
    target.chmod(0o777)

    with pytest.raises(HomebrewUpdateError, match="permissões de escrita inseguras para grupo/outros"):
        load_homebrew_marker(marker_file)

@pytest.mark.parametrize(
    "target_name, target_fn, check_symlink",
    [
        ("prefix", lambda p, v: p, False),
        ("bin_dir", lambda p, v: p / "bin", False),
        ("opt_dir", lambda p, v: p / "opt", False),
        ("opt_symlink", lambda p, v: p / "opt" / "falafacil", True),
        ("cellar_dir", lambda p, v: p / "Cellar", False),
        ("formula_dir", lambda p, v: p / "Cellar" / "falafacil", False),
        ("keg_dir", lambda p, v: p / "Cellar" / "falafacil" / v, False),
        ("libexec_dir", lambda p, v: p / "Cellar" / "falafacil" / v / "libexec", False),
        ("brew_bin", lambda p, v: p / "bin" / "brew", False),
        ("cellar_exec", lambda p, v: p / "Cellar" / "falafacil" / v / "libexec" / "falafacil", False),
        ("marker_file", lambda p, v: p / "Cellar" / "falafacil" / v / "libexec" / "falafacil-homebrew.json", False),
    ],
    ids=[
        "prefix",
        "bin_dir",
        "opt_dir",
        "opt_symlink",
        "cellar_dir",
        "formula_dir",
        "keg_dir",
        "libexec_dir",
        "brew_bin",
        "cellar_exec",
        "marker_file",
    ],
)
def test_load_homebrew_marker_fails_on_wrong_uid_via_stat_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
    target_fn: Any,
    check_symlink: bool,
) -> None:
    prefix, _cellar_exec, marker_file = _create_valid_homebrew_tree(tmp_path, version="0.2.0")
    target_obj = target_fn(prefix, "0.2.0")
    target_path_str = str(target_obj)
    target_resolved_str = str(target_obj.resolve()) if not check_symlink else target_path_str
    fake_uid = os.getuid() + 999

    orig_lstat = os.lstat
    orig_stat = os.stat

    def fake_lstat(path_val, *args, **kwargs):
        st = orig_lstat(path_val, *args, **kwargs)
        p_str = str(path_val)
        if p_str in (target_path_str, target_resolved_str) or os.path.abspath(p_str) in (target_path_str, target_resolved_str):
            return os.stat_result((
                st.st_mode,
                st.st_ino,
                st.st_dev,
                st.st_nlink,
                fake_uid,
                st.st_gid,
                st.st_size,
                st.st_atime,
                st.st_mtime,
                st.st_ctime,
            ))
        return st

    def fake_stat(path_val, *args, **kwargs):
        st = orig_stat(path_val, *args, **kwargs)
        p_str = str(path_val)
        if p_str in (target_path_str, target_resolved_str) or os.path.abspath(p_str) in (target_path_str, target_resolved_str):
            return os.stat_result((
                st.st_mode,
                st.st_ino,
                st.st_dev,
                st.st_nlink,
                fake_uid,
                st.st_gid,
                st.st_size,
                st.st_atime,
                st.st_mtime,
                st.st_ctime,
            ))
        return st

    monkeypatch.setattr(os, "lstat", fake_lstat)
    monkeypatch.setattr(os, "stat", fake_stat)

    with pytest.raises(HomebrewUpdateError, match="proprietário inválido"):
        load_homebrew_marker(marker_file)


@pytest.mark.parametrize(
    "target_dir_fn",
    [
        lambda prefix, version: prefix,
        lambda prefix, version: prefix / "bin",
        lambda prefix, version: prefix / "opt",
        lambda prefix, version: prefix / "Cellar",
        lambda prefix, version: prefix / "Cellar" / "falafacil",
        lambda prefix, version: prefix / "Cellar" / "falafacil" / version,
        lambda prefix, version: prefix / "Cellar" / "falafacil" / version / "libexec",
    ],
    ids=["prefix", "bin_dir", "opt_dir", "cellar_dir", "formula_dir", "keg_dir", "libexec_dir"],
)
def test_load_homebrew_marker_fails_on_shared_group_writable_directory_across_all_nodes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_dir_fn: Any,
) -> None:
    prefix, _cellar_exec, marker_file = _create_valid_homebrew_tree(tmp_path, version="0.2.0")
    target_dir = target_dir_fn(prefix, "0.2.0")
    target_dir_str = str(target_dir)
    target_dir_resolved_str = str(target_dir.resolve())
    test_gid = os.getgid()
    _force_shared_group(monkeypatch)

    orig_stat = os.stat
    orig_lstat = os.lstat

    def fake_stat(path_val, *args, **kwargs):
        st = orig_stat(path_val, *args, **kwargs)
        p_str = str(path_val)
        if p_str in (target_dir_str, target_dir_resolved_str) or os.path.abspath(p_str) in (target_dir_str, target_dir_resolved_str):
            return os.stat_result((
                st.st_mode | stat.S_IWGRP,
                st.st_ino,
                st.st_dev,
                st.st_nlink,
                st.st_uid,
                test_gid,
                st.st_size,
                st.st_atime,
                st.st_mtime,
                st.st_ctime,
            ))
        return st

    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setattr(os, "lstat", fake_stat)

    with pytest.raises(HomebrewUpdateError, match="permissões de escrita inseguras para grupo/outros"):
        load_homebrew_marker(marker_file)

def test_load_homebrew_marker_fails_on_marker_resolution_divergence(
    tmp_path: Path,
) -> None:
    prefix, _cellar_exec, marker_file = _create_valid_homebrew_tree(tmp_path, version="0.2.0")
    # Create an alternative marker file elsewhere in prefix and point payload to it
    alt_marker = prefix / "Cellar" / "falafacil" / "0.2.0" / "alt-marker.json"
    alt_marker.write_text(marker_file.read_text(encoding="utf-8"), encoding="utf-8")
    alt_marker.chmod(0o644)

    # Passing marker_file but payload specifies marker_path = alt_marker (via symlink redirection)
    alt_symlink = prefix / "opt" / "falafacil" / "libexec" / "alt-symlink.json"
    alt_symlink.symlink_to(alt_marker)

    payload = json.loads(marker_file.read_text(encoding="utf-8"))
    payload["marker_path"] = str(alt_symlink)
    marker_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(HomebrewUpdateError, match="não corresponde ao esperado"):
        load_homebrew_marker(marker_file)


def test_load_homebrew_marker_failure_performs_no_mutations(
    tmp_path: Path,
) -> None:
    prefix, _cellar_exec, marker_file = _create_valid_homebrew_tree(tmp_path, version="0.2.0")

    # Corrupt marker version to cause failure
    payload = json.loads(marker_file.read_text(encoding="utf-8"))
    payload["version"] = "invalid_version"
    marker_file.write_text(json.dumps(payload), encoding="utf-8")

    # Snapshot file states before call
    before_snapshot = {}
    for root_dir, dirs, files in os.walk(prefix):
        for name in [*dirs, *files]:
            full_p = Path(root_dir) / name
            st = full_p.lstat()
            before_snapshot[str(full_p)] = (st.st_mode, st.st_size, st.st_mtime)

    with pytest.raises(HomebrewUpdateError, match="Versão SemVer inválida"):
        load_homebrew_marker(marker_file)

    after_snapshot = {}
    for root_dir, dirs, files in os.walk(prefix):
        for name in [*dirs, *files]:
            full_p = Path(root_dir) / name
            st = full_p.lstat()
            after_snapshot[str(full_p)] = (st.st_mode, st.st_size, st.st_mtime)

    assert before_snapshot == after_snapshot


def test_detect_homebrew_installation_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import falafacil

    _prefix, cellar_exec, marker_file = _create_valid_homebrew_tree(tmp_path, version=falafacil.__version__)

    monkeypatch.setattr("falafacil.homebrew_update._resolve_self_executable", lambda: cellar_exec)

    installation = detect_homebrew_installation()
    assert installation is not None
    assert installation.version == falafacil.__version__
    assert installation.formula == HOMEBREW_FORMULA

def test_detect_homebrew_installation_returns_none_when_no_adjacent_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_exec = tmp_path / "source_bin" / "falafacil"
    source_exec.parent.mkdir(parents=True)
    source_exec.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    source_exec.chmod(0o755)

    monkeypatch.setattr("falafacil.homebrew_update._resolve_self_executable", lambda: source_exec)

    assert detect_homebrew_installation() is None


def test_detect_homebrew_installation_returns_none_on_resolution_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _failing_resolve():
        raise OSError("resolution failed")

    monkeypatch.setattr(
        "falafacil.homebrew_update._resolve_self_executable",
        _failing_resolve,
    )
    assert detect_homebrew_installation() is None


def test_detect_homebrew_installation_returns_none_on_corrupt_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prefix, cellar_exec, marker_file = _create_valid_homebrew_tree(tmp_path)
    marker_file.write_text("{corrupt", encoding="utf-8")

    monkeypatch.setattr("falafacil.homebrew_update._resolve_self_executable", lambda: cellar_exec)

    assert detect_homebrew_installation() is None


class FakeProcess(QObject):
    finished = Signal(int, QProcess.ExitStatus)
    errorOccurred = Signal(QProcess.ProcessError)
    readyReadStandardOutput = Signal()
    readyReadStandardError = Signal()

    def __init__(self, _parent: QObject | None = None) -> None:
        super().__init__(_parent)
        self.program: str | None = None
        self.arguments: list[str] = []
        self.channel_mode: QProcess.ProcessChannelMode = (
            QProcess.ProcessChannelMode.SeparateChannels
        )
        self.started = False
        self.terminated = False
        self.killed = False
        self._state = QProcess.ProcessState.NotRunning
        self._stdout_queue = bytearray()
        self._stderr_queue = bytearray()
        self.deleted = False

    def setProgram(self, program: str) -> None:
        self.program = program

    def setArguments(self, arguments: list[str]) -> None:
        self.arguments = list(arguments)

    def setProcessChannelMode(self, mode: QProcess.ProcessChannelMode) -> None:
        self.channel_mode = mode

    def start(self) -> None:
        self.started = True
        self._state = QProcess.ProcessState.Running

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def state(self) -> QProcess.ProcessState:
        return self._state

    def deleteLater(self) -> None:
        self.deleted = True

    def read(self, max_bytes: int) -> bytes:
        if max_bytes <= 0:
            return b""
        data = bytes(self._stdout_queue[:max_bytes])
        del self._stdout_queue[:max_bytes]
        return data

    def bytesAvailable(self) -> int:
        return len(self._stdout_queue)

    def readAllStandardOutput(self) -> bytes:
        data = bytes(self._stdout_queue)
        self._stdout_queue.clear()
        return data

    def readAllStandardError(self) -> bytes:
        data = bytes(self._stderr_queue)
        self._stderr_queue.clear()
        return data

    def feed_stdout(self, data: bytes) -> None:
        self._stdout_queue.extend(data)
        self.readyReadStandardOutput.emit()

    def feed_stderr(self, data: bytes) -> None:
        self._stderr_queue.extend(data)
        self.readyReadStandardError.emit()

    def emit_error_while_running(
        self,
        error: QProcess.ProcessError = QProcess.ProcessError.ReadError,
    ) -> None:
        self.errorOccurred.emit(error)

    def finish(
        self,
        exit_code: int = 0,
        exit_status: QProcess.ExitStatus = QProcess.ExitStatus.NormalExit,
    ) -> None:
        self._state = QProcess.ProcessState.NotRunning
        self.finished.emit(exit_code, exit_status)

    def fail_to_start(
        self,
        error: QProcess.ProcessError = QProcess.ProcessError.FailedToStart,
    ) -> None:
        self._state = QProcess.ProcessState.NotRunning
        self.errorOccurred.emit(error)

class FakeTimer(QObject):
    timeout = Signal()

    def __init__(self, _parent: QObject | None = None) -> None:
        super().__init__(_parent)
        self.single_shot = False
        self.interval_ms: int = 0
        self.active = False

    def setSingleShot(self, single_shot: bool) -> None:
        self.single_shot = single_shot

    def start(self, interval_ms: int) -> None:
        self.interval_ms = interval_ms
        self.active = True

    def stop(self) -> None:
        self.active = False

    def fire(self) -> None:
        if self.single_shot:
            self.active = False
        self.timeout.emit()


def _qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _make_valid_installation(tmp_path: Path, version: str = "0.2.0") -> HomebrewInstallation:
    _prefix, _cellar_exec, marker_file = _create_valid_homebrew_tree(tmp_path, version=version)
    return load_homebrew_marker(marker_file, expected_version=version)


def test_controller_requires_homebrew_installation_instance() -> None:
    _qapp()
    with pytest.raises(TypeError, match="instância de HomebrewInstallation"):
        HomebrewUpdateController("not an installation")  # type: ignore[arg-type]


def test_controller_up_to_date_runs_only_update_and_outdated(tmp_path: Path) -> None:
    _qapp()
    installation = _make_valid_installation(tmp_path, version="0.2.0")

    processes: list[FakeProcess] = []
    timers: list[FakeTimer] = []

    def process_factory(_parent: QObject | None) -> FakeProcess:
        p = FakeProcess(_parent)
        processes.append(p)
        return p

    def timer_factory(_parent: QObject | None) -> FakeTimer:
        t = FakeTimer(_parent)
        timers.append(t)
        return t

    controller = HomebrewUpdateController(
        installation,
        process_factory=process_factory,
        timer_factory=timer_factory,
    )

    statuses: list[str] = []
    up_to_dates: list[str] = []
    ready_to_restarts: list[str] = []
    failures: list[str] = []

    controller.status_changed.connect(statuses.append)
    controller.up_to_date.connect(up_to_dates.append)
    controller.ready_to_restart.connect(ready_to_restarts.append)
    controller.failed.connect(failures.append)

    assert not controller.running
    assert controller.install_latest() is True
    assert controller.running is True

    # Phase 1: UPDATE
    assert len(processes) == 1
    update_proc = processes[0]
    assert update_proc.started is True
    assert update_proc.program == str(installation.brew_path)
    assert update_proc.arguments == ["update-if-needed"]
    assert update_proc.channel_mode == QProcess.ProcessChannelMode.MergedChannels
    assert len(timers) == 1
    update_timer = timers[0]
    assert update_timer.active is True
    assert update_timer.interval_ms == UPDATE_TIMEOUT_MS
    assert statuses == [STATUS_UPDATING]

    # Feed some output and finish update
    update_proc.feed_stdout(b"Already up-to-date.\n")
    update_proc.finish(0, QProcess.ExitStatus.NormalExit)
    assert update_timer.active is False
    assert update_proc.deleted is True

    # Phase 2: OUTDATED
    assert len(processes) == 2
    outdated_proc = processes[1]
    assert outdated_proc.started is True
    assert outdated_proc.program == str(installation.brew_path)
    assert outdated_proc.arguments == [
        "outdated",
        "--formula",
        "--json=v2",
        HOMEBREW_FORMULA,
    ]
    assert outdated_proc.channel_mode == QProcess.ProcessChannelMode.SeparateChannels
    assert len(timers) == 2
    outdated_timer = timers[1]
    assert outdated_timer.active is True
    assert outdated_timer.interval_ms == OUTDATED_TIMEOUT_MS
    assert statuses == [STATUS_UPDATING, STATUS_CHECKING]

    # Feed empty formulae JSON and finish
    outdated_payload = json.dumps({"formulae": []}).encode("utf-8")
    outdated_proc.feed_stdout(outdated_payload)
    outdated_proc.finish(0, QProcess.ExitStatus.NormalExit)

    assert outdated_timer.active is False
    assert outdated_proc.deleted is True
    assert controller.running is False

    assert up_to_dates == [UP_TO_DATE_MESSAGE]
    assert ready_to_restarts == []
    assert failures == []
    assert len(processes) == 2  # No upgrade or probe processes created


def test_controller_outdated_runs_upgrade_reloads_marker_and_probes(tmp_path: Path) -> None:
    _qapp()
    installation = _make_valid_installation(tmp_path, version="0.2.0")

    processes: list[FakeProcess] = []
    timers: list[FakeTimer] = []

    def process_factory(_parent: QObject | None) -> FakeProcess:
        p = FakeProcess(_parent)
        processes.append(p)
        return p

    def timer_factory(_parent: QObject | None) -> FakeTimer:
        t = FakeTimer(_parent)
        timers.append(t)
        return t

    # Set up new installation marker that will be loaded post-upgrade
    new_installation_dto = HomebrewInstallation(
        version="0.3.0",
        formula=HOMEBREW_FORMULA,
        homebrew_prefix=installation.homebrew_prefix,
        brew_path=installation.brew_path,
        launch_path=installation.launch_path,
        marker_path=installation.marker_path,
    )
    marker_loader_calls: list[tuple[Path, str | None]] = []

    def fake_marker_loader(path: Path, *, expected_version: str | None = None) -> HomebrewInstallation:
        marker_loader_calls.append((path, expected_version))
        return new_installation_dto

    detached_calls: list[tuple[str, list[str]]] = []

    def fake_detached_starter(program: str, arguments: list[str]) -> tuple[bool, int]:
        detached_calls.append((program, arguments))
        return True, 8888

    controller = HomebrewUpdateController(
        installation,
        process_factory=process_factory,
        timer_factory=timer_factory,
        marker_loader=fake_marker_loader,
        detached_starter=fake_detached_starter,
    )

    statuses: list[str] = []
    up_to_dates: list[str] = []
    ready_to_restarts: list[str] = []
    failures: list[str] = []

    controller.status_changed.connect(statuses.append)
    controller.up_to_date.connect(up_to_dates.append)
    controller.ready_to_restart.connect(ready_to_restarts.append)
    controller.failed.connect(failures.append)

    assert controller.install_latest() is True
    assert controller.running is True

    # 1. UPDATE finish
    update_proc = processes[0]
    update_proc.finish(0, QProcess.ExitStatus.NormalExit)

    # 2. OUTDATED with non-empty formulae
    outdated_proc = processes[1]
    outdated_payload = json.dumps(
        {
            "formulae": [
                {
                    "name": "falafacil",
                    "installed_versions": ["0.2.0"],
                    "current_version": "0.3.0",
                    "pinned": False,
                    "pinned_version": None,
                }
            ]
        }
    ).encode("utf-8")
    outdated_proc.feed_stdout(outdated_payload)
    outdated_proc.finish(0, QProcess.ExitStatus.NormalExit)

    # 3. UPGRADE process started
    assert len(processes) == 3
    upgrade_proc = processes[2]
    assert upgrade_proc.started is True
    assert upgrade_proc.program == str(installation.brew_path)
    assert upgrade_proc.arguments == [
        "upgrade",
        "--formula",
        "--no-ask",
        HOMEBREW_FORMULA,
    ]
    assert upgrade_proc.channel_mode == QProcess.ProcessChannelMode.MergedChannels
    upgrade_timer = timers[2]
    assert upgrade_timer.active is True
    assert upgrade_timer.interval_ms == UPGRADE_TIMEOUT_MS

    # Finish UPGRADE
    upgrade_proc.feed_stdout(b"==> Upgrading OthonBreener/falafacil/falafacil\n")
    upgrade_proc.finish(0, QProcess.ExitStatus.NormalExit)
    assert upgrade_proc.deleted is True

    # Verify marker reload call
    assert marker_loader_calls == [(installation.marker_path, None)]

    # 4. PROBE process started
    assert len(processes) == 4
    probe_proc = processes[3]
    assert probe_proc.started is True
    assert probe_proc.program == str(new_installation_dto.launch_path)
    assert probe_proc.arguments == ["--update-probe", "0.3.0"]
    assert probe_proc.channel_mode == QProcess.ProcessChannelMode.MergedChannels
    probe_timer = timers[3]
    assert probe_timer.active is True
    assert probe_timer.interval_ms == PROBE_TIMEOUT_MS

    # Finish PROBE
    probe_proc.finish(0, QProcess.ExitStatus.NormalExit)
    assert probe_proc.deleted is True

    assert controller.running is False
    assert statuses == [
        STATUS_UPDATING,
        STATUS_CHECKING,
        STATUS_UPGRADING,
        STATUS_VERIFYING,
    ]
    assert up_to_dates == []
    assert ready_to_restarts == [READY_TO_RESTART_MESSAGE]
    assert failures == []

    # Now restart should succeed
    assert controller.restart() is True
    assert detached_calls == [(str(new_installation_dto.launch_path), [])]


def test_controller_rejects_duplicate_install_while_running(tmp_path: Path) -> None:
    _qapp()
    installation = _make_valid_installation(tmp_path)

    processes: list[FakeProcess] = []

    controller = HomebrewUpdateController(
        installation,
        process_factory=lambda p: processes.append(FakeProcess(p)) or processes[-1],
    )

    assert controller.install_latest() is True
    assert controller.running is True
    assert len(processes) == 1

    # Second call must be rejected immediately without starting any new process
    assert controller.install_latest() is False
    assert controller.running is True
    assert len(processes) == 1


@pytest.mark.parametrize("phase_idx", [0, 1, 2, 3])
@pytest.mark.parametrize(
    ("exit_code", "exit_status", "error"),
    [
        (1, QProcess.ExitStatus.NormalExit, None),
        (0, QProcess.ExitStatus.CrashExit, None),
        (0, QProcess.ExitStatus.NormalExit, QProcess.ProcessError.FailedToStart),
        (0, QProcess.ExitStatus.NormalExit, QProcess.ProcessError.Crashed),
    ],
)
def test_controller_process_failures_and_errors(
    tmp_path: Path,
    phase_idx: int,
    exit_code: int,
    exit_status: QProcess.ExitStatus,
    error: QProcess.ProcessError | None,
) -> None:
    _qapp()
    installation = _make_valid_installation(tmp_path, version="0.2.0")

    processes: list[FakeProcess] = []

    new_installation_dto = HomebrewInstallation(
        version="0.3.0",
        formula=HOMEBREW_FORMULA,
        homebrew_prefix=installation.homebrew_prefix,
        brew_path=installation.brew_path,
        launch_path=installation.launch_path,
        marker_path=installation.marker_path,
    )

    controller = HomebrewUpdateController(
        installation,
        process_factory=lambda p: processes.append(FakeProcess(p)) or processes[-1],
        marker_loader=lambda path, expected_version=None: new_installation_dto,
    )

    failures: list[str] = []
    controller.failed.connect(failures.append)

    assert controller.install_latest() is True

    # Advance to target phase
    if phase_idx > 0:
        processes[0].finish(0, QProcess.ExitStatus.NormalExit)
    if phase_idx > 1:
        outdated_payload = json.dumps(
            {"formulae": [{"name": "falafacil", "pinned": False}]}
        ).encode("utf-8")
        processes[1].feed_stdout(outdated_payload)
        processes[1].finish(0, QProcess.ExitStatus.NormalExit)
    if phase_idx > 2:
        processes[2].finish(0, QProcess.ExitStatus.NormalExit)

    target_proc = processes[phase_idx]
    if error is not None:
        target_proc.fail_to_start(error)
        # Late finish signal on same target_proc must be safely ignored
        target_proc.finish(1, QProcess.ExitStatus.CrashExit)
    else:
        target_proc.finish(exit_code, exit_status)
        # Late error signal on same target_proc must be safely ignored
        target_proc.fail_to_start(QProcess.ProcessError.Crashed)

    assert controller.running is False
    assert failures == [GENERIC_FAILURE_MESSAGE]
    assert len(processes) == phase_idx + 1  # No subsequent phase started


@pytest.mark.parametrize("phase_idx", [0, 1, 2, 3])
@pytest.mark.parametrize(
    "process_error",
    [
        QProcess.ProcessError.ReadError,
        QProcess.ProcessError.WriteError,
        QProcess.ProcessError.UnknownError,
        QProcess.ProcessError.Crashed,
        QProcess.ProcessError.Timedout,
    ],
)
def test_controller_running_process_errors_trigger_async_abort(
    tmp_path: Path,
    phase_idx: int,
    process_error: QProcess.ProcessError,
) -> None:
    _qapp()
    installation = _make_valid_installation(tmp_path, version="0.2.0")

    processes: list[FakeProcess] = []
    timers: list[FakeTimer] = []

    new_installation_dto = HomebrewInstallation(
        version="0.3.0",
        formula=HOMEBREW_FORMULA,
        homebrew_prefix=installation.homebrew_prefix,
        brew_path=installation.brew_path,
        launch_path=installation.launch_path,
        marker_path=installation.marker_path,
    )

    controller = HomebrewUpdateController(
        installation,
        process_factory=lambda p: processes.append(FakeProcess(p)) or processes[-1],
        timer_factory=lambda p: timers.append(FakeTimer(p)) or timers[-1],
        marker_loader=lambda path, expected_version=None: new_installation_dto,
    )

    failures: list[str] = []
    controller.failed.connect(failures.append)

    assert controller.install_latest() is True

    # Advance to target phase
    if phase_idx > 0:
        processes[0].finish(0, QProcess.ExitStatus.NormalExit)
    if phase_idx > 1:
        outdated_payload = json.dumps(
            {"formulae": [{"name": "falafacil", "pinned": False}]}
        ).encode("utf-8")
        processes[1].feed_stdout(outdated_payload)
        processes[1].finish(0, QProcess.ExitStatus.NormalExit)
    if phase_idx > 2:
        processes[2].finish(0, QProcess.ExitStatus.NormalExit)

    target_proc = processes[phase_idx]
    assert target_proc.state() == QProcess.ProcessState.Running

    # Emit error while process is still in Running state
    target_proc.emit_error_while_running(process_error)

    # Controller must initiate async abort: terminate requested, grace timer armed
    assert target_proc.terminated is True
    assert controller.running is True
    assert controller.install_latest() is False  # Cannot start another while aborting
    assert failures == []  # Not emitted yet until process terminates

    # Grace timer should be active
    grace_timer = timers[-1]
    assert grace_timer.interval_ms == KILL_GRACE_MS
    assert grace_timer.active is True

    # Late error signals and output must be safely ignored/drained
    target_proc.emit_error_while_running(QProcess.ProcessError.ReadError)
    target_proc.feed_stdout(b"late output\n")

    # Process finally finishes
    target_proc.finish(1, QProcess.ExitStatus.CrashExit)

    assert controller.running is False
    assert failures == [GENERIC_FAILURE_MESSAGE]
    assert len(processes) == phase_idx + 1


def test_controller_drains_all_non_json_output_without_leaking_secrets(tmp_path: Path) -> None:
    _qapp()
    installation = _make_valid_installation(tmp_path, version="0.2.0")

    processes: list[FakeProcess] = []

    new_installation_dto = HomebrewInstallation(
        version="0.3.0",
        formula=HOMEBREW_FORMULA,
        homebrew_prefix=installation.homebrew_prefix,
        brew_path=installation.brew_path,
        launch_path=installation.launch_path,
        marker_path=installation.marker_path,
    )

    controller = HomebrewUpdateController(
        installation,
        process_factory=lambda p: processes.append(FakeProcess(p)) or processes[-1],
        marker_loader=lambda path, expected_version=None: new_installation_dto,
    )

    captured_messages: list[str] = []
    controller.status_changed.connect(captured_messages.append)
    controller.up_to_date.connect(captured_messages.append)
    controller.ready_to_restart.connect(captured_messages.append)
    controller.failed.connect(captured_messages.append)

    secret = "SUPER_SECRET_TOKEN_987654321"

    assert controller.install_latest() is True

    # UPDATE with secret stdout
    processes[0].feed_stdout(f"token: {secret}\n".encode("utf-8"))
    processes[0].finish(0, QProcess.ExitStatus.NormalExit)

    # OUTDATED with secret stderr
    processes[1].feed_stderr(f"stderr token: {secret}\n".encode("utf-8"))
    processes[1].feed_stdout(
        json.dumps({"formulae": [{"name": "falafacil", "pinned": False}]}).encode("utf-8")
    )
    processes[1].finish(0, QProcess.ExitStatus.NormalExit)

    # UPGRADE with secret stdout
    processes[2].feed_stdout(f"upgrade token: {secret}\n".encode("utf-8"))
    processes[2].finish(0, QProcess.ExitStatus.NormalExit)

    # PROBE with secret stdout
    processes[3].feed_stdout(f"probe token: {secret}\n".encode("utf-8"))
    processes[3].finish(0, QProcess.ExitStatus.NormalExit)

    assert controller.running is False
    for msg in captured_messages:
        assert secret not in msg


def test_controller_outdated_stdout_size_boundary_and_overflow_modes(tmp_path: Path) -> None:
    _qapp()
    installation = _make_valid_installation(tmp_path)

    # 1. Exactly MAX_OUTDATED_STDOUT_BYTES (262144 bytes) is accepted
    processes: list[FakeProcess] = []
    controller = HomebrewUpdateController(
        installation,
        process_factory=lambda p: processes.append(FakeProcess(p)) or processes[-1],
    )

    up_to_dates: list[str] = []
    failures: list[str] = []
    controller.up_to_date.connect(up_to_dates.append)
    controller.failed.connect(failures.append)

    assert controller.install_latest() is True
    processes[0].finish(0, QProcess.ExitStatus.NormalExit)

    # Create exact 262144 bytes valid payload with padding in an extra key
    base_json = '{"formulae": [], "pad": ""}'
    padding_needed = MAX_OUTDATED_STDOUT_BYTES - len(base_json.encode("utf-8"))
    exact_payload = json.dumps({"formulae": [], "pad": "A" * padding_needed}).encode("utf-8")
    assert len(exact_payload) == MAX_OUTDATED_STDOUT_BYTES

    processes[1].feed_stdout(exact_payload)
    processes[1].finish(0, QProcess.ExitStatus.NormalExit)

    assert controller.running is False
    assert up_to_dates == [UP_TO_DATE_MESSAGE]
    assert failures == []
    assert len(controller._stdout_buffer) == 0

    # 2. Multi-MiB chunk (e.g. 5 MiB in single feed_stdout) bounds buffer and aborts
    processes.clear()
    failures.clear()
    up_to_dates.clear()

    controller_multimib = HomebrewUpdateController(
        installation,
        process_factory=lambda p: processes.append(FakeProcess(p)) or processes[-1],
    )
    controller_multimib.failed.connect(failures.append)

    assert controller_multimib.install_latest() is True
    processes[0].finish(0, QProcess.ExitStatus.NormalExit)

    multimib_chunk = b"X" * (5 * 1024 * 1024)
    processes[1].feed_stdout(multimib_chunk)

    # Process is terminated, controller is still running/aborting, buffer <= 256 KiB
    assert processes[1].terminated is True
    assert controller_multimib.running is True
    assert controller_multimib.install_latest() is False
    assert len(controller_multimib._stdout_buffer) <= MAX_OUTDATED_STDOUT_BYTES
    assert failures == []

    processes[1].finish(0, QProcess.ExitStatus.CrashExit)
    assert controller_multimib.running is False
    assert failures == [GENERIC_FAILURE_MESSAGE]
    assert len(controller_multimib._stdout_buffer) == 0

    # 3. Cross-chunk overflow (200 KiB then 100 KiB)
    processes.clear()
    failures.clear()

    controller_cross = HomebrewUpdateController(
        installation,
        process_factory=lambda p: processes.append(FakeProcess(p)) or processes[-1],
    )
    controller_cross.failed.connect(failures.append)

    assert controller_cross.install_latest() is True
    processes[0].finish(0, QProcess.ExitStatus.NormalExit)

    processes[1].feed_stdout(b"B" * (200 * 1024))
    assert processes[1].terminated is False
    assert controller_cross.running is True

    processes[1].feed_stdout(b"C" * (100 * 1024))
    assert processes[1].terminated is True
    assert controller_cross.running is True
    assert len(controller_cross._stdout_buffer) <= MAX_OUTDATED_STDOUT_BYTES

    processes[1].finish(0, QProcess.ExitStatus.CrashExit)
    assert controller_cross.running is False
    assert failures == [GENERIC_FAILURE_MESSAGE]
    assert len(controller_cross._stdout_buffer) == 0

    # 4. Finish-only overflow (300 KiB queued when process finishes directly)
    processes.clear()
    failures.clear()

    controller_finish = HomebrewUpdateController(
        installation,
        process_factory=lambda p: processes.append(FakeProcess(p)) or processes[-1],
    )
    controller_finish.failed.connect(failures.append)

    assert controller_finish.install_latest() is True
    processes[0].finish(0, QProcess.ExitStatus.NormalExit)

    # Directly append to queue without readyRead signal before finish
    processes[1]._stdout_queue.extend(b"D" * (300 * 1024))
    processes[1].finish(0, QProcess.ExitStatus.NormalExit)

    assert controller_finish.running is False
    assert failures == [GENERIC_FAILURE_MESSAGE]
    assert len(controller_finish._stdout_buffer) == 0


@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "up_to_date",
        "pinned",
        "malformed_json",
        "overflow",
        "watchdog_timeout",
        "process_error",
    ],
)
def test_controller_no_retention_sentinels_for_all_outcomes(
    tmp_path: Path,
    outcome: str,
) -> None:
    _qapp()
    installation = _make_valid_installation(tmp_path, version="0.2.0")

    processes: list[FakeProcess] = []
    timers: list[FakeTimer] = []

    new_installation_dto = HomebrewInstallation(
        version="0.3.0",
        formula=HOMEBREW_FORMULA,
        homebrew_prefix=installation.homebrew_prefix,
        brew_path=installation.brew_path,
        launch_path=installation.launch_path,
        marker_path=installation.marker_path,
    )

    controller = HomebrewUpdateController(
        installation,
        process_factory=lambda p: processes.append(FakeProcess(p)) or processes[-1],
        timer_factory=lambda p: timers.append(FakeTimer(p)) or timers[-1],
        marker_loader=lambda path, expected_version=None: new_installation_dto,
    )

    captured_messages: list[str] = []
    controller.status_changed.connect(captured_messages.append)
    controller.up_to_date.connect(captured_messages.append)
    controller.ready_to_restart.connect(captured_messages.append)
    controller.failed.connect(captured_messages.append)

    secret = "CONFIDENTIAL_SENTINEL_TOKEN_12345"

    assert controller.install_latest() is True
    processes[0].finish(0, QProcess.ExitStatus.NormalExit)

    if outcome == "success":
        outdated_payload = json.dumps(
            {
                "formulae": [{"name": "falafacil", "pinned": False}],
                "secret_field": secret,
            }
        ).encode("utf-8")
        processes[1].feed_stdout(outdated_payload)
        processes[1].finish(0, QProcess.ExitStatus.NormalExit)
        processes[2].feed_stdout(f"upgrade: {secret}\n".encode("utf-8"))
        processes[2].finish(0, QProcess.ExitStatus.NormalExit)
        processes[3].feed_stdout(f"probe: {secret}\n".encode("utf-8"))
        processes[3].finish(0, QProcess.ExitStatus.NormalExit)
    elif outcome == "up_to_date":
        outdated_payload = json.dumps(
            {"formulae": [], "secret_field": secret}
        ).encode("utf-8")
        processes[1].feed_stdout(outdated_payload)
        processes[1].finish(0, QProcess.ExitStatus.NormalExit)
    elif outcome == "pinned":
        outdated_payload = json.dumps(
            {
                "formulae": [
                    {
                        "name": "falafacil",
                        "pinned": True,
                        "pinned_version": "0.2.0",
                        "secret_field": secret,
                    }
                ]
            }
        ).encode("utf-8")
        processes[1].feed_stdout(outdated_payload)
        processes[1].finish(0, QProcess.ExitStatus.NormalExit)
    elif outcome == "malformed_json":
        processes[1].feed_stdout(f'{{"bad_json": "{secret}"'.encode("utf-8"))
        processes[1].finish(0, QProcess.ExitStatus.NormalExit)
    elif outcome == "overflow":
        oversized = f'{{"formulae": [], "pad": "{secret}"'.encode("utf-8") + (b"A" * (MAX_OUTDATED_STDOUT_BYTES + 100))
        processes[1].feed_stdout(oversized)
        processes[1].finish(0, QProcess.ExitStatus.CrashExit)
    elif outcome == "watchdog_timeout":
        processes[1].feed_stdout(f'{{"partial": "{secret}"'.encode("utf-8"))
        watchdog = timers[-1]
        watchdog.fire()
        processes[1].finish(1, QProcess.ExitStatus.CrashExit)
    elif outcome == "process_error":
        processes[1].feed_stdout(f'{{"partial": "{secret}"'.encode("utf-8"))
        processes[1].emit_error_while_running(QProcess.ProcessError.ReadError)
        processes[1].finish(1, QProcess.ExitStatus.CrashExit)

    assert controller.running is False
    assert len(controller._stdout_buffer) == 0
    for msg in captured_messages:
        assert secret not in msg


@pytest.mark.parametrize(
    "bad_payload",
    [
        b"\x80\x81\x82",  # Invalid UTF-8
        b"error: failed to check outdated",  # Non-JSON
        b'["a", "b", "c"]',  # Non-dict JSON list
        b'"string"',  # Non-dict JSON string
        b"12345",  # Non-dict JSON number
        b'{"other_key": []}',  # Missing "formulae"
        b'{"formulae": "not-a-list"}',  # "formulae" is not list
        b'{"formulae": 123}',  # "formulae" is int
        b'{"formulae": [123]}',  # Entry in "formulae" is not dict
        b'{"formulae": ["string_item"]}',  # Entry in "formulae" is string
        json.dumps({"formulae": [{"name": "falafacil", "pinned_version": None}]}).encode("utf-8"),  # Missing "pinned"
        json.dumps({"formulae": [{"name": "falafacil", "pinned": "false", "pinned_version": None}]}).encode("utf-8"),  # Pinned is str
        json.dumps({"formulae": [{"name": "falafacil", "pinned": 0, "pinned_version": None}]}).encode("utf-8"),  # Pinned is int
        json.dumps({"formulae": [{"name": "falafacil", "pinned": None, "pinned_version": None}]}).encode("utf-8"),  # Pinned is None
        json.dumps({"formulae": [{"name": "falafacil", "pinned": True, "pinned_version": "0.2.0"}]}).encode("utf-8"),  # Pinned True
        json.dumps({"formulae": [{"name": "falafacil", "pinned": True, "pinned_version": None}]}).encode("utf-8"),  # Pinned True with None version
        json.dumps({"formulae": [{"name": "falafacil", "pinned": False, "pinned_version": "0.2.0"}]}).encode("utf-8"),  # Pinned version non-null
        json.dumps({"formulae": [{"name": "falafacil", "pinned": False, "pinned_version": 1}]}).encode("utf-8"),  # Pinned version non-null int
        json.dumps({"formulae": [{"name": "other-formula", "pinned": False, "pinned_version": None}]}).encode("utf-8"),  # Divergent name
        json.dumps({"formulae": [{"full_name": "other/tap/formula", "pinned": False, "pinned_version": None}]}).encode("utf-8"),  # Divergent full_name
        json.dumps({"formulae": [{"name": "other", "full_name": "other/tap/formula", "pinned": False, "pinned_version": None}]}).encode("utf-8"),  # Divergent name & full_name
        json.dumps({"formulae": [{"name": "falafacil", "full_name": "other/tap/falafacil", "pinned": False}]}).encode("utf-8"),  # Valid short name with divergent full_name
        json.dumps({"formulae": [{"name": "other", "full_name": HOMEBREW_FORMULA, "pinned": False}]}).encode("utf-8"),  # Valid full_name with divergent name
        json.dumps({"formulae": [{"full_name": HOMEBREW_FORMULA, "pinned": False}]}).encode("utf-8"),  # Valid full_name with missing name
        json.dumps({"formulae": [{"name": "falafacil", "full_name": 12345, "pinned": False}]}).encode("utf-8"),  # full_name is non-string int
        json.dumps({"formulae": [{"name": "falafacil", "full_name": None, "pinned": False}]}).encode("utf-8"),  # Explicit null full_name
        json.dumps({"formulae": [{"name": "falafacil", "pinned": False}, {"name": "falafacil", "pinned": False}]}).encode("utf-8"),  # Duplicate valid records
        json.dumps({"formulae": [{"name": "falafacil", "pinned": False}, {"name": "other", "pinned": False}]}).encode("utf-8"),  # Extra record
        json.dumps({"formulae": [{"name": "falafacil", "pinned": False}, {"name": "pkg2", "pinned": False}, {"name": "pkg3", "pinned": False}]}).encode("utf-8"),  # 3 records
    ],
)
def test_controller_outdated_formula_schema_validation(tmp_path: Path, bad_payload: bytes) -> None:
    _qapp()
    installation = _make_valid_installation(tmp_path)

    processes: list[FakeProcess] = []
    controller = HomebrewUpdateController(
        installation,
        process_factory=lambda p: processes.append(FakeProcess(p)) or processes[-1],
    )

    failures: list[str] = []
    controller.failed.connect(failures.append)

    assert controller.install_latest() is True
    processes[0].finish(0, QProcess.ExitStatus.NormalExit)

    processes[1].feed_stdout(bad_payload)
    processes[1].finish(0, QProcess.ExitStatus.NormalExit)

    assert controller.running is False
    assert failures == [GENERIC_FAILURE_MESSAGE]
    assert len(controller._stdout_buffer) == 0
    assert len(processes) == 2  # No upgrade started

def test_controller_outdated_accepts_coherent_full_name(tmp_path: Path) -> None:
    _qapp()
    installation = _make_valid_installation(tmp_path, version="0.2.0")

    processes: list[FakeProcess] = []
    controller = HomebrewUpdateController(
        installation,
        process_factory=lambda p: processes.append(FakeProcess(p)) or processes[-1],
    )

    statuses: list[str] = []
    controller.status_changed.connect(statuses.append)

    assert controller.install_latest() is True
    processes[0].finish(0, QProcess.ExitStatus.NormalExit)

    coherent_payload = json.dumps(
        {
            "formulae": [
                {
                    "name": "falafacil",
                    "full_name": HOMEBREW_FORMULA,
                    "installed_versions": ["0.2.0"],
                    "current_version": "0.3.0",
                    "pinned": False,
                    "pinned_version": None,
                }
            ]
        }
    ).encode("utf-8")
    processes[1].feed_stdout(coherent_payload)
    processes[1].finish(0, QProcess.ExitStatus.NormalExit)

    assert controller.running is True
    assert len(processes) == 3
    assert processes[2].started is True
    assert processes[2].arguments == ["upgrade", "--formula", "--no-ask", HOMEBREW_FORMULA]


def test_controller_outdated_rejects_explicit_null_full_name(tmp_path: Path) -> None:
    _qapp()
    installation = _make_valid_installation(tmp_path, version="0.2.0")

    processes: list[FakeProcess] = []
    controller = HomebrewUpdateController(
        installation,
        process_factory=lambda p: processes.append(FakeProcess(p)) or processes[-1],
    )

    failures: list[str] = []
    controller.failed.connect(failures.append)

    assert controller.install_latest() is True
    processes[0].finish(0, QProcess.ExitStatus.NormalExit)

    null_full_name_payload = json.dumps(
        {
            "formulae": [
                {
                    "name": "falafacil",
                    "full_name": None,
                    "installed_versions": ["0.2.0"],
                    "current_version": "0.3.0",
                    "pinned": False,
                    "pinned_version": None,
                }
            ]
        }
    ).encode("utf-8")
    processes[1].feed_stdout(null_full_name_payload)
    processes[1].finish(0, QProcess.ExitStatus.NormalExit)

    assert controller.running is False
    assert failures == [GENERIC_FAILURE_MESSAGE]
    assert len(controller._stdout_buffer) == 0
    assert len(processes) == 2  # No upgrade started

@pytest.mark.parametrize(
    ("phase_idx", "expected_timeout_ms"),
    [
        (0, UPDATE_TIMEOUT_MS),
        (1, OUTDATED_TIMEOUT_MS),
        (2, UPGRADE_TIMEOUT_MS),
        (3, PROBE_TIMEOUT_MS),
    ],
)
def test_controller_watchdogs_and_kill_grace(
    tmp_path: Path,
    phase_idx: int,
    expected_timeout_ms: int,
) -> None:
    _qapp()
    installation = _make_valid_installation(tmp_path, version="0.2.0")

    processes: list[FakeProcess] = []
    timers: list[FakeTimer] = []

    new_installation_dto = HomebrewInstallation(
        version="0.3.0",
        formula=HOMEBREW_FORMULA,
        homebrew_prefix=installation.homebrew_prefix,
        brew_path=installation.brew_path,
        launch_path=installation.launch_path,
        marker_path=installation.marker_path,
    )

    controller = HomebrewUpdateController(
        installation,
        process_factory=lambda p: processes.append(FakeProcess(p)) or processes[-1],
        timer_factory=lambda p: timers.append(FakeTimer(p)) or timers[-1],
        marker_loader=lambda path, expected_version=None: new_installation_dto,
    )

    failures: list[str] = []
    controller.failed.connect(failures.append)

    assert controller.install_latest() is True

    # Advance to target phase
    if phase_idx > 0:
        processes[0].finish(0, QProcess.ExitStatus.NormalExit)
    if phase_idx > 1:
        outdated_payload = json.dumps(
            {"formulae": [{"name": "falafacil", "pinned": False}]}
        ).encode("utf-8")
        processes[1].feed_stdout(outdated_payload)
        processes[1].finish(0, QProcess.ExitStatus.NormalExit)
    if phase_idx > 2:
        processes[2].finish(0, QProcess.ExitStatus.NormalExit)

    watchdog_timer = timers[-1]
    assert watchdog_timer.interval_ms == expected_timeout_ms
    target_proc = processes[phase_idx]

    # Fire watchdog
    watchdog_timer.fire()
    assert target_proc.terminated is True
    assert target_proc.killed is False

    # Grace timer should have been created with 5000 ms
    grace_timer = timers[-1]
    assert grace_timer.interval_ms == KILL_GRACE_MS
    assert grace_timer.active is True

    # Scenario A: process does not finish within grace -> grace fires kill()
    grace_timer.fire()
    assert target_proc.killed is True

    # Process now finishes
    target_proc.finish(1, QProcess.ExitStatus.CrashExit)

    assert controller.running is False
    assert failures == [TIMEOUT_MESSAGE]
    assert len(controller._stdout_buffer) == 0


def test_controller_watchdog_finishes_during_grace_without_calling_kill(tmp_path: Path) -> None:
    _qapp()
    installation = _make_valid_installation(tmp_path)

    processes: list[FakeProcess] = []
    timers: list[FakeTimer] = []

    controller = HomebrewUpdateController(
        installation,
        process_factory=lambda p: processes.append(FakeProcess(p)) or processes[-1],
        timer_factory=lambda p: timers.append(FakeTimer(p)) or timers[-1],
    )

    failures: list[str] = []
    controller.failed.connect(failures.append)

    assert controller.install_latest() is True

    watchdog = timers[0]
    proc = processes[0]

    # Fire watchdog
    watchdog.fire()
    assert proc.terminated is True

    # Process finishes immediately during grace before grace timer expires
    proc.finish(0, QProcess.ExitStatus.NormalExit)

    assert proc.killed is False
    assert controller.running is False
    assert failures == [TIMEOUT_MESSAGE]


@pytest.mark.parametrize(
    "mutation",
    [
        "raise_error",
        "raise_os_error",
        "same_version",
        "downgrade_version",
        "numeric_lexical_trap",
        "invalid_semver",
        "divergent_formula",
        "divergent_prefix",
        "divergent_brew_path",
        "divergent_launch_path",
        "divergent_marker_path",
    ],
)
def test_controller_marker_verification_failures(tmp_path: Path, mutation: str) -> None:
    _qapp()
    installation = _make_valid_installation(tmp_path, version="0.2.0")

    processes: list[FakeProcess] = []

    def fake_marker_loader(path: Path, *, expected_version: str | None = None) -> HomebrewInstallation:
        if mutation == "raise_error":
            raise HomebrewUpdateError("marker error")
        if mutation == "raise_os_error":
            raise OSError("os error")
        if mutation == "same_version":
            ver = "0.2.0"
        elif mutation == "downgrade_version":
            ver = "0.1.0"
        elif mutation == "numeric_lexical_trap":
            # If old is 0.10.0 and new is 0.9.0, lexical "0.9.0" > "0.10.0" but numeric 0.9.0 < 0.10.0
            ver = "0.1.0"
        elif mutation == "invalid_semver":
            ver = "not-semver"
        else:
            ver = "0.3.0"

        formula = "other/formula" if mutation == "divergent_formula" else HOMEBREW_FORMULA
        prefix = Path("/other/prefix") if mutation == "divergent_prefix" else installation.homebrew_prefix
        brew_path = Path("/other/brew") if mutation == "divergent_brew_path" else installation.brew_path
        launch_path = Path("/other/launch") if mutation == "divergent_launch_path" else installation.launch_path
        marker_path = Path("/other/marker") if mutation == "divergent_marker_path" else installation.marker_path

        return HomebrewInstallation(
            version=ver,
            formula=formula,
            homebrew_prefix=prefix,
            brew_path=brew_path,
            launch_path=launch_path,
            marker_path=marker_path,
        )

    controller = HomebrewUpdateController(
        installation,
        process_factory=lambda p: processes.append(FakeProcess(p)) or processes[-1],
        marker_loader=fake_marker_loader,
    )

    failures: list[str] = []
    controller.failed.connect(failures.append)

    assert controller.install_latest() is True
    processes[0].finish(0, QProcess.ExitStatus.NormalExit)

    outdated_payload = json.dumps(
        {"formulae": [{"name": "falafacil", "pinned": False}]}
    ).encode("utf-8")
    processes[1].feed_stdout(outdated_payload)
    processes[1].finish(0, QProcess.ExitStatus.NormalExit)

    processes[2].finish(0, QProcess.ExitStatus.NormalExit)

    assert controller.running is False
    assert failures == [GENERIC_FAILURE_MESSAGE]
    assert len(processes) == 3  # No probe started


def test_controller_numeric_semver_downgrade_trap(tmp_path: Path) -> None:
    _qapp()
    # Old version is 0.10.0
    installation = _make_valid_installation(tmp_path, version="0.10.0")

    processes: list[FakeProcess] = []

    # New version is 0.9.0 (which is alphabetically greater than "0.10.0", but numerically a downgrade)
    new_installation_dto = HomebrewInstallation(
        version="0.9.0",
        formula=HOMEBREW_FORMULA,
        homebrew_prefix=installation.homebrew_prefix,
        brew_path=installation.brew_path,
        launch_path=installation.launch_path,
        marker_path=installation.marker_path,
    )

    controller = HomebrewUpdateController(
        installation,
        process_factory=lambda p: processes.append(FakeProcess(p)) or processes[-1],
        marker_loader=lambda path, expected_version=None: new_installation_dto,
    )

    failures: list[str] = []
    controller.failed.connect(failures.append)

    assert controller.install_latest() is True
    processes[0].finish(0, QProcess.ExitStatus.NormalExit)

    outdated_payload = json.dumps(
        {"formulae": [{"name": "falafacil", "pinned": False}]}
    ).encode("utf-8")
    processes[1].feed_stdout(outdated_payload)
    processes[1].finish(0, QProcess.ExitStatus.NormalExit)

    processes[2].finish(0, QProcess.ExitStatus.NormalExit)

    assert controller.running is False
    assert failures == [GENERIC_FAILURE_MESSAGE]
    assert len(processes) == 3  # No probe started


class _CustomIntSubclass(int):
    pass


@pytest.mark.parametrize(
    ("starter_result", "expected_restart_bool"),
    [
        ((False, 0), False),
        ((True, 0), False),
        ((True, -1), False),
        ((True, -999), False),
        ((False, 1234), False),
        ("invalid", False),
        (True, False),  # Bare bool True
        (False, False),  # Bare bool False
        ((), False),  # Empty tuple
        ((True,), False),  # Unitary tuple with bool
        ((1234,), False),  # Unitary tuple with int
        ((True, 1234, "extra"), False),  # 3-element tuple with str
        ((True, 1234, None), False),  # 3-element tuple with None
        ((True, 1234, 5678), False),  # 3-element tuple with int
        ((True, 1234, True, False), False),  # 4-element tuple
        ([True, 1234], False),  # List instead of tuple
        ({"started": True, "pid": 1234}, False),  # Dict instead of tuple
        ((True, True), False),  # Bool PID
        ((True, False), False),  # Bool False PID
        ((True, 1.0), False),  # Float PID
        ((True, "123"), False),  # Str PID
        ((True, _CustomIntSubclass(1234)), False),  # Subclass of int
        (None, False),
        ((True, 1), True),
        ((True, 5678), True),
    ],
)
def test_controller_restart_contract(
    tmp_path: Path,
    starter_result: Any,
    expected_restart_bool: bool,
) -> None:
    _qapp()
    installation = _make_valid_installation(tmp_path, version="0.2.0")

    processes: list[FakeProcess] = []

    new_installation_dto = HomebrewInstallation(
        version="0.3.0",
        formula=HOMEBREW_FORMULA,
        homebrew_prefix=installation.homebrew_prefix,
        brew_path=installation.brew_path,
        launch_path=installation.launch_path,
        marker_path=installation.marker_path,
    )

    detached_calls: list[tuple[str, list[str]]] = []

    def fake_detached_starter(program: str, arguments: list[str]) -> Any:
        detached_calls.append((program, arguments))
        return starter_result

    controller = HomebrewUpdateController(
        installation,
        process_factory=lambda p: processes.append(FakeProcess(p)) or processes[-1],
        marker_loader=lambda path, expected_version=None: new_installation_dto,
        detached_starter=fake_detached_starter,
    )

    # Before install: restart must return False without calling detached starter
    assert controller.restart() is False
    assert detached_calls == []

    assert controller.install_latest() is True

    # While running: restart must return False without calling detached starter
    assert controller.restart() is False
    assert detached_calls == []

    # Complete all 4 phases successfully
    processes[0].finish(0, QProcess.ExitStatus.NormalExit)
    processes[1].feed_stdout(
        json.dumps({"formulae": [{"name": "falafacil", "pinned": False}]}).encode("utf-8")
    )
    processes[1].finish(0, QProcess.ExitStatus.NormalExit)
    processes[2].finish(0, QProcess.ExitStatus.NormalExit)
    processes[3].finish(0, QProcess.ExitStatus.NormalExit)

    assert controller.running is False

    # Now call restart
    result = controller.restart()
    assert result is expected_restart_bool
    assert detached_calls == [(str(new_installation_dto.launch_path), [])]

@pytest.mark.parametrize(
    ("qprocess_result", "expected_result"),
    [
        ((True, 42), (True, 42)),
        ((True, 1), (True, 1)),
        ((True, 99999), (True, 99999)),
        ((True, 0), (False, 0)),
        ((True, -1), (False, 0)),
        ((True, -50), (False, 0)),
        ((False, 42), (False, 0)),
        ((False, 0), (False, 0)),
        ((True, 42, "extra"), (False, 0)),
        ((True, 42, None), (False, 0)),
        ((True, 42, 100), (False, 0)),
        ((), (False, 0)),
        ((True,), (False, 0)),
        ((42,), (False, 0)),
        ([True, 42], (False, 0)),
        ((True, True), (False, 0)),
        ((True, 1.0), (False, 0)),
        ((True, "42"), (False, 0)),
        ((True, _CustomIntSubclass(42)), (False, 0)),
        (None, (False, 0)),
        ("invalid", (False, 0)),
    ],
)
def test_default_detached_starter_contract(
    monkeypatch: pytest.MonkeyPatch,
    qprocess_result: Any,
    expected_result: tuple[bool, int],
) -> None:
    _qapp()
    from falafacil.homebrew_update import _default_detached_starter

    monkeypatch.setattr(QProcess, "startDetached", staticmethod(lambda prog, args: qprocess_result))
    assert _default_detached_starter("/usr/bin/echo", []) == expected_result


def test_default_detached_starter_handles_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    _qapp()
    from falafacil.homebrew_update import _default_detached_starter

    def failing_start(prog: str, args: list[str]) -> Any:
        raise RuntimeError("startDetached crashed")

    monkeypatch.setattr(QProcess, "startDetached", staticmethod(failing_start))
    assert _default_detached_starter("/usr/bin/echo", []) == (False, 0)


def test_controller_restart_fails_when_detached_starter_raises(tmp_path: Path) -> None:
    _qapp()
    installation = _make_valid_installation(tmp_path, version="0.2.0")

    processes: list[FakeProcess] = []
    new_installation_dto = HomebrewInstallation(
        version="0.3.0",
        formula=HOMEBREW_FORMULA,
        homebrew_prefix=installation.homebrew_prefix,
        brew_path=installation.brew_path,
        launch_path=installation.launch_path,
        marker_path=installation.marker_path,
    )

    def failing_detached_starter(program: str, arguments: list[str]) -> Any:
        raise RuntimeError("failed to launch detached process")

    controller = HomebrewUpdateController(
        installation,
        process_factory=lambda p: processes.append(FakeProcess(p)) or processes[-1],
        marker_loader=lambda path, expected_version=None: new_installation_dto,
        detached_starter=failing_detached_starter,
    )

    assert controller.install_latest() is True
    processes[0].finish(0, QProcess.ExitStatus.NormalExit)
    processes[1].feed_stdout(
        json.dumps({"formulae": [{"name": "falafacil", "pinned": False}]}).encode("utf-8")
    )
    processes[1].finish(0, QProcess.ExitStatus.NormalExit)
    processes[2].finish(0, QProcess.ExitStatus.NormalExit)
    processes[3].finish(0, QProcess.ExitStatus.NormalExit)

    assert controller.restart() is False


def test_controller_cleanup_and_race_safety(tmp_path: Path) -> None:
    _qapp()
    installation = _make_valid_installation(tmp_path)

    processes: list[FakeProcess] = []
    timers: list[FakeTimer] = []

    controller = HomebrewUpdateController(
        installation,
        process_factory=lambda p: processes.append(FakeProcess(p)) or processes[-1],
        timer_factory=lambda p: timers.append(FakeTimer(p)) or timers[-1],
    )

    failures: list[str] = []
    controller.failed.connect(failures.append)

    assert controller.install_latest() is True
    proc = processes[0]
    timer = timers[0]

    # Trigger error
    proc.fail_to_start()

    assert controller.running is False
    assert failures == [GENERIC_FAILURE_MESSAGE]
    assert timer.active is False
    assert proc.deleted is True

    # Late signals from old process or timer must be completely ignored
    proc.finish(0, QProcess.ExitStatus.NormalExit)
    proc.fail_to_start()
    timer.fire()

    assert failures == [GENERIC_FAILURE_MESSAGE]  # No additional emission
    assert controller.running is False


def test_controller_stale_signals_and_timers_during_successor_phases_and_new_run(
    tmp_path: Path,
) -> None:
    _qapp()
    installation = _make_valid_installation(tmp_path, version="0.2.0")

    processes: list[FakeProcess] = []
    timers: list[FakeTimer] = []

    new_installation_dto = HomebrewInstallation(
        version="0.3.0",
        formula=HOMEBREW_FORMULA,
        homebrew_prefix=installation.homebrew_prefix,
        brew_path=installation.brew_path,
        launch_path=installation.launch_path,
        marker_path=installation.marker_path,
    )

    controller = HomebrewUpdateController(
        installation,
        process_factory=lambda p: processes.append(FakeProcess(p)) or processes[-1],
        timer_factory=lambda p: timers.append(FakeTimer(p)) or timers[-1],
        marker_loader=lambda path, expected_version=None: new_installation_dto,
    )

    statuses: list[str] = []
    up_to_dates: list[str] = []
    ready_to_restarts: list[str] = []
    failures: list[str] = []

    controller.status_changed.connect(statuses.append)
    controller.up_to_date.connect(up_to_dates.append)
    controller.ready_to_restart.connect(ready_to_restarts.append)
    controller.failed.connect(failures.append)

    # Provoke an initial watchdog timeout to generate a real stale grace timer
    assert controller.install_latest() is True
    assert len(processes) == 1
    aborted_proc = processes[0]
    aborted_watchdog = timers[0]

    aborted_watchdog.fire()
    assert aborted_proc.terminated is True
    assert len(timers) == 2
    stale_grace_timer = timers[1]
    assert stale_grace_timer.interval_ms == KILL_GRACE_MS

    # Aborted process finishes during grace -> settles run with TIMEOUT_MESSAGE
    aborted_proc.finish(1, QProcess.ExitStatus.CrashExit)
    assert controller.running is False
    assert failures == [TIMEOUT_MESSAGE]

    # Reset signal collectors for the main multi-phase run
    statuses.clear()
    failures.clear()

    assert controller.install_latest() is True
    assert len(processes) == 2
    proc0 = processes[1]
    timer0 = timers[2]

    # Fire stale grace timer while in UPDATE phase
    stale_grace_timer.fire()
    assert proc0.killed is False
    assert proc0.terminated is False
    assert controller.running is True
    assert failures == []

    # Complete phase 0 (UPDATE) -> advances to phase 1 (OUTDATED)
    proc0.finish(0, QProcess.ExitStatus.NormalExit)
    assert len(processes) == 3
    proc1 = processes[2]
    timer1 = timers[3]

    # Inject stale signals from proc0, timer0, and stale_grace_timer while in OUTDATED phase
    proc0.finish(1, QProcess.ExitStatus.CrashExit)
    proc0.fail_to_start(QProcess.ProcessError.Crashed)
    proc0.emit_error_while_running(QProcess.ProcessError.ReadError)
    proc0.feed_stdout(b"stale update output\n")
    proc0.feed_stderr(b"stale update error\n")
    timer0.fire()
    stale_grace_timer.fire()

    # Controller must still be happily running OUTDATED phase
    assert controller.running is True
    assert failures == []
    assert proc1.started is True
    assert proc1.killed is False
    assert proc1.terminated is False

    # Feed valid outdated JSON to proc1 and finish phase 1 -> advances to phase 2 (UPGRADE)
    outdated_payload = json.dumps(
        {"formulae": [{"name": "falafacil", "pinned": False}]}
    ).encode("utf-8")
    proc1.feed_stdout(outdated_payload)
    proc1.finish(0, QProcess.ExitStatus.NormalExit)

    assert len(processes) == 4
    proc2 = processes[3]
    timer2 = timers[4]

    # Inject stale signals from proc0, proc1, timer0, timer1, and stale_grace_timer while in UPGRADE phase
    proc0.finish(0, QProcess.ExitStatus.NormalExit)
    proc1.finish(1, QProcess.ExitStatus.CrashExit)
    proc1.fail_to_start(QProcess.ProcessError.ReadError)
    proc1.feed_stdout(b"stale outdated output\n")
    timer0.fire()
    timer1.fire()
    stale_grace_timer.fire()

    # Controller must still be happily running UPGRADE phase
    assert controller.running is True
    assert failures == []
    assert proc2.started is True
    assert proc2.killed is False
    assert proc2.terminated is False

    # Complete phase 2 (UPGRADE) -> advances to phase 3 (PROBE)
    proc2.finish(0, QProcess.ExitStatus.NormalExit)

    assert len(processes) == 5
    proc3 = processes[4]
    timer3 = timers[5]

    # Inject stale signals from proc0..proc2, timer0..timer2, and stale_grace_timer while in PROBE phase
    proc0.finish(0, QProcess.ExitStatus.NormalExit)
    proc1.finish(0, QProcess.ExitStatus.NormalExit)
    proc2.finish(1, QProcess.ExitStatus.CrashExit)
    timer0.fire()
    timer1.fire()
    timer2.fire()
    stale_grace_timer.fire()

    # Controller must still be running PROBE phase
    assert controller.running is True
    assert failures == []
    assert proc3.started is True
    assert proc3.killed is False
    assert proc3.terminated is False

    # Complete phase 3 (PROBE) -> ready_to_restart
    proc3.finish(0, QProcess.ExitStatus.NormalExit)

    assert controller.running is False
    assert ready_to_restarts == [READY_TO_RESTART_MESSAGE]
    assert failures == []

    # Now start a completely NEW run of install_latest()
    statuses.clear()
    ready_to_restarts.clear()
    failures.clear()

    assert controller.install_latest() is True
    assert len(processes) == 6
    new_proc0 = processes[5]

    # Inject stale signals from previous processes, watchdogs, and stale grace timer
    proc0.finish(0, QProcess.ExitStatus.NormalExit)
    proc1.finish(0, QProcess.ExitStatus.NormalExit)
    proc2.finish(0, QProcess.ExitStatus.NormalExit)
    proc3.finish(0, QProcess.ExitStatus.NormalExit)
    timer0.fire()
    timer1.fire()
    timer2.fire()
    timer3.fire()
    stale_grace_timer.fire()

    assert controller.running is True
    assert failures == []
    assert new_proc0.killed is False
    assert new_proc0.terminated is False

    # Finish the new run to up_to_date
    new_proc0.finish(0, QProcess.ExitStatus.NormalExit)
    assert len(processes) == 7
    new_proc1 = processes[6]
    stale_grace_timer.fire()
    assert new_proc1.killed is False
    assert new_proc1.terminated is False
    assert controller.running is True

    new_proc1.feed_stdout(json.dumps({"formulae": []}).encode("utf-8"))
    new_proc1.finish(0, QProcess.ExitStatus.NormalExit)

    assert controller.running is False
    assert up_to_dates == [UP_TO_DATE_MESSAGE]
    assert failures == []

    # Firing stale grace timer when idle does nothing
    stale_grace_timer.fire()
    assert controller.running is False
    assert failures == []


def test_controller_stale_grace_timer_from_aborted_phase_does_not_mutate_successor_phase(
    tmp_path: Path,
) -> None:
    _qapp()
    installation = _make_valid_installation(tmp_path, version="0.2.0")

    processes: list[FakeProcess] = []
    timers: list[FakeTimer] = []

    controller = HomebrewUpdateController(
        installation,
        process_factory=lambda p: processes.append(FakeProcess(p)) or processes[-1],
        timer_factory=lambda p: timers.append(FakeTimer(p)) or timers[-1],
    )

    failures: list[str] = []
    up_to_dates: list[str] = []
    controller.failed.connect(failures.append)
    controller.up_to_date.connect(up_to_dates.append)

    # Run 1: Timeout on OUTDATED phase (creating a grace timer)
    assert controller.install_latest() is True
    processes[0].finish(0, QProcess.ExitStatus.NormalExit)  # UPDATE finishes -> OUTDATED starts
    assert len(processes) == 2
    outdated_proc = processes[1]
    outdated_watchdog = timers[1]

    outdated_watchdog.fire()  # OUTDATED watchdog fires -> grace timer started
    assert outdated_proc.terminated is True
    assert len(timers) == 3
    outdated_grace = timers[2]
    assert outdated_grace.interval_ms == KILL_GRACE_MS

    outdated_proc.finish(1, QProcess.ExitStatus.CrashExit)
    assert controller.running is False
    assert failures == [TIMEOUT_MESSAGE]
    failures.clear()

    # Run 2: Start new execution, test outdated_grace during UPDATE and OUTDATED
    assert controller.install_latest() is True
    assert len(processes) == 3
    run2_update_proc = processes[2]

    # Fire outdated_grace during Run 2 UPDATE
    outdated_grace.fire()
    assert run2_update_proc.killed is False
    assert run2_update_proc.terminated is False
    assert controller.running is True
    assert failures == []

    run2_update_proc.finish(0, QProcess.ExitStatus.NormalExit)
    assert len(processes) == 4
    run2_outdated_proc = processes[3]

    # Fire outdated_grace during Run 2 OUTDATED
    outdated_grace.fire()
    assert run2_outdated_proc.killed is False
    assert run2_outdated_proc.terminated is False
    assert controller.running is True
    assert failures == []

    # Finish Run 2 successfully with up_to_date
    run2_outdated_proc.feed_stdout(json.dumps({"formulae": []}).encode("utf-8"))
    run2_outdated_proc.finish(0, QProcess.ExitStatus.NormalExit)

    assert controller.running is False
    assert up_to_dates == [UP_TO_DATE_MESSAGE]
    assert failures == []
