"""Homebrew installation detection, marker validation, and update controller."""

from __future__ import annotations

import enum
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from .path_security import has_foreign_write

HOMEBREW_FORMULA = "OthonBreener/falafacil/falafacil"
HOMEBREW_CHANNEL = "homebrew"
HOMEBREW_SCHEMA_VERSION = 1
SEMVER_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
MAX_MARKER_SIZE_BYTES = 65536
EXPECTED_MARKER_KEYS = frozenset(
    {
        "schema",
        "channel",
        "formula",
        "version",
        "homebrew_prefix",
        "brew_path",
        "launch_path",
        "marker_path",
    }
)

STATUS_UPDATING = "Atualizando catálogo do Homebrew…"
STATUS_CHECKING = "Verificando versão disponível…"
STATUS_UPGRADING = "Instalando atualização pelo Homebrew…"
STATUS_VERIFYING = "Verificando nova versão…"

UP_TO_DATE_MESSAGE = "Você já usa a versão mais recente."
READY_TO_RESTART_MESSAGE = "Atualização instalada. Reinicie o FalaFácil para usar a nova versão."
TIMEOUT_MESSAGE = "A atualização pelo Homebrew excedeu o tempo limite."
GENERIC_FAILURE_MESSAGE = "O Homebrew não conseguiu concluir a atualização. Tente novamente."

MAX_OUTDATED_STDOUT_BYTES = 256 * 1024
UPDATE_TIMEOUT_MS = 300_000
OUTDATED_TIMEOUT_MS = 300_000
UPGRADE_TIMEOUT_MS = 900_000
PROBE_TIMEOUT_MS = 30_000
KILL_GRACE_MS = 5_000

ProcessFactory = Callable[[QObject | None], QProcess]
TimerFactory = Callable[[QObject | None], QTimer]
DetachedStarter = Callable[[str, list[str]], Any]
MarkerLoader = Callable[..., "HomebrewInstallation"]


class HomebrewUpdateError(Exception):
    """Base error for Homebrew detection and update operations."""


@dataclass(frozen=True)
class HomebrewInstallation:
    """Represents a validated Homebrew installation with stable opt paths."""

    version: str
    formula: str
    homebrew_prefix: Path
    brew_path: Path
    launch_path: Path
    marker_path: Path


def _resolve_self_executable() -> Path:
    return Path("/proc/self/exe").resolve(strict=True)



def _validate_directory_node(
    directory: Path,
    current_uid: int,
    name: str = "Diretório",
) -> None:
    try:
        lst = directory.lstat()
    except OSError as exc:
        raise HomebrewUpdateError(
            f"Falha ao inspecionar {name.lower()} '{directory}': {exc}"
        ) from exc

    if lst.st_uid != current_uid:
        raise HomebrewUpdateError(
            f"{name} '{directory}' possui proprietário inválido (UID {lst.st_uid} != {current_uid})."
        )

    try:
        st = directory.stat()
    except OSError as exc:
        raise HomebrewUpdateError(
            f"Falha ao obter metadados do {name.lower()} '{directory}': {exc}"
        ) from exc

    if not stat.S_ISDIR(st.st_mode):
        raise HomebrewUpdateError(f"Caminho '{directory}' não é um diretório.")

    if st.st_uid != current_uid:
        raise HomebrewUpdateError(
            f"{name} resolvido '{directory.resolve()}' possui proprietário inválido (UID {st.st_uid} != {current_uid})."
        )

    if has_foreign_write(st):
        raise HomebrewUpdateError(
            f"{name} '{directory}' possui permissões de escrita inseguras para grupo/outros."
        )


