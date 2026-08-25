from __future__ import annotations

import os
import gc
import sqlite3
import threading
import time
import numpy as np
import pytest
from PySide6.QtCore import QByteArray, QCoreApplication, QEvent, QObject, QPoint, QPointF, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent, QKeySequence, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from falafacil.audio import AudioCapture, AudioDevice, AudioRecorderError
from falafacil import __version__
from falafacil.config import DEFAULT_MODEL, Settings
from falafacil.credentials import CredentialStoreError
from falafacil.storage import LocalStore, LocalStoreError, TokenTotals, TokenUsageRecord
from falafacil.shortcuts import (
    PRIMARY_MOUSE_BUTTON_MESSAGE,
    UNSUPPORTED_MOUSE_BUTTON_MESSAGE,
)
from falafacil.terminal import TerminalBridgeError
from falafacil.transcription import TokenUsage, TranscriptionDebug, TranscriptionError
from falafacil.ui import (
    CAPTURE_WAITING_TEXT,
    AppState,
    MainWindow,
    TokenUsageChart,
)

class FakeRecorder:
    def __init__(
        self,
        capture: AudioCapture | None = None,
        *,
        low_error: bool = False,
        fail_start: bool = False,
        fail_stop_error: Exception | None = None,
    ) -> None:
        self.recording = False
        self.selected_devices: list[int | str | None] = []
        self.capture = capture or make_capture()
        self.low_error = low_error
        self.fail_start = fail_start
        self.fail_stop_error = fail_stop_error

    def set_device(self, device: int | str | None) -> None:
        self.selected_devices.append(device)

    def start(self) -> None:
        if self.fail_start:
            raise AudioRecorderError("Não foi possível acessar o microfone.")
        self.recording = True

    def stop(self) -> AudioCapture:
        self.recording = False
        if self.fail_stop_error is not None:
            raise self.fail_stop_error
        if self.low_error:
            raise AudioRecorderError("O áudio capturado está muito baixo.")
        return self.capture

    def last_capture(self) -> AudioCapture | None:
        return self.capture if self.low_error else None

    def is_recording(self) -> bool:
        return self.recording


class FakeTerminal:
    def __init__(self, *, fail_error: Exception | None = None) -> None:
        self.fail_error = fail_error

    def send_text(self, text: str, clipboard_setter) -> None:
        if self.fail_error is not None:
            raise self.fail_error
        clipboard_setter(text)

