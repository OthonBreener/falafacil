from __future__ import annotations

import subprocess

from falafacil.terminal import TerminalBridge


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