def _validate_chain_security(
    path: Path,
    prefix: Path,
    current_uid: int,
) -> None:
    """Validate all lexical and resolved directory components from prefix down to path."""
    try:
        resolved_prefix = prefix.resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise HomebrewUpdateError(f"Não foi possível resolver o prefixo '{prefix}': {exc}") from exc

    try:
        rel = path.relative_to(prefix)
    except ValueError:
        raise HomebrewUpdateError(f"Caminho '{path}' não está dentro do prefixo '{prefix}'.")

    current_lexical = prefix
    for part in rel.parts[:-1]:
        current_lexical = current_lexical / part
        _validate_directory_node(current_lexical, current_uid, name="Componente lexical")
        if current_lexical.is_symlink():
            try:
                resolved_sym = current_lexical.resolve(strict=True)
                resolved_sym.relative_to(resolved_prefix)
            except (OSError, ValueError) as exc:
                raise HomebrewUpdateError(
                    f"Symlink intermediário '{current_lexical}' não pode ser resolvido no prefixo: {exc}"
                ) from exc

    if path.is_symlink():
        try:
            lst = path.lstat()
            if lst.st_uid != current_uid:
                raise HomebrewUpdateError(
                    f"Symlink '{path}' possui proprietário inválido (UID {lst.st_uid} != {current_uid})."
                )
        except OSError as exc:
            raise HomebrewUpdateError(f"Falha ao inspecionar symlink '{path}': {exc}") from exc

    try:
        resolved_path = path.resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise HomebrewUpdateError(f"Não foi possível resolver o caminho '{path}': {exc}") from exc

    try:
        resolved_path.relative_to(resolved_prefix)
    except ValueError:
        raise HomebrewUpdateError(
            f"Caminho resolvido '{resolved_path}' escapa do prefixo '{resolved_prefix}'."
        )

    current_resolved = resolved_path.parent
    while current_resolved != resolved_prefix and current_resolved != current_resolved.parent:
        try:
            current_resolved.relative_to(resolved_prefix)
        except ValueError:
            break
        _validate_directory_node(current_resolved, current_uid, name="Diretório resolvido")
        current_resolved = current_resolved.parent


def _validate_path_security(
    path: Path,
    *,
    require_directory: bool = False,
    require_executable: bool = False,
) -> Path:
    if not path.is_absolute():
        raise HomebrewUpdateError(f"Caminho não é absoluto: '{path}'.")

    current_uid = os.getuid()
    if path.is_symlink():
        try:
            lst = path.lstat()
            if lst.st_uid != current_uid:
                raise HomebrewUpdateError(
                    f"Symlink '{path}' possui proprietário inválido (UID {lst.st_uid} != {current_uid})."
                )
        except OSError as exc:
            raise HomebrewUpdateError(f"Falha ao inspecionar symlink '{path}': {exc}") from exc

    try:
        resolved = path.resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise HomebrewUpdateError(f"Não foi possível resolver o caminho '{path}': {exc}") from exc

    try:
        st = resolved.stat()
    except OSError as exc:
        raise HomebrewUpdateError(f"Falha ao obter metadados de '{resolved}': {exc}") from exc

    if st.st_uid != current_uid:
        raise HomebrewUpdateError(
            f"Caminho '{resolved}' possui proprietário inválido (UID {st.st_uid} != {current_uid})."
        )
    if require_directory:
        if not stat.S_ISDIR(st.st_mode):
            raise HomebrewUpdateError(f"Caminho '{resolved}' não é um diretório.")
        if has_foreign_write(st):
            raise HomebrewUpdateError(
                f"Diretório '{resolved}' possui permissões de escrita inseguras para grupo/outros."
            )
    else:
        if has_foreign_write(st):
            raise HomebrewUpdateError(
                f"Caminho '{resolved}' possui permissões de escrita inseguras para grupo/outros."
            )
        if require_executable:
            if not stat.S_ISREG(st.st_mode):
                raise HomebrewUpdateError(f"Caminho '{resolved}' não é um arquivo regular.")
            if not os.access(resolved, os.X_OK) or (st.st_mode & 0o111) == 0:
                raise HomebrewUpdateError(f"Caminho '{resolved}' não possui permissão de execução.")
        else:
            if not stat.S_ISREG(st.st_mode):
                raise HomebrewUpdateError(f"Caminho '{resolved}' não é um arquivo regular.")

    return resolved


