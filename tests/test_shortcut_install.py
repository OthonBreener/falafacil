from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
from typing import Any

from PySide6.QtCore import QObject, QProcess, Signal
from PySide6.QtWidgets import QApplication

from falafacil.shortcut_install import (
    SERVICE_EXECUTABLE,
    SERVICE_UNIT,
    SERVICE_UNIT_PATH,
    SOCKET_UNIT,
    SOCKET_UNIT_PATH,
    ShortcutServiceInstaller,
    install_privileged_service,
)
from falafacil.shortcuts import (
    AUTHORIZATION_CANCELLED_MESSAGE,
    SOURCE_INSTALL_UNAVAILABLE_MESSAGE,
)


class FakeProcess(QObject):
    finished = Signal(int, QProcess.ExitStatus)
    errorOccurred = Signal(QProcess.ProcessError)

    def __init__(self, _parent: QObject | None = None, *, wait_result: bool = True) -> None:
        super().__init__(_parent)
        self.environment = None
        self.program: str | None = None
        self.arguments: list[str] = []
        self.started = False
        self.terminated = False
        self.killed = False
        self.wait_result = wait_result

    def setProcessEnvironment(self, environment) -> None:
        self.environment = environment

    def setProgram(self, program: str) -> None:
        self.program = program

    def setArguments(self, arguments: list[str]) -> None:
        self.arguments = list(arguments)

    def start(self) -> None:
        self.started = True

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def waitForFinished(self, timeout: int) -> bool:
        assert timeout == 1000
        return self.wait_result

    def state(self) -> QProcess.ProcessState:
        return QProcess.ProcessState.NotRunning


def _qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_ui_installer_uses_pkexec_without_shell_and_minimal_environment(
    tmp_path: Path, monkeypatch
) -> None:
    _qapp()
    bundle = tmp_path / "falafacil"
    bundle.write_bytes(b"bundle")
    bundle.chmod(0o755)
    process = FakeProcess()
    monkeypatch.setattr("falafacil.shortcut_install.shutil.which", lambda *_a, **_k: "/usr/bin/pkexec")
    monkeypatch.setenv("GEMINI_API_KEY", "secret-one")
    monkeypatch.setenv("GOOGLE_API_KEY", "secret-two")
    monkeypatch.setenv("GEMINI_MODEL", "secret-model")
    monkeypatch.setenv("UNRELATED_SECRET", "secret-three")
    installer = ShortcutServiceInstaller(
        process_factory=lambda _parent: process,
        bundle_resolver=lambda: bundle,
    )

    assert installer.install() is True
    assert process.program == "pkexec"
    assert process.arguments == [str(bundle), "--install-shortcut-service"]
    assert process.started is True
    keys = set(process.environment.keys())
    assert "PATH" in keys
    assert "LANG" in keys
    assert not keys.intersection(
        {"GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_MODEL", "UNRELATED_SECRET"}
    )


def test_ui_installer_reports_source_mode_without_installed_bundle() -> None:
    _qapp()
    results: list[tuple[bool, str]] = []
    installer = ShortcutServiceInstaller(bundle_resolver=lambda: None)
    installer.finished.connect(lambda ok, message: results.append((ok, message)))
    assert installer.install() is False
    assert results == [(False, SOURCE_INSTALL_UNAVAILABLE_MESSAGE)]


def test_ui_installer_reports_success_cancel_and_forces_slow_process(
    tmp_path: Path, monkeypatch
) -> None:
    _qapp()
    bundle = tmp_path / "falafacil"
    bundle.write_bytes(b"bundle")
    bundle.chmod(0o755)
    monkeypatch.setattr("falafacil.shortcut_install.shutil.which", lambda *_a, **_k: "/usr/bin/pkexec")

    success_process = FakeProcess()
    success_results: list[tuple[bool, str]] = []
    success = ShortcutServiceInstaller(
        process_factory=lambda _parent: success_process,
        bundle_resolver=lambda: bundle,
    )
    success.finished.connect(lambda ok, message: success_results.append((ok, message)))
    success.install()
    success_process.finished.emit(0, QProcess.ExitStatus.NormalExit)
    assert success_results == [(True, "")]

    slow_process = FakeProcess(wait_result=False)
    cancel_results: list[tuple[bool, str]] = []
    cancelled = ShortcutServiceInstaller(
        process_factory=lambda _parent: slow_process,
        bundle_resolver=lambda: bundle,
    )
    cancelled.finished.connect(lambda ok, message: cancel_results.append((ok, message)))
    cancelled.install()
    cancelled.cancel()
    assert slow_process.terminated is True
    assert slow_process.killed is True
    assert cancel_results == [(False, AUTHORIZATION_CANCELLED_MESSAGE)]


