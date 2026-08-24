from __future__ import annotations

import threading
from typing import Any, Callable

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from falafacil.shortcuts import (
    BACKEND_FAILURE_MESSAGE,
    SESSION_UNAVAILABLE_MESSAGE,
    MouseListenerLike,
    MouseShortcutBridge,
    MouseShortcutError,
    normalize_button_name,
)


class FakeMouseListener:
    def __init__(self, on_click: Any = None, suppress: bool = False) -> None:
        self.on_click = on_click
        self.suppress = suppress
        self.started = False
        self.stopped = False
        self.joined = False
        self.join_timeout: float | None = None

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def join(self, timeout: float | None = None) -> None:
        self.joined = True
        self.join_timeout = timeout

    def is_alive(self) -> bool:
        return self.started and not self.stopped


class FakeButtonObject:
    def __init__(self, name: str) -> None:
        self.name = name


def _ensure_qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _drain_events(
    bridge: MouseShortcutBridge | None = None,
    condition: Callable[[], bool] | None = None,
    max_passes: int = 50,
) -> None:
    app = _ensure_qapp()
    if condition is None:
        if bridge is not None:
            QCoreApplication.sendPostedEvents(bridge, 0)
        QCoreApplication.sendPostedEvents(None, 0)
        app.processEvents()
        return

    for _ in range(max_passes):
        if bridge is not None:
            QCoreApplication.sendPostedEvents(bridge, 0)
        QCoreApplication.sendPostedEvents(None, 0)
        app.processEvents()
        if condition():
            break


def test_mouse_shortcut_error_is_runtime_error() -> None:
    assert issubclass(MouseShortcutError, RuntimeError)


def test_x11_and_display_available() -> None:
    _ensure_qapp()
    bridge = MouseShortcutBridge(env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"})
    assert bridge.available is True
    assert bridge.last_error is None


def test_wayland_is_unavailable() -> None:
    _ensure_qapp()
    bridge = MouseShortcutBridge(env={"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"})
    assert bridge.available is False

    failed_messages: list[str] = []
    bridge.failed.connect(failed_messages.append)

    assert bridge.start("x1") is False
    assert bridge.begin_capture() is False
    assert failed_messages == [SESSION_UNAVAILABLE_MESSAGE, SESSION_UNAVAILABLE_MESSAGE]
    assert bridge.last_error == SESSION_UNAVAILABLE_MESSAGE


def test_x11_without_display_is_unavailable() -> None:
    _ensure_qapp()
    bridge = MouseShortcutBridge(env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ""})
    assert bridge.available is False

    failed_messages: list[str] = []
    bridge.failed.connect(failed_messages.append)

    assert bridge.start("x1") is False
    assert bridge.begin_capture() is False
    assert failed_messages == [SESSION_UNAVAILABLE_MESSAGE, SESSION_UNAVAILABLE_MESSAGE]


def test_normalize_button_name_canonical() -> None:
    assert normalize_button_name("x1") == "x1"
    assert normalize_button_name("Button.x1") == "x1"
    assert normalize_button_name("left") == "left"
    assert normalize_button_name("Button.left") == "left"
    assert normalize_button_name("right") == "right"
    assert normalize_button_name("Button.right") == "right"
    assert normalize_button_name("middle") == "middle"
    assert normalize_button_name("Button.middle") == "middle"
    assert normalize_button_name("x2") == "x2"
    assert normalize_button_name("Button.x2") == "x2"
    assert normalize_button_name("button8") == "x1"
    assert normalize_button_name("BUTTON8") == "x1"
    assert normalize_button_name("Button.button8") == "x1"
    assert normalize_button_name("button9") == "x2"
    assert normalize_button_name("BUTTON9") == "x2"
    assert normalize_button_name("Button.button9") == "x2"
    assert normalize_button_name(FakeButtonObject("x1")) == "x1"
    assert normalize_button_name(FakeButtonObject("Button.x2")) == "x2"
    assert normalize_button_name(FakeButtonObject("button8")) == "x1"
    assert normalize_button_name(FakeButtonObject("Button.button9")) == "x2"

def test_normalize_button_name_invalid_cases() -> None:
    assert normalize_button_name(None) is None
    assert normalize_button_name("") is None
    assert normalize_button_name("   ") is None
    assert normalize_button_name("unknown") is None
    assert normalize_button_name("Button.unknown") is None
    assert normalize_button_name("UNKNOWN") is None
    assert normalize_button_name("x1\n") is None
    assert normalize_button_name("button with spaces") is None
    assert normalize_button_name("a" * 65) is None
    assert normalize_button_name("button-invalid!") is None


def test_start_with_invalid_button_returns_false_and_emits_failed() -> None:
    _ensure_qapp()
    bridge = MouseShortcutBridge(env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"})

    failed_messages: list[str] = []
    bridge.failed.connect(failed_messages.append)

    assert bridge.start("unknown") is False
    assert bridge.start("") is False
    assert bridge.start("invalid button") is False
    assert failed_messages == [
        BACKEND_FAILURE_MESSAGE,
        BACKEND_FAILURE_MESSAGE,
        BACKEND_FAILURE_MESSAGE,
    ]
    assert bridge.last_error == BACKEND_FAILURE_MESSAGE


def test_start_active_pressed_emits_activated() -> None:
    _ensure_qapp()
    created_listeners: list[FakeMouseListener] = []

    def factory(**kwargs: Any) -> MouseListenerLike:
        listener = FakeMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    activated_count = 0

    def on_activated() -> None:
        nonlocal activated_count
        activated_count += 1

    bridge.activated.connect(on_activated)

    assert bridge.start("x1") is True
    assert len(created_listeners) == 1
    listener = created_listeners[0]
    assert listener.started is True
    assert listener.suppress is False

    # Press matching canonical token
    listener.on_click(100, 200, "x1", True)
    assert activated_count == 1

    # Press matching Button.x1 format
    listener.on_click(100, 200, "Button.x1", True)
    assert activated_count == 2


def test_start_active_released_and_other_button_ignored() -> None:
    _ensure_qapp()
    created_listeners: list[FakeMouseListener] = []

    def factory(**kwargs: Any) -> MouseListenerLike:
        listener = FakeMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    activated_calls: list[bool] = []
    bridge.activated.connect(lambda: activated_calls.append(True))

    assert bridge.start("x1") is True
    listener = created_listeners[0]

    # Release of the configured button is ignored
    listener.on_click(100, 200, "x1", False)
    assert len(activated_calls) == 0

    # Press of another button is ignored
    listener.on_click(100, 200, "x2", True)
    listener.on_click(100, 200, "left", True)
    listener.on_click(100, 200, "right", True)
    assert len(activated_calls) == 0


def test_begin_capture_emits_button_captured_once_and_stops() -> None:
    _ensure_qapp()
    created_listeners: list[FakeMouseListener] = []

    def factory(**kwargs: Any) -> MouseListenerLike:
        listener = FakeMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    captured_buttons: list[str] = []
    bridge.button_captured.connect(captured_buttons.append)

    assert bridge.begin_capture() is True
    assert len(created_listeners) == 1
    listener = created_listeners[0]
    assert listener.started is True

    # Release is ignored during capture
    listener.on_click(50, 50, "Button.x1", False)
    assert captured_buttons == []
    assert listener.stopped is False

    # First press captures normalized button and stops listener
    listener.on_click(50, 50, "Button.x1", True)
    _drain_events(bridge, condition=lambda: listener.stopped)
    assert captured_buttons == ["x1"]
    assert listener.stopped is True

    # Subsequent clicks do not emit again
    listener.on_click(50, 50, "Button.x2", True)
    assert captured_buttons == ["x1"]


def test_listener_factory_failure_handled_fail_soft_without_leaking_secrets() -> None:
    _ensure_qapp()
    secret = "secret-synthetic-token-shortcut-backend-9999"

    def failing_factory(**kwargs: Any) -> MouseListenerLike:
        raise RuntimeError(f"Failed to create listener with {secret}")

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=failing_factory,
    )

    failed_messages: list[str] = []
    bridge.failed.connect(failed_messages.append)

    assert bridge.start("x1") is False
    assert failed_messages == [BACKEND_FAILURE_MESSAGE]
    assert secret not in (bridge.last_error or "")
    assert "RuntimeError" not in (bridge.last_error or "")

    assert bridge.begin_capture() is False
    assert failed_messages == [BACKEND_FAILURE_MESSAGE, BACKEND_FAILURE_MESSAGE]


