from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from falafacil.shortcuts import (
    BACKEND_FAILURE_MESSAGE,
    InputShortcutBridge,
    MAX_PROTOCOL_LINE_BYTES,
    PRIMARY_MOUSE_BUTTON_MESSAGE,
    UNSUPPORTED_MOUSE_BUTTON_MESSAGE,
    normalize_keyboard_shortcut,
    normalize_mouse_button_name,
)


class FakeSocket(QObject):
    connected = Signal()
    readyRead = Signal()
    disconnected = Signal()
    errorOccurred = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.server_name: str | None = None
        self.writes: list[bytes] = []
        self.incoming = bytearray()
        self.aborted = False

    def connectToServer(self, server_name: str) -> None:
        self.server_name = server_name
        self.connected.emit()

    def write(self, payload: bytes) -> int:
        self.writes.append(bytes(payload))
        return len(payload)

    def readAll(self) -> bytes:
        payload = bytes(self.incoming)
        self.incoming.clear()
        return payload

    def abort(self) -> None:
        if not self.aborted:
            self.aborted = True
            self.disconnected.emit()

    def server_send(self, payload: bytes) -> None:
        self.incoming.extend(payload)
        self.readyRead.emit()


def _qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _bridge() -> tuple[InputShortcutBridge, FakeSocket]:
    _qapp()
    socket = FakeSocket()
    bridge = InputShortcutBridge(server_name="test-shortcutd", socket_factory=lambda: socket)
    return bridge, socket


def test_mouse_normalizer_accepts_only_safe_buttons_and_aliases() -> None:
    for value, expected in {
        "middle": "middle",
        "Button.middle": "middle",
        "x1": "x1",
        "Button.button8": "x1",
        "button9": "x2",
        "forward": "forward",
        "back": "back",
        "task": "task",
    }.items():
        assert normalize_mouse_button_name(value) == expected
    for value in (None, "", "left", "right", "Button.left", "unknown", "x1 extra"):
        assert normalize_mouse_button_name(value) is None


def test_keyboard_normalizer_enforces_canonical_safe_grammar() -> None:
    assert normalize_keyboard_shortcut("Alt+CTRL+R") == "ctrl+alt+r"
    assert normalize_keyboard_shortcut("shift+F24") == "shift+f24"
    assert normalize_keyboard_shortcut("play_pause") == "play_pause"
    assert normalize_keyboard_shortcut("META+1") == "meta+1"
    for value in (
        "r",
        "shift+r",
        "ctrl",
        "ctrl+alt",
        "ctrl+r+s",
        "ctrl+ctrl+r",
        "f25",
        "escape",
        "",
    ):
        assert normalize_keyboard_shortcut(value) is None


def test_bridge_handshake_supports_partial_and_multiple_frames() -> None:
    bridge, socket = _bridge()
    assert socket.server_name == "test-shortcutd"
    assert socket.writes == [b"HELLO 1\n"]
    ready: list[bool] = []
    bridge.ready_changed.connect(ready.append)

    socket.server_send(b"REA")
    assert bridge.ready is False
    socket.server_send(b"DY 1\n")
    assert bridge.ready is True

    mouse_gen = bridge.start_mouse("button8")
    keyboard_gen = bridge.start_keyboard("ALT+CTRL+R")
    observed: list[tuple[str, int, str]] = []
    bridge.mouse_binding_ready.connect(
        lambda gen, value: observed.append(("mouse", gen, value))
    )
    bridge.keyboard_binding_ready.connect(
        lambda gen, value: observed.append(("keyboard", gen, value))
    )
    socket.server_send(
        f"WATCHING_MOUSE {mouse_gen} x1\nWATCHING_KEYBOARD {keyboard_gen} ctrl+alt+r\n".encode()
    )

    assert observed == [
        ("mouse", mouse_gen, "x1"),
        ("keyboard", keyboard_gen, "ctrl+alt+r"),
    ]
    assert ready == [True]


