from __future__ import annotations

import os
import re
import threading
from typing import Any, Callable, Mapping, Protocol

from PySide6.QtCore import QObject, Qt, Signal

SESSION_UNAVAILABLE_MESSAGE = "Atalho global do mouse indisponível nesta sessão."
BACKEND_FAILURE_MESSAGE = "Não foi possível ativar o atalho global do mouse."

_BUTTON_NAME_PATTERN = re.compile(r"^[a-z0-9_]+$")
_CANONICAL_BUTTON_ALIASES = {
    "button8": "x1",
    "button9": "x2",
}
_CANONICAL_TO_BACKEND_ALIASES: dict[str, tuple[str, ...]] = {
    "x1": ("x1", "button8"),
    "x2": ("x2", "button9"),
}

class MouseShortcutError(RuntimeError):
    """Erro recuperável ao interagir com o backend de atalho de mouse."""


class MouseListenerLike(Protocol):
    """Protocolo estrutural para listeners de eventos de mouse."""

    def start(self) -> None:
        ...

    def stop(self) -> None:
        ...

    def join(self, timeout: float | None = None) -> None:
        ...

    def is_alive(self) -> bool:
        ...


MouseListenerFactory = Callable[..., MouseListenerLike]


def normalize_button_name(button: Any) -> str | None:
    """Normaliza e valida o nome de um botão do mouse para o identificador canônico."""
    if button is None:
        return None

    if hasattr(button, "name") and isinstance(button.name, str):
        raw = button.name
    elif isinstance(button, str):
        raw = button
    else:
        raw = str(button)

    if raw.startswith("Button."):
        raw = raw[len("Button."):]
    elif raw.startswith("button."):
        raw = raw[len("button."):]
    raw = raw.lower()
    if not raw or raw == "unknown" or len(raw) > 64:
        return None
    if not _BUTTON_NAME_PATTERN.fullmatch(raw):
        return None
    return _CANONICAL_BUTTON_ALIASES.get(raw, raw)


def _default_listener_factory(*args: Any, **kwargs: Any) -> MouseListenerLike:
    """Fábrica padrão com importação tardia de pynput.mouse."""
    from pynput import mouse

    kwargs.setdefault("suppress", False)
    return mouse.Listener(*args, **kwargs)