class FakeTranscriber:
    def __init__(
        self,
        text: str = "synthetic transcript",
        usage: TokenUsage | None = None,
        error: str | None = None,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self.text = text
        self.usage = usage
        self.error = error
        self.model = model
        self.calls: list[bytes] = []
        self._debug: TranscriptionDebug | None = None

    def transcribe(self, wav_bytes: bytes) -> str:
        self.calls.append(wav_bytes)
        self._debug = make_debug(len(wav_bytes), self.text, usage=self.usage)
        if self.error:
            raise TranscriptionError(self.error)
        return self.text

    def last_debug(self) -> TranscriptionDebug | None:
        return self._debug
class FakeInputShortcutBridge(QObject):
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
        ready: bool = True,
        auto_ack: bool = True,
        version_incompatible: bool = False,
        order_log: list[str] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.ready = ready
        self.auto_ack = auto_ack
        self.version_incompatible = version_incompatible
        self.mouse_generation = 0
        self.keyboard_generation = 0
        self.commands: list[tuple[str, int, str | None]] = []
        self.closed = False
        self.reconnect_count = 0
        self.order_log = order_log

    def start_mouse(self, button: str) -> int:
        self.mouse_generation += 1
        generation = self.mouse_generation
        self.commands.append(("watch_mouse", generation, button))
        if self.ready and self.auto_ack:
            self.mouse_binding_ready.emit(generation, button)
        return generation

    def begin_mouse_capture(self) -> int:
        self.mouse_generation += 1
        self.commands.append(("capture_mouse", self.mouse_generation, None))
        return self.mouse_generation

    def stop_mouse(self) -> int:
        self.mouse_generation += 1
        generation = self.mouse_generation
        self.commands.append(("stop_mouse", generation, None))
        if self.ready and self.auto_ack:
            self.stopped.emit("mouse", generation)
        return generation

    def start_keyboard(self, shortcut: str) -> int:
        self.keyboard_generation += 1
        generation = self.keyboard_generation
        self.commands.append(("watch_keyboard", generation, shortcut))
        if self.ready and self.auto_ack:
            self.keyboard_binding_ready.emit(generation, shortcut)
        return generation

    def begin_keyboard_capture(self) -> int:
        self.keyboard_generation += 1
        self.commands.append(("capture_keyboard", self.keyboard_generation, None))
        return self.keyboard_generation

    def stop_keyboard(self) -> int:
        self.keyboard_generation += 1
        generation = self.keyboard_generation
        self.commands.append(("stop_keyboard", generation, None))
        if self.ready and self.auto_ack:
            self.stopped.emit("keyboard", generation)
        return generation

    def reconnect(self) -> None:
        self.reconnect_count += 1
        self.ready = True
        self.ready_changed.emit(True)

    def close(self) -> None:
        self.closed = True
        if self.order_log is not None:
            self.order_log.append("bridge")


class FakeShortcutInstaller(QObject):
    finished = Signal(bool, str)

    def __init__(self, *, order_log: list[str] | None = None) -> None:
        super().__init__()
        self.install_count = 0
        self.cancel_count = 0
        self.order_log = order_log

    def install(self) -> bool:
        self.install_count += 1
        return True

    def cancel(self) -> None:
        self.cancel_count += 1
        if self.order_log is not None:
            self.order_log.append("installer")
class FakeStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.saved: list[str] = []

    def get_api_key(self) -> str | None:
        return None

    def set_api_key(self, api_key: str) -> None:
        if self.fail:
            raise CredentialStoreError("chaveiro indisponível")
        self.saved.append(api_key)

    def delete_api_key(self) -> None:
        return None


class FakeLocalStore:
    def __init__(
        self,
        *,
        fail_record: bool = False,
        fail_totals: bool = False,
        fail_close: bool = False,
        fail_mic: bool = False,
        fail_history: bool = False,
        fail_mouse_save: bool = False,
        fail_mouse_get: bool = False,
        fail_mouse_clear: bool = False,
        fail_keyboard_save: bool = False,
        fail_keyboard_get: bool = False,
        fail_keyboard_clear: bool = False,
        fail_model_save: bool = False,
        fail_model_get: bool = False,
        order_log: list[str] | None = None,
    ) -> None:
        self.fail_record = fail_record
        self.fail_totals = fail_totals
        self.fail_close = fail_close
        self.fail_mic = fail_mic
        self.fail_history = fail_history
        self.fail_mouse_save = fail_mouse_save
        self.fail_mouse_get = fail_mouse_get
        self.fail_mouse_clear = fail_mouse_clear
        self.fail_keyboard_save = fail_keyboard_save
        self.fail_keyboard_get = fail_keyboard_get
        self.fail_keyboard_clear = fail_keyboard_clear
        self.fail_model_save = fail_model_save
        self.fail_model_get = fail_model_get
        self.records: list[tuple[str, Any, str]] = []
        self.mic_identity: str | None = None
        self.mouse_button: str | None = None
        self.keyboard_shortcut: str | None = None
        self.gemini_model: str | None = None
        self.closed = False
        self.close_order_log = order_log if order_log is not None else []
    def get_last_microphone_identity(self) -> str | None:
        if self.fail_mic:
            raise LocalStoreError("erro ao ler microfone")
        return self.mic_identity

    def save_last_microphone_identity(self, identity: str) -> None:
        if self.fail_mic:
            raise LocalStoreError("erro ao salvar microfone")
        self.mic_identity = identity

    def get_recording_mouse_button(self) -> str | None:
        if self.fail_mouse_get:
            raise LocalStoreError("erro ao ler botão do mouse")
        return self.mouse_button

    def save_recording_mouse_button(self, button_name: str) -> None:
        if self.fail_mouse_save:
            raise LocalStoreError("erro ao salvar botão do mouse")
        self.mouse_button = button_name

    def clear_recording_mouse_button(self) -> None:
        if self.fail_mouse_clear:
            raise LocalStoreError("erro ao limpar botão do mouse")
        self.mouse_button = None

    def get_recording_keyboard_shortcut(self) -> str | None:
        if self.fail_keyboard_get:
            raise LocalStoreError("erro ao ler atalho do teclado")
        return self.keyboard_shortcut

    def save_recording_keyboard_shortcut(self, shortcut: str) -> None:
        if self.fail_keyboard_save:
            raise LocalStoreError("erro ao salvar atalho do teclado")
        self.keyboard_shortcut = shortcut

    def clear_recording_keyboard_shortcut(self) -> None:
        if self.fail_keyboard_clear:
            raise LocalStoreError("erro ao limpar atalho do teclado")
        self.keyboard_shortcut = None

    def get_gemini_model(self) -> str | None:
        if self.fail_model_get:
            raise LocalStoreError("erro ao ler modelo Gemini")
        return self.gemini_model

    def save_gemini_model(self, model: str) -> None:
        if self.fail_model_save:
            raise LocalStoreError("erro ao salvar modelo Gemini")
        self.gemini_model = model

    def record_token_usage(self, model: str, usage: Any, outcome: str) -> None:
        if self.fail_record:
            raise LocalStoreError("erro ao salvar tokens")
        self.records.append((model, usage, outcome))

    def get_token_totals(self) -> TokenTotals:
        if self.fail_totals:
            raise LocalStoreError("erro ao ler totais")
        if not self.records:
            return TokenTotals(
                input_tokens=0,
                output_tokens=0,
                thought_tokens=0,
                cached_tokens=0,
                tool_use_tokens=0,
                total_tokens=0,
            )

        def _calc_total(field: str) -> int | None:
            vals = [getattr(usage, field, None) for _, usage, _ in self.records]
            if any(v is None for v in vals):
                return None
            return sum(int(v) for v in vals)

        return TokenTotals(
            input_tokens=_calc_total("input_tokens"),
            output_tokens=_calc_total("output_tokens"),
            thought_tokens=_calc_total("thought_tokens"),
            cached_tokens=_calc_total("cached_tokens"),
            tool_use_tokens=_calc_total("tool_use_tokens"),
            total_tokens=_calc_total("total_tokens"),
        )

    def get_token_usage_history(self, limit: int = 30) -> tuple[TokenUsageRecord, ...]:
        if self.fail_history:
            raise LocalStoreError("erro ao ler histórico")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or limit > 100:
            raise LocalStoreError(f"Limite inválido para histórico de tokens: {limit!r}")
        history = [
            TokenUsageRecord(
                id=i + 1,
                recorded_at="",
                model=m,
                input_tokens=getattr(u, "input_tokens", None),
                output_tokens=getattr(u, "output_tokens", None),
                thought_tokens=getattr(u, "thought_tokens", None),
                cached_tokens=getattr(u, "cached_tokens", None),
                tool_use_tokens=getattr(u, "tool_use_tokens", None),
                total_tokens=getattr(u, "total_tokens", None),
                outcome=o,
            )
            for i, (m, u, o) in enumerate(self.records)
        ]
        return tuple(history[-limit:])
    def close(self) -> None:
        if self.fail_close:
            raise LocalStoreError("erro ao fechar")
        self.closed = True
        self.close_order_log.append("store")
class FakeSignal:
    def connect(self, slot) -> None:
        self.slot = slot


class FakeMediaPlayer:
    def __init__(self, *, fail_play: Exception | None = None) -> None:
        self.fail_play = fail_play
        self.audio_output = None
        self.source_device = None
        self.source_url = None
        self.play_count = 0
        self.stop_count = 0
        self.cleared_source = None
        self.played_bytes: bytes | None = None

    def setAudioOutput(self, output) -> None:
        self.audio_output = output

    def setSourceDevice(self, device, url) -> None:
        self.source_device = device
        self.source_url = url
        if device is not None:
            try:
                self.played_bytes = bytes(device.data())
            except Exception:
                self.played_bytes = None
        else:
            self.played_bytes = None

    def play(self) -> None:
        if self.fail_play is not None:
            raise self.fail_play
        self.play_count += 1
        if self.source_device is not None:
            try:
                self.played_bytes = bytes(self.source_device.data())
            except Exception:
                pass

    def stop(self) -> None:
        self.stop_count += 1

    def setSource(self, url) -> None:
        self.cleared_source = url

class FakeAudioOutput:
    pass


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()
    for window in QApplication.topLevelWidgets():
        window.close()
        window.deleteLater()
    app.processEvents()
    app.quit()
    app.processEvents()


def make_capture() -> AudioCapture:
    pcm = np.array([[1000], [-1000], [500], [-500]], dtype=np.int16).tobytes()
    return AudioCapture(
        wav_bytes=b"RIFFsynthetic-wav",
        pcm_bytes=pcm,
        frames=4,
        duration_seconds=4 / 16_000,
        rms=0.03,
        peak=1000 / 32768,
    )


def make_debug(
    audio_bytes: int = 18,
    text: str = "synthetic transcript",
    usage: TokenUsage | None = None,
) -> TranscriptionDebug:
    return TranscriptionDebug(
        model="synthetic-model",
        prompt="prompt em português do Brasil",
        audio_bytes=audio_bytes,
        audio_mime_type="audio/wav",
        audio_base64_length=24,
        audio_base64_preview="UklGRHN5bnRoZXRpYw==",
        response_text=text,
        error=None,
        usage=usage,
    )


class FakeHomebrewUpdateController(QObject):
    status_changed = Signal(str)
    up_to_date = Signal(str)
    ready_to_restart = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        *,
        running: bool = False,
        restart_result: bool = True,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._running = running
        self.restart_result = restart_result
        self.install_calls = 0
        self.restart_calls = 0

    @property
    def running(self) -> bool:
        return self._running

    @running.setter
    def running(self, value: bool) -> None:
        self._running = value

    def install_latest(self) -> bool:
        self.install_calls += 1
        self._running = True
        return True

    def restart(self) -> bool:
        self.restart_calls += 1
        return self.restart_result

def make_window(
    qapp,
    *,
    store=None,
    factory=None,
    settings=None,
    transcriber=None,
    recorder=None,
    microphone_provider=None,
    media_player=None,
    local_store=None,
    terminal=None,
    terminal_bridge=None,
    input_shortcut_bridge=None,
    shortcut_service_installer=None,
    homebrew_update_controller=None,
):
    media_player = media_player or FakeMediaPlayer()
    resolved_terminal = (
        terminal_bridge
        if terminal_bridge is not None
        else (terminal if terminal is not None else FakeTerminal())
    )
    window = MainWindow(
        settings or Settings(),
        recorder=recorder or FakeRecorder(),
        transcriber=transcriber,
        terminal_bridge=resolved_terminal,
        api_key_store=store,
        transcriber_factory=factory,
        microphone_provider=microphone_provider
        or (lambda: (AudioDevice(0, "Microfone sintético", 1, True),)),
        media_player_factory=lambda parent: (media_player, FakeAudioOutput()),
        local_store=local_store,
        input_shortcut_bridge=input_shortcut_bridge or FakeInputShortcutBridge(),
        shortcut_service_installer=(
            shortcut_service_installer or FakeShortcutInstaller()
        ),
        homebrew_update_controller=homebrew_update_controller,
    )
    window.show()
    qapp.processEvents()
    return window, media_player


def wait_for_worker(qapp, window) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and window._thread is not None:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()


def test_configure_api_key_accepts_key_and_enables_recording(qapp, monkeypatch) -> None:
    store = FakeStore()
    factory_calls: list[tuple[str, str]] = []

    def factory(api_key: str, model: str):
        factory_calls.append((api_key, model))
        return FakeTranscriber()

    window, _ = make_window(qapp, store=store, factory=factory)
    monkeypatch.setattr(window, "_acquire_api_key", lambda: ("  ui-session-token  ", True))

    window._configure_api_key()

    assert factory_calls == [("ui-session-token", "gemini-2.5-flash-lite")]
    assert store.saved == ["ui-session-token"]
    assert window.settings.api_key == "ui-session-token"
    assert window.transcriber is not None
    assert window.record_button.isEnabled()
    assert "ui-session-token" not in window.status_label.text()
    assert "sucesso" in window.status_label.text()
    window.close()


def test_configure_api_key_cancel_or_empty_preserves_state(qapp, monkeypatch) -> None:
    store = FakeStore()
    factory_calls: list[tuple[str, str]] = []
    window, _ = make_window(
        qapp,
        store=store,
        factory=lambda api_key, model: factory_calls.append((api_key, model)) or FakeTranscriber(),
    )
    original_status = window.status_label.text()

    monkeypatch.setattr(window, "_acquire_api_key", lambda: ("ignored-token", False))
    window._configure_api_key()
    assert window.settings.api_key is None
    assert window.transcriber is None
    assert not window.record_button.isEnabled()
    assert store.saved == []

    monkeypatch.setattr(window, "_acquire_api_key", lambda: (" \t", True))
    window._configure_api_key()
    assert window.settings.api_key is None
    assert window.transcriber is None
    assert factory_calls == []
    assert store.saved == []
    assert window.status_label.text() == original_status
    window.close()


def test_configure_api_key_store_failure_keeps_session_key_only(qapp, monkeypatch) -> None:
    store = FakeStore(fail=True)
    window, _ = make_window(
        qapp,
        store=store,
        factory=lambda api_key, model: FakeTranscriber(),
    )
    monkeypatch.setattr(window, "_acquire_api_key", lambda: ("session-only-token", True))

    window._configure_api_key()

    assert window.settings.api_key == "session-only-token"
    assert window.transcriber is not None
    assert window.record_button.isEnabled()
    assert "session-only-token" not in window.status_label.text()
    assert "apenas nesta sessão" in window.status_label.text()
    window.close()


def test_configure_api_key_factory_failure_rolls_back(qapp, monkeypatch) -> None:
    store = FakeStore()
    original = FakeTranscriber()

    def broken_factory(api_key: str, model: str):
        raise RuntimeError("construction failed")

    window, _ = make_window(qapp, store=store, factory=broken_factory, transcriber=original)
    monkeypatch.setattr(window, "_acquire_api_key", lambda: ("failed-factory-token", True))

    window._configure_api_key()

    assert window.settings.api_key is None
    assert window.transcriber is original
    assert not window.record_button.isEnabled()
    assert store.saved == []
    assert "failed-factory-token" not in window.status_label.text()
    assert "Não foi possível configurar" in window.status_label.text()
    window.close()


def test_settings_dialog_model_choices_populated_and_selected(qapp) -> None:
    window, _ = make_window(
        qapp,
        settings=Settings(model="gemini-3.5-flash-lite"),
    )

    def inspect() -> None:
        dialog = window._settings_dialog
        assert dialog is not None
        assert window.model_combo.count() == 3
        labels = [window.model_combo.itemText(i) for i in range(3)]
        data = [window.model_combo.itemData(i) for i in range(3)]
        assert data == [
            "gemini-2.5-flash-lite",
            "gemini-3.5-flash-lite",
            "gemini-3.7-flash",
        ]
        assert labels == [
            "Mais econômico — Gemini 2.5 Flash-Lite",
            "Flash-Lite mais recente — Gemini 3.5 Flash-Lite",
            "Flash mais capaz — Gemini 3.7 Flash",
        ]
        assert window.model_combo.currentData() == "gemini-3.5-flash-lite"
        dialog.reject()

    QTimer.singleShot(0, inspect)
    window._open_settings_dialog()
    window.close()


def test_apply_model_preference_with_active_key(qapp) -> None:
    local_store = FakeLocalStore()
    factory_calls: list[tuple[str, str]] = []

    def factory(api_key: str, model: str):
        factory_calls.append((api_key, model))
        return FakeTranscriber()

    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-key", model="gemini-2.5-flash-lite"),
        local_store=local_store,
        factory=factory,
    )

    def apply_choice() -> None:
        idx = window.model_combo.findData("gemini-3.5-flash-lite")
        assert idx >= 0
        window.model_combo.setCurrentIndex(idx)
        window.apply_model_button.click()
        window._settings_dialog.accept()

    QTimer.singleShot(0, apply_choice)
    window._open_settings_dialog()

    assert factory_calls == [("active-key", "gemini-3.5-flash-lite")]
    assert window.settings.model == "gemini-3.5-flash-lite"
    assert local_store.get_gemini_model() == "gemini-3.5-flash-lite"
    assert window.transcriber is not None
    assert "Modelo Gemini configurado com sucesso." in window.status_label.text()
    window.close()


def test_apply_model_preference_without_key(qapp) -> None:
    local_store = FakeLocalStore()
    factory_calls: list[tuple[str, str]] = []

    window, _ = make_window(
        qapp,
        settings=Settings(model="gemini-2.5-flash-lite"),
        local_store=local_store,
        factory=lambda k, m: factory_calls.append((k, m)) or FakeTranscriber(),
    )

    def apply_choice() -> None:
        idx = window.model_combo.findData("gemini-3.7-flash")
        assert idx >= 0
        window.model_combo.setCurrentIndex(idx)
        window.apply_model_button.click()
        window._settings_dialog.accept()

    QTimer.singleShot(0, apply_choice)
    window._open_settings_dialog()

    assert factory_calls == []
    assert window.settings.model == "gemini-3.7-flash"
    assert local_store.get_gemini_model() == "gemini-3.7-flash"
    assert window.transcriber is None
    assert "Modelo Gemini configurado com sucesso." in window.status_label.text()
    window.close()


