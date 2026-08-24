from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from falafacil.shortcut_service import (
    InputDeviceMonitor,
    ShortcutSession,
    SocketSession,
    _adopt_systemd_socket,
)
from falafacil.shortcuts import MAX_PROTOCOL_LINE_BYTES


class FakeWireSocket(QObject):
    readyRead = Signal()
    disconnected = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.incoming = bytearray()
        self.outgoing: list[bytes] = []
        self.aborted = False

    def readAll(self) -> bytes:
        payload = bytes(self.incoming)
        self.incoming.clear()
        return payload

    def write(self, payload: bytes) -> int:
        self.outgoing.append(bytes(payload))
        return len(payload)

    def abort(self) -> None:
        self.aborted = True

    def receive(self, payload: bytes) -> None:
        self.incoming.extend(payload)
        self.readyRead.emit()


class FakeNotifier(QObject):
    activated = Signal(object, object)

    def __init__(self, _fd: int, _kind: object, parent: QObject) -> None:
        super().__init__(parent)
        self.enabled = True

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = enabled


class FakeEcodes:
    EV_KEY = 1
    EV_REL = 2
    REL_X = 10
    REL_Y = 11
    BTN_LEFT = 18
    BTN_RIGHT = 19
    BTN_MIDDLE = 20
    BTN_SIDE = 21
    BTN_EXTRA = 22
    BTN_FORWARD = 23
    BTN_BACK = 24
    BTN_TASK = 25
    BTN_0 = 26
    KEY_LEFTCTRL = 30
    KEY_RIGHTCTRL = 31
    KEY_LEFTALT = 32
    KEY_RIGHTALT = 33
    KEY_LEFTSHIFT = 34
    KEY_RIGHTSHIFT = 35
    KEY_LEFTMETA = 36
    KEY_RIGHTMETA = 37
    KEY_R = 40
    KEY_A = 41
    KEY_F1 = 42
    KEY_PLAYPAUSE = 43
    KEY_NEXTSONG = 44
    KEY_PREVIOUSSONG = 45
    KEY_MUTE = 46
    KEY_0 = 50
    KEY_1 = 51
    KEY_2 = 52
    KEY_3 = 53
    KEY_4 = 54
    KEY_5 = 55
    KEY_6 = 56
    KEY_7 = 57
    KEY_8 = 58
    KEY_9 = 59


@dataclass
class FakeEvent:
    type: int
    code: int
    value: int


class FakeDevice:
    next_fd = 100

    def __init__(self, capabilities: dict[int, list[int]]) -> None:
        self.fd = FakeDevice.next_fd
        FakeDevice.next_fd += 1
        self._capabilities = capabilities
        self.events: list[FakeEvent] = []
        self.closed = False

    def capabilities(self, *, absinfo: bool = False) -> dict[int, list[int]]:
        assert absinfo is False
        return self._capabilities

    def read(self) -> list[FakeEvent]:
        events, self.events = self.events, []
        return events

    def close(self) -> None:
        self.closed = True


class FakeServer:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.descriptors: list[int] = []

    def listen(self, descriptor: int) -> bool:
        self.descriptors.append(descriptor)
        return self.result


def _qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_socket_session_frames_partial_and_multiple_commands() -> None:
    _qapp()
    socket = FakeWireSocket()
    session = SocketSession(socket)

    socket.receive(b"HEL")
    assert socket.outgoing == []
    socket.receive(b"LO 1\nWATCH_MOUSE 1 x1\nWATCH_KEYBOARD 1 ctrl+alt+r\n")

    assert socket.outgoing == [
        b"READY 1\n",
        b"WATCHING_MOUSE 1 x1\n",
        b"WATCHING_KEYBOARD 1 ctrl+alt+r\n",
    ]
    assert session.protocol.mouse_binding == "x1"
    assert session.protocol.keyboard_binding == "ctrl+alt+r"


def test_handshake_ready_callback_runs_after_ready_frame() -> None:
    lines: list[str] = []
    session = ShortcutSession(
        lines.append,
        lambda: None,
        lambda: lines.append("ERROR service 1 no_devices"),
    )
    session.handle_line("HELLO 1")
    assert lines == ["READY 1", "ERROR service 1 no_devices"]