def test_stop_is_idempotent_and_cleans_listener() -> None:
    _ensure_qapp()
    created_listeners: list[FakeMouseListener] = []

    def factory(**kwargs: Any) -> MouseListenerLike:
        listener = FakeMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    # Calling stop before any listener is safe
    bridge.stop()

    assert bridge.start("x1") is True
    listener = created_listeners[0]
    assert listener.stopped is False

    bridge.stop()
    assert listener.stopped is True
    assert listener.joined is True

    # Repeated calls are safe no-ops
    bridge.stop()


def test_start_stops_previous_listener_before_starting_new() -> None:
    _ensure_qapp()
    created_listeners: list[FakeMouseListener] = []

    def factory(**kwargs: Any) -> MouseListenerLike:
        listener = FakeMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    assert bridge.start("x1") is True
    assert len(created_listeners) == 1
    first_listener = created_listeners[0]
    assert first_listener.stopped is False

    assert bridge.start("x2") is True
    assert len(created_listeners) == 2
    assert first_listener.stopped is True
    second_listener = created_listeners[1]
    assert second_listener.started is True
    assert second_listener.stopped is False


def test_start_rejects_lexically_valid_button_absent_in_backend() -> None:
    _ensure_qapp()
    bridge = MouseShortcutBridge(env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"})

    failed_messages: list[str] = []
    bridge.failed.connect(failed_messages.append)

    # Button is lexically valid [a-z0-9_]+ but does not exist in pynput.mouse.Button
    assert bridge.start("button_nonexistent_xyz_123") is False
    assert failed_messages == [BACKEND_FAILURE_MESSAGE]
    assert bridge.last_error == BACKEND_FAILURE_MESSAGE


def test_start_and_begin_capture_do_not_import_pynput_on_unavailable_platform(monkeypatch: Any) -> None:
    _ensure_qapp()
    bridge = MouseShortcutBridge(env={"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"})

    import sys

    imported_modules: list[str] = []
    orig_import = __import__

    def tracking_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("pynput"):
            imported_modules.append(name)
        return orig_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", tracking_import)

    failed_messages: list[str] = []
    bridge.failed.connect(failed_messages.append)

    assert bridge.start("x1") is False
    assert bridge.begin_capture() is False
    assert failed_messages == [SESSION_UNAVAILABLE_MESSAGE, SESSION_UNAVAILABLE_MESSAGE]
    assert imported_modules == []


def test_injected_factory_succeeds_when_pynput_is_uninstalled(monkeypatch: Any) -> None:
    _ensure_qapp()
    created_listeners: list[FakeMouseListener] = []

    def factory(**kwargs: Any) -> MouseListenerLike:
        listener = FakeMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    orig_import = __import__

    def failing_pynput_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("pynput"):
            raise ModuleNotFoundError("No module named 'pynput'")
        return orig_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", failing_pynput_import)

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    activated_calls: list[bool] = []
    bridge.activated.connect(lambda: activated_calls.append(True))

    assert bridge.start("x1") is True
    assert len(created_listeners) == 1
    assert created_listeners[0].started is True

    created_listeners[0].on_click(10, 20, "x1", True)
    assert len(activated_calls) == 1


def test_default_factory_fails_when_pynput_is_uninstalled(monkeypatch: Any) -> None:
    _ensure_qapp()
    orig_import = __import__

    def failing_pynput_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("pynput"):
            raise ModuleNotFoundError("No module named 'pynput'")
        return orig_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", failing_pynput_import)

    bridge = MouseShortcutBridge(env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"})

    failed_messages: list[str] = []
    bridge.failed.connect(failed_messages.append)

    assert bridge.start("x1") is False
    assert bridge.begin_capture() is False
    assert failed_messages == [BACKEND_FAILURE_MESSAGE, BACKEND_FAILURE_MESSAGE]
    assert bridge.last_error == BACKEND_FAILURE_MESSAGE


def test_default_factory_rejects_missing_button_when_pynput_is_present(monkeypatch: Any) -> None:
    _ensure_qapp()
    import sys
    import types

    fake_mouse = types.ModuleType("pynput.mouse")

    class FakeXorgButtonEnum:
        left = "left"
        right = "right"
        middle = "middle"
        button8 = "button8"
        button9 = "button9"

    fake_mouse.Button = FakeXorgButtonEnum  # type: ignore[attr-defined]
    fake_mouse.Listener = FakeMouseListener  # type: ignore[attr-defined]

    fake_pynput = types.ModuleType("pynput")
    fake_pynput.mouse = fake_mouse  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "pynput", fake_pynput)
    monkeypatch.setitem(sys.modules, "pynput.mouse", fake_mouse)

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
    )

    failed_messages: list[str] = []
    bridge.failed.connect(failed_messages.append)

    assert bridge.start("button_nonexistent_xyz_456") is False
    assert failed_messages == [BACKEND_FAILURE_MESSAGE]
    assert bridge.last_error == BACKEND_FAILURE_MESSAGE

    # Canonical x1 and x2 resolve to button8 and button9 in Xorg backend
    assert bridge.start("x1") is True
    assert bridge.start("x2") is True
    assert bridge.start("button8") is True
    assert bridge.start("button9") is True
    assert bridge.start("left") is True


