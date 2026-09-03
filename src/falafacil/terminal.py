from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence


TERMINAL_PROCESSES = frozenset(
    {
        "gnome-terminal-server",
        "konsole",
        "kitty",
        "alacritty",
        "xfce4-terminal",
        "lxterminal",
        "xterm",
        "wezterm-gui",
        "foot",
    }
)


class TerminalBridgeError(RuntimeError):
    """Erro recuperável ao acessar o terminal ativo."""


@dataclass(frozen=True, slots=True)
class TerminalTarget:
    window_id: str
    pid: str
    process_name: str


class TerminalBridge:
    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        which: Callable[[str], str | None] = shutil.which,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        read_comm: Callable[[str], str] | None = None,
    ) -> None:
        self._env = dict(env if env is not None else os.environ)
        self._which = which
        self._run = run
        self._read_comm = read_comm or _read_process_name
        self._xdotool: str | None = None
        self._reason = "Terminal ativo não detectado."

    @property
    def last_reason(self) -> str:
        return self._reason

    def detect_active_terminal(self) -> TerminalTarget | None:
        if self._env.get("XDG_SESSION_TYPE", "").lower() != "x11":
            self._reason = "A colagem automática requer uma sessão X11; use Copiar novamente ou Copiar e arquivar."
            return None

        self._xdotool = self._which("xdotool")
        if not self._xdotool:
            self._reason = "xdotool não está instalado; use Copiar novamente ou Copiar e arquivar."
            return None

        try:
            window_id = self._command_output([self._xdotool, "getactivewindow"])
            pid = self._command_output([self._xdotool, "getwindowpid", window_id])
            process_name = self._read_comm(pid).strip().lower()
        except (OSError, subprocess.SubprocessError, ValueError):
            self._reason = "Não foi possível identificar a janela ativa."
            return None

        if process_name not in TERMINAL_PROCESSES:
            self._reason = "A janela ativa não é um terminal reconhecido."
            return None

        self._reason = f"Terminal detectado: {process_name}."
        return TerminalTarget(window_id=window_id, pid=pid, process_name=process_name)

    def send_text(
        self,
        text: str,
        set_clipboard: Callable[[str], None],
        *,
        target: TerminalTarget | None = None,
    ) -> None:
        if not text.strip():
            raise TerminalBridgeError("Não há texto para enviar ao terminal.")

        if target is None:
            detected = self.detect_active_terminal()
            if detected is None:
                raise TerminalBridgeError(self._reason)
            if not self._xdotool:
                raise TerminalBridgeError("xdotool não está disponível.")
            set_clipboard(text)
            try:
                self._run(
                    [
                        self._xdotool,
                        "key",
                        "--window",
                        detected.window_id,
                        "--clearmodifiers",
                        "ctrl+shift+v",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise TerminalBridgeError("Não foi possível colar no terminal.") from exc
            return

        if self._env.get("XDG_SESSION_TYPE", "").lower() != "x11":
            self._reason = "A colagem automática requer uma sessão X11; use Copiar novamente ou Copiar e arquivar."
            raise TerminalBridgeError(self._reason)

        self._xdotool = self._which("xdotool")
        if not self._xdotool:
            self._reason = "xdotool não está instalado; use Copiar novamente ou Copiar e arquivar."
            raise TerminalBridgeError(self._reason)

        try:
            current_pid = self._command_output([self._xdotool, "getwindowpid", target.window_id])
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            self._reason = "Não foi possível identificar a janela do terminal de origem."
            raise TerminalBridgeError(self._reason) from exc

        if current_pid != target.pid:
            self._reason = "O processo do terminal de origem foi alterado ou encerrado."
            raise TerminalBridgeError(self._reason)

        try:
            current_process_name = self._read_comm(current_pid).strip().lower()
        except (OSError, ValueError) as exc:
            self._reason = "Não foi possível verificar o processo do terminal de origem."
            raise TerminalBridgeError(self._reason) from exc

        if (
            current_process_name != target.process_name
            or current_process_name not in TERMINAL_PROCESSES
        ):
            self._reason = "A janela de origem não é mais um terminal reconhecido."
            raise TerminalBridgeError(self._reason)

        try:
            self._run(
                [self._xdotool, "windowactivate", "--sync", target.window_id],
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._reason = "Não foi possível ativar a janela do terminal de origem."
            raise TerminalBridgeError(self._reason) from exc

        try:
            active_window = self._command_output([self._xdotool, "getactivewindow"])
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            self._reason = "Não foi possível confirmar a ativação do terminal de origem."
            raise TerminalBridgeError(self._reason) from exc

        if active_window != target.window_id:
            self._reason = "A ativação da janela do terminal de origem não foi confirmada."
            raise TerminalBridgeError(self._reason)

        set_clipboard(text)
        try:
            self._run(
                [
                    self._xdotool,
                    "key",
                    "--window",
                    target.window_id,
                    "--clearmodifiers",
                    "ctrl+shift+v",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise TerminalBridgeError("Não foi possível colar no terminal.") from exc

    def _command_output(self, command: Sequence[str]) -> str:
        result = self._run(
            list(command),
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        output = result.stdout.strip()
        if not output:
            raise ValueError(f"Comando sem resultado: {' '.join(command)}")
        return output.splitlines()[-1].strip()


def _read_process_name(pid: str) -> str:
    with open(f"/proc/{pid}/comm", encoding="utf-8") as process_file:
        return process_file.read()
