from __future__ import annotations

import pytest
import subprocess

from falafacil.terminal import TerminalBridge, TerminalBridgeError, TerminalTarget


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


def test_detect_active_terminal_captures_origin_with_pid() -> None:
    runner = Runner()
    bridge = TerminalBridge(
        env={"XDG_SESSION_TYPE": "x11"},
        which=lambda name: "/usr/bin/xdotool",
        run=runner,
        read_comm=lambda pid: "gnome-terminal-server\n",
    )

    target = bridge.detect_active_terminal()

    assert target == TerminalTarget(
        window_id="123",
        pid="456",
        process_name="gnome-terminal-server",
    )
    assert target.window_id == "123"
    assert target.pid == "456"
    assert target.process_name == "gnome-terminal-server"


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
    assert bridge.last_reason == "A colagem automática requer uma sessão X11; use Copiar novamente ou Copiar e arquivar."


def test_detect_active_terminal_without_xdotool_rejects() -> None:
    runner = Runner()
    bridge = TerminalBridge(
        env={"XDG_SESSION_TYPE": "x11"},
        which=lambda name: None,
        run=runner,
        read_comm=lambda pid: "gnome-terminal-server\n",
    )

    assert bridge.detect_active_terminal() is None
    assert runner.calls == []
    assert bridge.last_reason == "xdotool não está instalado; use Copiar novamente ou Copiar e arquivar."


def test_send_text_without_target_in_wayland_rejects() -> None:
    runner = Runner()
    clipboard: list[str] = []
    bridge = TerminalBridge(
        env={"XDG_SESSION_TYPE": "wayland"},
        which=lambda name: "/usr/bin/xdotool",
        run=runner,
        read_comm=lambda pid: "gnome-terminal-server\n",
    )

    with pytest.raises(TerminalBridgeError) as exc_info:
        bridge.send_text("texto", clipboard.append)

    assert str(exc_info.value) == "A colagem automática requer uma sessão X11; use Copiar novamente ou Copiar e arquivar."
    assert runner.calls == []
    assert clipboard == []


def test_send_text_without_target_without_xdotool_rejects() -> None:
    runner = Runner()
    clipboard: list[str] = []
    bridge = TerminalBridge(
        env={"XDG_SESSION_TYPE": "x11"},
        which=lambda name: None,
        run=runner,
        read_comm=lambda pid: "gnome-terminal-server\n",
    )

    with pytest.raises(TerminalBridgeError) as exc_info:
        bridge.send_text("texto", clipboard.append)

    assert str(exc_info.value) == "xdotool não está instalado; use Copiar novamente ou Copiar e arquivar."
    assert runner.calls == []
    assert clipboard == []

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


def test_send_text_with_saved_target_executes_exact_sequence_no_shell_or_enter() -> None:
    timeline: list[str] = []
    commands: list[list[str]] = []

    def tracking_run(command, **kwargs):
        cmd = list(command)
        commands.append(cmd)
        subcmd = cmd[1] if len(cmd) > 1 else ""
        timeline.append(f"run:{subcmd}")
        if subcmd == "getwindowpid":
            return subprocess.CompletedProcess(cmd, 0, stdout="456\n", stderr="")
        if subcmd == "windowactivate":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if subcmd == "getactivewindow":
            return subprocess.CompletedProcess(cmd, 0, stdout="123\n", stderr="")
        if subcmd == "key":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    clipboard: list[str] = []

    def set_clipboard(text: str) -> None:
        timeline.append("set_clipboard")
        clipboard.append(text)

    bridge = TerminalBridge(
        env={"XDG_SESSION_TYPE": "x11"},
        which=lambda name: "/usr/bin/xdotool",
        run=tracking_run,
        read_comm=lambda pid: "gnome-terminal-server\n",
    )
    target = TerminalTarget(
        window_id="123",
        pid="456",
        process_name="gnome-terminal-server",
    )

    bridge.send_text("texto seguro", set_clipboard, target=target)

    assert clipboard == ["texto seguro"]
    assert commands == [
        ["/usr/bin/xdotool", "getwindowpid", "123"],
        ["/usr/bin/xdotool", "windowactivate", "--sync", "123"],
        ["/usr/bin/xdotool", "getactivewindow"],
        ["/usr/bin/xdotool", "key", "--window", "123", "--clearmodifiers", "ctrl+shift+v"],
    ]
    assert timeline == [
        "run:getwindowpid",
        "run:windowactivate",
        "run:getactivewindow",
        "set_clipboard",
        "run:key",
    ]

    for cmd in commands:
        for arg in cmd:
            arg_lower = arg.lower()
            assert "sh" not in arg_lower or "/usr/bin/xdotool" in arg_lower or "ctrl+shift+v" in arg_lower
            assert arg != "-c"
            assert "enter" not in arg_lower
            assert "return" not in arg_lower
            assert "\n" not in arg
            assert "\r" not in arg