def test_xorg_backend_aliases_callback_activation_and_capture(monkeypatch: Any) -> None:
    _ensure_qapp()
    import sys
    import types

    fake_mouse = types.ModuleType("pynput.mouse")

    class FakeXorgButtonEnum:
        left = FakeButtonObject("left")
        right = FakeButtonObject("right")
        middle = FakeButtonObject("middle")
        button8 = FakeButtonObject("button8")
        button9 = FakeButtonObject("button9")

    active_listeners: list[FakeMouseListener] = []

    def fake_listener_ctor(*args: Any, **kwargs: Any) -> FakeMouseListener:
        listener = FakeMouseListener(*args, **kwargs)
        active_listeners.append(listener)
        return listener

    fake_mouse.Button = FakeXorgButtonEnum  # type: ignore[attr-defined]
    fake_mouse.Listener = fake_listener_ctor  # type: ignore[attr-defined]

    fake_pynput = types.ModuleType("pynput")
    fake_pynput.mouse = fake_mouse  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "pynput", fake_pynput)
    monkeypatch.setitem(sys.modules, "pynput.mouse", fake_mouse)

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
    )

    activated_events: list[int] = []
    bridge.activated.connect(lambda: activated_events.append(1))

    # 1. Start with canonical "x1"
    assert bridge.start("x1") is True
    assert len(active_listeners) == 1
    listener = active_listeners[0]

    # Physical Xorg event button8 (press=True) triggers activation
    listener.on_click(0, 0, FakeButtonObject("button8"), True)
    _drain_events()
    assert len(activated_events) == 1

    # Physical Xorg event button8 (press=False) does not trigger
    listener.on_click(0, 0, FakeButtonObject("button8"), False)
    _drain_events()
    assert len(activated_events) == 1

    # Physical Xorg event button9 does not match x1
    listener.on_click(0, 0, FakeButtonObject("button9"), True)
    _drain_events()
    assert len(activated_events) == 1

    # 2. Start with canonical "x2"
    assert bridge.start("x2") is True
    listener_x2 = active_listeners[-1]
    listener_x2.on_click(0, 0, FakeButtonObject("button9"), True)
    _drain_events()
    assert len(activated_events) == 2

    # 3. Capture mode with button8 emits canonical "x1"
    captured: list[str] = []
    bridge.button_captured.connect(captured.append)
    assert bridge.begin_capture() is True
    capture_listener = active_listeners[-1]
    capture_listener.on_click(0, 0, FakeButtonObject("button8"), True)
    _drain_events()
    assert captured == ["x1"]

    # 4. Capture mode with button9 emits canonical "x2"
    captured.clear()
    assert bridge.begin_capture() is True
    capture_listener_2 = active_listeners[-1]
    capture_listener_2.on_click(0, 0, FakeButtonObject("button9"), True)
    _drain_events()
    assert captured == ["x2"]

def test_injected_factory_allows_canonical_buttons_without_pynput_enum() -> None:
    _ensure_qapp()
    created_listeners: list[FakeMouseListener] = []

    def factory(**kwargs: Any) -> MouseListenerLike:
        listener = FakeMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    failed_messages: list[str] = []
    bridge.failed.connect(failed_messages.append)

    # Injected factory does not require button to exist in pynput.mouse.Button enum
    assert bridge.start("x1") is True
    assert bridge.start("custom_lateral_btn_99") is True
    assert len(created_listeners) == 2
    assert failed_messages == []

def test_callback_during_start_is_processed() -> None:
    _ensure_qapp()
    created_listeners: list[FakeMouseListener] = []
    activated_count = 0

    class ImmediateClickMouseListener(FakeMouseListener):
        def start(self) -> None:
            super().start()
            if self.on_click is not None:
                self.on_click(10, 10, "x1", True)

    def factory(**kwargs: Any) -> MouseListenerLike:
        listener = ImmediateClickMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )
    bridge.activated.connect(lambda: nonlocal_inc())

    def nonlocal_inc() -> None:
        nonlocal activated_count
        activated_count += 1

    assert bridge.start("x1") is True
    assert activated_count == 1


def test_listener_cleanup_when_start_fails() -> None:
    _ensure_qapp()
    created_listeners: list[FakeMouseListener] = []

    class FailingStartMouseListener(FakeMouseListener):
        def start(self) -> None:
            raise RuntimeError("Failure during listener start")

    def factory(**kwargs: Any) -> MouseListenerLike:
        listener = FailingStartMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    failed_messages: list[str] = []
    bridge.failed.connect(failed_messages.append)

    assert bridge.start("x1") is False
    assert len(created_listeners) == 1
    listener = created_listeners[0]
    assert listener.stopped is True
    assert listener.joined is True
    assert failed_messages == [BACKEND_FAILURE_MESSAGE]

    # Repeat for begin_capture
    assert bridge.begin_capture() is False
    assert len(created_listeners) == 2
    listener2 = created_listeners[1]
    assert listener2.stopped is True
    assert listener2.joined is True
    assert failed_messages == [BACKEND_FAILURE_MESSAGE, BACKEND_FAILURE_MESSAGE]


def test_pending_callback_from_previous_listener_ignored_after_reconfiguration() -> None:
    _ensure_qapp()
    created_listeners: list[FakeMouseListener] = []

    def factory(**kwargs: Any) -> MouseListenerLike:
        listener = FakeMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    activated_events: list[bool] = []
    bridge.activated.connect(lambda: activated_events.append(True))

    assert bridge.start("x1") is True
    listener_1 = created_listeners[0]

    assert bridge.start("x2") is True
    listener_2 = created_listeners[1]

    # Stale callback from listener 1 with its old button
    listener_1.on_click(10, 10, "x1", True)
    assert len(activated_events) == 0

    # Stale callback from listener 1 with listener 2's button
    listener_1.on_click(10, 10, "x2", True)
    assert len(activated_events) == 0

    # Callback from active listener 2
    listener_2.on_click(10, 10, "x2", True)
    assert len(activated_events) == 1


def test_pending_callback_from_previous_capture_ignored_after_stop_or_new_capture() -> None:
    _ensure_qapp()
    created_listeners: list[FakeMouseListener] = []

    def factory(**kwargs: Any) -> MouseListenerLike:
        listener = FakeMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    captured_buttons: list[str] = []
    bridge.button_captured.connect(captured_buttons.append)

    assert bridge.begin_capture() is True
    capture_listener_1 = created_listeners[0]

    # Restart capture (creates generation 2)
    assert bridge.begin_capture() is True
    capture_listener_2 = created_listeners[1]

    # Delayed callback from capture listener 1
    capture_listener_1.on_click(10, 10, "x1", True)
    assert captured_buttons == []
    assert capture_listener_2.stopped is False

    # Callback from active capture listener 2
    capture_listener_2.on_click(10, 10, "x2", True)
    _drain_events(bridge, condition=lambda: capture_listener_2.stopped)
    assert captured_buttons == ["x2"]
    assert capture_listener_2.stopped is True


def test_stop_failure_emits_sanitized_failed_and_remains_idempotent() -> None:
    _ensure_qapp()

    class FailingStopMouseListener(FakeMouseListener):
        def stop(self) -> None:
            raise RuntimeError("stop() error with sensitive token secret-12345")

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=lambda **kw: FailingStopMouseListener(**kw),
    )

    failed_messages: list[str] = []
    bridge.failed.connect(failed_messages.append)

    assert bridge.start("x1") is True
    bridge.stop()

    assert failed_messages == [BACKEND_FAILURE_MESSAGE]
    assert bridge.last_error == BACKEND_FAILURE_MESSAGE
    assert "secret-12345" not in (bridge.last_error or "")
    assert "RuntimeError" not in (bridge.last_error or "")

    # Subsequent stop call is an idempotent no-op
    bridge.stop()
    assert failed_messages == [BACKEND_FAILURE_MESSAGE]


