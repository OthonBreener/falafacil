from __future__ import annotations

import os
import re
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalSocket

PROTOCOL_VERSION = 1
MAX_PROTOCOL_LINE_BYTES = 128

INTEGRATION_NOT_INSTALLED_MESSAGE = "Integração global não instalada."
AUTHORIZATION_CANCELLED_MESSAGE = "A autorização foi cancelada."
INPUT_ACCESS_MESSAGE = "O serviço global não conseguiu acessar os dispositivos de entrada."
NO_COMPATIBLE_DEVICE_MESSAGE = "Nenhum dispositivo compatível foi detectado."
BACKEND_FAILURE_MESSAGE = "Não foi possível ativar o atalho global."
SOURCE_INSTALL_UNAVAILABLE_MESSAGE = (
    "A autorização automática está disponível no aplicativo instalado."
)
PRIMARY_MOUSE_BUTTON_MESSAGE = (
    "Os botões esquerdo e direito não são aceitos. "
    "Use um botão lateral ou o botão do meio."
)
UNSUPPORTED_MOUSE_BUTTON_MESSAGE = (
    "Esse botão não é reconhecido pela integração global. Use um botão lateral, "
    "o botão do meio, ou remapeie-o no software do mouse."
)
MOUSE_CAPTURE_HINT_MESSAGES = frozenset(
    {PRIMARY_MOUSE_BUTTON_MESSAGE, UNSUPPORTED_MOUSE_BUTTON_MESSAGE}
)

_ALLOWED_MOUSE_BUTTONS = frozenset({"middle", "x1", "x2", "forward", "back", "task"})
_MOUSE_ALIASES = {"button8": "x1", "button9": "x2"}
_MODIFIER_ORDER = ("ctrl", "alt", "shift", "meta")
_MODIFIERS = frozenset(_MODIFIER_ORDER)
_MEDIA_KEYS = frozenset({"play_pause", "next", "previous", "mute"})
_FUNCTION_KEY_RE = re.compile(r"f(?:[1-9]|1[0-9]|2[0-4])\Z")
_ERROR_MESSAGES = {
    "not_installed": INTEGRATION_NOT_INSTALLED_MESSAGE,
    "input_access": INPUT_ACCESS_MESSAGE,
    "no_devices": NO_COMPATIBLE_DEVICE_MESSAGE,
    "unsafe_key": BACKEND_FAILURE_MESSAGE,
    "primary_button": PRIMARY_MOUSE_BUTTON_MESSAGE,
    "unsupported_button": UNSUPPORTED_MOUSE_BUTTON_MESSAGE,
}

SocketFactory = Callable[[], QLocalSocket]