def test_bridge_generations_are_independent_and_stale_responses_are_ignored() -> None:
    bridge, socket = _bridge()
    socket.server_send(b"READY 1\n")
    mouse_old = bridge.start_mouse("x1")
    mouse_current = bridge.start_mouse("x2")
    keyboard_current = bridge.start_keyboard("ctrl+alt+r")
    activations: list[tuple[str, int, str]] = []
    bridge.mouse_activated.connect(
        lambda gen, value: activations.append(("mouse", gen, value))
    )
    bridge.keyboard_activated.connect(
        lambda gen, value: activations.append(("keyboard", gen, value))
    )

    socket.server_send(
        (
            f"ACTIVATED_MOUSE {mouse_old} x1\n"
            f"ACTIVATED_KEYBOARD {keyboard_current} ctrl+alt+r\n"
            f"ACTIVATED_MOUSE {mouse_current} x2\n"
        ).encode()
    )

    assert bridge.mouse_generation == mouse_current
    assert bridge.keyboard_generation == keyboard_current
    assert activations == [
        ("keyboard", keyboard_current, "ctrl+alt+r"),
        ("mouse", mouse_current, "x2"),
    ]


def test_bridge_emits_capture_stop_and_sanitized_error() -> None:
    bridge, socket = _bridge()
    socket.server_send(b"READY 1\n")
    mouse_gen = bridge.begin_mouse_capture()
    keyboard_gen = bridge.begin_keyboard_capture()
    captured: list[tuple[str, int, str]] = []
    stopped: list[tuple[str, int]] = []
    failed: list[tuple[str, int, str]] = []
    bridge.mouse_captured.connect(lambda g, v: captured.append(("mouse", g, v)))
    bridge.keyboard_captured.connect(lambda g, v: captured.append(("keyboard", g, v)))
    bridge.stopped.connect(lambda kind, g: stopped.append((kind, g)))
    bridge.failed.connect(lambda kind, g, message: failed.append((kind, g, message)))

    socket.server_send(
        f"CAPTURED_MOUSE {mouse_gen} middle\nCAPTURED_KEYBOARD {keyboard_gen} f12\n".encode()
    )
    stop_gen = bridge.stop_mouse()
    socket.server_send(
        f"STOPPED mouse {stop_gen}\nERROR keyboard {keyboard_gen} raw-secret-exception\n".encode()
    )

    assert captured == [("mouse", mouse_gen, "middle"), ("keyboard", keyboard_gen, "f12")]
    assert stopped == [("mouse", stop_gen)]
    assert failed == [("keyboard", keyboard_gen, BACKEND_FAILURE_MESSAGE)]


def test_bridge_closes_on_oversized_non_ascii_or_invalid_protocol() -> None:
    for payload in (
        b"x" * (MAX_PROTOCOL_LINE_BYTES + 1),
        b"\xff\n",
        b"READY 999\n",
    ):
        bridge, socket = _bridge()
        failed: list[str] = []
        bridge.failed.connect(lambda _kind, _gen, message: failed.append(message))
        socket.server_send(payload)
        assert socket.aborted is True
        assert failed == [BACKEND_FAILURE_MESSAGE]


def test_close_invalidates_both_generations_and_discards_late_frames() -> None:
    bridge, socket = _bridge()
    socket.server_send(b"READY 1\n")
    mouse_gen = bridge.start_mouse("x1")
    keyboard_gen = bridge.start_keyboard("ctrl+alt+r")
    bridge.close()
    assert bridge.mouse_generation == mouse_gen + 1
    assert bridge.keyboard_generation == keyboard_gen + 1
    assert bridge.ready is False
    assert socket.aborted is True


def test_bridge_translates_mouse_rejection_error_codes() -> None:
    bridge, socket = _bridge()
    socket.server_send(b"READY 1\n")
    failed: list[tuple[str, int, str]] = []
    bridge.failed.connect(lambda kind, g, message: failed.append((kind, g, message)))

    stale = bridge.begin_mouse_capture()
    generation = bridge.begin_mouse_capture()
    assert generation > stale
    socket.server_send(f"ERROR mouse {generation} primary_button\n".encode())
    socket.server_send(f"ERROR mouse {generation} unsupported_button\n".encode())
    socket.server_send(f"ERROR mouse {stale} primary_button\n".encode())

    assert failed == [
        ("mouse", generation, PRIMARY_MOUSE_BUTTON_MESSAGE),
        ("mouse", generation, UNSUPPORTED_MOUSE_BUTTON_MESSAGE),
    ]