class MouseShortcutBridge(QObject):
    """Ponte de integração entre cliques globais do mouse e a UI Qt."""

    _activated_event = Signal(int, int, int)
    _button_captured_event = Signal(int, str, int, int)
    activated = Signal()
    button_captured = Signal(str)
    failed = Signal(str)
    _cleanup_capture_requested = Signal(int)

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        listener_factory: MouseListenerFactory | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._env = dict(env if env is not None else os.environ)
        self._listener_factory = listener_factory
        self._listener: MouseListenerLike | None = None
        self._lock = threading.RLock()
        self._generation = 0
        self._capturing = False
        self._active_button: str | None = None
        self._last_error: str | None = None
        self._cleanup_capture_requested.connect(
            self._on_cleanup_capture,
            Qt.ConnectionType.QueuedConnection,
        )
    @property
    def generation(self) -> int:
        """Geração atual do ciclo de vida do bridge."""
        return self._generation

    @property
    def available(self) -> bool:
        """Indica se o ambiente de sessão suporta atalhos globais de mouse (X11 com DISPLAY)."""
        return (
            self._env.get("XDG_SESSION_TYPE", "").strip().lower() == "x11"
            and bool(self._env.get("DISPLAY", "").strip())
        )

    @property
    def last_error(self) -> str | None:
        """Último erro sanitizado registrado pelo bridge."""
        return self._last_error

    def _cleanup_listener(self, listener: MouseListenerLike) -> bool:
        """Encerra e junta o listener fornecido de forma segura. Retorna True se houve erro."""
        has_error = False
        try:
            listener.stop()
        except Exception:
            has_error = True

        if threading.current_thread() is not listener:
            try:
                listener.join(timeout=0.5)
            except Exception:
                has_error = True

        return has_error

    def _on_cleanup_capture(self, gen: int) -> None:
        """Processa a limpeza assíncrona do listener de captura fora do callback."""
        target: MouseListenerLike | None = None
        with self._lock:
            if self._generation == gen:
                target = self._listener
                self._listener = None
                self._generation += 1
                self._capturing = False
                self._active_button = None
        if target is not None:
            if self._cleanup_listener(target):
                self._last_error = BACKEND_FAILURE_MESSAGE
                self.failed.emit(self._last_error)

    def _validate_button_backend(self, canonical: str) -> bool:
        """Valida se o botão canônico é reconhecido pelo backend pynput quando usando a fábrica padrão."""
        if self._listener_factory is not None:
            return True

        try:
            from pynput import mouse
        except Exception:
            return False

        candidates = _CANONICAL_TO_BACKEND_ALIASES.get(canonical, (canonical,))
        for candidate in candidates:
            btn = getattr(mouse.Button, candidate, None)
            if btn is not None and getattr(btn, "name", None) != "unknown":
                return True
        return False
    def start(self, button_name: str) -> bool:
        """Inicia a escuta ativa para um botão de mouse configurado."""
        if not self.available:
            self._last_error = SESSION_UNAVAILABLE_MESSAGE
            self.failed.emit(self._last_error)
            return False

        canonical = normalize_button_name(button_name)
        if canonical is None:
            self._last_error = BACKEND_FAILURE_MESSAGE
            self.failed.emit(self._last_error)
            return False

        if not self._validate_button_backend(canonical):
            self.stop()
            self._last_error = BACKEND_FAILURE_MESSAGE
            self.failed.emit(self._last_error)
            return False

        factory = self._listener_factory or _default_listener_factory
        listener: MouseListenerLike | None = None
        gen = 0

        with self._lock:
            self.stop()

            self._generation += 1
            gen = self._generation
            self._active_button = canonical
            self._capturing = False

            def _on_click(x: Any, y: Any, button: Any, pressed: bool) -> None:
                if not pressed:
                    return
                name = normalize_button_name(button)
                if name is None:
                    return
                if self._generation != gen or self._active_button != name:
                    return
                try:
                    px = int(x)
                    py = int(y)
                except (TypeError, ValueError):
                    px = 0
                    py = 0
                self._activated_event.emit(gen, px, py)
                self.activated.emit()
            try:
                listener = factory(on_click=_on_click, suppress=False)
                self._listener = listener
                listener.start()

                if self._generation == gen and self._listener is listener:
                    self._last_error = None
                    return True
                else:
                    if self._listener is listener:
                        self._listener = None
                    if listener is not None:
                        if self._cleanup_listener(listener):
                            self._last_error = BACKEND_FAILURE_MESSAGE
                            self.failed.emit(self._last_error)
                    return False
            except Exception:
                if self._listener is listener:
                    self._listener = None
                if self._generation == gen:
                    self._generation += 1
                    self._active_button = None
                if listener is not None:
                    self._cleanup_listener(listener)
                self._last_error = BACKEND_FAILURE_MESSAGE
                self.failed.emit(self._last_error)
                return False

    def begin_capture(self) -> bool:
        """Inicia a escuta temporária para capturar um único clique de mouse."""
        if not self.available:
            self._last_error = SESSION_UNAVAILABLE_MESSAGE
            self.failed.emit(self._last_error)
            return False

        factory = self._listener_factory or _default_listener_factory
        listener: MouseListenerLike | None = None
        gen = 0

        with self._lock:
            self.stop()

            self._generation += 1
            gen = self._generation
            self._capturing = True
            self._active_button = None
            captured = False

            def _on_click(x: Any, y: Any, button: Any, pressed: bool) -> None:
                nonlocal captured
                if not pressed:
                    return
                name = normalize_button_name(button)
                if name is None:
                    return
                if self._generation != gen or not self._capturing or captured:
                    return
                captured = True

                try:
                    px = int(x)
                    py = int(y)
                except (TypeError, ValueError):
                    px = 0
                    py = 0
                self._button_captured_event.emit(gen, name, px, py)
                self.button_captured.emit(name)
                self._cleanup_capture_requested.emit(gen)
            try:
                listener = factory(on_click=_on_click, suppress=False)
                self._listener = listener
                listener.start()

                if self._generation == gen and self._listener is listener:
                    self._last_error = None
                    return True
                else:
                    if self._listener is listener:
                        self._listener = None
                    if listener is not None:
                        if self._cleanup_listener(listener):
                            self._last_error = BACKEND_FAILURE_MESSAGE
                            self.failed.emit(self._last_error)
                    return False
            except Exception:
                if self._listener is listener:
                    self._listener = None
                if self._generation == gen:
                    self._generation += 1
                    self._capturing = False
                if listener is not None:
                    self._cleanup_listener(listener)
                self._last_error = BACKEND_FAILURE_MESSAGE
                self.failed.emit(self._last_error)
                return False

    def stop(self) -> None:
        """Interrompe qualquer listener ativo ou em modo de captura de forma idempotente."""
        with self._lock:
            self._generation += 1
            self._capturing = False
            self._active_button = None
            listener = self._listener
            self._listener = None

            if listener is not None:
                if self._cleanup_listener(listener):
                    self._last_error = BACKEND_FAILURE_MESSAGE
                    self.failed.emit(self._last_error)