def test_socket_session_closes_on_length_encoding_command_and_generation_errors() -> None:
    payloads = (
        b"x" * (MAX_PROTOCOL_LINE_BYTES + 1),
        b"\xff\n",
        b"HELLO 2\n",
        b"HELLO 1\nUNKNOWN 1\n",
        b"HELLO 1\nWATCH_MOUSE 0 x1\n",
        b"HELLO 1\nWATCH_MOUSE 2 x1\nSTOP_MOUSE 1\n",
    )
    for payload in payloads:
        socket = FakeWireSocket()
        session = SocketSession(socket)
        socket.receive(payload)
        assert session.closed is True
        assert socket.aborted is True


def test_session_mouse_and_keyboard_bindings_capture_and_stop_are_independent() -> None:
    lines: list[str] = []
    closed: list[bool] = []
    session = ShortcutSession(lines.append, lambda: closed.append(True))
    assert session.handle_line("HELLO 1")
    assert session.handle_line("WATCH_MOUSE 1 x1")
    assert session.handle_line("WATCH_KEYBOARD 1 ctrl+alt+r")
    lines.clear()

    session.handle_mouse_press("x2")
    session.handle_keyboard_press("ctrl+alt+shift+r")
    assert lines == []
    session.handle_mouse_press("x1")
    session.handle_keyboard_press("ctrl+alt+r")
    assert lines == ["ACTIVATED_MOUSE 1 x1", "ACTIVATED_KEYBOARD 1 ctrl+alt+r"]

    assert session.handle_line("STOP_KEYBOARD 2")
    session.handle_mouse_press("x1")
    session.handle_keyboard_press("ctrl+alt+r")
    assert lines[-2:] == ["STOPPED keyboard 2", "ACTIVATED_MOUSE 1 x1"]
    assert closed == []


def test_capture_is_one_shot_and_unsafe_keyboard_keeps_capture_open() -> None:
    lines: list[str] = []
    session = ShortcutSession(lines.append, lambda: None)
    session.handle_line("HELLO 1")
    session.handle_line("CAPTURE_MOUSE 1")
    session.handle_line("CAPTURE_KEYBOARD 1")
    lines.clear()

    session.handle_mouse_press("middle")
    session.handle_mouse_press("x1")
    session.handle_keyboard_press(None, unsafe=True)
    session.handle_keyboard_press("f12")
    session.handle_keyboard_press("f12")

    assert lines == [
        "CAPTURED_MOUSE 1 middle",
        "ERROR keyboard 1 unsafe_key",
        "CAPTURED_KEYBOARD 1 f12",
    ]


def test_two_clients_are_isolated() -> None:
    first_lines: list[str] = []
    second_lines: list[str] = []
    first = ShortcutSession(first_lines.append, lambda: None)
    second = ShortcutSession(second_lines.append, lambda: None)
    for session in (first, second):
        session.handle_line("HELLO 1")
    first.handle_line("WATCH_MOUSE 1 x1")
    second.handle_line("WATCH_MOUSE 1 x2")
    first_lines.clear()
    second_lines.clear()

    for session in (first, second):
        session.handle_mouse_press("x1")

    assert first_lines == ["ACTIVATED_MOUSE 1 x1"]
    assert second_lines == []


