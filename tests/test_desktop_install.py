"""Tests for user desktop entry installation."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from falafacil import __version__, path_security
from falafacil.desktop_install import (
    DesktopInstallError,
    desktop_escape,
    generic_escape,
    install_user_desktop_entry,
)
from falafacil.homebrew_update import (
    HOMEBREW_CHANNEL,
    HOMEBREW_FORMULA,
    HOMEBREW_SCHEMA_VERSION,
)


def _decode_generic_string(value: str) -> str:
    """Generic Desktop Entry value decoding for scalar string."""
    decoded: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character != "\\":
            decoded.append(character)
            index += 1
            continue
        assert index + 1 < len(value), "Dangling backslash at end of generic desktop entry string"
        escaped = value[index + 1]
        if escaped == "\\":
            decoded.append("\\")
        elif escaped == "s":
            decoded.append(" ")
        elif escaped == "n":
            decoded.append("\n")
        elif escaped == "t":
            decoded.append("\t")
        elif escaped == "r":
            decoded.append("\r")
        elif escaped == ";":
            decoded.append(";")
        else:
            raise AssertionError(f"Invalid generic desktop entry escape sequence '\\{escaped}'.")
        index += 2
    return "".join(decoded)

def _decode_exec_quoted_argument(value: str) -> str:
    """Two-stage decoding for quoted Exec command line argument according to spec."""
    assert value.startswith('"') and value.endswith('"'), f"Exec argument must be quoted: {value}"
    inner = value[1:-1]
    # Stage 1: Generic unescaping
    generic_str = _decode_generic_string(inner)

    # Stage 2: Exec quote unescaping inside double quotes (\" -> ", \` -> `, \$ -> $, \\ -> \)
    exec_decoded: list[str] = []
    i = 0
    while i < len(generic_str):
        if generic_str[i] == "\\":
            assert i + 1 < len(generic_str), "Dangling backslash in Exec argument"
            esc = generic_str[i + 1]
            assert esc in {'"', "`", "$", "\\"}, f"Invalid Exec escape '\\{esc}'"
            exec_decoded.append(esc)
            i += 2
        else:
            exec_decoded.append(generic_str[i])
            i += 1
    return "".join(exec_decoded)

def _create_developer_bin(home: Path) -> Path:
    dev_bin = home / ".local" / "bin" / "falafacil"
    for parent in (home, home / ".local", home / ".local" / "bin"):
        parent.mkdir(parents=True, exist_ok=True)
        parent.chmod(0o755)
    dev_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    dev_bin.chmod(0o755)
    return dev_bin


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


def _create_valid_homebrew_tree(
    root: Path,
    version: str = __version__,
) -> tuple[Path, Path, Path]:
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

    cellar_bin_exec = opt_bin_dir / "falafacil"
    cellar_bin_exec.symlink_to(Path("..") / "libexec" / "falafacil")

    launch_symlink = bin_dir / "falafacil"
    launch_symlink.symlink_to(Path("..") / "opt" / "falafacil" / "bin" / "falafacil")

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

    return prefix, prefix / "opt" / "falafacil" / "bin" / "falafacil", marker_file


def test_developer_install_success_and_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    dev_bin = _create_developer_bin(home)
    desktop_path = install_user_desktop_entry(dev_bin)
    assert desktop_path == home / ".local" / "share" / "applications" / "falafacil.desktop"
    assert desktop_path.is_file()
    assert stat.S_IMODE(desktop_path.stat().st_mode) == 0o644

    content = desktop_path.read_text(encoding="utf-8")
    assert f'Exec="{dev_bin}"' in content
    assert f"TryExec={dev_bin}" in content
    assert "Type=Application" in content
    assert "Name=FalaFácil" in content
    assert "Comment=Transcrição de voz em português com Gemini" in content
    assert "Terminal=false" in content
    assert "Categories=Utility;AudioVideo;" in content
    for secret in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "api_key", "$HOME", "~", "sh -c"):
        assert secret not in content


def test_homebrew_install_success_and_preserves_stable_launch_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))

    prefix, launch_path, _marker = _create_valid_homebrew_tree(tmp_path)
    desktop_path = install_user_desktop_entry(launch_path)
    assert desktop_path == home / ".local" / "share" / "applications" / "falafacil.desktop"
    assert desktop_path.is_file()
    assert stat.S_IMODE(desktop_path.stat().st_mode) == 0o644

    content = desktop_path.read_text(encoding="utf-8")
    assert f'Exec="{launch_path}"' in content
    assert f"TryExec={launch_path}" in content
    assert "Categories=Utility;AudioVideo;" in content


def test_homebrew_install_under_umask_002_registers_desktop_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a Homebrew prefix with 0o775 dirs and a 0o664 marker must register."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))

    prefix, launch_path, marker = _create_valid_homebrew_tree(tmp_path)
    version = __version__
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
    marker.chmod(0o664)
    _force_private_group(monkeypatch)

    desktop_path = install_user_desktop_entry(launch_path)

    assert desktop_path.is_file()
    assert f'Exec="{launch_path}"' in desktop_path.read_text(encoding="utf-8")


def test_atomic_replacement_and_reinstall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    dev_bin = _create_developer_bin(home)
    p1 = install_user_desktop_entry(dev_bin)
    assert p1.is_file()

    # Second install overwrites atomically
    p2 = install_user_desktop_entry(dev_bin)
    assert p2 == p1
    assert stat.S_IMODE(p2.stat().st_mode) == 0o644


def test_escaping_of_complex_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home with spaces;and\\slashes\"`$"
    monkeypatch.setenv("HOME", str(home))

    dev_bin = _create_developer_bin(home)
    desktop_path = install_user_desktop_entry(dev_bin)
    content = desktop_path.read_text(encoding="utf-8")

    exec_line = next(line for line in content.splitlines() if line.startswith("Exec="))
    tryexec_line = next(line for line in content.splitlines() if line.startswith("TryExec="))

    exec_val = exec_line.removeprefix("Exec=")
    tryexec_val = tryexec_line.removeprefix("TryExec=")

    # Normative raw contents checks:
    # Raw backslash becomes \\\\\\\\ in Exec, \\\\ in TryExec
    assert "\\\\\\\\" in exec_val
    assert "\\\\" in tryexec_val
    # Raw quote becomes \\" (2 backslashes before quote) in Exec, literal " in TryExec
    assert '\\\\"' in exec_val
    assert '"' in tryexec_val
    # Raw backtick becomes \\` (2 backslashes before backtick) in Exec, literal ` in TryExec
    assert "\\\\`" in exec_val
    assert "`" in tryexec_val
    # Raw dollar becomes \\\\$ in Exec, literal $ in TryExec
    assert "\\\\$" in exec_val
    assert "$" in tryexec_val
    # Semicolon is literal in both Exec and TryExec (NOT escaped as \\;)
    assert '\\;' not in content
    assert ";" in exec_val
    assert ";" in tryexec_val

    assert _decode_exec_quoted_argument(exec_val) == str(dev_bin)
    assert _decode_generic_string(tryexec_val) == str(dev_bin)


def test_rejects_relative_path() -> None:
    with pytest.raises(DesktopInstallError, match="deve ser absoluto"):
        install_user_desktop_entry(Path("relative/falafacil"))


@pytest.mark.parametrize(
    "bad_char",
    ["\n", "\r", "\t", "%", "=", "\u00e1", "\u00e7", "\u00f5", "\u20ac", "\u00ff"],
    ids=["newline", "cr", "tab", "percent", "equal", "a-acute", "c-cedilla", "o-tilde", "euro", "y-umlaut"],
)
def test_rejects_invalid_path_characters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_char: str,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    bad_path = Path(f"/opt/falafacil{bad_char}/bin/falafacil")
    with pytest.raises(DesktopInstallError, match="contém caractere"):
        install_user_desktop_entry(bad_path)
    assert not (home / ".local" / "share" / "applications" / "falafacil.desktop").exists()


def test_rejects_non_ascii_executable_path_and_preserves_existing_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    dev_bin = _create_developer_bin(home)
    apps_dir = home / ".local" / "share" / "applications"
    apps_dir.mkdir(parents=True, exist_ok=True)
    for d in (home / ".local" / "share", apps_dir):
        d.chmod(0o755)
    desktop_file = apps_dir / "falafacil.desktop"
    desktop_file.write_text("existing-valid-content", encoding="utf-8")
    desktop_file.chmod(0o644)

    bad_bin = home / ".local" / "bin" / "falaf\u00e1cil"
    with pytest.raises(DesktopInstallError, match="contém caractere não-ASCII"):
        install_user_desktop_entry(bad_bin)
    assert desktop_file.read_text(encoding="utf-8") == "existing-valid-content"


def test_rejects_non_ascii_home_path_and_performs_no_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home_usu\u00e1rio"
    monkeypatch.setenv("HOME", str(home))

    dev_bin = _create_developer_bin(home)
    with pytest.raises(DesktopInstallError, match="contém caractere não-ASCII"):
        install_user_desktop_entry(dev_bin)

    assert not (home / ".local" / "share" / "applications" / "falafacil.desktop").exists()
def test_rejects_developer_executable_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    real_bin = tmp_path / "real_falafacil"
    real_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real_bin.chmod(0o755)
    dev_bin = _create_developer_bin(home)
    dev_bin.unlink()
    dev_bin.symlink_to(real_bin)

    with pytest.raises(DesktopInstallError, match="não pode ser um symlink"):
        install_user_desktop_entry(dev_bin)

def test_rejects_developer_executable_not_regular_or_not_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    dev_bin = _create_developer_bin(home)
    dev_bin.chmod(0o644)  # not executable

    with pytest.raises(DesktopInstallError, match="não possui permissão de execução"):
        install_user_desktop_entry(dev_bin)


def test_rejects_developer_executable_shared_group_writable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    dev_bin = _create_developer_bin(home)
    dev_bin.chmod(0o775)  # group writable
    _force_shared_group(monkeypatch)

    with pytest.raises(DesktopInstallError, match="permissões de escrita para grupo/outros"):
        install_user_desktop_entry(dev_bin)


def test_accepts_developer_tree_created_under_umask_002(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A developer install whose binary and parents are 0o775 stays valid on a private group."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    dev_bin = _create_developer_bin(home)
    dev_bin.chmod(0o775)
    (home / ".local").chmod(0o775)
    (home / ".local" / "bin").chmod(0o775)
    _force_private_group(monkeypatch)

    desktop_entry_path = install_user_desktop_entry(dev_bin)

    assert desktop_entry_path.is_file()
    assert f'Exec="{dev_bin}"' in desktop_entry_path.read_text(encoding="utf-8")


def test_rejects_arbitrary_non_canonical_non_homebrew_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    other_bin = tmp_path / "other" / "falafacil"
    (tmp_path / "other").mkdir(parents=True, exist_ok=True)
    (tmp_path / "other").chmod(0o755)
    other_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    other_bin.chmod(0o755)

    with pytest.raises(DesktopInstallError, match="não possui marker Homebrew adjacente"):
        install_user_desktop_entry(other_bin)


def test_rejects_unsafe_existing_destination_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    dev_bin = _create_developer_bin(home)
    apps_dir = home / ".local" / "share" / "applications"
    apps_dir.mkdir(parents=True, exist_ok=True)
    for d in (home / ".local" / "share", apps_dir):
        d.chmod(0o755)
    target = tmp_path / "some_target"
    target.write_text("dummy", encoding="utf-8")
    (apps_dir / "falafacil.desktop").symlink_to(target)

    with pytest.raises(DesktopInstallError, match="não pode ser um symlink"):
        install_user_desktop_entry(dev_bin)


def test_rejects_unsafe_existing_destination_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    dev_bin = _create_developer_bin(home)
    apps_dir = home / ".local" / "share" / "applications"
    (apps_dir / "falafacil.desktop").mkdir(parents=True, exist_ok=True)
    for d in (home / ".local" / "share", apps_dir, apps_dir / "falafacil.desktop"):
        d.chmod(0o755)
    with pytest.raises(DesktopInstallError, match="deve ser um arquivo regular"):
        install_user_desktop_entry(dev_bin)

def test_atomic_replacement_avoids_post_rename_pathname_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    dev_bin = _create_developer_bin(home)
    apps_dir = home / ".local" / "share" / "applications"
    apps_dir.mkdir(parents=True, exist_ok=True)
    for d in (home / ".local" / "share", apps_dir):
        d.chmod(0o755)
    desktop_entry = apps_dir / "falafacil.desktop"
    sensitive_target = tmp_path / "sensitive_file"
    sensitive_target.write_text("sensitive", encoding="utf-8")
    sensitive_target.chmod(0o700)

    chmod_calls: list[tuple[Any, ...]] = []
    orig_chmod = os.chmod
    orig_replace = os.replace

    def fake_replace(src, dst):
        orig_replace(src, dst)
        # Simulate a race where dst is swapped for a symlink to sensitive target right after replace
        os.unlink(dst)
        os.symlink(str(sensitive_target), str(dst))

    def tracking_chmod(path_arg, mode, *args, **kwargs):
        chmod_calls.append((path_arg, mode))
        return orig_chmod(path_arg, mode, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fake_replace)
    monkeypatch.setattr(os, "chmod", tracking_chmod)

    install_user_desktop_entry(dev_bin)

    # Confirm no os.chmod was called on the desktop entry path after replace
    assert not any(call[0] == desktop_entry or call[0] == str(desktop_entry) for call in chmod_calls)
    # Confirm sensitive target mode was never modified
    assert stat.S_IMODE(sensitive_target.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "target_dir_fn",
    [
        lambda home: home / ".local",
        lambda home: home / ".local" / "bin",
        lambda home: home / ".local" / "share",
        lambda home: home / ".local" / "share" / "applications",
    ],
    ids=["dot_local", "dot_local_bin", "dot_local_share", "applications"],
)
def test_rejects_user_directory_wrong_uid_via_stat_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_dir_fn: Any,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    dev_bin = _create_developer_bin(home)
    apps_dir = home / ".local" / "share" / "applications"
    apps_dir.mkdir(parents=True, exist_ok=True)
    for d in (home / ".local" / "share", apps_dir):
        d.chmod(0o755)
    target_dir = target_dir_fn(home)
    target_dir_str = str(target_dir)
    target_dir_resolved_str = str(target_dir.resolve())
    fake_uid = os.getuid() + 999
    orig_lstat = os.lstat
    orig_stat = os.stat

    def fake_lstat(path_val, *args, **kwargs):
        st = orig_lstat(path_val, *args, **kwargs)
        p_str = str(path_val)
        if p_str in (target_dir_str, target_dir_resolved_str) or os.path.abspath(p_str) in (target_dir_str, target_dir_resolved_str):
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
        if p_str in (target_dir_str, target_dir_resolved_str) or os.path.abspath(p_str) in (target_dir_str, target_dir_resolved_str):
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
    with pytest.raises(DesktopInstallError, match="pertence a outro usuário"):
        install_user_desktop_entry(dev_bin)


def test_rejects_developer_executable_wrong_uid_via_stat_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    dev_bin = _create_developer_bin(home)
    fake_uid = os.getuid() + 999
    orig_stat = os.stat
    orig_lstat = os.lstat
    dev_bin_str = str(dev_bin)
    dev_bin_resolved_str = str(dev_bin.resolve())

    def fake_stat(path_val, *args, **kwargs):
        st = orig_stat(path_val, *args, **kwargs)
        p_str = str(path_val)
        if p_str in (dev_bin_str, dev_bin_resolved_str) or os.path.abspath(p_str) in (dev_bin_str, dev_bin_resolved_str):
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

    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setattr(os, "lstat", fake_stat)
    with pytest.raises(DesktopInstallError, match="não pertence ao usuário atual"):
        install_user_desktop_entry(dev_bin)


def test_rejects_existing_destination_wrong_uid_via_stat_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    dev_bin = _create_developer_bin(home)
    apps_dir = home / ".local" / "share" / "applications"
    apps_dir.mkdir(parents=True, exist_ok=True)
    for d in (home / ".local" / "share", apps_dir):
        d.chmod(0o755)
    desktop_file = apps_dir / "falafacil.desktop"
    desktop_file.write_text("existing", encoding="utf-8")
    desktop_file.chmod(0o644)
    fake_uid = os.getuid() + 999
    orig_stat = os.stat
    orig_lstat = os.lstat
    desktop_file_str = str(desktop_file)
    desktop_file_resolved_str = str(desktop_file.resolve())

    def fake_stat(path_val, *args, **kwargs):
        st = orig_stat(path_val, *args, **kwargs)
        p_str = str(path_val)
        if p_str in (desktop_file_str, desktop_file_resolved_str) or os.path.abspath(p_str) in (desktop_file_str, desktop_file_resolved_str):
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

    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setattr(os, "lstat", fake_stat)
    with pytest.raises(DesktopInstallError, match="pertence a outro usuário"):
        install_user_desktop_entry(dev_bin)


def test_rejects_applications_directory_world_writable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    dev_bin = home / ".local" / "bin" / "falafacil"
    dev_bin.parent.mkdir(parents=True)
    dev_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    dev_bin.chmod(0o755)

    apps_dir = home / ".local" / "share" / "applications"
    apps_dir.mkdir(parents=True)
    apps_dir.chmod(0o777)

    with pytest.raises(DesktopInstallError, match="possui permissões de escrita para grupo/outros"):
        install_user_desktop_entry(dev_bin)
    assert not (apps_dir / "falafacil.desktop").exists()


@pytest.mark.parametrize(
    "target_dir_fn",
    [
        lambda home: home / ".local",
        lambda home: home / ".local" / "bin",
        lambda home: home / ".local" / "share",
        lambda home: home / ".local" / "share" / "applications",
    ],
    ids=["dot_local", "dot_local_bin", "dot_local_share", "applications"],
)
def test_rejects_user_directory_shared_group_writable_across_all_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_dir_fn: Any,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    dev_bin = home / ".local" / "bin" / "falafacil"
    dev_bin.parent.mkdir(parents=True)
    dev_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    dev_bin.chmod(0o755)

    apps_dir = home / ".local" / "share" / "applications"
    apps_dir.mkdir(parents=True)

    target_dir = target_dir_fn(home)
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

    with pytest.raises(DesktopInstallError, match="possui permissões de escrita para grupo/outros"):
        install_user_desktop_entry(dev_bin)
    assert not (apps_dir / "falafacil.desktop").exists()

def test_rejects_existing_destination_group_writable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    dev_bin = home / ".local" / "bin" / "falafacil"
    dev_bin.parent.mkdir(parents=True)
    dev_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    dev_bin.chmod(0o755)

    apps_dir = home / ".local" / "share" / "applications"
    apps_dir.mkdir(parents=True)
    desktop_file = apps_dir / "falafacil.desktop"
    desktop_file.write_text("existing", encoding="utf-8")
    desktop_file.chmod(0o666)  # group + other writable

    with pytest.raises(DesktopInstallError, match="permissões de escrita para grupo/outros"):
        install_user_desktop_entry(dev_bin)