def test_apply_model_preference_factory_failure_rolls_back(qapp) -> None:
    local_store = FakeLocalStore()
    original_transcriber = FakeTranscriber(model="gemini-2.5-flash-lite")

    def failing_factory(api_key: str, model: str):
        raise RuntimeError("factory construction failed")

    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-key", model="gemini-2.5-flash-lite"),
        transcriber=original_transcriber,
        local_store=local_store,
        factory=failing_factory,
    )

    def apply_choice() -> None:
        idx = window.model_combo.findData("gemini-3.5-flash-lite")
        window.model_combo.setCurrentIndex(idx)
        window.apply_model_button.click()
        # Verify visual rollback while dialog is still open
        assert window.model_combo.currentData() == "gemini-2.5-flash-lite"
        assert window.model_combo.currentIndex() == window.model_combo.findData(
            "gemini-2.5-flash-lite"
        )
        window._settings_dialog.reject()

    QTimer.singleShot(0, apply_choice)
    window._open_settings_dialog()

    assert window.settings.model == "gemini-2.5-flash-lite"
    assert window.transcriber is original_transcriber
    assert local_store.get_gemini_model() is None
    assert "Não foi possível configurar o modelo Gemini." in window.status_label.text()
    window.close()


def test_apply_model_preference_store_failure_keeps_session_model_only(qapp) -> None:
    local_store = FakeLocalStore(fail_model_save=True)

    window, _ = make_window(
        qapp,
        settings=Settings(model="gemini-2.5-flash-lite"),
        local_store=local_store,
    )

    def apply_choice() -> None:
        idx = window.model_combo.findData("gemini-3.5-flash-lite")
        window.model_combo.setCurrentIndex(idx)
        window.apply_model_button.click()
        window._settings_dialog.accept()

    QTimer.singleShot(0, apply_choice)
    window._open_settings_dialog()

    assert window.settings.model == "gemini-3.5-flash-lite"
    assert "apenas nesta sessão" in window.status_label.text()
    window.close()


def test_apply_model_preference_store_failure_with_active_key_keeps_session_model_only(
    qapp,
) -> None:
    local_store = FakeLocalStore(fail_model_save=True)
    original_transcriber = FakeTranscriber(model="gemini-2.5-flash-lite")
    factory_calls: list[tuple[str, str]] = []

    def tracking_factory(api_key: str, model: str):
        factory_calls.append((api_key, model))
        return FakeTranscriber(model=model)

    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-key", model="gemini-2.5-flash-lite"),
        transcriber=original_transcriber,
        local_store=local_store,
        factory=tracking_factory,
    )

    def apply_choice() -> None:
        idx = window.model_combo.findData("gemini-3.5-flash-lite")
        window.model_combo.setCurrentIndex(idx)
        window.apply_model_button.click()
        window._settings_dialog.accept()

    QTimer.singleShot(0, apply_choice)
    window._open_settings_dialog()

    assert factory_calls == [("active-key", "gemini-3.5-flash-lite")]
    assert window.settings.model == "gemini-3.5-flash-lite"
    assert window.transcriber is not original_transcriber
    assert window.transcriber.model == "gemini-3.5-flash-lite"
    assert local_store.gemini_model is None
    assert "apenas nesta sessão" in window.status_label.text()
    window.close()


def test_apply_model_preference_locked_when_model_from_environment(qapp) -> None:
    local_store = FakeLocalStore()
    original_transcriber = FakeTranscriber(model="gemini-opaque-custom")
    factory_calls: list[tuple[str, str]] = []

    def tracking_factory(api_key: str, model: str):
        factory_calls.append((api_key, model))
        return FakeTranscriber(model=model)

    window, _ = make_window(
        qapp,
        settings=Settings(
            api_key="active-key",
            model="gemini-opaque-custom",
            model_from_environment=True,
        ),
        transcriber=original_transcriber,
        local_store=local_store,
        factory=tracking_factory,
    )

    def inspect() -> None:
        assert window.model_combo.isEnabled() is False
        assert window.apply_model_button.isEnabled() is False
        assert window.model_combo.currentIndex() == -1
        window._apply_model_preference()
        assert window.settings.model == "gemini-opaque-custom"
        assert window.transcriber is original_transcriber
        assert len(factory_calls) == 0
        assert local_store.gemini_model is None
        assert local_store.records == []
        window._settings_dialog.reject()

    QTimer.singleShot(0, inspect)
    window._open_settings_dialog()

    assert window.settings.model == "gemini-opaque-custom"
    assert window.transcriber is original_transcriber
    assert len(factory_calls) == 0
    assert local_store.gemini_model is None
    window.close()

def test_apply_model_preference_locked_when_busy(qapp) -> None:
    window, _ = make_window(qapp, settings=Settings(api_key="active-key"))

    def inspect() -> None:
        assert window.model_combo.isEnabled() is True
        assert window.apply_model_button.isEnabled() is True

        window.state = AppState.RECORDING
        window._update_actions()
        assert window.model_combo.isEnabled() is False
        assert window.apply_model_button.isEnabled() is False

        window.state = AppState.TRANSCRIBING
        window._update_actions()
        assert window.model_combo.isEnabled() is False
        assert window.apply_model_button.isEnabled() is False

        window.state = AppState.IDLE
        window._update_actions()
        assert window.model_combo.isEnabled() is True
        assert window.apply_model_button.isEnabled() is True
        window._settings_dialog.reject()

    QTimer.singleShot(0, inspect)
    window._open_settings_dialog()
    window.close()


def test_main_window_default_factory_creates_transcriber_with_model(qapp) -> None:
    settings = Settings(model="gemini-3.5-flash-lite")
    window, _ = make_window(qapp, settings=settings)
    transcriber = window.transcriber_factory("test-key", settings.model)
    assert transcriber.model == "gemini-3.5-flash-lite"
    assert transcriber._api_key == "test-key"
    window.close()

def test_update_actions_tracks_key_text_and_busy_state(qapp) -> None:
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
    )

    assert window.record_button.isEnabled()
    assert not window.copy_button.isEnabled()
    assert not window.clear_text_button.isEnabled()
    assert not window.terminal_button.isEnabled()
    assert window.settings_button.isEnabled()

    window.editor.setPlainText("texto sintético")
    window._update_actions()
    assert window.copy_button.isEnabled()
    assert window.clear_text_button.isEnabled()
    assert window.terminal_button.isEnabled()

    window.state = AppState.RECORDING
    window._update_actions()
    assert window.settings_button.isEnabled()
    assert window.record_button.isEnabled()

    window.state = AppState.TRANSCRIBING
    window._update_actions()
    assert not window.record_button.isEnabled()
    assert not window.copy_button.isEnabled()
    assert not window.clear_text_button.isEnabled()
    assert not window.terminal_button.isEnabled()
    assert window.settings_button.isEnabled()
    window.close()


def test_send_after_settings_dialog_closes_does_not_use_deleted_widgets(qapp) -> None:
    """Closing Settings must not leave stale Qt wrappers on MainWindow."""
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=FakeRecorder(),
    )

    def close_settings() -> None:
        assert window._settings_dialog is not None
        window._settings_dialog.reject()

    QTimer.singleShot(0, close_settings)
    window._open_settings_dialog()
    assert window._settings_dialog is None

    # Let Qt destroy the dialog's C++ children, matching the real event-loop
    # lifecycle that previously exposed the orphaned QPushButton wrapper.
    gc.collect()
    qapp.processEvents()
    window._update_actions()

    window._start_recording()
    window._finish_recording()
    window._send_pending_audio()
    wait_for_worker(qapp, window)

    assert window.state is AppState.READY
    assert window.editor.toPlainText() == "synthetic transcript"
    window.close()


def test_clear_text_button_removes_editor_text(qapp) -> None:
    window, _ = make_window(qapp)
    window.editor.setPlainText("texto para apagar")

    window.clear_text_button.click()

    assert window.editor.toPlainText() == ""
    assert not window.clear_text_button.isEnabled()
    assert window.status_label.text() == "Texto apagado."
    window.close()


def test_stop_enters_audio_ready_without_network_call(qapp) -> None:
    transcriber = FakeTranscriber()
    recorder = FakeRecorder()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        recorder=recorder,
    )

    window._start_recording()
    window._finish_recording()

    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is recorder.capture
    assert transcriber.calls == []
    assert window.play_audio_button.isEnabled()
    assert window.send_to_gemini_button.isEnabled()
    assert "RMS" in window.audio_debug.toPlainText()
    window.close()


def test_play_then_send_uses_memory_wav_and_creates_worker(qapp) -> None:
    transcriber = FakeTranscriber()
    media_player = FakeMediaPlayer()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        media_player=media_player,
    )
    window._start_recording()
    window._finish_recording()

    window._play_pending_audio()
    assert window.record_button.isEnabled()
    assert media_player.play_count == 1
    assert media_player.source_device is window._audio_buffer
    assert bytes(media_player.source_device.data()) == window._pending_capture.wav_bytes
    assert transcriber.calls == []

    window._send_pending_audio()
    wait_for_worker(qapp, window)
    assert transcriber.calls == [window._pending_capture.wav_bytes]
    assert window.editor.toPlainText() == "synthetic transcript"
    window.close()


def test_play_pending_audio_failure_omits_raw_exception_and_secret(qapp) -> None:
    secret = "secret-token-media-player-5678"
    media_player = FakeMediaPlayer(
        fail_play=RuntimeError(f"falha interna no player com {secret}")
    )
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        media_player=media_player,
    )
    window._start_recording()
    window._finish_recording()
    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is not None

    window._play_pending_audio()

    assert window.status_label.text() == "Não foi possível reproduzir o áudio."
    assert secret not in window.status_label.text()
    assert "RuntimeError" not in window.status_label.text()
    assert "falha interna" not in window.status_label.text()
    assert secret not in window.audio_debug.toPlainText()
    assert secret not in window.payload_debug.toPlainText()
    assert secret not in window.return_debug.toPlainText()
    assert secret not in window.usage_debug.toPlainText()

    assert window.state is AppState.AUDIO_READY
    assert window._audio_buffer is None
    assert window.record_button.isEnabled()
    assert window.send_to_gemini_button.isEnabled()
    window.close()


def test_low_audio_enters_error_without_worker(qapp) -> None:
    transcriber = FakeTranscriber()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        recorder=FakeRecorder(low_error=True),
    )

    window._start_recording()
    window._finish_recording()

    assert window.state is AppState.ERROR
    assert "baixo" in window.status_label.text()
    assert transcriber.calls == []
    assert "Erro" in window.audio_debug.toPlainText()
    window.close()


