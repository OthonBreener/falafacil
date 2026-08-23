from __future__ import annotations

import pytest
import subprocess

from falafacil.terminal import TerminalBridge, TerminalBridgeError


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command[1] == "getactivewindow":
            return subprocess.CompletedProcess(command, 0, stdout="123\n", stderr="")
        if command[1] == "getwindowpid":
            return subprocess.CompletedProcess(command, 0, stdout="456\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def test_send_text_pastes_into_recognized_active_terminal() -> None:
    runner = Runner()
    clipboard: list[str] = []
    bridge = TerminalBridge(
        env={"XDG_SESSION_TYPE": "x11"},
        which=lambda name: "/usr/bin/xdotool",
        run=runner,
        read_comm=lambda pid: "gnome-terminal-server\n",
    )

    bridge.send_text("Olá terminal", clipboard.append)

    assert clipboard == ["Olá terminal"]
    assert runner.calls[-1][0] == [
        "/usr/bin/xdotool",
        "key",
        "--window",
        "123",
        "--clearmodifiers",
        "ctrl+shift+v",
    ]


def test_non_terminal_window_is_rejected() -> None:
    runner = Runner()
    bridge = TerminalBridge(
        env={"XDG_SESSION_TYPE": "x11"},
        which=lambda name: "/usr/bin/xdotool",
        run=runner,
        read_comm=lambda pid: "code\n",
    )

    assert bridge.detect_active_terminal() is None
    assert "não é um terminal" in bridge.last_reason


def test_wayland_does_not_call_xdotool() -> None:
    runner = Runner()
    bridge = TerminalBridge(
        env={"XDG_SESSION_TYPE": "wayland"},
        which=lambda name: "/usr/bin/xdotool",
        run=runner,
        read_comm=lambda pid: "gnome-terminal-server\n",
    )

    assert bridge.detect_active_terminal() is None
    assert runner.calls == []
    assert "X11" in bridge.last_reason


def test_detect_active_terminal_failure_omits_raw_exception_and_secret() -> None:
    secret = "secret-token-xdotool-detect-7777"

    def failing_run(command, **kwargs):
        raise subprocess.SubprocessError(f"xdotool getactivewindow failed with {secret}")

    bridge = TerminalBridge(
        env={"XDG_SESSION_TYPE": "x11"},
        which=lambda name: "/usr/bin/xdotool",
        run=failing_run,
        read_comm=lambda pid: "gnome-terminal-server\n",
    )

    assert bridge.detect_active_terminal() is None
    assert bridge.last_reason == "Não foi possível identificar a janela ativa."
    assert secret not in bridge.last_reason
    assert "SubprocessError" not in bridge.last_reason


def test_send_text_failure_omits_raw_exception_and_secret() -> None:
    secret = "secret-token-xdotool-paste-8888"

    def run_with_failing_key(command, **kwargs):
        if command[1] == "getactivewindow":
            return subprocess.CompletedProcess(command, 0, stdout="123\n", stderr="")
        if command[1] == "getwindowpid":
            return subprocess.CompletedProcess(command, 0, stdout="456\n", stderr="")
        if command[1] == "key":
            raise subprocess.SubprocessError(f"xdotool key failed with {secret}")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    clipboard: list[str] = []
    bridge = TerminalBridge(
        env={"XDG_SESSION_TYPE": "x11"},
        which=lambda name: "/usr/bin/xdotool",
        run=run_with_failing_key,
        read_comm=lambda pid: "gnome-terminal-server\n",
    )

    with pytest.raises(TerminalBridgeError) as exc_info:
        bridge.send_text("texto para o terminal", clipboard.append)

    message = str(exc_info.value)
    assert message == "Não foi possível colar no terminal."
    assert secret not in message
    assert "SubprocessError" not in message
    assert clipboard == ["texto para o terminal"]