def test_monitor_filters_mouse_keyboard_release_repeat_and_extra_modifiers(tmp_path: Path) -> None:
    _qapp()
    mouse = FakeDevice(
        {FakeEcodes.EV_REL: [FakeEcodes.REL_X], FakeEcodes.EV_KEY: [FakeEcodes.BTN_SIDE]}
    )
    keyboard = FakeDevice(
        {
            FakeEcodes.EV_KEY: [
                FakeEcodes.KEY_LEFTCTRL,
                FakeEcodes.KEY_LEFTALT,
                FakeEcodes.KEY_LEFTSHIFT,
                FakeEcodes.KEY_R,
            ]
        }
    )
    devices = {"/dev/input/mouse": mouse, "/dev/input/keyboard": keyboard}
    paths = list(devices)
    mouse_events: list[tuple[str | None, str | None]] = []
    keyboard_events: list[tuple[str | None, bool]] = []
    errors: list[str] = []
    monitor = InputDeviceMonitor(
        lambda button, rejection: mouse_events.append((button, rejection)),
        lambda shortcut, unsafe: keyboard_events.append((shortcut, unsafe)),
        errors.append,
        input_dir=tmp_path,
        list_devices=lambda: list(paths),
        device_factory=devices.__getitem__,
        notifier_factory=FakeNotifier,
        ecodes=FakeEcodes,
    )

    mouse.events.extend(
        [
            FakeEvent(FakeEcodes.EV_KEY, FakeEcodes.BTN_SIDE, 0),
            FakeEvent(FakeEcodes.EV_KEY, FakeEcodes.BTN_SIDE, 2),
            FakeEvent(FakeEcodes.EV_KEY, FakeEcodes.BTN_SIDE, 1),
            FakeEvent(FakeEcodes.EV_KEY, FakeEcodes.KEY_R, 1),
        ]
    )
    monitor._drain("/dev/input/mouse")
    keyboard.events.extend(
        [
            FakeEvent(FakeEcodes.EV_KEY, FakeEcodes.KEY_LEFTCTRL, 1),
            FakeEvent(FakeEcodes.EV_KEY, FakeEcodes.KEY_LEFTALT, 1),
            FakeEvent(FakeEcodes.EV_KEY, FakeEcodes.KEY_R, 2),
            FakeEvent(FakeEcodes.EV_KEY, FakeEcodes.KEY_R, 0),
            FakeEvent(FakeEcodes.EV_KEY, FakeEcodes.KEY_R, 1),
            FakeEvent(FakeEcodes.EV_KEY, FakeEcodes.KEY_LEFTSHIFT, 1),
            FakeEvent(FakeEcodes.EV_KEY, FakeEcodes.KEY_R, 1),
        ]
    )
    monitor._drain("/dev/input/keyboard")

    assert mouse_events == [("x1", None)]
    assert keyboard_events == [
        (None, True),
        ("ctrl+alt+r", False),
        ("ctrl+alt+shift+r", False),
    ]
    assert errors == []


def test_monitor_hotplug_removal_closes_device_and_clears_stuck_modifiers(tmp_path: Path) -> None:
    _qapp()
    first = FakeDevice({FakeEcodes.EV_KEY: [FakeEcodes.KEY_LEFTCTRL, FakeEcodes.KEY_R]})
    second = FakeDevice({FakeEcodes.EV_KEY: [FakeEcodes.KEY_R]})
    devices = {"first": first, "second": second}
    paths = ["first", "second"]
    observed: list[tuple[str | None, bool]] = []
    monitor = InputDeviceMonitor(
        lambda _button, _rejection: None,
        lambda shortcut, unsafe: observed.append((shortcut, unsafe)),
        lambda _code: None,
        input_dir=tmp_path,
        list_devices=lambda: list(paths),
        device_factory=devices.__getitem__,
        notifier_factory=FakeNotifier,
        ecodes=FakeEcodes,
    )
    first.events.append(FakeEvent(FakeEcodes.EV_KEY, FakeEcodes.KEY_LEFTCTRL, 1))
    monitor._drain("first")
    paths.remove("first")
    monitor.rescan()
    second.events.append(FakeEvent(FakeEcodes.EV_KEY, FakeEcodes.KEY_R, 1))
    monitor._drain("second")

    assert first.closed is True
    assert observed == [(None, True)]


def test_monitor_reports_access_and_no_device_errors(tmp_path: Path) -> None:
    errors: list[str] = []
    InputDeviceMonitor(
        lambda _button, _rejection: None,
        lambda _shortcut, _unsafe: None,
        errors.append,
        input_dir=tmp_path,
        list_devices=lambda: [],
        device_factory=lambda _path: None,
        notifier_factory=FakeNotifier,
        ecodes=FakeEcodes,
    )
    assert errors == ["no_devices"]