def test_close_stops_player_and_clears_audio_references(qapp) -> None:
    media_player = FakeMediaPlayer()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        media_player=media_player,
    )
    window._start_recording()
    window._finish_recording()
    window._play_pending_audio()
    assert window._pending_capture is not None
    assert window._audio_buffer is not None

    window.close()

    assert media_player.stop_count == 1
    assert media_player.cleared_source is not None
    assert window._pending_capture is None
    assert window._audio_buffer is None
    assert window._audio_byte_array is None


def test_diagnostic_panel_is_permanent_without_toggle_or_dock(qapp) -> None:
    window, _ = make_window(qapp)
    assert not hasattr(window, "debug_button")
    assert not hasattr(window, "debug_dock")
    assert window.diagnostic_tabs.isVisible()
    assert window.usage_chart.isVisible()
    window.close()


def test_debug_trace_renders_audio_payload_and_return(qapp) -> None:
    window, _ = make_window(qapp)
    debug = make_debug()
    window._pending_capture = make_capture()
    window._render_audio_debug(window._pending_capture)
    window._on_transcription_finished("synthetic transcript", debug)

    assert "Forma de onda" in window.audio_debug.toPlainText()
    assert "Prompt" in window.payload_debug.toPlainText()
    assert "Modelo: synthetic-model" in window.payload_debug.toPlainText()
    assert "Base64: 24 caracteres" in window.payload_debug.toPlainText()
    assert debug.audio_base64_preview not in window.payload_debug.toPlainText()
    assert "Preview Base64" not in window.payload_debug.toPlainText()
    assert "synthetic transcript" in window.return_debug.toPlainText()
    window.close()


def test_microphone_selection_preserves_index_and_refresh_recovers_empty_provider(qapp) -> None:
    devices = [
        AudioDevice(3, "USB", 1, False),
        AudioDevice(8, "Interno", 2, True),
    ]
    recorder = FakeRecorder()
    provider_state = {"devices": tuple(devices)}
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        microphone_provider=lambda: provider_state["devices"],
    )

    assert window.microphone_combo.count() == 2
    window.microphone_combo.setCurrentIndex(0)
    assert window.microphone_combo.currentData() == 3
    assert recorder.selected_devices[-1] == 3

    provider_state["devices"] = ()
    window._refresh_microphones()
    assert window.microphone_combo.count() == 0
    assert not window.record_button.isEnabled()
    assert window.refresh_microphones_button.isEnabled()
    assert window.status_label.text() == "Nenhum microfone de entrada foi detectado."
    window.close()


def test_microphone_provider_failure_keeps_refresh_available(qapp) -> None:
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        microphone_provider=lambda: (_ for _ in ()).throw(RuntimeError("sem PortAudio")),
    )

    assert window.microphone_combo.count() == 0
    assert not window.record_button.isEnabled()
    assert window.refresh_microphones_button.isEnabled()
    assert window.status_label.text() == "Não foi possível detectar microfones."
    window.close()


def test_microphone_provider_failure_omits_raw_exception_and_secret(qapp) -> None:
    secret = "secret-token-provider-leak-1234"
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        microphone_provider=lambda: (_ for _ in ()).throw(
            RuntimeError(f"falha interna com segredo: {secret}")
        ),
    )

    assert window.status_label.text() == "Não foi possível detectar microfones."
    assert secret not in window.status_label.text()
    assert "RuntimeError" not in window.status_label.text()
    assert "falha interna" not in window.status_label.text()
    assert secret not in window.audio_debug.toPlainText()
    assert secret not in window.payload_debug.toPlainText()
    assert secret not in window.return_debug.toPlainText()
    assert secret not in window.usage_debug.toPlainText()

    assert window.microphone_combo.count() == 0
    assert not window.record_button.isEnabled()
    assert window.refresh_microphones_button.isEnabled()
    window.close()


def test_microphone_selection_prioritizes_headset_over_remembered_current_and_internal(qapp) -> None:
    dev_internal = AudioDevice(0, "Built-in Microphone", 2, True, host_api="ALSA", kind="internal")
    dev_headset = AudioDevice(1, "Bluetooth Headset", 1, False, host_api="ALSA", kind="headset")
    dev_other = AudioDevice(2, "USB Audio", 1, False, host_api="ALSA", kind="other")

    local_store = FakeLocalStore()
    local_store.mic_identity = dev_internal.identity
    recorder = FakeRecorder()

    devices = [dev_internal, dev_headset, dev_other]
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        local_store=local_store,
        microphone_provider=lambda: tuple(devices),
    )

    # Initial selection must be headset (index 1 in devices list, device.index == 1)
    assert window.microphone_combo.currentIndex() == 1
    assert window.microphone_combo.currentData() == 1
    assert recorder.selected_devices[-1] == 1

    # Manually switch selection to USB audio (index 2 in combo)
    window.microphone_combo.setCurrentIndex(2)
    assert window.microphone_combo.currentData() == 2
    assert recorder.selected_devices[-1] == 2

    # Refresh microphones: headset still present -> headset beats current session and remembered
    window._refresh_microphones()
    assert window.microphone_combo.currentIndex() == 1
    assert window.microphone_combo.currentData() == 1
    assert recorder.selected_devices[-1] == 1
    window.close()


def test_microphone_selection_restores_remembered_identity_when_headset_removed(qapp) -> None:
    dev_internal = AudioDevice(0, "Built-in Microphone", 2, True, host_api="ALSA", kind="internal")
    dev_headset = AudioDevice(1, "Bluetooth Headset", 1, False, host_api="ALSA", kind="headset")
    dev_other = AudioDevice(2, "USB Audio", 1, False, host_api="ALSA", kind="other")

    local_store = FakeLocalStore()
    local_store.mic_identity = dev_other.identity
    provider_state = {"devices": (dev_internal, dev_headset, dev_other)}

    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=local_store,
        microphone_provider=lambda: provider_state["devices"],
    )

    # Headset is prioritized initially
    assert window.microphone_combo.currentIndex() == 1
    assert window.microphone_combo.currentData() == 1

    # Headset is disconnected, remaining devices: internal and other (which was remembered)
    provider_state["devices"] = (dev_internal, dev_other)
    window._refresh_microphones()

    # Remembered identity (dev_other) is restored over internal/default
    assert window.microphone_combo.currentIndex() == 1
    assert window.microphone_combo.currentData() == 2
    window.close()


def test_microphone_identity_persisted_only_after_successful_recorder_start(qapp) -> None:
    dev_usb = AudioDevice(5, "USB PodMic", 1, True, host_api="ALSA", kind="other")
    local_store = FakeLocalStore()
    recorder = FakeRecorder()

    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        local_store=local_store,
        microphone_provider=lambda: (dev_usb,),
    )

    # Creating window and refreshing microphones does NOT persist identity
    assert local_store.mic_identity is None

    # Start recording successfully -> identity is persisted
    window._start_recording()
    assert recorder.recording is True
    assert local_store.mic_identity == dev_usb.identity
    window._finish_recording()
    assert recorder.recording is False

    # Failed recorder.start does NOT overwrite or save identity
    local_store.mic_identity = "previously-persisted-identity"
    recorder_failing = FakeRecorder(fail_start=True)
    dev_new = AudioDevice(9, "New Mic", 1, True, host_api="ALSA", kind="other")
    window_fail, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder_failing,
        local_store=local_store,
        microphone_provider=lambda: (dev_new,),
    )

    window_fail._start_recording()
    assert window_fail.state is AppState.ERROR
    assert "Não foi possível acessar o microfone." in window_fail.status_label.text()
    assert local_store.mic_identity == "previously-persisted-identity"
    window.close()
    window_fail.close()


def test_diagnostic_panel_contains_five_preserved_widgets(qapp) -> None:
    window, _ = make_window(qapp)
    widgets = {
        window.audio_debug,
        window.payload_debug,
        window.return_debug,
        window.usage_debug,
        window.usage_chart,
    }
    assert len(widgets) == 5
    assert isinstance(window.usage_chart, TokenUsageChart)
    assert [window.diagnostic_tabs.tabText(index) for index in range(4)] == [
        "Áudio",
        "Payload",
        "Retorno",
        "Consumo",
    ]
    window.close()


def test_diagnostic_panel_renders_with_positive_dimensions(qapp) -> None:
    window, _ = make_window(qapp)
    qapp.processEvents()
    assert window.diagnostic_tabs.width() > 0
    assert window.diagnostic_tabs.height() > 0
    assert window.usage_chart.width() > 0
    assert window.usage_chart.height() > 0
    for index, editor in enumerate(
        (
            window.audio_debug,
            window.payload_debug,
            window.return_debug,
            window.usage_debug,
        )
    ):
        window.diagnostic_tabs.setCurrentIndex(index)
        qapp.processEvents()
        assert editor.width() > 0
        assert editor.height() > 0
    window.close()


def test_usage_debug_recorded_and_rendered_on_success(qapp) -> None:
    local_store = FakeLocalStore()
    window, _ = make_window(qapp, local_store=local_store)
    usage = TokenUsage(
        input_tokens=10,
        output_tokens=4,
        thought_tokens=0,
        cached_tokens=0,
        tool_use_tokens=0,
        total_tokens=14,
    )
    debug = make_debug(usage=usage)

    window._on_transcription_finished("transcrito com sucesso", debug)

    assert len(local_store.records) == 1
    assert local_store.records[0] == ("synthetic-model", usage, "success")

    usage_text = window.usage_debug.toPlainText()
    assert "Chamada atual:" in usage_text
    assert "Modelo: synthetic-model" in usage_text
    assert "Entrada: 10" in usage_text
    assert "Saída: 4" in usage_text
    assert "Pensamento: 0" in usage_text
    assert "Cache: 0" in usage_text
    assert "Ferramentas: 0" in usage_text
    assert "Total: 14" in usage_text
    assert "Total acumulado:" in usage_text
    assert "Total: 14" in usage_text

    assert "10" not in window.payload_debug.toPlainText()
    assert "14" not in window.payload_debug.toPlainText()
    assert "Tokens" not in window.return_debug.toPlainText()
    assert "14" not in window.return_debug.toPlainText()

    usage2 = TokenUsage(
        input_tokens=5,
        output_tokens=2,
        thought_tokens=0,
        cached_tokens=0,
        tool_use_tokens=0,
        total_tokens=7,
    )
    debug2 = make_debug(usage=usage2)
    window._on_transcription_finished("segunda transcrição", debug2)
    assert len(local_store.records) == 2
    usage_text2 = window.usage_debug.toPlainText()
    assert "Entrada: 5" in usage_text2
    assert "Total: 7" in usage_text2
    assert "Total: 21" in usage_text2
    assert len(window.usage_chart.records) == 2
    assert window.usage_chart.records[0].outcome == "success"
    assert window.usage_chart.records[1].outcome == "success"
    assert window.usage_chart.status_message == ""
    window.close()

