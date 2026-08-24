from __future__ import annotations

import locale
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Callable

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

from .shortcuts import (
    AUTHORIZATION_CANCELLED_MESSAGE,
    BACKEND_FAILURE_MESSAGE,
    SOURCE_INSTALL_UNAVAILABLE_MESSAGE,
)

SERVICE_EXECUTABLE = Path("/usr/local/libexec/falafacil-shortcutd")
SOCKET_UNIT_PATH = Path("/etc/systemd/system/falafacil-shortcutd@.socket")
SERVICE_UNIT_PATH = Path("/etc/systemd/system/falafacil-shortcutd@.service")

SOCKET_UNIT = """[Unit]
Description=FalaFácil global shortcut socket for UID %i

[Socket]
ListenStream=/run/falafacil-shortcutd-%i.sock
SocketUser=%i
SocketMode=0600
RemoveOnStop=yes
Accept=no

[Install]
WantedBy=sockets.target
"""

SERVICE_UNIT = """[Unit]
Description=FalaFácil global shortcut service for UID %i
Requires=falafacil-shortcutd@%i.socket
After=falafacil-shortcutd@%i.socket

[Service]
Type=simple
ExecStart=/usr/local/libexec/falafacil-shortcutd --shortcut-daemon
DynamicUser=yes
SupplementaryGroups=input
DevicePolicy=closed
DeviceAllow=char-input r
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_UNIX
RestrictSUIDSGID=yes
LockPersonality=yes
SystemCallFilter=@system-service
StandardOutput=null
StandardError=null
"""

_MINIMAL_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
_GRAPHICAL_ENV_KEYS = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "DISPLAY",
    "XAUTHORITY",
    "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
)

ProcessFactory = Callable[[QObject | None], QProcess]


def _current_bundle() -> Path | None:
    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable)
    else:
        candidate = Path.home() / ".local/bin/falafacil"
    try:
        metadata = candidate.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or candidate.is_symlink():
        return None
    if not os.access(candidate, os.X_OK):
        return None
    return candidate.resolve()


def _minimal_environment(source: os._Environ[str] | dict[str, str] | None = None) -> dict[str, str]:
    environment = os.environ if source is None else source
    result = {"PATH": _MINIMAL_PATH}
    for key in _GRAPHICAL_ENV_KEYS:
        value = environment.get(key)
        if value:
            result[key] = value
    if "LANG" not in result:
        default_locale = locale.getlocale()[0]
        result["LANG"] = f"{default_locale}.UTF-8" if default_locale else "C.UTF-8"
    return result