def test_join_failure_emits_sanitized_failed_and_remains_idempotent() -> None:
    _ensure_qapp()

    class FailingJoinMouseListener(FakeMouseListener):
        def join(self, timeout: float | None = None) -> None:
            raise RuntimeError("join() error with sensitive token secret-67890")

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=lambda **kw: FailingJoinMouseListener(**kw),
    )

    failed_messages: list[str] = []
    bridge.failed.connect(failed_messages.append)

    assert bridge.start("x1") is True
    bridge.stop()

    assert failed_messages == [BACKEND_FAILURE_MESSAGE]
    assert bridge.last_error == BACKEND_FAILURE_MESSAGE
    assert "secret-67890" not in (bridge.last_error or "")
    assert "RuntimeError" not in (bridge.last_error or "")

    # Subsequent stop call is an idempotent no-op
    bridge.stop()
    assert failed_messages == [BACKEND_FAILURE_MESSAGE]


def test_interleaved_stop_during_start_cleans_listener_and_returns_false() -> None:
    _ensure_qapp()
    created_listeners: list[FakeMouseListener] = []

    class InterleavingStopMouseListener(FakeMouseListener):
        def __init__(self, bridge_ref: MouseShortcutBridge, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.bridge_ref = bridge_ref

        def start(self) -> None:
            self.bridge_ref.stop()
            super().start()

    bridge: MouseShortcutBridge | None = None

    def factory(**kwargs: Any) -> MouseListenerLike:
        assert bridge is not None
        listener = InterleavingStopMouseListener(bridge_ref=bridge, **kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    assert bridge.start("x1") is False
    assert len(created_listeners) == 1
    stale_listener = created_listeners[0]
    assert stale_listener.started is True
    assert stale_listener.stopped is True
    assert stale_listener.joined is True
    assert stale_listener.is_alive() is False
    assert bridge._listener is None


def test_interleaved_new_start_during_start_cleans_old_listener_and_preserves_new() -> None:
    _ensure_qapp()
    created_listeners: list[FakeMouseListener] = []

    class InterleavingReconfigMouseListener(FakeMouseListener):
        def __init__(self, bridge_ref: MouseShortcutBridge, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.bridge_ref = bridge_ref
            self.triggered = False

        def start(self) -> None:
            if not self.triggered:
                self.triggered = True
                self.bridge_ref.start("x2")
            super().start()

    bridge: MouseShortcutBridge | None = None

    def factory(**kwargs: Any) -> MouseListenerLike:
        assert bridge is not None
        if not created_listeners:
            listener = InterleavingReconfigMouseListener(bridge_ref=bridge, **kwargs)
        else:
            listener = FakeMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    activated_events: list[str] = []
    bridge.activated.connect(lambda: activated_events.append("activated"))

    assert bridge.start("x1") is False
    assert len(created_listeners) == 2
    old_listener = created_listeners[0]
    new_listener = created_listeners[1]

    assert old_listener.started is True
    assert old_listener.stopped is True
    assert old_listener.joined is True
    assert old_listener.is_alive() is False

    assert new_listener.started is True
    assert new_listener.stopped is False
    assert new_listener.is_alive() is True
    assert bridge._listener is new_listener

    old_listener.on_click(10, 10, "x1", True)
    assert len(activated_events) == 0

    new_listener.on_click(10, 10, "x2", True)
    assert len(activated_events) == 1


def test_interleaved_stop_during_begin_capture_cleans_listener_and_returns_false() -> None:
    _ensure_qapp()
    created_listeners: list[FakeMouseListener] = []

    class InterleavingStopCaptureListener(FakeMouseListener):
        def __init__(self, bridge_ref: MouseShortcutBridge, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.bridge_ref = bridge_ref

        def start(self) -> None:
            self.bridge_ref.stop()
            super().start()

    bridge: MouseShortcutBridge | None = None

    def factory(**kwargs: Any) -> MouseListenerLike:
        assert bridge is not None
        listener = InterleavingStopCaptureListener(bridge_ref=bridge, **kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    assert bridge.begin_capture() is False
    assert len(created_listeners) == 1
    stale_listener = created_listeners[0]
    assert stale_listener.started is True
    assert stale_listener.stopped is True
    assert stale_listener.joined is True
    assert stale_listener.is_alive() is False
    assert bridge._listener is None


def test_interleaved_new_capture_during_begin_capture_cleans_old_listener_and_preserves_new() -> None:
    _ensure_qapp()
    created_listeners: list[FakeMouseListener] = []

    class InterleavingCaptureReconfigListener(FakeMouseListener):
        def __init__(self, bridge_ref: MouseShortcutBridge, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.bridge_ref = bridge_ref
            self.triggered = False

        def start(self) -> None:
            if not self.triggered:
                self.triggered = True
                self.bridge_ref.begin_capture()
            super().start()

    bridge: MouseShortcutBridge | None = None

    def factory(**kwargs: Any) -> MouseListenerLike:
        assert bridge is not None
        if not created_listeners:
            listener = InterleavingCaptureReconfigListener(bridge_ref=bridge, **kwargs)
        else:
            listener = FakeMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    captured_buttons: list[str] = []
    bridge.button_captured.connect(captured_buttons.append)

    assert bridge.begin_capture() is False
    assert len(created_listeners) == 2
    old_listener = created_listeners[0]
    new_listener = created_listeners[1]

    assert old_listener.started is True
    assert old_listener.stopped is True
    assert old_listener.joined is True
    assert old_listener.is_alive() is False

    assert new_listener.started is True
    assert new_listener.stopped is False
    assert new_listener.is_alive() is True
    assert bridge._listener is new_listener

    old_listener.on_click(10, 10, "x1", True)
    assert captured_buttons == []

    new_listener.on_click(10, 10, "x2", True)
    _drain_events(bridge, condition=lambda: new_listener.stopped)
    assert captured_buttons == ["x2"]
    assert new_listener.stopped is True


def test_interleaved_cleanup_failure_after_stale_start_emits_sanitized_failed() -> None:
    _ensure_qapp()

    class FailingStopInterleavingListener(FakeMouseListener):
        def __init__(self, bridge_ref: MouseShortcutBridge, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.bridge_ref = bridge_ref

        def start(self) -> None:
            self.bridge_ref.stop()
            super().start()

        def stop(self) -> None:
            super().stop()
            raise RuntimeError("stop() failure with secret token 999")

    bridge: MouseShortcutBridge | None = None

    def factory(**kwargs: Any) -> MouseListenerLike:
        assert bridge is not None
        return FailingStopInterleavingListener(bridge_ref=bridge, **kwargs)

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    failed_messages: list[str] = []
    bridge.failed.connect(failed_messages.append)

    assert bridge.start("x1") is False
    assert BACKEND_FAILURE_MESSAGE in failed_messages
    assert bridge.last_error == BACKEND_FAILURE_MESSAGE
    assert "999" not in (bridge.last_error or "")
    assert "RuntimeError" not in (bridge.last_error or "")


def test_synchronous_capture_click_during_start_stops_cleanly() -> None:
    _ensure_qapp()
    created_listeners: list[FakeMouseListener] = []

    class SynchronousClickCaptureListener(FakeMouseListener):
        def start(self) -> None:
            super().start()
            if self.on_click is not None:
                self.on_click(10, 10, "x1", True)

    def factory(**kwargs: Any) -> MouseListenerLike:
        listener = SynchronousClickCaptureListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    captured_buttons: list[str] = []
    bridge.button_captured.connect(captured_buttons.append)

    assert bridge.begin_capture() is True
    assert len(created_listeners) == 1
    listener = created_listeners[0]
    assert listener.started is True
    assert captured_buttons == ["x1"]
    assert listener.stopped is False
    assert listener.joined is False
    assert bridge._listener is listener

    _drain_events(bridge, condition=lambda: listener.stopped and bridge._listener is None)
    assert listener.stopped is True
    assert listener.joined is True
    assert bridge._listener is None
    assert captured_buttons == ["x1"]


def test_concurrent_stop_blocks_until_startup_finishes_and_cleans_listener() -> None:
    _ensure_qapp()
    startup_started = threading.Event()
    allow_startup_to_finish = threading.Event()
    stop_attempted = threading.Event()
    stop_finished = threading.Event()
    created_listeners: list[FakeMouseListener] = []

    class BlockingStartupMouseListener(FakeMouseListener):
        def start(self) -> None:
            startup_started.set()
            allow_startup_to_finish.wait(timeout=2.0)
            super().start()

    def factory(**kwargs: Any) -> MouseListenerLike:
        listener = BlockingStartupMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    start_result: list[bool] = []

    def run_start() -> None:
        res = bridge.start("x1")
        start_result.append(res)

    t_start = threading.Thread(target=run_start)
    t_start.start()

    assert startup_started.wait(timeout=2.0) is True

    def run_stop() -> None:
        stop_attempted.set()
        bridge.stop()
        stop_finished.set()

    t_stop = threading.Thread(target=run_stop)
    t_stop.start()

    assert stop_attempted.wait(timeout=2.0) is True
    assert stop_finished.is_set() is False

    allow_startup_to_finish.set()

    t_start.join(timeout=2.0)
    t_stop.join(timeout=2.0)

    assert not t_start.is_alive()
    assert not t_stop.is_alive()
    assert len(created_listeners) == 1
    listener = created_listeners[0]
    assert listener.started is True
    assert listener.stopped is True
    assert listener.joined is True
    assert bridge._listener is None


def test_concurrent_reconfiguration_waits_for_startup_and_cleans_old_listener_before_binding() -> None:
    _ensure_qapp()
    first_startup_started = threading.Event()
    allow_first_startup_to_finish = threading.Event()
    second_start_attempted = threading.Event()
    second_start_finished = threading.Event()
    created_listeners: list[FakeMouseListener] = []

    class BlockingStartupMouseListener(FakeMouseListener):
        def start(self) -> None:
            first_startup_started.set()
            allow_first_startup_to_finish.wait(timeout=2.0)
            super().start()

    def factory(**kwargs: Any) -> MouseListenerLike:
        if not created_listeners:
            listener = BlockingStartupMouseListener(**kwargs)
        else:
            listener = FakeMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    t1 = threading.Thread(target=lambda: bridge.start("x1"))
    t1.start()

    assert first_startup_started.wait(timeout=2.0) is True

    def run_start_x2() -> None:
        second_start_attempted.set()
        bridge.start("x2")
        second_start_finished.set()

    t2 = threading.Thread(target=run_start_x2)
    t2.start()

    assert second_start_attempted.wait(timeout=2.0) is True
    assert second_start_finished.is_set() is False

    allow_first_startup_to_finish.set()

    t1.join(timeout=2.0)
    t2.join(timeout=2.0)

    assert not t1.is_alive()
    assert not t2.is_alive()
    assert len(created_listeners) == 2
    old_listener = created_listeners[0]
    new_listener = created_listeners[1]

    assert old_listener.started is True
    assert old_listener.stopped is True
    assert old_listener.joined is True

    assert new_listener.started is True
    assert new_listener.stopped is False
    assert bridge._listener is new_listener


def test_concurrent_stop_during_begin_capture_waits_for_startup_and_cleans_listener() -> None:
    _ensure_qapp()
    startup_started = threading.Event()
    allow_startup_to_finish = threading.Event()
    stop_attempted = threading.Event()
    stop_finished = threading.Event()
    created_listeners: list[FakeMouseListener] = []

    class BlockingStartupCaptureListener(FakeMouseListener):
        def start(self) -> None:
            startup_started.set()
            allow_startup_to_finish.wait(timeout=2.0)
            super().start()

    def factory(**kwargs: Any) -> MouseListenerLike:
        listener = BlockingStartupCaptureListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    t_capture = threading.Thread(target=lambda: bridge.begin_capture())
    t_capture.start()

    assert startup_started.wait(timeout=2.0) is True

    def run_stop() -> None:
        stop_attempted.set()
        bridge.stop()
        stop_finished.set()

    t_stop = threading.Thread(target=run_stop)
    t_stop.start()

    assert stop_attempted.wait(timeout=2.0) is True
    assert stop_finished.is_set() is False

    allow_startup_to_finish.set()

    t_capture.join(timeout=2.0)
    t_stop.join(timeout=2.0)

    assert not t_capture.is_alive()
    assert not t_stop.is_alive()
    assert len(created_listeners) == 1
    listener = created_listeners[0]
    assert listener.started is True
    assert listener.stopped is True
    assert listener.joined is True
    assert bridge._listener is None


def test_concurrent_start_while_previous_listener_join_blocks_serializes_and_preserves_last_active() -> None:
    _ensure_qapp()
    join_started = threading.Event()
    allow_join = threading.Event()
    created_listeners: list[FakeMouseListener] = []

    class BlockingJoinMouseListener(FakeMouseListener):
        def join(self, timeout: float | None = None) -> None:
            join_started.set()
            allow_join.wait(timeout=2.0)
            super().join(timeout=timeout)

    def factory(**kwargs: Any) -> MouseListenerLike:
        if not created_listeners:
            listener = BlockingJoinMouseListener(**kwargs)
        else:
            listener = FakeMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    activated_events: list[str] = []
    bridge.activated.connect(lambda: activated_events.append("activated"))

    # Start initial listener (index 0)
    assert bridge.start("x1") is True
    assert len(created_listeners) == 1
    listener_0 = created_listeners[0]
    assert listener_0.started is True
    assert listener_0.stopped is False

    # Start first transition in background thread (will block cleaning up listener_0's join)
    t1_started = threading.Event()
    t1_finished = threading.Event()

    def run_start_x2() -> None:
        t1_started.set()
        bridge.start("x2")
        t1_finished.set()

    t1 = threading.Thread(target=run_start_x2)
    t1.start()

    # Wait until listener_0 is being joined inside bridge._lock
    assert join_started.wait(timeout=2.0) is True

    # Start second concurrent transition in another background thread
    t2_started = threading.Event()
    t2_finished = threading.Event()

    def run_start_x3() -> None:
        t2_started.set()
        bridge.start("x3")
        t2_finished.set()

    t2 = threading.Thread(target=run_start_x3)
    t2.start()

    assert t2_started.wait(timeout=2.0) is True
    # Neither t1 nor t2 should have finished while join is blocked
    assert t1_finished.is_set() is False
    assert t2_finished.is_set() is False

    # Release blocked join of listener_0
    allow_join.set()

    t1.join(timeout=2.0)
    t2.join(timeout=2.0)

    assert not t1.is_alive()
    assert not t2.is_alive()
    assert len(created_listeners) == 3

    listener_0 = created_listeners[0]
    listener_1 = created_listeners[1]
    listener_2 = created_listeners[2]

    # Listener 0 was stopped and joined
    assert listener_0.started is True
    assert listener_0.stopped is True
    assert listener_0.joined is True

    # Listener 1 was started then stopped and joined by transition 2
    assert listener_1.started is True
    assert listener_1.stopped is True
    assert listener_1.joined is True

    # Listener 2 is the only active listener
    assert listener_2.started is True
    assert listener_2.stopped is False
    assert listener_2.is_alive() is True
    assert bridge._listener is listener_2

    # Events from superseded listeners are ignored
    listener_0.on_click(10, 10, "x1", True)
    listener_1.on_click(10, 10, "x2", True)
    assert len(activated_events) == 0

    # Event from active listener 2 triggers activation
    listener_2.on_click(10, 10, "x3", True)
    assert len(activated_events) == 1


def test_concurrent_begin_capture_while_previous_listener_join_blocks_serializes_and_preserves_last_active() -> None:
    _ensure_qapp()
    join_started = threading.Event()
    allow_join = threading.Event()
    created_listeners: list[FakeMouseListener] = []

    class BlockingJoinMouseListener(FakeMouseListener):
        def join(self, timeout: float | None = None) -> None:
            join_started.set()
            allow_join.wait(timeout=2.0)
            super().join(timeout=timeout)

    def factory(**kwargs: Any) -> MouseListenerLike:
        if not created_listeners:
            listener = BlockingJoinMouseListener(**kwargs)
        else:
            listener = FakeMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    captured_buttons: list[str] = []
    bridge.button_captured.connect(captured_buttons.append)

    # Start initial listener (index 0)
    assert bridge.start("x1") is True
    assert len(created_listeners) == 1
    listener_0 = created_listeners[0]
    assert listener_0.started is True
    assert listener_0.stopped is False

    # Start first capture transition in background thread
    t1_started = threading.Event()
    t1_finished = threading.Event()

    def run_capture_1() -> None:
        t1_started.set()
        bridge.begin_capture()
        t1_finished.set()

    t1 = threading.Thread(target=run_capture_1)
    t1.start()

    # Wait until listener_0 is being joined inside bridge._lock
    assert join_started.wait(timeout=2.0) is True

    # Start second concurrent capture transition in another background thread
    t2_started = threading.Event()
    t2_finished = threading.Event()

    def run_capture_2() -> None:
        t2_started.set()
        bridge.begin_capture()
        t2_finished.set()

    t2 = threading.Thread(target=run_capture_2)
    t2.start()

    assert t2_started.wait(timeout=2.0) is True
    assert t1_finished.is_set() is False
    assert t2_finished.is_set() is False

    # Release blocked join of listener_0
    allow_join.set()

    t1.join(timeout=2.0)
    t2.join(timeout=2.0)

    assert not t1.is_alive()
    assert not t2.is_alive()
    assert len(created_listeners) == 3

    listener_0 = created_listeners[0]
    capture_1 = created_listeners[1]
    capture_2 = created_listeners[2]

    assert listener_0.started is True
    assert listener_0.stopped is True
    assert listener_0.joined is True

    assert capture_1.started is True
    assert capture_1.stopped is True
    assert capture_1.joined is True

    assert capture_2.started is True
    assert capture_2.stopped is False
    assert bridge._listener is capture_2

    # Delayed click from capture 1 is ignored
    capture_1.on_click(10, 10, "x2", True)
    assert captured_buttons == []

    # Click on active capture 2 captures and stops cleanly
    capture_2.on_click(10, 10, "x3", True)
    _drain_events(bridge, condition=lambda: capture_2.stopped)
    assert captured_buttons == ["x3"]
    assert capture_2.stopped is True


def test_capture_callback_does_not_join_synchronously_during_callback() -> None:
    _ensure_qapp()
    created_listeners: list[FakeMouseListener] = []

    class NoJoinInCallbackListener(FakeMouseListener):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.in_callback = False
            self.joined_during_callback = False
            self.start_returned = threading.Event()

        def start(self) -> None:
            super().start()
            if self.on_click is not None:
                self.in_callback = True
                try:
                    self.on_click(10, 10, "x1", True)
                finally:
                    self.in_callback = False
            self.start_returned.set()

        def join(self, timeout: float | None = None) -> None:
            if self.in_callback:
                self.joined_during_callback = True
            super().join(timeout=timeout)

    def factory(**kwargs: Any) -> MouseListenerLike:
        listener = NoJoinInCallbackListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    captured_buttons: list[str] = []
    bridge.button_captured.connect(captured_buttons.append)

    bridge.begin_capture()
    _drain_events(bridge, condition=lambda: len(created_listeners) == 1 and created_listeners[0].stopped)

    assert len(created_listeners) == 1
    listener = created_listeners[0]
    assert captured_buttons == ["x1"]
    assert listener.joined_during_callback is False
    assert listener.stopped is True
    assert listener.joined is True


def test_button_captured_slot_calling_start_preserves_new_listener() -> None:
    _ensure_qapp()
    created_listeners: list[FakeMouseListener] = []

    def factory(**kwargs: Any) -> MouseListenerLike:
        listener = FakeMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    activated_count = 0

    def on_activated() -> None:
        nonlocal activated_count
        activated_count += 1

    bridge.activated.connect(on_activated)

    def on_captured(button: str) -> None:
        bridge.start("x2")

    bridge.button_captured.connect(on_captured)

    assert bridge.begin_capture() is True
    assert len(created_listeners) == 1
    capture_listener = created_listeners[0]
    assert capture_listener.started is True
    assert capture_listener.stopped is False

    # Simulate button press during capture; slot connects and synchronously starts 'x2'
    capture_listener.on_click(50, 50, "Button.x1", True)
    _drain_events(bridge, condition=lambda: len(created_listeners) >= 2 and created_listeners[0].stopped)

    assert len(created_listeners) == 2
    old_capture_listener = created_listeners[0]
    new_active_listener = created_listeners[1]

    # Old capture listener was cleanly stopped/joined
    assert old_capture_listener.stopped is True
    assert old_capture_listener.joined is True

    # New listener started by the slot remains active and is not killed by capture cleanup
    assert new_active_listener.started is True
    assert new_active_listener.stopped is False
    assert new_active_listener.is_alive() is True
    assert bridge._listener is new_active_listener

    # Triggering x2 on new active listener produces activation
    new_active_listener.on_click(50, 50, "Button.x2", True)
    assert activated_count == 1

    # Stale capture listener does not produce activation
    old_capture_listener.on_click(50, 50, "Button.x1", True)
    assert activated_count == 1


def test_factory_raising_typeerror_is_called_once_with_suppress_and_fails_sanitized() -> None:
    _ensure_qapp()
    factory_calls: list[dict[str, Any]] = []

    def failing_factory(**kwargs: Any) -> MouseListenerLike:
        factory_calls.append(kwargs)
        raise TypeError("Listener factory unexpected keyword argument 'suppress'")

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=failing_factory,
    )

    failed_messages: list[str] = []
    bridge.failed.connect(failed_messages.append)

    assert bridge.start("x1") is False
    assert len(factory_calls) == 1
    assert factory_calls[0].get("suppress") is False
    assert failed_messages == [BACKEND_FAILURE_MESSAGE]
    assert bridge.last_error == BACKEND_FAILURE_MESSAGE
    assert "TypeError" not in (bridge.last_error or "")
    assert "suppress" not in (bridge.last_error or "")

    # Also verify for begin_capture
    assert bridge.begin_capture() is False
    assert len(factory_calls) == 2
    assert factory_calls[1].get("suppress") is False
    assert failed_messages == [BACKEND_FAILURE_MESSAGE, BACKEND_FAILURE_MESSAGE]


def test_callback_returns_non_blocking_during_lifecycle_join_and_discards_stale_event() -> None:
    _ensure_qapp()
    join_started = threading.Event()
    allow_join = threading.Event()
    created_listeners: list[FakeMouseListener] = []

    class BlockingJoinMouseListener(FakeMouseListener):
        def join(self, timeout: float | None = None) -> None:
            join_started.set()
            allow_join.wait(timeout=2.0)
            super().join(timeout=timeout)

    def factory(**kwargs: Any) -> MouseListenerLike:
        if not created_listeners:
            listener = BlockingJoinMouseListener(**kwargs)
        else:
            listener = FakeMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    activated_events: list[str] = []
    bridge.activated.connect(lambda: activated_events.append("activated"))

    # Start initial listener (index 0)
    assert bridge.start("x1") is True
    assert len(created_listeners) == 1
    listener_0 = created_listeners[0]
    assert listener_0.started is True
    assert listener_0.stopped is False

    # Transition to x2 in background thread; blocks inside listener_0.join() holding lifecycle lock
    t_transition_started = threading.Event()
    t_transition_finished = threading.Event()

    def run_transition() -> None:
        t_transition_started.set()
        bridge.start("x2")
        t_transition_finished.set()

    t_transition = threading.Thread(target=run_transition)
    t_transition.start()

    assert join_started.wait(timeout=2.0) is True
    assert t_transition_finished.is_set() is False

    # While lifecycle lock is held during join(), invoke listener_0's callback from a separate thread
    callback_started = threading.Event()
    callback_returned = threading.Event()

    def run_callback() -> None:
        callback_started.set()
        listener_0.on_click(10, 10, "x1", True)
        callback_returned.set()

    t_callback = threading.Thread(target=run_callback)
    t_callback.start()

    assert callback_started.wait(timeout=2.0) is True
    # Callback must return immediately without waiting for allow_join or lifecycle lock
    assert callback_returned.wait(timeout=0.3) is True
    t_callback.join(timeout=0.5)
    assert not t_callback.is_alive()

    # Since listener_0 was already invalidated by transition, no activation is emitted
    assert len(activated_events) == 0

    # Transition is still blocked in join
    assert t_transition_finished.is_set() is False

    # Unblock join and allow transition to finish
    allow_join.set()
    t_transition.join(timeout=2.0)
    assert not t_transition.is_alive()

    assert len(created_listeners) == 2
    listener_1 = created_listeners[1]
    assert listener_1.started is True
    assert listener_1.stopped is False
    assert bridge._listener is listener_1

    # New binding works and triggers activation
    listener_1.on_click(10, 10, "x2", True)
    assert len(activated_events) == 1

    # Old listener click continues to be discarded
    listener_0.on_click(10, 10, "x1", True)
    assert len(activated_events) == 1


def test_callback_returns_non_blocking_during_lifecycle_start() -> None:
    _ensure_qapp()
    start_entered = threading.Event()
    allow_start = threading.Event()
    created_listeners: list[FakeMouseListener] = []

    class BlockingStartMouseListener(FakeMouseListener):
        def start(self) -> None:
            start_entered.set()
            allow_start.wait(timeout=2.0)
            super().start()

    def factory(**kwargs: Any) -> MouseListenerLike:
        listener = BlockingStartMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    activated_events: list[str] = []
    bridge.activated.connect(lambda: activated_events.append("activated"))

    t_start_started = threading.Event()
    t_start_finished = threading.Event()

    def run_start() -> None:
        t_start_started.set()
        bridge.start("x1")
        t_start_finished.set()

    t_start = threading.Thread(target=run_start)
    t_start.start()

    assert start_entered.wait(timeout=2.0) is True
    assert t_start_finished.is_set() is False

    # Invoke callback on the listener being started while start() is blocking holding lifecycle lock
    assert len(created_listeners) == 1
    listener = created_listeners[0]

    callback_started = threading.Event()
    callback_returned = threading.Event()

    def run_callback() -> None:
        callback_started.set()
        listener.on_click(10, 10, "x1", True)
        callback_returned.set()

    t_callback = threading.Thread(target=run_callback)
    t_callback.start()

    assert callback_started.wait(timeout=2.0) is True
    # Callback must return immediately without waiting for allow_start or lock
    assert callback_returned.wait(timeout=0.3) is True
    t_callback.join(timeout=0.5)
    assert not t_callback.is_alive()

    # Callback executes and emits activation without blocking
    _drain_events(bridge, condition=lambda: len(activated_events) == 1)
    assert len(activated_events) == 1
    # Release start
    allow_start.set()
    t_start.join(timeout=2.0)
    assert not t_start.is_alive()
    assert bridge._listener is listener


def test_capture_callback_returns_non_blocking_during_lifecycle_join_and_discards_stale_event() -> None:
    _ensure_qapp()
    join_started = threading.Event()
    allow_join = threading.Event()
    created_listeners: list[FakeMouseListener] = []

    class BlockingJoinMouseListener(FakeMouseListener):
        def join(self, timeout: float | None = None) -> None:
            join_started.set()
            allow_join.wait(timeout=2.0)
            super().join(timeout=timeout)

    def factory(**kwargs: Any) -> MouseListenerLike:
        if not created_listeners:
            listener = BlockingJoinMouseListener(**kwargs)
        else:
            listener = FakeMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    captured_buttons: list[str] = []
    bridge.button_captured.connect(captured_buttons.append)

    # Start initial listener (index 0)
    assert bridge.start("x1") is True
    assert len(created_listeners) == 1
    listener_0 = created_listeners[0]

    # Transition to begin_capture in background thread; blocks in listener_0.join()
    t_capture_started = threading.Event()
    t_capture_finished = threading.Event()

    def run_capture() -> None:
        t_capture_started.set()
        bridge.begin_capture()
        t_capture_finished.set()

    t_capture = threading.Thread(target=run_capture)
    t_capture.start()

    assert join_started.wait(timeout=2.0) is True
    assert t_capture_finished.is_set() is False

    # While lifecycle lock is held during join(), invoke listener_0's callback from a separate thread
    callback_started = threading.Event()
    callback_returned = threading.Event()

    def run_callback() -> None:
        callback_started.set()
        listener_0.on_click(10, 10, "x1", True)
        callback_returned.set()

    t_callback = threading.Thread(target=run_callback)
    t_callback.start()

    assert callback_started.wait(timeout=2.0) is True
    # Callback must return immediately without waiting for allow_join or lock
    assert callback_returned.wait(timeout=0.3) is True
    t_callback.join(timeout=0.5)
    assert not t_callback.is_alive()

    # No button captured from the stale listener
    assert captured_buttons == []

    # Allow join to finish
    allow_join.set()
    t_capture.join(timeout=2.0)
    assert not t_capture.is_alive()

    assert len(created_listeners) == 2
    capture_listener = created_listeners[1]
    assert capture_listener.started is True
    assert capture_listener.stopped is False
    assert bridge._listener is capture_listener

    # Valid click on capture listener captures button and stops
    capture_listener.on_click(10, 10, "x2", True)
    _drain_events(bridge, condition=lambda: capture_listener.stopped)
    assert captured_buttons == ["x2"]
    assert capture_listener.stopped is True

def test_activated_event_emitted_before_activated_with_generation_and_coordinates() -> None:
    _ensure_qapp()
    created_listeners: list[FakeMouseListener] = []

    def factory(**kwargs: Any) -> MouseListenerLike:
        listener = FakeMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    emitted_order: list[str] = []
    received_events: list[tuple[int, int, int]] = []

    bridge._activated_event.connect(
        lambda gen, x, y: (emitted_order.append("_activated_event"), received_events.append((gen, x, y)))
    )
    bridge.activated.connect(lambda: emitted_order.append("activated"))

    assert bridge.start("x1") is True
    active_gen = bridge.generation

    listener = created_listeners[0]
    # 1. Normal integer coordinates
    listener.on_click(150, 250, "x1", True)

    assert emitted_order == ["_activated_event", "activated"]
    assert received_events == [(active_gen, 150, 250)]

    # 2. Float and alias coordinates
    listener.on_click(300.7, 400.2, "Button.button8", True)
    assert emitted_order == ["_activated_event", "activated", "_activated_event", "activated"]
    assert received_events == [(active_gen, 150, 250), (active_gen, 300, 400)]

    # 3. Non-numeric fallback to 0
    listener.on_click("invalid", None, "x1", True)
    assert emitted_order == [
        "_activated_event",
        "activated",
        "_activated_event",
        "activated",
        "_activated_event",
        "activated",
    ]
    assert received_events == [
        (active_gen, 150, 250),
        (active_gen, 300, 400),
        (active_gen, 0, 0),
    ]


def test_button_captured_event_emitted_before_button_captured_with_generation_canonical_and_coordinates() -> None:
    _ensure_qapp()
    created_listeners: list[FakeMouseListener] = []

    def factory(**kwargs: Any) -> MouseListenerLike:
        listener = FakeMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    emitted_order: list[str] = []
    received_events: list[tuple[int, str, int, int]] = []
    captured_buttons: list[str] = []

    bridge._button_captured_event.connect(
        lambda gen, name, x, y: (
            emitted_order.append("_button_captured_event"),
            received_events.append((gen, name, x, y)),
        )
    )
    bridge.button_captured.connect(
        lambda name: (emitted_order.append("button_captured"), captured_buttons.append(name))
    )

    assert bridge.begin_capture() is True
    capture_gen = bridge.generation

    listener = created_listeners[0]
    listener.on_click(300, 450, "Button.button8", True)
    _drain_events(bridge, condition=lambda: listener.stopped)

    assert emitted_order == [
        "_button_captured_event",
        "button_captured",
    ]
    assert received_events == [(capture_gen, "x1", 300, 450)]
    assert captured_buttons == ["x1"]


def test_activated_event_generation_matches_property_generation() -> None:
    _ensure_qapp()
    created_listeners: list[FakeMouseListener] = []

    def factory(**kwargs: Any) -> MouseListenerLike:
        listener = FakeMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    assert isinstance(bridge.generation, int)
    initial_gen = bridge.generation

    received_generations: list[int] = []

    bridge._activated_event.connect(
        lambda gen, x, y: received_generations.append(gen)
    )

    assert bridge.start("x1") is True
    active_gen = bridge.generation
    assert active_gen > initial_gen

    listener = created_listeners[0]
    listener.on_click(100, 200, "x1", True)

    assert received_generations == [active_gen]

    # Stop increments generation
    bridge.stop()
    stopped_gen = bridge.generation
    assert stopped_gen > active_gen

    # New start creates a new generation
    assert bridge.start("x2") is True
    new_active_gen = bridge.generation
    assert new_active_gen > stopped_gen

    listener2 = created_listeners[1]
    listener2.on_click(100, 200, "x2", True)

    assert received_generations == [active_gen, new_active_gen]


def test_paused_old_capture_callback_after_stop_and_new_capture_does_not_lose_new_click() -> None:
    _ensure_qapp()
    created_listeners: list[FakeMouseListener] = []

    def factory(**kwargs: Any) -> MouseListenerLike:
        listener = FakeMouseListener(**kwargs)
        created_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=factory,
    )

    captured_events: list[tuple[int, str, int, int]] = []
    bridge._button_captured_event.connect(
        lambda gen, name, x, y: captured_events.append((gen, name, x, y))
    )

    # 1. Inicia primeira captura (geração 1)
    assert bridge.begin_capture() is True
    gen1 = bridge.generation
    assert len(created_listeners) == 1
    old_listener = created_listeners[0]

    # 2. Primeira captura é interrompida (stop ou timeout)
    bridge.stop()
    assert bridge.generation > gen1

    # 3. Inicia segunda captura (nova geração)
    assert bridge.begin_capture() is True
    gen2 = bridge.generation
    assert gen2 > gen1
    assert len(created_listeners) == 2
    new_listener = created_listeners[1]

    # 4. Callback antigo do listener 1 roda tardiamente (após ter pausado/atrasado)
    old_listener.on_click(100, 200, "x1", True)
    _drain_events(bridge)

    # O callback antigo não deve ter emitido evento e não deve ter desarmado a captura ativa do bridge
    assert len(captured_events) == 0

    # 5. Novo clique ocorre no listener 2 da captura ativa
    new_listener.on_click(300, 400, "x2", True)
    _drain_events(bridge, condition=lambda: new_listener.stopped)

    # O novo clique deve ser capturado com sucesso com a nova geração
    assert captured_events == [(gen2, "x2", 300, 400)]