def test_usage_debug_recorded_and_rendered_on_failure(qapp) -> None:
    local_store = FakeLocalStore()
    window, _ = make_window(qapp, local_store=local_store)
    usage = TokenUsage(
        input_tokens=20,
        output_tokens=0,
        thought_tokens=0,
        cached_tokens=0,
        tool_use_tokens=0,
        total_tokens=20,
    )
    debug = make_debug(usage=usage)

    window._on_transcription_failed("A API Gemini retornou uma resposta sem texto.", debug)

    assert len(local_store.records) == 1
    assert local_store.records[0] == ("synthetic-model", usage, "error")

    usage_text = window.usage_debug.toPlainText()
    assert "Entrada: 20" in usage_text
    assert "Total: 20" in usage_text
    assert window.state is AppState.ERROR
    assert "sem texto" in window.status_label.text()
    assert len(window.usage_chart.records) == 1
    assert window.usage_chart.records[0].outcome == "error"
    assert window.usage_chart.records[0].total_tokens == 20
    assert window.usage_chart.status_message == ""
    window.close()

def test_no_usage_does_not_record_to_store_or_crash(qapp) -> None:
    local_store = FakeLocalStore()
    window, _ = make_window(qapp, local_store=local_store)

    debug_no_usage = make_debug(usage=None)
    window._on_transcription_finished("texto", debug_no_usage)
    assert local_store.records == []
    assert window.usage_debug.toPlainText() == "metadados de consumo não fornecidos"

    window._on_transcription_failed("erro", None)
    assert local_store.records == []
    assert window.usage_debug.toPlainText() == "metadados de consumo não fornecidos"
    assert window.usage_chart.records == ()
    assert window.usage_chart.status_message == "Nenhum registro de consumo no histórico."
    window.close()

def test_usage_debug_cleared_on_new_recording(qapp) -> None:
    local_store = FakeLocalStore()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=local_store,
    )
    usage = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
    debug = make_debug(usage=usage)
    window._on_transcription_finished("texto", debug)
    assert window.usage_debug.toPlainText() != ""

    window._start_recording()
    assert window.usage_debug.toPlainText() == ""
    assert window.payload_debug.toPlainText() == ""
    assert window.return_debug.toPlainText() == ""
    window.close()


def test_usage_with_none_fields_displays_indisponivel(qapp) -> None:
    local_store = FakeLocalStore()
    window, _ = make_window(qapp, local_store=local_store)
    usage = TokenUsage(
        input_tokens=8,
        output_tokens=None,
        thought_tokens=None,
        cached_tokens=None,
        tool_use_tokens=None,
        total_tokens=8,
    )
    debug = make_debug(usage=usage)
    window._on_transcription_finished("texto", debug)

    usage_text = window.usage_debug.toPlainText()
    assert "Entrada: 8" in usage_text
    assert "Saída: indisponível" in usage_text
    assert "Pensamento: indisponível" in usage_text
    assert "Cache: indisponível" in usage_text
    assert "Ferramentas: indisponível" in usage_text
    assert "Total: 8" in usage_text
    window.close()


def test_usage_store_failure_displays_diagnostic_and_preserves_flow(qapp) -> None:
    local_store = FakeLocalStore(fail_record=True)
    window, _ = make_window(qapp, local_store=local_store)
    usage = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
    debug = make_debug(usage=usage)

    window._on_transcription_finished("texto ok", debug)

    assert window.state is AppState.READY
    assert "Transcrição pronta" in window.status_label.text()
    assert window.editor.toPlainText() == "texto ok"
    assert "Não foi possível persistir o consumo de tokens." in window.usage_debug.toPlainText()
    assert window.usage_chart.status_message == "Não foi possível persistir o consumo de tokens."
    window.close()

def test_usage_without_local_store_renders_current_usage_and_indisponivel_totals(qapp) -> None:
    window, _ = make_window(qapp, local_store=None)
    usage = TokenUsage(input_tokens=12, output_tokens=3, total_tokens=15)
    debug = make_debug(usage=usage)

    window._on_transcription_finished("texto", debug)

    usage_text = window.usage_debug.toPlainText()
    assert "Chamada atual:" in usage_text
    assert "Entrada: 12" in usage_text
    assert "Saída: 3" in usage_text
    assert "Total: 15" in usage_text
    assert "Total acumulado: indisponível" in usage_text
    assert window.usage_chart.records == ()
    assert window.usage_chart.status_message == "Histórico local indisponível."
    window.close()


def test_token_usage_chart_empty_state_renders_safely(qapp) -> None:
    chart = TokenUsageChart()
    chart.set_history((), status_message="")
    assert chart.records == ()
    assert chart.status == ""
    pixmap = chart.grab()
    assert pixmap.width() > 0
    assert pixmap.height() > 0


def test_token_usage_chart_status_message_state_renders_safely(qapp) -> None:
    chart = TokenUsageChart()
    chart.set_status_message("Histórico local indisponível.")
    assert chart.status == "Histórico local indisponível."
    pixmap = chart.grab()
    assert pixmap.width() > 0
    assert pixmap.height() > 0


def test_token_usage_chart_known_records_distinguish_success_and_error(qapp) -> None:
    chart = TokenUsageChart()
    rec1 = TokenUsageRecord(
        id=1,
        recorded_at="2026-08-23T10:00:00Z",
        model="gemini-3.7-flash",
        input_tokens=10,
        output_tokens=4,
        thought_tokens=0,
        cached_tokens=0,
        tool_use_tokens=0,
        total_tokens=14,
        outcome="success",
    )
    rec2 = TokenUsageRecord(
        id=2,
        recorded_at="2026-08-23T10:05:00Z",
        model="gemini-3.7-flash",
        input_tokens=20,
        output_tokens=0,
        thought_tokens=0,
        cached_tokens=0,
        tool_use_tokens=0,
        total_tokens=20,
        outcome="error",
    )
    chart.set_records((rec1, rec2))
    assert chart.records == (rec1, rec2)
    assert chart.status_message == ""

    success_colors = chart.get_outcome_colors(rec1.outcome)
    error_colors = chart.get_outcome_colors(rec2.outcome)
    unknown_colors = chart.get_outcome_colors("unknown")

    assert success_colors == TokenUsageChart.OUTCOME_COLORS["success"]
    assert error_colors == TokenUsageChart.OUTCOME_COLORS["error"]
    assert success_colors != error_colors
    assert success_colors != unknown_colors
    assert error_colors != unknown_colors

    pixmap = chart.grab()
    assert pixmap.width() > 0
    assert pixmap.height() > 0
    image = pixmap.toImage()

    assert len(chart.last_rendered_bar_rects) == 2
    c1 = chart.last_rendered_bar_rects[0].center()
    c2 = chart.last_rendered_bar_rects[1].center()
    color1 = image.pixelColor(int(c1.x()), int(c1.y()))
    color2 = image.pixelColor(int(c2.x()), int(c2.y()))

    assert color1 == TokenUsageChart.SUCCESS_FILL_COLOR
    assert color2 == TokenUsageChart.ERROR_FILL_COLOR
    assert color1 != color2


def test_token_usage_chart_null_or_unknown_totals_render_safely(qapp) -> None:
    chart = TokenUsageChart()
    rec_unknown = TokenUsageRecord(
        id=1,
        recorded_at="2026-08-23T10:00:00Z",
        model="gemini-3.7-flash",
        input_tokens=10,
        output_tokens=None,
        thought_tokens=None,
        cached_tokens=None,
        tool_use_tokens=None,
        total_tokens=None,
        outcome="success",
    )
    chart.set_records((rec_unknown,))
    assert rec_unknown.total_tokens is None

    unknown_colors = TokenUsageChart.get_outcome_colors("unknown")
    none_colors = TokenUsageChart.get_outcome_colors(None)
    assert unknown_colors == TokenUsageChart.OUTCOME_COLORS["unknown"]
    assert none_colors == TokenUsageChart.OUTCOME_COLORS["unknown"]
    assert TokenUsageChart.UNKNOWN_FILL_COLOR == unknown_colors[0]
    assert TokenUsageChart.UNKNOWN_BORDER_COLOR == unknown_colors[1]

    pixmap = chart.grab()
    assert pixmap.width() > 0
    assert pixmap.height() > 0
    image = pixmap.toImage()

    assert len(chart.last_rendered_bar_rects) == 1
    c = chart.last_rendered_bar_rects[0].center()
    color = image.pixelColor(int(c.x()), int(c.y()))
    assert color == TokenUsageChart.UNKNOWN_FILL_COLOR