def test_send_text_with_saved_target_rejects_changed_pid() -> None:
    calls: list[list[str]] = []
    clipboard: list[str] = []

    def run_with_new_pid(command, **kwargs):
        cmd = list(command)
        calls.append(cmd)
        if cmd[1] == "getwindowpid":
            return subprocess.CompletedProcess(cmd, 0, stdout="999\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    bridge = TerminalBridge(
        env={"XDG_SESSION_TYPE": "x11"},
        which=lambda name: "/usr/bin/xdotool",
        run=run_with_new_pid,
        read_comm=lambda pid: "gnome-terminal-server\n",
    )
    target = TerminalTarget(
        window_id="123",
        pid="456",
        process_name="gnome-terminal-server",
    )

    with pytest.raises(TerminalBridgeError) as exc_info:
        bridge.send_text("texto seguro", clipboard.append, target=target)

    assert "alterado ou encerrado" in str(exc_info.value)
    assert clipboard == []
    assert len(calls) == 1
    assert calls[0][1] == "getwindowpid"


def test_send_text_with_saved_target_rejects_changed_or_unallowed_process() -> None:
    calls: list[list[str]] = []
    clipboard: list[str] = []

    def run_pid(command, **kwargs):
        cmd = list(command)
        calls.append(cmd)
        if cmd[1] == "getwindowpid":
            return subprocess.CompletedProcess(cmd, 0, stdout="456\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    bridge = TerminalBridge(
        env={"XDG_SESSION_TYPE": "x11"},
        which=lambda name: "/usr/bin/xdotool",
        run=run_pid,
        read_comm=lambda pid: "firefox\n",
    )
    target = TerminalTarget(
        window_id="123",
        pid="456",
        process_name="gnome-terminal-server",
    )

    with pytest.raises(TerminalBridgeError) as exc_info:
        bridge.send_text("texto seguro", clipboard.append, target=target)

    assert "não é mais um terminal" in str(exc_info.value)
    assert clipboard == []
    assert len(calls) == 1


def test_send_text_with_saved_target_rejects_process_name_changed_to_another_allowlisted_process() -> None:
    calls: list[list[str]] = []
    clipboard: list[str] = []

    def run_pid(command, **kwargs):
        cmd = list(command)
        calls.append(cmd)
        if cmd[1] == "getwindowpid":
            return subprocess.CompletedProcess(cmd, 0, stdout="456\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    bridge = TerminalBridge(
        env={"XDG_SESSION_TYPE": "x11"},
        which=lambda name: "/usr/bin/xdotool",
        run=run_pid,
        read_comm=lambda pid: "konsole\n",  # different allowlisted process than target's gnome-terminal-server
    )
    target = TerminalTarget(
        window_id="123",
        pid="456",
        process_name="gnome-terminal-server",
    )

    with pytest.raises(TerminalBridgeError) as exc_info:
        bridge.send_text("texto seguro", clipboard.append, target=target)

    assert "não é mais um terminal reconhecido" in str(exc_info.value)
    assert clipboard == []
    assert len(calls) == 1


def test_send_text_with_saved_target_rejects_stale_window_error() -> None:
    secret = "secret-stale-window-pid-9999"
    calls: list[list[str]] = []
    clipboard: list[str] = []

    def failing_getwindowpid(command, **kwargs):
        cmd = list(command)
        calls.append(cmd)
        raise subprocess.SubprocessError(f"window 123 no longer exists: {secret}")

    bridge = TerminalBridge(
        env={"XDG_SESSION_TYPE": "x11"},
        which=lambda name: "/usr/bin/xdotool",
        run=failing_getwindowpid,
        read_comm=lambda pid: "gnome-terminal-server\n",
    )
    target = TerminalTarget(
        window_id="123",
        pid="456",
        process_name="gnome-terminal-server",
    )

    with pytest.raises(TerminalBridgeError) as exc_info:
        bridge.send_text("texto seguro", clipboard.append, target=target)

    message = str(exc_info.value)
    assert message == "Não foi possível identificar a janela do terminal de origem."
    assert secret not in message
    assert "SubprocessError" not in message
    assert clipboard == []