def load_homebrew_marker(
    path: Path,
    *,
    expected_version: str | None = None,
) -> HomebrewInstallation:
    """Load and strictly validate a Homebrew marker JSON file.

    Preserves the stable opt paths in the returned HomebrewInstallation DTO while
    verifying that their resolved targets reside inside the validated prefix.
    """
    if isinstance(path, str):
        path = Path(path)

    if not path.is_absolute():
        raise HomebrewUpdateError(f"Caminho do marker deve ser absoluto: '{path}'.")

    resolved_marker_file = _validate_path_security(
        path, require_directory=False, require_executable=False
    )

    try:
        marker_size = resolved_marker_file.stat().st_size
    except OSError as exc:
        raise HomebrewUpdateError(f"Falha ao ler tamanho do marker '{path}': {exc}") from exc

    if marker_size > MAX_MARKER_SIZE_BYTES:
        raise HomebrewUpdateError(
            f"Arquivo de marker '{path}' excede o tamanho máximo permitido ({marker_size} bytes)."
        )

    try:
        raw_text = resolved_marker_file.read_text(encoding="utf-8")
        payload = json.loads(raw_text)
    except Exception as exc:
        raise HomebrewUpdateError(f"Falha ao ler/analisar marker JSON '{path}': {exc}") from exc

    if not isinstance(payload, dict):
        raise HomebrewUpdateError("Payload do marker deve ser um objeto JSON.")

    if set(payload.keys()) != EXPECTED_MARKER_KEYS:
        raise HomebrewUpdateError(
            f"Chaves do marker inválidas: esperava {sorted(EXPECTED_MARKER_KEYS)}, obteve {sorted(payload.keys())}."
        )

    schema = payload["schema"]
    if type(schema) is not int or schema != HOMEBREW_SCHEMA_VERSION:
        raise HomebrewUpdateError(f"Schema do marker inválido: {schema!r}.")

    channel = payload["channel"]
    if channel != HOMEBREW_CHANNEL:
        raise HomebrewUpdateError(f"Canal do marker inválido: {channel!r}.")

    formula = payload["formula"]
    if formula != HOMEBREW_FORMULA:
        raise HomebrewUpdateError(f"Fórmula do marker inválida: {formula!r}.")

    version = payload["version"]
    if not isinstance(version, str) or not SEMVER_PATTERN.fullmatch(version):
        raise HomebrewUpdateError(f"Versão SemVer inválida no marker: {version!r}.")

    if expected_version is not None and version != expected_version:
        raise HomebrewUpdateError(
            f"Versão no marker '{version}' diverge da versão esperada '{expected_version}'."
        )

    path_fields = ("homebrew_prefix", "brew_path", "launch_path", "marker_path")
    for field in path_fields:
        val = payload[field]
        if not isinstance(val, str) or not val.strip():
            raise HomebrewUpdateError(f"Campo '{field}' deve ser uma string de caminho não vazia.")

    homebrew_prefix = Path(payload["homebrew_prefix"])
    brew_path = Path(payload["brew_path"])
    launch_path = Path(payload["launch_path"])
    marker_path = Path(payload["marker_path"])

    if not homebrew_prefix.is_absolute():
        raise HomebrewUpdateError(f"homebrew_prefix não é absoluto: '{homebrew_prefix}'.")
    if not brew_path.is_absolute():
        raise HomebrewUpdateError(f"brew_path não é absoluto: '{brew_path}'.")
    if not launch_path.is_absolute():
        raise HomebrewUpdateError(f"launch_path não é absoluto: '{launch_path}'.")
    if not marker_path.is_absolute():
        raise HomebrewUpdateError(f"marker_path não é absoluto: '{marker_path}'.")

    expected_brew = homebrew_prefix / "bin" / "brew"
    expected_launch = homebrew_prefix / "opt" / "falafacil" / "bin" / "falafacil"
    expected_marker = homebrew_prefix / "opt" / "falafacil" / "libexec" / "falafacil-homebrew.json"

    if brew_path != expected_brew:
        raise HomebrewUpdateError(
            f"brew_path '{brew_path}' não corresponde ao esperado '{expected_brew}'."
        )
    if launch_path != expected_launch:
        raise HomebrewUpdateError(
            f"launch_path '{launch_path}' não corresponde ao esperado '{expected_launch}'."
        )
    if marker_path != expected_marker:
        raise HomebrewUpdateError(
            f"marker_path '{marker_path}' não corresponde ao esperado '{expected_marker}'."
        )

    resolved_prefix = _validate_path_security(
        homebrew_prefix, require_directory=True, require_executable=False
    )
    current_uid = os.getuid()
    _validate_directory_node(homebrew_prefix, current_uid, name="Prefixo Homebrew")

    _validate_chain_security(brew_path, homebrew_prefix, current_uid)
    _validate_chain_security(launch_path, homebrew_prefix, current_uid)
    _validate_chain_security(marker_path, homebrew_prefix, current_uid)
    _validate_chain_security(path, homebrew_prefix, current_uid)

    resolved_brew = _validate_path_security(
        brew_path, require_directory=False, require_executable=True
    )
    resolved_launch = _validate_path_security(
        launch_path, require_directory=False, require_executable=True
    )
    resolved_marker = _validate_path_security(
        marker_path, require_directory=False, require_executable=False
    )

    for name, resolved_target in (
        ("brew_path", resolved_brew),
        ("launch_path", resolved_launch),
        ("marker_path", resolved_marker),
    ):
        try:
            resolved_target.relative_to(resolved_prefix)
        except ValueError:
            raise HomebrewUpdateError(
                f"{name} resolvido '{resolved_target}' escapa do prefixo '{resolved_prefix}'."
            )

    if resolved_marker != resolved_marker_file:
        raise HomebrewUpdateError(
            f"marker_path resolvido '{resolved_marker}' difere do arquivo carregado '{resolved_marker_file}'."
        )
    return HomebrewInstallation(
        version=version,
        formula=formula,
        homebrew_prefix=homebrew_prefix,
        brew_path=brew_path,
        launch_path=launch_path,
        marker_path=marker_path,
    )