def test_token_usage_chart_legend_and_outcome_color_mappings(qapp) -> None:
    labels = [item[0] for item in TokenUsageChart.LEGEND_ITEMS]
    keys = [item[1] for item in TokenUsageChart.LEGEND_ITEMS]
    dashed_flags = [item[2] for item in TokenUsageChart.LEGEND_ITEMS]

    assert "Sucesso" in labels
    assert "Erro" in labels
    assert "Indisponível" in labels

    assert keys == ["success", "error", "unknown"]
    assert dashed_flags == [False, False, True]

    fills = {k: colors[0] for k, colors in TokenUsageChart.OUTCOME_COLORS.items()}
    borders = {k: colors[1] for k, colors in TokenUsageChart.OUTCOME_COLORS.items()}

    assert len({f.name() for f in fills.values()}) == 3
    assert len({b.name() for b in borders.values()}) == 3

    chart = TokenUsageChart()
    chart.resize(200, 140)
    rec_ok = TokenUsageRecord(1, "t1", "m", 10, 5, 0, 0, 0, 15, "success")
    rec_err = TokenUsageRecord(2, "t2", "m", 5, 0, 0, 0, 0, 5, "error")
    rec_unk = TokenUsageRecord(3, "t3", "m", 0, 0, 0, 0, 0, None, "other")
    chart.set_records((rec_ok, rec_err, rec_unk))
    pixmap = chart.grab()
    assert not pixmap.isNull()
    assert pixmap.width() >= 200
    assert pixmap.height() >= 140
    image = pixmap.toImage()

    # Assert visual seam populated
    assert set(chart.last_rendered_legend_rects.keys()) == {"success", "error", "unknown"}
    assert set(chart.last_rendered_legend_text_rects.keys()) == {"success", "error", "unknown"}
    assert len(chart.last_rendered_bar_rects) == 3
    assert chart.last_rendered_plot_rect is not None

    # Assert legend geometry fits strictly within minimum width 200 with no clipping
    for k in ("success", "error", "unknown"):
        icon_rect = chart.last_rendered_legend_rects[k]
        text_rect = chart.last_rendered_legend_text_rects[k]
        assert icon_rect.left() >= 0
        assert icon_rect.right() <= 200
        assert text_rect.left() >= 0
        assert text_rect.right() <= 200
        assert icon_rect.top() >= 0
        assert text_rect.bottom() <= chart.last_rendered_plot_rect.top()

    # Verify pixel colors at center of legend icons
    c_ok = chart.last_rendered_legend_rects["success"].center()
    c_err = chart.last_rendered_legend_rects["error"].center()
    c_unk = chart.last_rendered_legend_rects["unknown"].center()
    color_ok = image.pixelColor(int(c_ok.x()), int(c_ok.y()))
    color_err = image.pixelColor(int(c_err.x()), int(c_err.y()))
    color_unk = image.pixelColor(int(c_unk.x()), int(c_unk.y()))

    assert color_ok == TokenUsageChart.SUCCESS_FILL_COLOR
    assert color_err == TokenUsageChart.ERROR_FILL_COLOR
    assert color_unk == TokenUsageChart.UNKNOWN_FILL_COLOR
    assert color_ok != color_err
    assert color_ok != color_unk
    assert color_err != color_unk

    # Verify pixel colors at center of bars
    bar_c_ok = chart.last_rendered_bar_rects[0].center()
    bar_c_err = chart.last_rendered_bar_rects[1].center()
    bar_c_unk = chart.last_rendered_bar_rects[2].center()
    bar_color_ok = image.pixelColor(int(bar_c_ok.x()), int(bar_c_ok.y()))
    bar_color_err = image.pixelColor(int(bar_c_err.x()), int(bar_c_err.y()))
    bar_color_unk = image.pixelColor(int(bar_c_unk.x()), int(bar_c_unk.y()))

    assert bar_color_ok == TokenUsageChart.SUCCESS_FILL_COLOR
    assert bar_color_err == TokenUsageChart.ERROR_FILL_COLOR
    assert bar_color_unk == TokenUsageChart.UNKNOWN_FILL_COLOR
    assert bar_color_ok != bar_color_err
    assert bar_color_ok != bar_color_unk
    assert bar_color_err != bar_color_unk


def test_token_usage_chart_layout_at_various_widths(qapp) -> None:
    chart = TokenUsageChart()
    rec_ok = TokenUsageRecord(1, "t1", "m", 10, 5, 0, 0, 0, 15, "success")
    rec_err = TokenUsageRecord(2, "t2", "m", 5, 0, 0, 0, 0, 5, "error")
    rec_unk = TokenUsageRecord(3, "t3", "m", 0, 0, 0, 0, 0, None, "other")
    chart.set_records((rec_ok, rec_err, rec_unk))

    # Test width 200: all legend items fit inside 200px (wrapping across lines)
    chart.resize(200, 140)
    chart.grab()
    assert chart.last_rendered_plot_rect is not None
    assert chart.last_rendered_plot_rect.width() > 10
    assert chart.last_rendered_plot_rect.height() > 10
    for k in ("success", "error", "unknown"):
        assert chart.last_rendered_legend_rects[k].right() <= 200
        assert chart.last_rendered_legend_text_rects[k].right() <= 200

    # Test width 280: sizeHint width
    chart.resize(280, 180)
    chart.grab()
    assert chart.last_rendered_plot_rect is not None
    for k in ("success", "error", "unknown"):
        assert chart.last_rendered_legend_rects[k].right() <= 280
        assert chart.last_rendered_legend_text_rects[k].right() <= 280

    # Test width 500: wide dock
    chart.resize(500, 200)
    chart.grab()
    assert chart.last_rendered_plot_rect is not None
    for k in ("success", "error", "unknown"):
        assert chart.last_rendered_legend_rects[k].right() <= 500
        assert chart.last_rendered_legend_text_rects[k].right() <= 500

    # Clearing or status message clears seam
    chart.set_status_message("Erro no carregamento")
    chart.grab()
    assert chart.last_rendered_legend_rects == {}
    assert chart.last_rendered_legend_text_rects == {}
    assert chart.last_rendered_bar_rects == ()
    assert chart.last_rendered_plot_rect is None


def test_token_usage_chart_small_size_boundary(qapp) -> None:
    chart = TokenUsageChart()
    chart.resize(10, 10)
    pixmap = chart.grab()
    assert not pixmap.isNull()

def test_token_usage_chart_refreshed_on_startup(qapp) -> None:
    local_store = FakeLocalStore()
    usage = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
    local_store.record_token_usage("gemini-3.7-flash", usage, "success")

    window, _ = make_window(qapp, local_store=local_store)
    assert len(window.usage_chart.records) == 1
    assert window.usage_chart.records[0].total_tokens == 15
    assert window.usage_chart.records[0].outcome == "success"
    assert window.usage_chart.status_message == ""
    window.close()


def test_token_usage_chart_history_load_failure_handled(qapp) -> None:
    local_store = FakeLocalStore(fail_history=True)
    window, _ = make_window(qapp, local_store=local_store)
    assert window.usage_chart.records == ()
    assert window.usage_chart.status_message == "Não foi possível carregar o histórico de consumo."
    window.close()

def test_close_window_closes_local_store_and_absorbs_store_error(qapp) -> None:
    store1 = FakeLocalStore()
    window1, _ = make_window(qapp, local_store=store1)
    window1.close()
    assert store1.closed is True

    store2 = FakeLocalStore(fail_close=True)
    window2, _ = make_window(qapp, local_store=store2)
    window2.close()


class FakeRunningThread:
    def __init__(self, *, timeout_first: bool = False) -> None:
        self.timeout_first = timeout_first
        self.quit_called = False
        self.wait_calls: list[int | None] = []
        self._running = True

    def isRunning(self) -> bool:
        return self._running

    def quit(self) -> None:
        self.quit_called = True

    def wait(self, msecs: int | None = None) -> bool:
        self.wait_calls.append(msecs)
        if msecs is not None and self.timeout_first and len(self.wait_calls) == 1:
            return False
        self._running = False
        return True


def test_close_waits_for_running_thread_before_closing_store(qapp) -> None:
    store = FakeLocalStore()
    window, _ = make_window(qapp, local_store=store)
    thread = FakeRunningThread(timeout_first=False)
    window._thread = thread  # type: ignore[assignment]

    close_event = QCloseEvent()
    window.closeEvent(close_event)

    assert thread.quit_called is True
    assert thread.wait_calls == [5000]
    assert thread.isRunning() is False
    assert store.closed is True
    assert close_event.isAccepted() is True


def test_close_handles_thread_wait_timeout_with_final_wait_before_store_close(qapp) -> None:
    store = FakeLocalStore()
    window, _ = make_window(qapp, local_store=store)
    thread = FakeRunningThread(timeout_first=True)
    window._thread = thread  # type: ignore[assignment]

    close_event = QCloseEvent()
    window.closeEvent(close_event)

    assert thread.quit_called is True
    assert thread.wait_calls == [5000, None]
    assert thread.isRunning() is False
    assert store.closed is True
    assert close_event.isAccepted() is True


def test_on_media_error_ignores_raw_error_string_and_secret(qapp) -> None:
    secret = "secret-token-media-error-3333"
    window, _ = make_window(qapp)
    window._on_media_error(
        object(),
        f"GStreamer critical pipeline failure with {secret}",
    )

    assert window.status_label.text() == "Não foi possível reproduzir o áudio capturado."
    assert secret not in window.status_label.text()
    assert "GStreamer" not in window.status_label.text()
    assert secret not in window.audio_debug.toPlainText()
    assert secret not in window.payload_debug.toPlainText()
    assert secret not in window.return_debug.toPlainText()
    assert secret not in window.usage_debug.toPlainText()
    window.close()


def test_start_recording_backend_error_omits_raw_exception_and_secret(qapp) -> None:
    secret = "secret-token-mic-start-ui-4444"
    recorder = FakeRecorder(fail_start=True)
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
    )
    window._start_recording()

    assert window.state is AppState.ERROR
    assert window.status_label.text() == "Não foi possível acessar o microfone."
    assert secret not in window.status_label.text()
    assert secret not in window.audio_debug.toPlainText()
    window.close()


def test_finish_recording_backend_stop_error_omits_raw_exception_and_secret(qapp) -> None:
    secret = "secret-token-mic-stop-ui-5555"
    recorder = FakeRecorder(
        fail_stop_error=AudioRecorderError("Não foi possível parar o microfone.")
    )
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
    )
    window._start_recording()
    window._finish_recording()

    assert window.state is AppState.ERROR
    assert window.status_label.text() == "Não foi possível parar o microfone."
    assert secret not in window.status_label.text()
    assert secret not in window.audio_debug.toPlainText()
    window.close()


def test_send_to_terminal_failure_omits_raw_exception_and_secret(qapp) -> None:
    secret = "secret-token-terminal-ui-6666"
    terminal = FakeTerminal(
        fail_error=TerminalBridgeError("Não foi possível colar no terminal.")
    )
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        terminal=terminal,
    )
    window.editor.setPlainText("Texto para colar")
    window.state = AppState.READY
    window.send_to_terminal()

    assert window.status_label.text() == "Não foi possível colar no terminal."
    assert secret not in window.status_label.text()
    assert secret not in window.audio_debug.toPlainText()
    assert secret not in window.payload_debug.toPlainText()
    assert secret not in window.return_debug.toPlainText()
    assert secret not in window.usage_debug.toPlainText()

def test_default_window_has_no_global_shortcuts(qapp) -> None:
    store = FakeLocalStore()
    bridge = FakeInputShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        input_shortcut_bridge=bridge,
    )
    assert window._active_mouse_button is None
    assert window._active_keyboard_shortcut is None
    assert window.shortcut_indicator_label.text() == "Atalhos globais: 0 ativos"
    window.close()


def test_restore_dual_shortcuts_waits_for_each_binding_ack(qapp) -> None:
    store = FakeLocalStore()
    store.mouse_button = "x1"
    store.keyboard_shortcut = "ctrl+alt+r"
    bridge = FakeInputShortcutBridge(auto_ack=False)
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        input_shortcut_bridge=bridge,
    )
    assert window._active_mouse_button is None
    assert window._active_keyboard_shortcut is None
    mouse_generation = bridge.mouse_generation
    keyboard_generation = bridge.keyboard_generation
    bridge.mouse_binding_ready.emit(mouse_generation, "x1")
    assert window._active_mouse_button == "x1"
    assert window._active_keyboard_shortcut is None
    bridge.keyboard_binding_ready.emit(keyboard_generation, "ctrl+alt+r")
    assert window._active_keyboard_shortcut == "ctrl+alt+r"
    assert window.shortcut_indicator_label.text() == "Atalhos globais: 2 ativos"
    window.close()