def normalize_mouse_button_name(value: Any) -> str | None:
    """Return a safe canonical global mouse trigger, or ``None``."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = getattr(value, "name", None)
        if not isinstance(value, str):
            return None
    raw = value.strip().lower()
    while raw.startswith("button."):
        raw = raw[7:]
    raw = _MOUSE_ALIASES.get(raw, raw)
    return raw if raw in _ALLOWED_MOUSE_BUTTONS else None


def normalize_keyboard_shortcut(value: str) -> str | None:
    """Normalize the restricted global keyboard shortcut grammar."""
    if not isinstance(value, str):
        return None
    parts = [part.strip().lower() for part in value.split("+")]
    if not parts or any(not part for part in parts):
        return None

    modifiers: set[str] = set()
    terminals: list[str] = []
    for part in parts:
        if part in _MODIFIERS:
            if part in modifiers:
                return None
            modifiers.add(part)
        else:
            terminals.append(part)
    if len(terminals) != 1:
        return None

    terminal = terminals[0]
    is_alnum = len(terminal) == 1 and terminal.isascii() and terminal.isalnum()
    is_function = _FUNCTION_KEY_RE.fullmatch(terminal) is not None
    if not (is_alnum or is_function or terminal in _MEDIA_KEYS):
        return None
    if is_alnum and not modifiers.intersection({"ctrl", "alt", "meta"}):
        return None

    ordered = [modifier for modifier in _MODIFIER_ORDER if modifier in modifiers]
    return "+".join((*ordered, terminal))


class InputShortcutBridge(QObject):
    """Asynchronous, generation-safe client for the local shortcut service."""

    mouse_binding_ready = Signal(int, str)
    mouse_activated = Signal(int, str)
    mouse_captured = Signal(int, str)
    keyboard_binding_ready = Signal(int, str)
    keyboard_activated = Signal(int, str)
    keyboard_captured = Signal(int, str)
    stopped = Signal(str, int)
    failed = Signal(str, int, str)
    ready_changed = Signal(bool)

    def __init__(
        self,
        *,
        server_name: str | None = None,
        socket_factory: SocketFactory | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.server_name = server_name or f"/run/falafacil-shortcutd-{os.getuid()}.sock"
        self._socket_factory = socket_factory or QLocalSocket
        self._socket: QLocalSocket | None = None
        self._buffer = bytearray()
        self._ready = False
        self._version_incompatible = False
        self._closed = False
        self._mouse_generation = 0
        self._keyboard_generation = 0
        self._connect_socket()

    @property
    def mouse_generation(self) -> int:
        return self._mouse_generation

    @property
    def keyboard_generation(self) -> int:
        return self._keyboard_generation

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def version_incompatible(self) -> bool:
        return self._version_incompatible

    def reconnect(self) -> None:
        self._closed = False
        self._disconnect_socket()
        self._connect_socket()

    def start_mouse(self, button: str) -> int:
        self._mouse_generation += 1
        generation = self._mouse_generation
        canonical = normalize_mouse_button_name(button)
        if canonical is None or not self._send_command(
            "mouse", generation, f"WATCH_MOUSE {generation} {canonical}"
        ):
            self.failed.emit("mouse", generation, BACKEND_FAILURE_MESSAGE)
        return generation

    def begin_mouse_capture(self) -> int:
        self._mouse_generation += 1
        generation = self._mouse_generation
        if not self._send_command("mouse", generation, f"CAPTURE_MOUSE {generation}"):
            self.failed.emit("mouse", generation, INTEGRATION_NOT_INSTALLED_MESSAGE)
        return generation

    def stop_mouse(self) -> int:
        self._mouse_generation += 1
        generation = self._mouse_generation
        if not self._send_command("mouse", generation, f"STOP_MOUSE {generation}"):
            self.failed.emit("mouse", generation, INTEGRATION_NOT_INSTALLED_MESSAGE)
        return generation

    def start_keyboard(self, shortcut: str) -> int:
        self._keyboard_generation += 1
        generation = self._keyboard_generation
        canonical = normalize_keyboard_shortcut(shortcut)
        if canonical is None or not self._send_command(
            "keyboard", generation, f"WATCH_KEYBOARD {generation} {canonical}"
        ):
            self.failed.emit("keyboard", generation, BACKEND_FAILURE_MESSAGE)
        return generation

    def begin_keyboard_capture(self) -> int:
        self._keyboard_generation += 1
        generation = self._keyboard_generation
        if not self._send_command(
            "keyboard", generation, f"CAPTURE_KEYBOARD {generation}"
        ):
            self.failed.emit("keyboard", generation, INTEGRATION_NOT_INSTALLED_MESSAGE)
        return generation

    def stop_keyboard(self) -> int:
        self._keyboard_generation += 1
        generation = self._keyboard_generation
        if not self._send_command("keyboard", generation, f"STOP_KEYBOARD {generation}"):
            self.failed.emit("keyboard", generation, INTEGRATION_NOT_INSTALLED_MESSAGE)
        return generation

    def close(self) -> None:
        self._closed = True
        self._mouse_generation += 1
        self._keyboard_generation += 1
        self._disconnect_socket()

    def _connect_socket(self) -> None:
        if self._closed:
            return
        socket = self._socket_factory()
        self._socket = socket
        socket.connected.connect(self._on_connected)
        socket.readyRead.connect(self._on_ready_read)
        socket.disconnected.connect(self._on_disconnected)
        socket.errorOccurred.connect(self._on_socket_error)
        socket.connectToServer(self.server_name)

    def _disconnect_socket(self) -> None:
        socket, self._socket = self._socket, None
        was_ready = self._ready
        self._ready = False
        self._buffer.clear()
        if socket is not None:
            try:
                socket.abort()
            except RuntimeError:
                pass
            socket.deleteLater()
        if was_ready:
            self.ready_changed.emit(False)

    def _on_connected(self) -> None:
        self._write_line(f"HELLO {PROTOCOL_VERSION}")

    def _on_disconnected(self) -> None:
        if self.sender() is not self._socket:
            return
        if self._ready:
            self._ready = False
            self.ready_changed.emit(False)

    def _on_socket_error(self, _error: object) -> None:
        if self.sender() is not self._socket or self._closed:
            return
        if self._ready:
            self._ready = False
            self.ready_changed.emit(False)

    def _on_ready_read(self) -> None:
        socket = self._socket
        if socket is None or self.sender() is not socket:
            return
        self._consume_data(bytes(socket.readAll()))

    def _consume_data(self, data: bytes) -> None:
        self._buffer.extend(data)
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                if len(self._buffer) > MAX_PROTOCOL_LINE_BYTES:
                    self._protocol_failure()
                return
            if newline > MAX_PROTOCOL_LINE_BYTES:
                self._protocol_failure()
                return
            raw = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            try:
                line = raw.decode("ascii")
            except UnicodeDecodeError:
                self._protocol_failure()
                return
            if not line or not self._handle_line(line):
                self._protocol_failure()
                return

    def _handle_line(self, line: str) -> bool:
        parts = line.split(" ")
        if not self._ready:
            if parts == ["READY", str(PROTOCOL_VERSION)]:
                self._ready = True
                self._version_incompatible = False
                self.ready_changed.emit(True)
                return True
            if len(parts) == 2 and parts[0] == "READY":
                self._version_incompatible = True
            return False

        command = parts[0]
        if command == "STOPPED" and len(parts) == 3:
            kind, generation = parts[1], self._parse_generation(parts[2])
            if kind not in {"mouse", "keyboard"} or generation is None:
                return False
            if generation == self._current_generation(kind):
                self.stopped.emit(kind, generation)
            return True

        if command == "ERROR" and len(parts) == 4:
            kind, generation = parts[1], self._parse_generation(parts[2])
            if kind not in {"mouse", "keyboard", "service"} or generation is None:
                return False
            if kind == "service" or generation == self._current_generation(kind):
                message = _ERROR_MESSAGES.get(parts[3], BACKEND_FAILURE_MESSAGE)
                self.failed.emit(kind, generation, message)
            return True

        response_map = {
            "WATCHING_MOUSE": ("mouse", normalize_mouse_button_name, self.mouse_binding_ready),
            "ACTIVATED_MOUSE": ("mouse", normalize_mouse_button_name, self.mouse_activated),
            "CAPTURED_MOUSE": ("mouse", normalize_mouse_button_name, self.mouse_captured),
            "WATCHING_KEYBOARD": (
                "keyboard",
                normalize_keyboard_shortcut,
                self.keyboard_binding_ready,
            ),
            "ACTIVATED_KEYBOARD": (
                "keyboard",
                normalize_keyboard_shortcut,
                self.keyboard_activated,
            ),
            "CAPTURED_KEYBOARD": (
                "keyboard",
                normalize_keyboard_shortcut,
                self.keyboard_captured,
            ),
        }
        entry = response_map.get(command)
        if entry is None or len(parts) != 3:
            return False
        kind, normalizer, signal = entry
        generation = self._parse_generation(parts[1])
        trigger = normalizer(parts[2])
        if generation is None or trigger is None:
            return False
        if generation == self._current_generation(kind):
            signal.emit(generation, trigger)
        return True

    @staticmethod
    def _parse_generation(value: str) -> int | None:
        if not value.isascii() or not value.isdigit():
            return None
        generation = int(value)
        return generation if generation > 0 else None

    def _current_generation(self, kind: str) -> int:
        return self._mouse_generation if kind == "mouse" else self._keyboard_generation

    def _send_command(self, kind: str, generation: int, line: str) -> bool:
        del kind, generation
        return self._ready and self._write_line(line)

    def _write_line(self, line: str) -> bool:
        socket = self._socket
        if socket is None:
            return False
        try:
            payload = line.encode("ascii") + b"\n"
        except UnicodeEncodeError:
            return False
        if len(payload) - 1 > MAX_PROTOCOL_LINE_BYTES:
            return False
        try:
            return socket.write(payload) == len(payload)
        except (RuntimeError, TypeError):
            return False

    def _protocol_failure(self) -> None:
        self.failed.emit("service", 0, BACKEND_FAILURE_MESSAGE)
        self._disconnect_socket()
