from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Any, Callable, Iterable

from PySide6.QtCore import QCoreApplication, QFileSystemWatcher, QObject, QSocketNotifier
from PySide6.QtNetwork import QLocalServer

from .shortcuts import (
    MAX_PROTOCOL_LINE_BYTES,
    PROTOCOL_VERSION,
    normalize_keyboard_shortcut,
    normalize_mouse_button_name,
)

_MOUSE_CODE_NAMES = {
    "BTN_MIDDLE": "middle",
    "BTN_SIDE": "x1",
    "BTN_EXTRA": "x2",
    "BTN_FORWARD": "forward",
    "BTN_BACK": "back",
    "BTN_TASK": "task",
}
_MODIFIER_CODE_NAMES = {
    "KEY_LEFTCTRL": "ctrl",
    "KEY_RIGHTCTRL": "ctrl",
    "KEY_LEFTALT": "alt",
    "KEY_RIGHTALT": "alt",
    "KEY_LEFTSHIFT": "shift",
    "KEY_RIGHTSHIFT": "shift",
    "KEY_LEFTMETA": "meta",
    "KEY_RIGHTMETA": "meta",
}
_MEDIA_CODE_NAMES = {
    "KEY_PLAYPAUSE": "play_pause",
    "KEY_NEXTSONG": "next",
    "KEY_PREVIOUSSONG": "previous",
    "KEY_MUTE": "mute",
}
_MODIFIER_ORDER = ("ctrl", "alt", "shift", "meta")


def _terminal_code_names() -> dict[str, str]:
    mapping = {f"KEY_{letter.upper()}": letter for letter in "abcdefghijklmnopqrstuvwxyz"}
    mapping.update({f"KEY_{digit}": digit for digit in "0123456789"})
    mapping.update({f"KEY_F{number}": f"f{number}" for number in range(1, 25)})
    mapping.update(_MEDIA_CODE_NAMES)
    return mapping


_TERMINAL_CODE_NAMES = _terminal_code_names()


def _evdev_module() -> Any:
    import evdev

    return evdev


def _code_map(names: dict[str, str], ecodes: Any) -> dict[int, str]:
    return {
        int(getattr(ecodes, code_name)): value
        for code_name, value in names.items()
        if hasattr(ecodes, code_name)
    }