def test_legacy_primary_mouse_binding_is_cleared_without_touching_keyboard(qapp) -> None:
    store = FakeLocalStore()
    store.mouse_button = "Button.left"
    store.keyboard_shortcut = "ctrl+alt+r"
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
    )
    assert store.mouse_button is None
    assert store.keyboard_shortcut == "ctrl+alt+r"
    assert window._active_mouse_button is None
    assert window._active_keyboard_shortcut == "ctrl+alt+r"
    assert (
        window.status_label.text()
        == "O atalho anterior usava um botão principal; configure um botão lateral ou central."
    )
    window.close()


def test_mouse_and_keyboard_persist_independently_after_ack(qapp) -> None:
    store = FakeLocalStore(fail_keyboard_save=True)
    bridge = FakeInputShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        input_shortcut_bridge=bridge,
    )
    window._activate_shortcut("mouse", "x1", persist=True)
    assert store.mouse_button == "x1"
    window._activate_shortcut("keyboard", "ctrl+alt+r", persist=True)
    assert window._active_mouse_button == "x1"
    assert window._active_keyboard_shortcut == "ctrl+alt+r"
    assert store.keyboard_shortcut is None
    assert "nesta sessão" in window.status_label.text()
    window.close()


def test_dual_activations_share_toggle_and_ignore_stale_busy_or_wrong_trigger(
    qapp,
) -> None:
    bridge = FakeInputShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        input_shortcut_bridge=bridge,
    )
    window._activate_shortcut("mouse", "x1", persist=False)
    window._activate_shortcut("keyboard", "ctrl+alt+r", persist=False)
    toggles: list[str] = []
    window._toggle_recording = lambda: toggles.append("toggle")
    bridge.mouse_activated.emit(bridge.mouse_generation - 1, "x1")
    bridge.mouse_activated.emit(bridge.mouse_generation, "x2")
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    bridge.keyboard_activated.emit(bridge.keyboard_generation, "ctrl+alt+r")
    assert toggles == ["toggle", "toggle"]
    window.state = AppState.TRANSCRIBING
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    assert toggles == ["toggle", "toggle"]
    window.close()


def test_disabling_keyboard_after_stop_ack_preserves_mouse(qapp) -> None:
    store = FakeLocalStore()
    bridge = FakeInputShortcutBridge(auto_ack=False)
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        input_shortcut_bridge=bridge,
    )
    window._activate_shortcut("mouse", "x1", persist=True)
    bridge.mouse_binding_ready.emit(bridge.mouse_generation, "x1")
    window._activate_shortcut("keyboard", "f12", persist=True)
    bridge.keyboard_binding_ready.emit(bridge.keyboard_generation, "f12")
    window._deactivate_shortcut("keyboard")
    stop_generation = bridge.keyboard_generation
    assert window._active_keyboard_shortcut == "f12"
    bridge.stopped.emit("keyboard", stop_generation)
    assert window._active_keyboard_shortcut is None
    assert window._active_mouse_button == "x1"
    assert store.mouse_button == "x1"
    assert store.keyboard_shortcut is None
    window.close()


def test_capture_dialog_accepts_mouse_and_restores_previous_on_cancel(
    qapp, monkeypatch
) -> None:
    bridge = FakeInputShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        input_shortcut_bridge=bridge,
    )
    window._activate_shortcut("mouse", "x1", persist=False)

    def capture_exec(_dialog: QDialog) -> int:
        bridge.mouse_captured.emit(bridge.mouse_generation, "x2")
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(QDialog, "exec", capture_exec)
    window._capture_shortcut("mouse")
    assert window._active_mouse_button == "x2"

    monkeypatch.setattr(
        QDialog, "exec", lambda _dialog: int(QDialog.DialogCode.Rejected)
    )
    command_count = len(bridge.commands)
    window._capture_shortcut("mouse")
    assert bridge.commands[command_count][0] == "capture_mouse"
    assert bridge.commands[-1][0:3] == (
        "watch_mouse",
        bridge.mouse_generation,
        "x2",
    )
    window.close()


def test_keyboard_unsafe_key_keeps_capture_dialog_open(qapp) -> None:
    bridge = FakeInputShortcutBridge()
    window, _ = make_window(qapp, input_shortcut_bridge=bridge)
    dialog = QDialog(window)
    status = QLineEdit(dialog)
    window._capture_dialog = dialog
    window._capture_status_label = status
    window._capture_kind = "keyboard"
    window._capture_generation = 7
    bridge.failed.emit("keyboard", 7, "Não foi possível ativar o atalho global.")
    assert dialog.result() == 0
    assert "isolado" in status.text()
    window._capture_dialog = None
    window._capture_status_label = None
    window.close()


def test_successful_authorization_reconnects_and_resumes_original_capture(
    qapp, monkeypatch
) -> None:
    bridge = FakeInputShortcutBridge(ready=False)
    installer = FakeShortcutInstaller()
    window, _ = make_window(
        qapp,
        input_shortcut_bridge=bridge,
        shortcut_service_installer=installer,
    )
    resumed: list[str] = []
    monkeypatch.setattr(window, "_capture_shortcut", resumed.append)
    window._pending_authorization_kind = "keyboard"
    installer.finished.emit(True, "")
    qapp.processEvents()
    assert bridge.reconnect_count == 1
    assert resumed == ["keyboard"]
    window.close()


def test_configuration_requests_are_ignored_while_busy(qapp, monkeypatch) -> None:
    window, _ = make_window(qapp)
    opened: list[bool] = []
    monkeypatch.setattr(
        window, "_show_shortcut_authorization_dialog", lambda: opened.append(True)
    )
    window.input_shortcut_bridge.ready = False
    for state in (AppState.RECORDING, AppState.TRANSCRIBING):
        window.state = state
        window._request_shortcut_configuration("mouse")
        window._request_shortcut_configuration("keyboard")
    assert opened == []
    window.close()


def test_close_cancels_installer_and_bridge_before_store(qapp) -> None:
    order: list[str] = []
    store = FakeLocalStore(order_log=order)
    bridge = FakeInputShortcutBridge(order_log=order)
    installer = FakeShortcutInstaller(order_log=order)
    window, _ = make_window(
        qapp,
        local_store=store,
        input_shortcut_bridge=bridge,
        shortcut_service_installer=installer,
    )
    window.close()
    assert order == ["installer", "bridge", "store"]


def test_main_window_layout_settings_fullscreen_and_grabs(qapp) -> None:
    window, _ = make_window(qapp)
    assert window.size().width() == 1120
    assert window.size().height() == 700
    assert window.minimumWidth() == 760
    assert window.minimumHeight() == 560
    assert window.editor.minimumHeight() == 120
    assert window.editor.maximumHeight() == 190
    assert not window.main_splitter.childrenCollapsible()
    assert not window.diagnostic_splitter.childrenCollapsible()
    assert window.settings_button.accessibleName() == "Configurações"
    assert window.fullscreen_button.accessibleName() == "Entrar em tela cheia"
    assert not window.grab().isNull()
    window.resize(760, 560)
    qapp.processEvents()
    assert window.editor.height() <= 190
    assert not window.grab().isNull()

    observed: dict[str, object] = {}

    def inspect_settings() -> None:
        dialog = window._settings_dialog
        assert dialog is not None
        observed["titles"] = [
            group.title() for group in dialog.findChildren(QGroupBox)
        ]
        observed["parents"] = [
            window.configure_key_button.parent(),
            window.model_combo.parent(),
            window.apply_model_button.parent(),
            window.configure_mouse_button.parent(),
            window.configure_keyboard_button.parent(),
            window.install_update_button.parent(),
        ]
        dialog.reject()

    QTimer.singleShot(0, inspect_settings)
    window._open_settings_dialog()
    assert observed["titles"] == [
        "Chave API",
        "Modelo Gemini",
        "Atalho do mouse",
        "Atalho do teclado",
        "Atualizações",
    ]
    assert all(isinstance(parent, QGroupBox) for parent in observed["parents"])

    window._toggle_fullscreen()
    qapp.processEvents()
    assert window.isFullScreen()
    assert window.fullscreen_button.accessibleName() == "Sair da tela cheia"
    window._toggle_fullscreen()
    qapp.processEvents()
    assert not window.isFullScreen()
    window.close()


def test_mouse_rejected_button_keeps_capture_dialog_open(qapp) -> None:
    for message, marker in (
        (PRIMARY_MOUSE_BUTTON_MESSAGE, "esquerdo"),
        (UNSUPPORTED_MOUSE_BUTTON_MESSAGE, "remapeie"),
    ):
        bridge = FakeInputShortcutBridge()
        window, _ = make_window(qapp, input_shortcut_bridge=bridge)
        dialog = QDialog(window)
        status = QLineEdit(dialog)
        window._capture_dialog = dialog
        window._capture_status_label = status
        window._capture_kind = "mouse"
        window._capture_generation = 7

        bridge.failed.emit("mouse", 7, message)
        assert dialog.result() == 0
        assert marker in status.text()

        hinted = status.text()
        bridge.failed.emit("mouse", 8, message)
        assert status.text() == hinted
        assert window.status_label.text() == message

        window._capture_dialog = None
        window._capture_status_label = None
        window.close()


def test_mouse_capture_timeout_hint_respects_generation_and_existing_text(qapp) -> None:
    bridge = FakeInputShortcutBridge()
    window, _ = make_window(qapp, input_shortcut_bridge=bridge)
    status = QLineEdit(window)
    status.setText(CAPTURE_WAITING_TEXT)
    window._capture_status_label = status
    window._capture_kind = "mouse"
    window._capture_generation = 7

    window._hint_capture_timeout("mouse", 8)
    assert status.text() == CAPTURE_WAITING_TEXT
    window._hint_capture_timeout("keyboard", 7)
    assert status.text() == CAPTURE_WAITING_TEXT

    window._hint_capture_timeout("mouse", 7)
    assert "firmware" in status.text()

    already = status.text()
    window._hint_capture_timeout("mouse", 7)
    assert status.text() == already

    window._capture_status_label = None
    window.close()


def test_global_shortcut_raises_minimized_window_before_toggling(qapp) -> None:
    bridge = FakeInputShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        input_shortcut_bridge=bridge,
    )
    window._activate_shortcut("mouse", "x1", persist=False)
    order: list[str] = []
    raise_to_front = window._raise_to_front
    window._raise_to_front = lambda: (order.append("raise"), raise_to_front())[1]
    window._toggle_recording = lambda: order.append("toggle")

    window.showMinimized()
    assert window.isMinimized() is True
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")

    assert order == ["raise", "toggle"]
    assert window.isMinimized() is False

    order.clear()
    window.state = AppState.TRANSCRIBING
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    assert order == []
    window.close()