class ShortcutServiceInstaller(QObject):
    """Runs the fixed pkexec installer asynchronously, without a shell."""

    finished = Signal(bool, str)

    def __init__(
        self,
        *,
        process_factory: ProcessFactory | None = None,
        bundle_resolver: Callable[[], Path | None] = _current_bundle,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._process_factory = process_factory or (lambda parent: QProcess(parent))
        self._bundle_resolver = bundle_resolver
        self._process: QProcess | None = None
        self._cancel_requested = False

    @property
    def running(self) -> bool:
        return self._process is not None

    def install(self) -> bool:
        if self._process is not None:
            return False
        bundle = self._bundle_resolver()
        if bundle is None:
            self.finished.emit(False, SOURCE_INSTALL_UNAVAILABLE_MESSAGE)
            return False
        if shutil.which("pkexec", path=_MINIMAL_PATH) is None:
            self.finished.emit(False, BACKEND_FAILURE_MESSAGE)
            return False

        process = self._process_factory(self)
        environment = QProcessEnvironment()
        for key, value in _minimal_environment().items():
            environment.insert(key, value)
        process.setProcessEnvironment(environment)
        process.setProgram("pkexec")
        process.setArguments([str(bundle), "--install-shortcut-service"])
        process.finished.connect(self._on_finished)
        process.errorOccurred.connect(self._on_error)
        self._process = process
        self._cancel_requested = False
        process.start()
        return True

    def cancel(self) -> None:
        process = self._process
        if process is None:
            return
        self._cancel_requested = True
        process.terminate()
        if not process.waitForFinished(1000):
            process.kill()
            process.waitForFinished(1000)
        if self._process is process:
            self._process = None
            process.deleteLater()
            self.finished.emit(False, AUTHORIZATION_CANCELLED_MESSAGE)

    def _on_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        process = self.sender()
        if process is not self._process:
            return
        self._process = None
        process.deleteLater()
        if self._cancel_requested or exit_code in {126, 127}:
            self.finished.emit(False, AUTHORIZATION_CANCELLED_MESSAGE)
        elif exit_status == QProcess.ExitStatus.NormalExit and exit_code == 0:
            self.finished.emit(True, "")
        else:
            self.finished.emit(False, BACKEND_FAILURE_MESSAGE)

    def _on_error(self, _error: QProcess.ProcessError) -> None:
        process = self.sender()
        if process is not self._process:
            return
        if process.state() == QProcess.ProcessState.NotRunning:
            self._process = None
            process.deleteLater()
            self.finished.emit(False, BACKEND_FAILURE_MESSAGE)


def _resolve_self_executable() -> Path:
    return Path("/proc/self/exe").resolve(strict=True)


def _rooted(root: Path, absolute: Path) -> Path:
    return root.joinpath(*absolute.parts[1:])


def _validate_destination(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise OSError("unsafe destination")


def _atomic_write_bytes(path: Path, payload: bytes, mode: int) -> None:
    _validate_destination(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), 0, 0)
        os.replace(temporary, path)
        os.chmod(path, mode, follow_symlinks=False)
        os.chown(path, 0, 0, follow_symlinks=False)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_copy_executable(source: Path, destination: Path) -> None:
    try:
        metadata = source.stat()
    except OSError as error:
        raise OSError("invalid source") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError("invalid source")
    _validate_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as input_stream, os.fdopen(
            descriptor, "wb", closefd=True
        ) as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
            os.fchmod(output_stream.fileno(), 0o755)
            os.fchown(output_stream.fileno(), 0, 0)
        os.replace(temporary, destination)
        os.chmod(destination, 0o755, follow_symlinks=False)
        os.chown(destination, 0, 0, follow_symlinks=False)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def install_privileged_service(
    *,
    root: Path = Path("/"),
    command_runner: Callable[..., object] = subprocess.run,
) -> int:
    """Install the fixed service for the authenticated desktop user's UID."""
    if os.geteuid() != 0:
        return 1
    raw_uid = os.environ.get("PKEXEC_UID", "")
    if not raw_uid.isascii() or not raw_uid.isdigit():
        return 1
    uid = int(raw_uid)
    if uid <= 0:
        return 1
    try:
        pwd.getpwuid(uid)
        source = _resolve_self_executable()
        executable = _rooted(root, SERVICE_EXECUTABLE)
        socket_unit = _rooted(root, SOCKET_UNIT_PATH)
        service_unit = _rooted(root, SERVICE_UNIT_PATH)
        _atomic_copy_executable(source, executable)
        _atomic_write_bytes(socket_unit, SOCKET_UNIT.encode("utf-8"), 0o644)
        _atomic_write_bytes(service_unit, SERVICE_UNIT.encode("utf-8"), 0o644)
        environment = {"PATH": _MINIMAL_PATH, "LANG": "C.UTF-8"}
        command_runner(
            ["systemctl", "daemon-reload"],
            check=True,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        command_runner(
            ["systemctl", "enable", "--now", f"falafacil-shortcutd@{uid}.socket"],
            check=True,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        command_runner(
            ["systemctl", "stop", f"falafacil-shortcutd@{uid}.service"],
            check=True,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (KeyError, OSError, subprocess.SubprocessError):
        return 1
    return 0