def detect_homebrew_installation() -> HomebrewInstallation | None:
    """Detect if running under a valid Homebrew installation matching __version__."""
    from falafacil import __version__

    try:
        self_exe = _resolve_self_executable()
    except Exception:
        return None

    candidate_marker = self_exe.parent / "falafacil-homebrew.json"
    try:
        if not candidate_marker.is_file():
            return None
        return load_homebrew_marker(candidate_marker, expected_version=__version__)
    except Exception:
        return None


def _parse_semver(version: str) -> tuple[int, int, int]:
    match = SEMVER_PATTERN.fullmatch(version)
    if not match:
        raise HomebrewUpdateError(f"Versão SemVer inválida: {version!r}.")
    parts = version.split(".")
    return int(parts[0]), int(parts[1]), int(parts[2])


def _default_detached_starter(program: str, arguments: list[str]) -> tuple[bool, int]:
    try:
        result = QProcess.startDetached(program, arguments)
        if type(result) is tuple and len(result) == 2:
            started, pid = result[0], result[1]
            if started is True and type(pid) is int and pid > 0:
                return True, pid
        return False, 0
    except Exception:
        return False, 0


class _Phase(enum.Enum):
    IDLE = enum.auto()
    UPDATE = enum.auto()
    OUTDATED = enum.auto()
    UPGRADE = enum.auto()
    PROBE = enum.auto()