def _prepare_privileged_install(tmp_path: Path, monkeypatch) -> Path:
    source = tmp_path / "source-bundle"
    source.write_bytes(b"executable-bundle")
    source.chmod(0o755)
    monkeypatch.setattr("falafacil.shortcut_install.os.geteuid", lambda: 0)
    monkeypatch.setattr("falafacil.shortcut_install.pwd.getpwuid", lambda uid: object())
    monkeypatch.setattr("falafacil.shortcut_install._resolve_self_executable", lambda: source)
    monkeypatch.setattr("falafacil.shortcut_install.os.fchown", lambda *_a, **_k: None)
    monkeypatch.setattr("falafacil.shortcut_install.os.chown", lambda *_a, **_k: None)
    monkeypatch.setenv("PKEXEC_UID", "1000")
    return source


def test_privileged_installer_writes_fixed_atomic_bundle_units_and_systemctl(
    tmp_path: Path, monkeypatch
) -> None:
    _prepare_privileged_install(tmp_path, monkeypatch)
    root = tmp_path / "root"
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(command: list[str], **kwargs: Any) -> object:
        calls.append((command, kwargs))
        return object()

    assert install_privileged_service(root=root, command_runner=runner) == 0
    executable = root.joinpath(*SERVICE_EXECUTABLE.parts[1:])
    socket_unit = root.joinpath(*SOCKET_UNIT_PATH.parts[1:])
    service_unit = root.joinpath(*SERVICE_UNIT_PATH.parts[1:])
    assert executable.read_bytes() == b"executable-bundle"
    assert socket_unit.read_text() == SOCKET_UNIT
    assert service_unit.read_text() == SERVICE_UNIT
    assert stat.S_IMODE(executable.stat().st_mode) == 0o755
    assert stat.S_IMODE(socket_unit.stat().st_mode) == 0o644
    assert stat.S_IMODE(service_unit.stat().st_mode) == 0o644
    assert len(calls) == 3
    assert calls[0][0] == ["systemctl", "daemon-reload"]
    assert calls[1][0] == [
        "systemctl",
        "enable",
        "--now",
        "falafacil-shortcutd@1000.socket",
    ]
    assert calls[2][0] == [
        "systemctl",
        "stop",
        "falafacil-shortcutd@1000.service",
    ]
    assert all(
        call[1]["env"] == {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
        and call[1]["check"] is True
        and call[1]["stdin"] == subprocess.DEVNULL
        and call[1]["stdout"] == subprocess.DEVNULL
        and call[1]["stderr"] == subprocess.DEVNULL
        for call in calls
    )
    assert "SocketMode=0600" in SOCKET_UNIT
    assert "SocketUser=%i" in SOCKET_UNIT
    assert "Accept=no" in SOCKET_UNIT
    assert "ExecStart=/usr/local/libexec/falafacil-shortcutd --shortcut-daemon" in SERVICE_UNIT
    assert "StandardInput=socket" not in SERVICE_UNIT
    service_lines = set(SERVICE_UNIT.splitlines())
    mandatory_hardening = {
        "DynamicUser=yes",
        "SupplementaryGroups=input",
        "DevicePolicy=closed",
        "DeviceAllow=char-input r",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "RestrictAddressFamilies=AF_UNIX",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "SystemCallFilter=@system-service",
    }
    assert mandatory_hardening.issubset(service_lines)
    assert "DeviceAllow=char-input r" in service_lines
    assert "DeviceAllow=char-input rw" not in service_lines


def test_privileged_installer_rejects_non_root_invalid_uid_and_root_uid(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("falafacil.shortcut_install.os.geteuid", lambda: 1000)
    assert install_privileged_service(root=tmp_path, command_runner=lambda *_a, **_k: None) == 1
    monkeypatch.setattr("falafacil.shortcut_install.os.geteuid", lambda: 0)
    for uid in ("", "abc", "0"):
        monkeypatch.setenv("PKEXEC_UID", uid)
        assert install_privileged_service(root=tmp_path, command_runner=lambda *_a, **_k: None) == 1


def test_privileged_installer_rejects_symlink_destination(
    tmp_path: Path, monkeypatch
) -> None:
    _prepare_privileged_install(tmp_path, monkeypatch)
    root = tmp_path / "root"
    destination = root.joinpath(*SERVICE_EXECUTABLE.parts[1:])
    destination.parent.mkdir(parents=True)
    target = tmp_path / "target"
    target.write_bytes(b"do-not-overwrite")
    destination.symlink_to(target)
    called: list[bool] = []

    assert install_privileged_service(
        root=root, command_runner=lambda *_a, **_k: called.append(True)
    ) == 1
    assert target.read_bytes() == b"do-not-overwrite"
    assert called == []