def test_systemd_socket_adoption_accepts_only_own_single_descriptor(monkeypatch) -> None:
    server = FakeServer()
    monkeypatch.setattr("falafacil.shortcut_service.os.getpid", lambda: 123)
    assert _adopt_systemd_socket(server, {"LISTEN_PID": "123", "LISTEN_FDS": "1"})
    assert server.descriptors == [3]
    for env in (
        {},
        {"LISTEN_PID": "other", "LISTEN_FDS": "1"},
        {"LISTEN_PID": "123", "LISTEN_FDS": "2"},
        {"LISTEN_PID": "124", "LISTEN_FDS": "1"},
    ):
        assert not _adopt_systemd_socket(FakeServer(), env)


def test_input_device_monitor_default_list_devices_requests_readable_only(
    tmp_path: Path, monkeypatch
) -> None:
    _qapp()
    calls: list[dict[str, Any]] = []

    class FakeEvdevModule:
        ecodes = FakeEcodes

        @staticmethod
        def list_devices(*args: Any, **kwargs: Any) -> list[str]:
            calls.append({"args": args, "kwargs": kwargs})
            return []

        @staticmethod
        def InputDevice(path: str) -> Any:
            raise AssertionError("unexpected InputDevice call")

    monkeypatch.setattr("falafacil.shortcut_service._evdev_module", lambda: FakeEvdevModule)

    monitor = InputDeviceMonitor(
        lambda _button, _rejection: None,
        lambda _shortcut, _unsafe: None,
        lambda _error: None,
        input_dir=tmp_path,
        device_factory=lambda _path: None,
        notifier_factory=FakeNotifier,
    )
    assert len(calls) == 1
    assert calls[0]["args"] == ()
    assert calls[0]["kwargs"] == {"writable": False}

    calls.clear()
    injected_called = False

    def custom_list_devices() -> list[str]:
        nonlocal injected_called
        injected_called = True
        return []

    injected_monitor = InputDeviceMonitor(
        lambda _button, _rejection: None,
        lambda _shortcut, _unsafe: None,
        lambda _error: None,
        input_dir=tmp_path,
        list_devices=custom_list_devices,
        device_factory=lambda _path: None,
        notifier_factory=FakeNotifier,
    )
    assert injected_called is True
    assert calls == []


def test_rejected_button_reports_error_only_while_capturing() -> None:
    lines: list[str] = []
    session = ShortcutSession(lines.append, lambda: None)
    session.handle_line("HELLO 1")
    session.handle_line("WATCH_MOUSE 1 x1")
    lines.clear()

    session.handle_mouse_press(None, rejection="primary_button")
    session.handle_mouse_press(None, rejection="unsupported_button")
    assert lines == []

    assert session.handle_line("CAPTURE_MOUSE 2")
    session.handle_mouse_press(None, rejection="primary_button")
    session.handle_mouse_press(None, rejection="unsupported_button")
    assert lines == [
        "ERROR mouse 2 primary_button",
        "ERROR mouse 2 unsupported_button",
    ]

    session.handle_mouse_press("x1")
    assert lines[-1] == "CAPTURED_MOUSE 2 x1"
    session.handle_mouse_press("x1")
    assert lines[-1] == "CAPTURED_MOUSE 2 x1"


def test_rejection_vocabulary_is_closed() -> None:
    lines: list[str] = []
    session = ShortcutSession(lines.append, lambda: None)
    session.handle_line("HELLO 1")
    session.handle_line("CAPTURE_MOUSE 1")
    lines.clear()

    session.handle_mouse_press(None, rejection="/dev/input/event3")
    session.handle_mouse_press(None, rejection="BTN_TRIGGER_HAPPY")
    session.handle_mouse_press(None)
    assert lines == []