def test_raise_to_front_preserves_fullscreen_and_shows_hidden_window(qapp) -> None:
    window, _ = make_window(qapp, settings=Settings(api_key="active-token"))
    window.showFullScreen()
    assert window.isFullScreen() is True
    window._raise_to_front()
    assert window.isFullScreen() is True

    window.showNormal()
    window.hide()
    assert window.isVisible() is False
    window._raise_to_front()
    assert window.isVisible() is True
    assert window.isMinimized() is False
    window.close()


def test_settings_dialog_five_groups_order_and_widgets(qapp) -> None:
    window, _ = make_window(qapp)
    observed: dict[str, object] = {}

    def inspect() -> None:
        dialog = window._settings_dialog
        assert dialog is not None
        observed["titles"] = [
            group.title() for group in dialog.findChildren(QGroupBox)
        ]
        dialog.reject()

    QTimer.singleShot(0, inspect)
    window._open_settings_dialog()
    assert observed["titles"] == [
        "Chave API",
        "Modelo Gemini",
        "Atalho do mouse",
        "Atalho do teclado",
        "Atualizações",
    ]
    window.close()


def test_settings_without_controller_shows_brew_instruction_and_disabled_button(
    qapp,
) -> None:
    window, _ = make_window(qapp, homebrew_update_controller=None)

    def inspect() -> None:
        dialog = window._settings_dialog
        assert dialog is not None
        assert (
            window.installed_version_label.text()
            == f"Versão instalada: {__version__}"
        )
        assert (
            window.update_status_label.text()
            == "Instale o FalaFácil com: brew install OthonBreener/falafacil/falafacil"
        )
        assert not window.update_progress_bar.isVisible()
        assert not window.install_update_button.isEnabled()
        window._on_install_updates_clicked()
        assert not window.install_update_button.isEnabled()
        dialog.reject()

    QTimer.singleShot(0, inspect)
    window._open_settings_dialog()
    window.close()


def test_install_updates_button_triggers_controller_and_disables_duplicate_clicks(
    qapp,
) -> None:
    fake_controller = FakeHomebrewUpdateController()
    window, _ = make_window(qapp, homebrew_update_controller=fake_controller)

    def inspect() -> None:
        dialog = window._settings_dialog
        assert dialog is not None
        assert window.install_update_button.isEnabled()
        assert not window.update_progress_bar.isVisible()
        window.install_update_button.click()
        assert fake_controller.install_calls == 1
        assert fake_controller.running is True
        assert not window.install_update_button.isEnabled()
        assert window.update_progress_bar.isVisible()
        assert window.update_progress_bar.minimum() == 0
        assert window.update_progress_bar.maximum() == 0

        # Duplicate click or direct invocation while running does nothing
        window.install_update_button.click()
        window._on_install_updates_clicked()
        assert fake_controller.install_calls == 1

        fake_controller.running = False
        fake_controller.up_to_date.emit("Você já usa a versão mais recente.")
        dialog.reject()

    QTimer.singleShot(0, inspect)
    window._open_settings_dialog()
    window.close()


def test_update_button_disabled_during_recording_and_transcribing(qapp) -> None:
    fake_controller = FakeHomebrewUpdateController()
    window, _ = make_window(qapp, homebrew_update_controller=fake_controller)

    def inspect() -> None:
        dialog = window._settings_dialog
        assert dialog is not None
        for busy_state in (AppState.RECORDING, AppState.TRANSCRIBING):
            window.state = busy_state
            window._update_actions()
            assert not window.install_update_button.isEnabled()
            assert window.settings_button.isEnabled()
            assert window.installed_version_label.isVisible()
            assert window.update_status_label.isVisible()

        window.state = AppState.IDLE
        window._update_actions()
        assert window.install_update_button.isEnabled()
        dialog.reject()

    QTimer.singleShot(0, inspect)
    window._open_settings_dialog()
    window.close()


def test_update_status_and_progress_updates_on_signals(qapp) -> None:
    fake_controller = FakeHomebrewUpdateController()
    window, _ = make_window(qapp, homebrew_update_controller=fake_controller)

    def inspect() -> None:
        dialog = window._settings_dialog
        assert dialog is not None

        fake_controller.status_changed.emit("Atualizando catálogo do Homebrew…")
        assert window.update_status_label.text() == "Atualizando catálogo do Homebrew…"

        fake_controller.running = False
        fake_controller.up_to_date.emit("Você já usa a versão mais recente.")
        assert window.update_status_label.text() == "Você já usa a versão mais recente."
        assert not window.update_progress_bar.isVisible()
        assert window.install_update_button.isEnabled()

        fake_controller.running = True
        fake_controller.status_changed.emit("Instalando atualização pelo Homebrew…")
        assert (
            window.update_status_label.text()
            == "Instalando atualização pelo Homebrew…"
        )

        fake_controller.running = False
        fake_controller.failed.emit(
            "O Homebrew não conseguiu concluir a atualização. Tente novamente."
        )
        assert (
            window.update_status_label.text()
            == "O Homebrew não conseguiu concluir a atualização. Tente novamente."
        )
        assert not window.update_progress_bar.isVisible()
        assert window.install_update_button.isEnabled()

        dialog.reject()

    QTimer.singleShot(0, inspect)
    window._open_settings_dialog()
    window.close()


def test_ready_to_restart_dialog_later_choice_keeps_window_open(qapp) -> None:
    fake_controller = FakeHomebrewUpdateController()
    window, _ = make_window(qapp, homebrew_update_controller=fake_controller)

    def handle_restart_dialog() -> None:
        for widget in QApplication.topLevelWidgets():
            if (
                isinstance(widget, QDialog)
                and widget.windowTitle() == "Atualização concluída"
            ):
                button_texts = [
                    btn.text() for btn in widget.findChildren(QPushButton)
                ]
                assert "Reiniciar agora" in button_texts
                assert "Mais tarde" in button_texts
                widget.reject()
                return
        QTimer.singleShot(10, handle_restart_dialog)

    QTimer.singleShot(0, handle_restart_dialog)
    fake_controller.running = False
    fake_controller.ready_to_restart.emit(
        "Atualização instalada. Reinicie o FalaFácil para usar a nova versão."
    )
    qapp.processEvents()

    assert fake_controller.restart_calls == 0
    assert window.isVisible() is True
    window.close()


def test_ready_to_restart_dialog_restart_success_closes_window(qapp) -> None:
    fake_controller = FakeHomebrewUpdateController(restart_result=True)
    window, _ = make_window(qapp, homebrew_update_controller=fake_controller)

    def handle_restart_dialog() -> None:
        for widget in QApplication.topLevelWidgets():
            if (
                isinstance(widget, QDialog)
                and widget.windowTitle() == "Atualização concluída"
            ):
                widget.accept()
                return
        QTimer.singleShot(10, handle_restart_dialog)

    QTimer.singleShot(0, handle_restart_dialog)
    fake_controller.running = False
    fake_controller.ready_to_restart.emit(
        "Atualização instalada. Reinicie o FalaFácil para usar a nova versão."
    )
    qapp.processEvents()

    assert fake_controller.restart_calls == 1
    assert window.isVisible() is False


def test_ready_to_restart_dialog_restart_failure_keeps_window_open_with_error(
    qapp,
) -> None:
    fake_controller = FakeHomebrewUpdateController(restart_result=False)
    window, _ = make_window(qapp, homebrew_update_controller=fake_controller)

    def handle_restart_dialog() -> None:
        for widget in QApplication.topLevelWidgets():
            if (
                isinstance(widget, QDialog)
                and widget.windowTitle() == "Atualização concluída"
            ):
                widget.accept()
                return
        QTimer.singleShot(10, handle_restart_dialog)

    QTimer.singleShot(0, handle_restart_dialog)
    fake_controller.running = False
    fake_controller.ready_to_restart.emit(
        "Atualização instalada. Reinicie o FalaFácil para usar a nova versão."
    )
    qapp.processEvents()

    assert fake_controller.restart_calls == 1
    assert window.isVisible() is True
    assert (
        window._update_status
        == "O Homebrew não conseguiu concluir a atualização. Tente novamente."
    )
    window.close()


def test_close_event_blocked_while_update_controller_is_running(qapp) -> None:
    order: list[str] = []
    store = FakeLocalStore(order_log=order)
    bridge = FakeInputShortcutBridge(order_log=order)
    installer = FakeShortcutInstaller(order_log=order)
    fake_controller = FakeHomebrewUpdateController(running=True)
    window, _ = make_window(
        qapp,
        local_store=store,
        input_shortcut_bridge=bridge,
        shortcut_service_installer=installer,
        homebrew_update_controller=fake_controller,
    )

    close_event = QCloseEvent()
    window.closeEvent(close_event)
    assert not close_event.isAccepted()
    assert window._is_closing is False
    assert (
        window.status_label.text()
        == "A atualização pelo Homebrew está em andamento. Aguarde a conclusão."
    )
    assert order == []

    fake_controller.running = False
    fake_controller.up_to_date.emit("Você já usa a versão mais recente.")
    window.close()
    assert order == ["installer", "bridge", "store"]


def test_settings_dialog_closed_during_running_receives_signals_and_reopening_reflects_state(
    qapp,
) -> None:
    fake_controller = FakeHomebrewUpdateController()
    window, _ = make_window(qapp, homebrew_update_controller=fake_controller)

    def start_and_close_dialog() -> None:
        dialog = window._settings_dialog
        assert dialog is not None
        window.install_update_button.click()
        dialog.reject()

    QTimer.singleShot(0, start_and_close_dialog)
    window._open_settings_dialog()
    assert fake_controller.running is True
    assert window._settings_dialog is None

    # Signals arrive while settings dialog is closed - must not crash
    fake_controller.status_changed.emit("Verificando versão disponível…")
    fake_controller.running = False
    fake_controller.failed.emit(
        "O Homebrew não conseguiu concluir a atualização. Tente novamente."
    )

    observed: dict[str, object] = {}

    def inspect_reopened() -> None:
        dialog = window._settings_dialog
        assert dialog is not None
        observed["status"] = window.update_status_label.text()
        observed["progress_visible"] = window.update_progress_bar.isVisible()
        observed["button_enabled"] = window.install_update_button.isEnabled()
        dialog.reject()

    QTimer.singleShot(0, inspect_reopened)
    window._open_settings_dialog()
    assert (
        observed["status"]
        == "O Homebrew não conseguiu concluir a atualização. Tente novamente."
    )
    assert observed["progress_visible"] is False
    assert observed["button_enabled"] is True
    window.close()