class ShortcutSession:
    """Protocol and bindings for one local UI client."""

    def __init__(
        self,
        send_line: Callable[[str], None],
        close: Callable[[], None],
        on_ready: Callable[[], None] | None = None,
    ) -> None:
        self._send_line = send_line
        self._close = close
        self._on_ready = on_ready or (lambda: None)
        self._handshake_complete = False
        self._mouse_generation = 0
        self._keyboard_generation = 0
        self._mouse_binding: str | None = None
        self._keyboard_binding: str | None = None
        self._mouse_capture = False
        self._keyboard_capture = False

    @property
    def handshake_complete(self) -> bool:
        return self._handshake_complete

    @property
    def mouse_binding(self) -> str | None:
        return self._mouse_binding

    @property
    def keyboard_binding(self) -> str | None:
        return self._keyboard_binding

    def handle_line(self, line: str) -> bool:
        parts = line.split(" ")
        if not self._handshake_complete:
            if parts != ["HELLO", str(PROTOCOL_VERSION)]:
                self._close()
                return False
            self._handshake_complete = True
            self._send_line(f"READY {PROTOCOL_VERSION}")
            self._on_ready()
            return True

        if len(parts) not in {2, 3}:
            self._close()
            return False
        command = parts[0]
        try:
            generation = int(parts[1])
        except ValueError:
            self._close()
            return False
        if generation <= 0:
            self._close()
            return False

        if command in {"WATCH_MOUSE", "CAPTURE_MOUSE", "STOP_MOUSE"}:
            if generation <= self._mouse_generation:
                self._close()
                return False
            self._mouse_generation = generation
            return self._handle_mouse_command(command, generation, parts)
        if command in {"WATCH_KEYBOARD", "CAPTURE_KEYBOARD", "STOP_KEYBOARD"}:
            if generation <= self._keyboard_generation:
                self._close()
                return False
            self._keyboard_generation = generation
            return self._handle_keyboard_command(command, generation, parts)
        self._close()
        return False

    def _handle_mouse_command(self, command: str, generation: int, parts: list[str]) -> bool:
        if command == "WATCH_MOUSE" and len(parts) == 3:
            button = normalize_mouse_button_name(parts[2])
            if button is None:
                self._close()
                return False
            self._mouse_binding = button
            self._mouse_capture = False
            self._send_line(f"WATCHING_MOUSE {generation} {button}")
            return True
        if command == "CAPTURE_MOUSE" and len(parts) == 2:
            self._mouse_binding = None
            self._mouse_capture = True
            return True
        if command == "STOP_MOUSE" and len(parts) == 2:
            self._mouse_binding = None
            self._mouse_capture = False
            self._send_line(f"STOPPED mouse {generation}")
            return True
        self._close()
        return False

    def _handle_keyboard_command(
        self, command: str, generation: int, parts: list[str]
    ) -> bool:
        if command == "WATCH_KEYBOARD" and len(parts) == 3:
            shortcut = normalize_keyboard_shortcut(parts[2])
            if shortcut is None:
                self._close()
                return False
            self._keyboard_binding = shortcut
            self._keyboard_capture = False
            self._send_line(f"WATCHING_KEYBOARD {generation} {shortcut}")
            return True
        if command == "CAPTURE_KEYBOARD" and len(parts) == 2:
            self._keyboard_binding = None
            self._keyboard_capture = True
            return True
        if command == "STOP_KEYBOARD" and len(parts) == 2:
            self._keyboard_binding = None
            self._keyboard_capture = False
            self._send_line(f"STOPPED keyboard {generation}")
            return True
        self._close()
        return False

    def handle_mouse_press(self, button: str) -> None:
        canonical = normalize_mouse_button_name(button)
        if canonical is None:
            return
        if self._mouse_capture:
            self._mouse_capture = False
            self._send_line(f"CAPTURED_MOUSE {self._mouse_generation} {canonical}")
        elif self._mouse_binding == canonical:
            self._send_line(f"ACTIVATED_MOUSE {self._mouse_generation} {canonical}")

    def handle_keyboard_press(self, shortcut: str | None, *, unsafe: bool = False) -> None:
        canonical = normalize_keyboard_shortcut(shortcut) if shortcut is not None else None
        if self._keyboard_capture:
            if canonical is None:
                if unsafe:
                    self._send_line(
                        f"ERROR keyboard {self._keyboard_generation} unsafe_key"
                    )
                return
            self._keyboard_capture = False
            self._send_line(
                f"CAPTURED_KEYBOARD {self._keyboard_generation} {canonical}"
            )
        elif canonical is not None and self._keyboard_binding == canonical:
            self._send_line(
                f"ACTIVATED_KEYBOARD {self._keyboard_generation} {canonical}"
            )

    def notify_service_error(self, code: str) -> None:
        generation = max(self._mouse_generation, self._keyboard_generation, 1)
        self._send_line(f"ERROR service {generation} {code}")


