from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_desktop.sh"


def run_installer(home: Path, source: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    return subprocess.run(
        ["sh", str(INSTALLER), str(source)],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_installer_copies_executable_and_writes_safe_desktop_entry(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source = tmp_path / "fake-launcher"
    source.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    source.chmod(0o755)

    result = run_installer(home, source)

    assert result.returncode == 0, result.stderr
    installed = home / ".local" / "bin" / "falafacil"
    desktop = home / ".local" / "share" / "applications" / "falafacil.desktop"
    assert installed.is_file()
    assert stat.S_IMODE(installed.stat().st_mode) == 0o755
    assert installed.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")

    content = desktop.read_text(encoding="utf-8")
    expected_path = str(installed)
    assert f'Exec="{expected_path}"' in content
    assert f"TryExec={expected_path}" in content
    assert "Terminal=false" in content
    assert "Categories=Utility;AudioVideo;" in content
    assert "Type=Application" in content
    assert "Name=FalaFácil" in content
    assert "$HOME" not in content
    assert "~" not in content
    assert "sh -c" not in content
    assert "Environment=" not in content
    for marker in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "API_KEY", "api_key"):
        assert marker not in content


def _decode_generic_string(value: str) -> str:
    decoded: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character != "\\":
            decoded.append(character)
            index += 1
            continue
        assert index + 1 < len(value)
        escaped = value[index + 1]
        assert escaped in {"\\", ";"}
        decoded.append(escaped)
        index += 2
    return "".join(decoded)


def _decode_exec_quoted_argument(value: str) -> str:
    value = _decode_generic_string(value)
    assert value.startswith('"') and value.endswith('"')
    decoded: list[str] = []
    index = 1
    while index < len(value) - 1:
        character = value[index]
        if character != "\\":
            decoded.append(character)
            index += 1
            continue
        assert index + 1 < len(value) - 1
        escaped = value[index + 1]
        assert escaped in {'"', "\\", "`", "$"}
        decoded.append(escaped)
        index += 2
    return "".join(decoded)


def test_installer_escapes_generic_tryexec_path(tmp_path) -> None:
    home = tmp_path / "home with spaces;and\\slashes"
    home.mkdir()
    source = tmp_path / "fake-launcher"
    source.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    source.chmod(0o755)

    result = run_installer(home, source)

    assert result.returncode == 0, result.stderr
    installed = home / ".local" / "bin" / "falafacil"
    desktop = home / ".local" / "share" / "applications" / "falafacil.desktop"
    content = desktop.read_text(encoding="utf-8")
    exec_value = next(
        line.removeprefix("Exec=")
        for line in content.splitlines()
        if line.startswith("Exec=")
    )
    assert _decode_exec_quoted_argument(exec_value) == str(installed)

    try_exec = next(
        line.removeprefix("TryExec=")
        for line in content.splitlines()
        if line.startswith("TryExec=")
    )

    assert '"' not in try_exec
    assert "\\;" in try_exec
    assert "\\\\" in try_exec
    assert _decode_generic_string(try_exec) == str(installed)
    assert installed.is_file()

@pytest.mark.parametrize("control", ["\t", "\r"], ids=["tab", "carriage-return"])
def test_installer_rejects_unsafe_home_control_character(tmp_path, control: str) -> None:
    home = tmp_path / f"home{control}unsafe"
    home.mkdir()
    source = tmp_path / "fake-launcher"
    source.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    source.chmod(0o755)

    result = run_installer(home, source)

    assert result.returncode != 0
    assert not (home / ".local").exists()



@pytest.mark.parametrize("destination_kind", ["directory", "symlink"])
def test_installer_rejects_unsafe_existing_destination(tmp_path, destination_kind: str) -> None:
    home = tmp_path / "home"
    install_dir = home / ".local" / "bin"
    install_dir.mkdir(parents=True)
    destination = install_dir / "falafacil"
    if destination_kind == "directory":
        destination.mkdir()
    else:
        target = tmp_path / "existing-target"
        target.write_text("existing", encoding="utf-8")
        destination.symlink_to(target)

    source = tmp_path / "fake-launcher"
    source.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    source.chmod(0o755)

    result = run_installer(home, source)

    assert result.returncode != 0
    assert destination.exists() or destination.is_symlink()
    assert not (home / ".local" / "share" / "applications" / "falafacil.desktop").exists()


def test_module_dispatches_only_exact_internal_shortcut_modes(monkeypatch) -> None:
    from falafacil import __main__ as module_entry

    daemon_module = ModuleType("falafacil.shortcut_service")
    daemon_module.main = lambda: 21
    installer_module = ModuleType("falafacil.shortcut_install")
    installer_module.install_privileged_service = lambda: 22
    app_module = ModuleType("falafacil.app")
    app_module.main = lambda: 23
    monkeypatch.setitem(sys.modules, "falafacil.shortcut_service", daemon_module)
    monkeypatch.setitem(sys.modules, "falafacil.shortcut_install", installer_module)
    monkeypatch.setitem(sys.modules, "falafacil.app", app_module)

    monkeypatch.setattr(sys, "argv", ["falafacil", "--shortcut-daemon"])
    assert module_entry.main() == 21
    monkeypatch.setattr(sys, "argv", ["falafacil", "--install-shortcut-service"])
    assert module_entry.main() == 22
    monkeypatch.setattr(sys, "argv", ["falafacil", "--shortcut-daemon", "extra"])
    assert module_entry.main() == 23
    monkeypatch.setattr(sys, "argv", ["falafacil", "--unknown-internal-mode"])
    assert module_entry.main() == 23


def test_pyproject_requires_evdev_two_point_zero_floor() -> None:
    pyproject_path = ROOT / "pyproject.toml"
    with pyproject_path.open("rb") as fp:
        data = tomllib.load(fp)

    dependencies: list[str] = data.get("project", {}).get("dependencies", [])
    evdev_spec = next((dep for dep in dependencies if dep.startswith("evdev")), None)
    assert evdev_spec is not None, "evdev dependency not found in pyproject.toml"

    match = re.match(r"^evdev\s*>=\s*([0-9]+(?:\.[0-9]+)*)", evdev_spec)
    assert match is not None, f"Unexpected evdev spec format: {evdev_spec}"
    floor_version_tuple = tuple(int(part) for part in match.group(1).split("."))
    while len(floor_version_tuple) < 3:
        floor_version_tuple = (*floor_version_tuple, 0)

    assert floor_version_tuple >= (2, 0, 0), (
        f"Declared evdev minimum floor '{evdev_spec}' is below 2.0.0; "
        "InputDeviceMonitor requires evdev>=2.0.0 for list_devices(writable=False)."
    )
    assert evdev_spec == "evdev>=2.0.0"