def test_monitor_routes_button_codes_on_a_keyboard_classified_node(
    tmp_path: Path,
) -> None:
    _qapp()
    combo = FakeDevice(
        {
            FakeEcodes.EV_KEY: [
                FakeEcodes.KEY_LEFTCTRL,
                FakeEcodes.KEY_R,
                FakeEcodes.BTN_0,
                FakeEcodes.BTN_SIDE,
            ]
        }
    )
    devices = {"/dev/input/combo": combo}
    mouse_events: list[tuple[str | None, str | None]] = []
    keyboard_events: list[tuple[str | None, bool]] = []
    monitor = InputDeviceMonitor(
        lambda button, rejection: mouse_events.append((button, rejection)),
        lambda shortcut, unsafe: keyboard_events.append((shortcut, unsafe)),
        lambda _error: None,
        input_dir=tmp_path,
        list_devices=lambda: list(devices),
        device_factory=devices.__getitem__,
        notifier_factory=FakeNotifier,
        ecodes=FakeEcodes,
    )
    assert monitor.device_paths == frozenset({"/dev/input/combo"})
    assert combo.closed is False

    combo.events.extend(
        [
            FakeEvent(FakeEcodes.EV_KEY, FakeEcodes.BTN_SIDE, 1),
            FakeEvent(FakeEcodes.EV_KEY, FakeEcodes.BTN_0, 1),
            FakeEvent(FakeEcodes.EV_KEY, FakeEcodes.KEY_LEFTCTRL, 1),
            FakeEvent(FakeEcodes.EV_KEY, FakeEcodes.KEY_R, 1),
        ]
    )
    monitor._drain("/dev/input/combo")

    assert mouse_events == [("x1", None), (None, "unsupported_button")]
    assert keyboard_events == [("ctrl+r", False)]


def test_monitor_admits_button_only_node(tmp_path: Path) -> None:
    _qapp()
    device = FakeDevice({FakeEcodes.EV_KEY: [FakeEcodes.BTN_SIDE]})
    devices = {"/dev/input/buttons": device}
    mouse_events: list[tuple[str | None, str | None]] = []
    monitor = InputDeviceMonitor(
        lambda button, rejection: mouse_events.append((button, rejection)),
        lambda _shortcut, _unsafe: None,
        lambda _error: None,
        input_dir=tmp_path,
        list_devices=lambda: list(devices),
        device_factory=devices.__getitem__,
        notifier_factory=FakeNotifier,
        ecodes=FakeEcodes,
    )
    assert monitor.device_paths == frozenset({"/dev/input/buttons"})
    assert device.closed is False

    device.events.append(FakeEvent(FakeEcodes.EV_KEY, FakeEcodes.BTN_SIDE, 1))
    monitor._drain("/dev/input/buttons")
    assert mouse_events == [("x1", None)]


def test_primary_and_unknown_buttons_report_distinct_rejections(
    tmp_path: Path,
) -> None:
    _qapp()
    mouse = FakeDevice(
        {
            FakeEcodes.EV_REL: [FakeEcodes.REL_X, FakeEcodes.REL_Y],
            FakeEcodes.EV_KEY: [
                FakeEcodes.BTN_LEFT,
                FakeEcodes.BTN_RIGHT,
                FakeEcodes.BTN_SIDE,
                FakeEcodes.BTN_0,
            ],
        }
    )
    devices = {"/dev/input/mouse": mouse}
    mouse_events: list[tuple[str | None, str | None]] = []
    monitor = InputDeviceMonitor(
        lambda button, rejection: mouse_events.append((button, rejection)),
        lambda _shortcut, _unsafe: None,
        lambda _error: None,
        input_dir=tmp_path,
        list_devices=lambda: list(devices),
        device_factory=devices.__getitem__,
        notifier_factory=FakeNotifier,
        ecodes=FakeEcodes,
    )

    mouse.events.extend(
        [
            FakeEvent(FakeEcodes.EV_KEY, FakeEcodes.BTN_LEFT, 0),
            FakeEvent(FakeEcodes.EV_KEY, FakeEcodes.BTN_LEFT, 2),
            FakeEvent(FakeEcodes.EV_KEY, FakeEcodes.BTN_LEFT, 1),
            FakeEvent(FakeEcodes.EV_KEY, FakeEcodes.BTN_RIGHT, 1),
            FakeEvent(FakeEcodes.EV_KEY, FakeEcodes.BTN_0, 1),
        ]
    )
    monitor._drain("/dev/input/mouse")

    assert mouse_events == [
        (None, "primary_button"),
        (None, "primary_button"),
        (None, "unsupported_button"),
    ]