class SocketSession(QObject):
    """Bounded line framing around a QLocalSocket-like object."""

    def __init__(
        self,
        socket: Any,
        parent: QObject | None = None,
        on_ready: Callable[[ShortcutSession], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.socket = socket
        self.buffer = bytearray()
        self.closed = False
        self.protocol = ShortcutSession(
            self.send_line,
            self.close,
            lambda: on_ready(self.protocol) if on_ready is not None else None,
        )
        socket.readyRead.connect(self.read_available)
        socket.disconnected.connect(self.deleteLater)

    def read_available(self) -> None:
        self.feed(bytes(self.socket.readAll()))

    def feed(self, data: bytes) -> None:
        if self.closed:
            return
        self.buffer.extend(data)
        while True:
            newline = self.buffer.find(b"\n")
            if newline < 0:
                if len(self.buffer) > MAX_PROTOCOL_LINE_BYTES:
                    self.close()
                return
            if newline > MAX_PROTOCOL_LINE_BYTES:
                self.close()
                return
            raw = bytes(self.buffer[:newline])
            del self.buffer[: newline + 1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            try:
                line = raw.decode("ascii")
            except UnicodeDecodeError:
                self.close()
                return
            if not line or not self.protocol.handle_line(line):
                return

    def send_line(self, line: str) -> None:
        if self.closed:
            return
        try:
            payload = line.encode("ascii") + b"\n"
        except UnicodeEncodeError:
            self.close()
            return
        if len(payload) - 1 > MAX_PROTOCOL_LINE_BYTES:
            self.close()
            return
        if self.socket.write(payload) != len(payload):
            self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.socket.abort()


class InputDeviceMonitor(QObject):
    """Read-only evdev monitor that emits only canonical matching triggers."""

    def __init__(
        self,
        dispatch_mouse: Callable[[str], None],
        dispatch_keyboard: Callable[[str | None, bool], None],
        notify_error: Callable[[str], None],
        *,
        input_dir: Path = Path("/dev/input"),
        list_devices: Callable[[], Iterable[str]] | None = None,
        device_factory: Callable[[str], Any] | None = None,
        notifier_factory: Callable[[int, Any, QObject], Any] | None = None,
        watcher: QFileSystemWatcher | None = None,
        ecodes: Any | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        evdev = (
            _evdev_module()
            if (ecodes is None or list_devices is None or device_factory is None)
            else None
        )
        self._ecodes = ecodes or evdev.ecodes
        self._list_devices = (
            list_devices
            if list_devices is not None
            else (lambda: evdev.list_devices(writable=False))
        )
        self._device_factory = device_factory or evdev.InputDevice
        self._notifier_factory = notifier_factory or QSocketNotifier
        self._dispatch_mouse = dispatch_mouse
        self._dispatch_keyboard = dispatch_keyboard
        self._notify_error = notify_error
        self._input_dir = input_dir
        self._devices: dict[str, Any] = {}
        self._notifiers: dict[str, Any] = {}
        self._kinds: dict[str, str] = {}
        self._modifiers_by_device: dict[str, set[str]] = {}
        self._mouse_codes = _code_map(_MOUSE_CODE_NAMES, self._ecodes)
        self._modifier_codes = _code_map(_MODIFIER_CODE_NAMES, self._ecodes)
        self._terminal_codes = _code_map(_TERMINAL_CODE_NAMES, self._ecodes)
        self._watcher = watcher or QFileSystemWatcher(self)
        if input_dir.exists():
            self._watcher.addPath(str(input_dir))
        self._watcher.directoryChanged.connect(lambda _path: self.rescan())
        self.rescan()

    @property
    def device_paths(self) -> frozenset[str]:
        return frozenset(self._devices)

    def rescan(self) -> None:
        try:
            current = {str(path) for path in self._list_devices()}
        except (OSError, RuntimeError):
            self._notify_error("input_access")
            return
        for path in tuple(self._devices):
            if path not in current:
                self._remove_device(path)
        for path in sorted(current - self._devices.keys()):
            self._add_device(path)
        if not self._devices:
            self._notify_error("no_devices")

    def _add_device(self, path: str) -> None:
        try:
            device = self._device_factory(path)
            capabilities = device.capabilities(absinfo=False)
            kind = self._classify(capabilities)
            if kind is None:
                device.close()
                return
            notifier = self._notifier_factory(
                device.fd, QSocketNotifier.Type.Read, self
            )
            notifier.activated.connect(
                lambda _fd=None, _type=None, target=path: self._drain(target)
            )
        except (OSError, PermissionError, RuntimeError):
            self._notify_error("input_access")
            return
        self._devices[path] = device
        self._notifiers[path] = notifier
        self._kinds[path] = kind
        self._modifiers_by_device[path] = set()

    def _classify(self, capabilities: dict[int, Any]) -> str | None:
        ev_rel = int(getattr(self._ecodes, "EV_REL"))
        ev_key = int(getattr(self._ecodes, "EV_KEY"))
        rel_x = int(getattr(self._ecodes, "REL_X"))
        rel_y = int(getattr(self._ecodes, "REL_Y"))
        rel_codes = set(capabilities.get(ev_rel, ()))
        if rel_codes.intersection({rel_x, rel_y}):
            return "mouse"
        key_codes = set(capabilities.get(ev_key, ()))
        if key_codes.intersection(self._modifier_codes | self._terminal_codes):
            return "keyboard"
        return None

    def _drain(self, path: str) -> None:
        device = self._devices.get(path)
        if device is None:
            return
        try:
            events = device.read()
            for event in events:
                if int(event.type) != int(getattr(self._ecodes, "EV_KEY")):
                    continue
                if self._kinds[path] == "mouse":
                    self._handle_mouse_event(int(event.code), int(event.value))
                else:
                    self._handle_keyboard_event(
                        path, int(event.code), int(event.value)
                    )
        except BlockingIOError:
            return
        except (OSError, RuntimeError):
            self._remove_device(path)
            if not self._devices:
                self._notify_error("no_devices")

    def _handle_mouse_event(self, code: int, value: int) -> None:
        if value != 1:
            return
        button = self._mouse_codes.get(code)
        if button is not None:
            self._dispatch_mouse(button)

    def _handle_keyboard_event(self, path: str, code: int, value: int) -> None:
        modifier = self._modifier_codes.get(code)
        if modifier is not None:
            if value == 1:
                self._modifiers_by_device[path].add(modifier)
            elif value == 0:
                self._modifiers_by_device[path].discard(modifier)
            return
        if value != 1:
            return
        terminal = self._terminal_codes.get(code)
        if terminal is None:
            return
        modifiers = set().union(*self._modifiers_by_device.values())
        raw = "+".join(
            (*[name for name in _MODIFIER_ORDER if name in modifiers], terminal)
        )
        canonical = normalize_keyboard_shortcut(raw)
        unsafe = len(terminal) == 1 and terminal.isalnum() and canonical is None
        self._dispatch_keyboard(canonical, unsafe)

    def _remove_device(self, path: str) -> None:
        notifier = self._notifiers.pop(path, None)
        if notifier is not None:
            notifier.setEnabled(False)
            notifier.deleteLater()
        device = self._devices.pop(path, None)
        if device is not None:
            try:
                device.close()
            except OSError:
                pass
        self._kinds.pop(path, None)
        self._modifiers_by_device.pop(path, None)

    def close(self) -> None:
        for path in tuple(self._devices):
            self._remove_device(path)


class ShortcutService(QObject):
    def __init__(
        self,
        server: QLocalServer,
        *,
        monitor_factory: Callable[..., InputDeviceMonitor] = InputDeviceMonitor,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.server = server
        self.sessions: set[SocketSession] = set()
        self._last_error: str | None = None
        server.newConnection.connect(self._accept_connections)
        self.monitor = monitor_factory(
            self._dispatch_mouse,
            self._dispatch_keyboard,
            self._notify_error,
            parent=self,
        )
        self._accept_connections()

    def _accept_connections(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            session = SocketSession(socket, self, self._on_session_ready)
            self.sessions.add(session)
            socket.disconnected.connect(lambda target=session: self.sessions.discard(target))

    def _dispatch_mouse(self, button: str) -> None:
        for session in tuple(self.sessions):
            session.protocol.handle_mouse_press(button)

    def _dispatch_keyboard(self, shortcut: str | None, unsafe: bool) -> None:
        for session in tuple(self.sessions):
            session.protocol.handle_keyboard_press(shortcut, unsafe=unsafe)

    def _on_session_ready(self, protocol: ShortcutSession) -> None:
        if self._last_error is not None and not self.monitor.device_paths:
            protocol.notify_service_error(self._last_error)

    def _notify_error(self, code: str) -> None:
        self._last_error = code
        for session in tuple(self.sessions):
            if session.protocol.handshake_complete:
                session.protocol.notify_service_error(code)


def _adopt_systemd_socket(server: QLocalServer, env: dict[str, str] | None = None) -> bool:
    environment = os.environ if env is None else env
    try:
        listen_pid = int(environment.get("LISTEN_PID", ""))
        listen_fds = int(environment.get("LISTEN_FDS", ""))
    except ValueError:
        return False
    return listen_pid == os.getpid() and listen_fds == 1 and server.listen(3)


def main() -> int:
    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    server = QLocalServer()
    if not _adopt_systemd_socket(server):
        return 2
    service = ShortcutService(server)
    app.aboutToQuit.connect(service.monitor.close)
    return app.exec()