class HomebrewUpdateController(QObject):
    """QProcess-based state machine for updating FalaFácil via Homebrew."""

    status_changed = Signal(str)
    up_to_date = Signal(str)
    ready_to_restart = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        installation: HomebrewInstallation,
        *,
        process_factory: ProcessFactory | None = None,
        timer_factory: TimerFactory | None = None,
        marker_loader: MarkerLoader = load_homebrew_marker,
        detached_starter: DetachedStarter | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if not isinstance(installation, HomebrewInstallation):
            raise TypeError("installation deve ser uma instância de HomebrewInstallation.")
        self._installation = installation
        self._process_factory = process_factory or (lambda p: QProcess(p))
        self._timer_factory = timer_factory or (lambda p: QTimer(p))
        self._marker_loader = marker_loader
        self._detached_starter = detached_starter or _default_detached_starter

        self._phase = _Phase.IDLE
        self._running = False
        self._process: QProcess | None = None
        self._watchdog_timer: QTimer | None = None
        self._grace_timer: QTimer | None = None
        self._timed_out = False
        self._aborting = False
        self._abort_message: str | None = None
        self._overflowed = False
        self._stdout_buffer = bytearray()
        self._candidate_installation: HomebrewInstallation | None = None
        self._ready_installation: HomebrewInstallation | None = None

    @property
    def running(self) -> bool:
        return self._running

    def install_latest(self) -> bool:
        if self._running:
            return False
        self._running = True
        self._aborting = False
        self._abort_message = None
        self._overflowed = False
        self._ready_installation = None
        self._candidate_installation = None
        self._stdout_buffer.clear()
        self._start_update_phase()
        return True

    def restart(self) -> bool:
        if self._running or self._ready_installation is None:
            return False
        try:
            result = self._detached_starter(str(self._ready_installation.launch_path), [])
            if type(result) is tuple and len(result) == 2:
                started, pid = result[0], result[1]
                return started is True and type(pid) is int and pid > 0
            return False
        except Exception:
            return False
    def _create_process(self) -> QProcess:
        process = self._process_factory(self)
        process.finished.connect(self._on_finished)
        process.errorOccurred.connect(self._on_error)
        return process

    def _arm_watchdog(self, timeout_ms: int) -> None:
        self._stop_timers()
        self._timed_out = False
        timer = self._timer_factory(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self._on_watchdog_timeout)
        self._watchdog_timer = timer
        timer.start(timeout_ms)

    def _stop_timers(self) -> None:
        self._stop_watchdog_timer()
        self._stop_grace_timer()

    def _stop_watchdog_timer(self) -> None:
        if self._watchdog_timer is not None:
            try:
                self._watchdog_timer.stop()
            except Exception:
                pass
            self._watchdog_timer = None

    def _stop_grace_timer(self) -> None:
        if self._grace_timer is not None:
            try:
                self._grace_timer.stop()
            except Exception:
                pass
            self._grace_timer = None

    def _start_grace_timer(self) -> None:
        if self._grace_timer is not None:
            return
        grace = self._timer_factory(self)
        grace.setSingleShot(True)
        grace.timeout.connect(self._on_grace_timeout)
        self._grace_timer = grace
        grace.start(KILL_GRACE_MS)

    def _on_watchdog_timeout(self) -> None:
        if self.sender() is not self._watchdog_timer:
            return
        self._watchdog_timer = None
        process = self._process
        if process is None:
            return
        self._timed_out = True
        self._abort_current_process(generic_failure=False, message=TIMEOUT_MESSAGE)

    def _on_grace_timeout(self) -> None:
        if self.sender() is not self._grace_timer:
            return
        self._grace_timer = None
        process = self._process
        if process is None:
            return
        if process.state() != QProcess.ProcessState.NotRunning:
            try:
                process.kill()
            except Exception:
                pass

    def _abort_current_process(
        self,
        *,
        generic_failure: bool = True,
        message: str | None = None,
    ) -> None:
        if self._aborting:
            return

        process = self._process
        if process is None:
            self._emit_terminal_failure(message or GENERIC_FAILURE_MESSAGE)
            return

        self._aborting = True
        self._abort_message = message or (GENERIC_FAILURE_MESSAGE if generic_failure else TIMEOUT_MESSAGE)
        self._stop_watchdog_timer()

        if process.state() == QProcess.ProcessState.NotRunning:
            self._cleanup_process()
            self._emit_terminal_failure(self._abort_message)
            return

        try:
            process.terminate()
        except Exception:
            pass

        self._start_grace_timer()
    def _drain_process_channel(self, process: QProcess) -> None:
        try:
            while True:
                chunk = process.read(65536)
                if not chunk:
                    break
        except Exception:
            pass

    def _drain_output(self) -> None:
        process = self.sender()
        if process is not self._process:
            return
        self._drain_process_channel(process)
        try:
            process.readAllStandardError()
        except Exception:
            pass

    def _drain_stderr(self) -> None:
        process = self.sender()
        if process is not self._process:
            return
        try:
            process.readAllStandardError()
        except Exception:
            pass

    def _read_outdated_stdout_bounded(self, process: QProcess) -> None:
        if self._aborting:
            self._drain_process_channel(process)
            return

        try:
            while True:
                remaining = MAX_OUTDATED_STDOUT_BYTES - len(self._stdout_buffer)
                if remaining <= 0:
                    chunk = process.read(1)
                    chunk_bytes = bytes(chunk) if chunk else b""
                    if chunk_bytes:
                        self._overflowed = True
                        self._drain_process_channel(process)
                        self._abort_current_process(generic_failure=True)
                    break

                read_size = min(remaining, 65536)
                chunk = process.read(read_size)
                chunk_bytes = bytes(chunk) if chunk else b""
                if not chunk_bytes:
                    break

                self._stdout_buffer.extend(chunk_bytes)
                if len(self._stdout_buffer) == MAX_OUTDATED_STDOUT_BYTES:
                    excess = process.read(1)
                    excess_bytes = bytes(excess) if excess else b""
                    if excess_bytes:
                        self._overflowed = True
                        self._drain_process_channel(process)
                        self._abort_current_process(generic_failure=True)
                        break
        except Exception:
            self._drain_process_channel(process)
            self._abort_current_process(generic_failure=True)

    def _on_outdated_stdout(self) -> None:
        process = self.sender()
        if process is not self._process:
            return
        self._read_outdated_stdout_bounded(process)

    def _on_error(self, _error: QProcess.ProcessError) -> None:
        process = self.sender()
        if process is not self._process:
            return
        if self._aborting:
            return
        self._abort_current_process(generic_failure=True)

    def _on_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        process = self.sender()
        if process is not self._process:
            return

        if self._aborting:
            msg = self._abort_message or (TIMEOUT_MESSAGE if self._timed_out else GENERIC_FAILURE_MESSAGE)
            self._cleanup_process()
            self._emit_terminal_failure(msg)
            return

        if self._phase == _Phase.UPDATE:
            self._handle_update_finished(process, exit_code, exit_status)
        elif self._phase == _Phase.OUTDATED:
            self._handle_outdated_finished(process, exit_code, exit_status)
        elif self._phase == _Phase.UPGRADE:
            self._handle_upgrade_finished(process, exit_code, exit_status)
        elif self._phase == _Phase.PROBE:
            self._handle_probe_finished(process, exit_code, exit_status)
        else:
            self._cleanup_process()
    def _start_update_phase(self) -> None:
        self._phase = _Phase.UPDATE
        self.status_changed.emit(STATUS_UPDATING)
        process = self._create_process()
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.setProgram(str(self._installation.brew_path))
        process.setArguments(["update-if-needed"])
        process.readyReadStandardOutput.connect(self._drain_output)
        self._process = process
        self._arm_watchdog(UPDATE_TIMEOUT_MS)
        process.start()

    def _handle_update_finished(
        self,
        process: QProcess,
        exit_code: int,
        exit_status: QProcess.ExitStatus,
    ) -> None:
        try:
            process.readAllStandardOutput()
            process.readAllStandardError()
        except Exception:
            pass

        if exit_status == QProcess.ExitStatus.NormalExit and exit_code == 0:
            self._cleanup_process()
            self._start_outdated_phase()
        else:
            self._fail_current()

    def _start_outdated_phase(self) -> None:
        self._phase = _Phase.OUTDATED
        self._stdout_buffer.clear()
        self.status_changed.emit(STATUS_CHECKING)
        process = self._create_process()
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.setProgram(str(self._installation.brew_path))
        process.setArguments(["outdated", "--formula", "--json=v2", HOMEBREW_FORMULA])
        process.readyReadStandardOutput.connect(self._on_outdated_stdout)
        process.readyReadStandardError.connect(self._drain_stderr)
        self._process = process
        self._arm_watchdog(OUTDATED_TIMEOUT_MS)
        process.start()

    def _handle_outdated_finished(
        self,
        process: QProcess,
        exit_code: int,
        exit_status: QProcess.ExitStatus,
    ) -> None:
        self._read_outdated_stdout_bounded(process)
        try:
            process.readAllStandardError()
        except Exception:
            pass

        if not self._running or self._phase != _Phase.OUTDATED:
            return

        if self._aborting:
            msg = self._abort_message or GENERIC_FAILURE_MESSAGE
            self._cleanup_process()
            self._emit_terminal_failure(msg)
            return
        if self._overflowed or len(self._stdout_buffer) > MAX_OUTDATED_STDOUT_BYTES:
            self._cleanup_process()
            self._emit_terminal_failure(GENERIC_FAILURE_MESSAGE)
            return

        if exit_status != QProcess.ExitStatus.NormalExit or exit_code != 0:
            self._cleanup_process()
            self._emit_terminal_failure(GENERIC_FAILURE_MESSAGE)
            return

        try:
            try:
                text = self._stdout_buffer.decode("utf-8")
                data = json.loads(text)
            except Exception:
                self._cleanup_process()
                self._emit_terminal_failure(GENERIC_FAILURE_MESSAGE)
                return
        finally:
            self._stdout_buffer.clear()

        if not isinstance(data, dict) or "formulae" not in data or not isinstance(data["formulae"], list):
            self._cleanup_process()
            self._emit_terminal_failure(GENERIC_FAILURE_MESSAGE)
            return

        formulae = data["formulae"]
        if len(formulae) == 0:
            self._emit_up_to_date(UP_TO_DATE_MESSAGE)
            return

        if len(formulae) != 1:
            self._cleanup_process()
            self._emit_terminal_failure(GENERIC_FAILURE_MESSAGE)
            return

        item = formulae[0]
        if not isinstance(item, dict):
            self._cleanup_process()
            self._emit_terminal_failure(GENERIC_FAILURE_MESSAGE)
            return

        name = item.get("name")
        if name != "falafacil":
            self._cleanup_process()
            self._emit_terminal_failure(GENERIC_FAILURE_MESSAGE)
            return

        if "full_name" in item and item["full_name"] != HOMEBREW_FORMULA:
            self._cleanup_process()
            self._emit_terminal_failure(GENERIC_FAILURE_MESSAGE)
            return

        if "pinned" not in item or type(item["pinned"]) is not bool or item["pinned"] is not False:
            self._cleanup_process()
            self._emit_terminal_failure(GENERIC_FAILURE_MESSAGE)
            return

        if item.get("pinned_version") is not None:
            self._cleanup_process()
            self._emit_terminal_failure(GENERIC_FAILURE_MESSAGE)
            return
        self._cleanup_process()
        self._start_upgrade_phase()

    def _start_upgrade_phase(self) -> None:
        self._phase = _Phase.UPGRADE
        self.status_changed.emit(STATUS_UPGRADING)
        process = self._create_process()
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.setProgram(str(self._installation.brew_path))
        process.setArguments(["upgrade", "--formula", "--no-ask", HOMEBREW_FORMULA])
        process.readyReadStandardOutput.connect(self._drain_output)
        self._process = process
        self._arm_watchdog(UPGRADE_TIMEOUT_MS)
        process.start()

    def _handle_upgrade_finished(
        self,
        process: QProcess,
        exit_code: int,
        exit_status: QProcess.ExitStatus,
    ) -> None:
        try:
            process.readAllStandardOutput()
            process.readAllStandardError()
        except Exception:
            pass

        if exit_status != QProcess.ExitStatus.NormalExit or exit_code != 0:
            self._fail_current()
            return

        self._cleanup_process()

        try:
            new_installation = self._marker_loader(
                self._installation.marker_path, expected_version=None
            )
        except Exception:
            self._emit_terminal_failure(GENERIC_FAILURE_MESSAGE)
            return

        if (
            new_installation.formula != self._installation.formula
            or new_installation.homebrew_prefix != self._installation.homebrew_prefix
            or new_installation.brew_path != self._installation.brew_path
            or new_installation.launch_path != self._installation.launch_path
            or new_installation.marker_path != self._installation.marker_path
        ):
            self._emit_terminal_failure(GENERIC_FAILURE_MESSAGE)
            return

        try:
            old_semver = _parse_semver(self._installation.version)
            new_semver = _parse_semver(new_installation.version)
        except Exception:
            self._emit_terminal_failure(GENERIC_FAILURE_MESSAGE)
            return

        if new_semver <= old_semver:
            self._emit_terminal_failure(GENERIC_FAILURE_MESSAGE)
            return

        self._candidate_installation = new_installation
        self._start_probe_phase(new_installation)

    def _start_probe_phase(self, new_installation: HomebrewInstallation) -> None:
        self._phase = _Phase.PROBE
        self.status_changed.emit(STATUS_VERIFYING)
        process = self._create_process()
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.setProgram(str(new_installation.launch_path))
        process.setArguments(["--update-probe", new_installation.version])
        process.readyReadStandardOutput.connect(self._drain_output)
        self._process = process
        self._arm_watchdog(PROBE_TIMEOUT_MS)
        process.start()

    def _handle_probe_finished(
        self,
        process: QProcess,
        exit_code: int,
        exit_status: QProcess.ExitStatus,
    ) -> None:
        try:
            process.readAllStandardOutput()
            process.readAllStandardError()
        except Exception:
            pass

        if exit_status == QProcess.ExitStatus.NormalExit and exit_code == 0:
            self._ready_installation = self._candidate_installation
            self._candidate_installation = None
            self._emit_ready_to_restart(READY_TO_RESTART_MESSAGE)
        else:
            self._candidate_installation = None
            self._fail_current()

    def _fail_current(self) -> None:
        timed_out = self._timed_out
        self._cleanup_process()
        message = TIMEOUT_MESSAGE if timed_out else GENERIC_FAILURE_MESSAGE
        self._emit_terminal_failure(message)

    def _cleanup_process(self) -> None:
        self._stop_timers()
        self._stdout_buffer.clear()
        process = self._process
        self._process = None
        if process is not None:
            try:
                process.deleteLater()
            except Exception:
                pass

    def _emit_up_to_date(self, message: str) -> None:
        self._cleanup_process()
        self._phase = _Phase.IDLE
        self._running = False
        self._aborting = False
        self._abort_message = None
        self._overflowed = False
        self.up_to_date.emit(message)

    def _emit_ready_to_restart(self, message: str) -> None:
        self._cleanup_process()
        self._phase = _Phase.IDLE
        self._running = False
        self._aborting = False
        self._abort_message = None
        self._overflowed = False
        self.ready_to_restart.emit(message)

    def _emit_terminal_failure(self, message: str) -> None:
        self._cleanup_process()
        self._phase = _Phase.IDLE
        self._running = False
        self._aborting = False
        self._abort_message = None
        self._overflowed = False
        self.failed.emit(message)