def test_send_text_with_saved_target_fails_when_activation_fails() -> None:
    secret = "secret-windowactivate-fail-5555"
    calls: list[list[str]] = []
    clipboard: list[str] = []

    def failing_activate(command, **kwargs):
        cmd = list(command)
        calls.append(cmd)
        if cmd[1] == "getwindowpid":
            return subprocess.CompletedProcess(cmd, 0, stdout="456\n", stderr="")
        if cmd[1] == "windowactivate":
            raise subprocess.SubprocessError(f"activation failed: {secret}")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    bridge = TerminalBridge(
        env={"XDG_SESSION_TYPE": "x11"},
        which=lambda name: "/usr/bin/xdotool",
        run=failing_activate,
        read_comm=lambda pid: "gnome-terminal-server\n",
    )
    target = TerminalTarget(
        window_id="123",
        pid="456",
        process_name="gnome-terminal-server",
    )

    with pytest.raises(TerminalBridgeError) as exc_info:
        bridge.send_text("texto seguro", clipboard.append, target=target)

    message = str(exc_info.value)
    assert message == "Não foi possível ativar a janela do terminal de origem."
    assert secret not in message
    assert "SubprocessError" not in message
    assert clipboard == []


def test_send_text_with_saved_target_fails_when_activation_confirmation_mismatches() -> None:
    calls: list[list[str]] = []
    clipboard: list[str] = []

    def mismatch_active_window(command, **kwargs):
        cmd = list(command)
        calls.append(cmd)
        if cmd[1] == "getwindowpid":
            return subprocess.CompletedProcess(cmd, 0, stdout="456\n", stderr="")
        if cmd[1] == "windowactivate":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[1] == "getactivewindow":
            return subprocess.CompletedProcess(cmd, 0, stdout="999\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    bridge = TerminalBridge(
        env={"XDG_SESSION_TYPE": "x11"},
        which=lambda name: "/usr/bin/xdotool",
        run=mismatch_active_window,
        read_comm=lambda pid: "gnome-terminal-server\n",
    )
    target = TerminalTarget(
        window_id="123",
        pid="456",
        process_name="gnome-terminal-server",
    )

    with pytest.raises(TerminalBridgeError) as exc_info:
        bridge.send_text("texto seguro", clipboard.append, target=target)

    assert "ativação da janela do terminal de origem não foi confirmada" in str(exc_info.value)
    assert clipboard == []


def test_send_text_with_saved_target_in_wayland_rejects() -> None:
    calls: list[list[str]] = []
    clipboard: list[str] = []

    bridge = TerminalBridge(
        env={"XDG_SESSION_TYPE": "wayland"},
        which=lambda name: "/usr/bin/xdotool",
        run=lambda cmd, **kw: calls.append(cmd),
        read_comm=lambda pid: "gnome-terminal-server\n",
    )
    target = TerminalTarget(
        window_id="123",
        pid="456",
        process_name="gnome-terminal-server",
    )

    with pytest.raises(TerminalBridgeError) as exc_info:
        bridge.send_text("texto seguro", clipboard.append, target=target)

    assert str(exc_info.value) == "A colagem automática requer uma sessão X11; use Copiar novamente ou Copiar e arquivar."
    assert calls == []
    assert clipboard == []


def test_send_text_with_saved_target_without_xdotool_rejects() -> None:
    calls: list[list[str]] = []
    clipboard: list[str] = []

    bridge = TerminalBridge(
        env={"XDG_SESSION_TYPE": "x11"},
        which=lambda name: None,
        run=lambda cmd, **kw: calls.append(cmd),
        read_comm=lambda pid: "gnome-terminal-server\n",
    )
    target = TerminalTarget(
        window_id="123",
        pid="456",
        process_name="gnome-terminal-server",
    )

    with pytest.raises(TerminalBridgeError) as exc_info:
        bridge.send_text("texto seguro", clipboard.append, target=target)

    assert str(exc_info.value) == "xdotool não está instalado; use Copiar novamente ou Copiar e arquivar."
    assert calls == []
    assert clipboard == []

def test_send_text_with_saved_target_paste_failure_retains_clipboard() -> None:
    secret = "secret-paste-fail-4444"
    clipboard: list[str] = []

    def failing_paste_key(command, **kwargs):
        cmd = list(command)
        if cmd[1] == "getwindowpid":
            return subprocess.CompletedProcess(cmd, 0, stdout="456\n", stderr="")
        if cmd[1] == "windowactivate":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[1] == "getactivewindow":
            return subprocess.CompletedProcess(cmd, 0, stdout="123\n", stderr="")
        if cmd[1] == "key":
            raise subprocess.SubprocessError(f"key failed: {secret}")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    bridge = TerminalBridge(
        env={"XDG_SESSION_TYPE": "x11"},
        which=lambda name: "/usr/bin/xdotool",
        run=failing_paste_key,
        read_comm=lambda pid: "gnome-terminal-server\n",
    )
    target = TerminalTarget(
        window_id="123",
        pid="456",
        process_name="gnome-terminal-server",
    )

    with pytest.raises(TerminalBridgeError) as exc_info:
        bridge.send_text("texto retido", clipboard.append, target=target)

    message = str(exc_info.value)
    assert message == "Não foi possível colar no terminal."
    assert secret not in message
    assert "SubprocessError" not in message
    assert clipboard == ["texto retido"]
