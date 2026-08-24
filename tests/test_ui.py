from __future__ import annotations

import os
import sqlite3
import threading
import time
import numpy as np
import pytest
from PySide6.QtCore import QByteArray, QCoreApplication, QEvent, QObject, QPoint, QPointF, QThread, Qt, Signal
from PySide6.QtGui import QCloseEvent, QKeySequence, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from falafacil.audio import AudioCapture, AudioDevice, AudioRecorderError
from falafacil.config import Settings
from falafacil.credentials import CredentialStoreError
from falafacil.storage import LocalStore, LocalStoreError, TokenTotals, TokenUsageRecord
from falafacil.terminal import TerminalBridgeError
from falafacil.transcription import TokenUsage, TranscriptionDebug, TranscriptionError
from falafacil.shortcuts import MouseShortcutBridge
from falafacil.ui import AppState, MainWindow, TokenUsageChart, _ShortcutCaptureRelay

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
    ) -> None:
        self.text = text
        self.usage = usage
        self.error = error
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
class FakeMouseShortcutBridge(QObject):
    """Bridge fake padrão com suporte a eventos atômicos privados (espelho do MouseShortcutBridge)."""
    _activated_event = Signal(int, int, int)
    _button_captured_event = Signal(int, str, int, int)
    activated = Signal()
    button_captured = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        *,
        available: bool = True,
        fail_start: bool = False,
        fail_capture: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.available = available
        self.fail_start = fail_start
        self.fail_capture = fail_capture
        self.started_buttons: list[str] = []
        self.stop_count = 0
        self.capture_count = 0
        self._last_error: str | None = None
        self._generation = 1

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def start(self, button_name: str) -> bool:
        self._generation += 1
        if not self.available:
            self._last_error = "Atalho global do mouse indisponível nesta sessão."
            self.failed.emit(self._last_error)
            return False
        if self.fail_start:
            self._last_error = "Não foi possível ativar o atalho global do mouse."
            self.failed.emit(self._last_error)
            return False
        self.started_buttons.append(button_name)
        self._last_error = None
        return True

    def begin_capture(self) -> bool:
        self._generation += 1
        if not self.available:
            self._last_error = "Atalho global do mouse indisponível nesta sessão."
            self.failed.emit(self._last_error)
            return False
        if self.fail_capture:
            self._last_error = "Não foi possível ativar o atalho global do mouse."
            self.failed.emit(self._last_error)
            return False
        self.capture_count += 1
        self._last_error = None
        return True

    def stop(self) -> None:
        self._generation += 1
        self.stop_count += 1

    def emit_activated(self, x: int = 0, y: int = 0, gen: int | None = None) -> None:
        g = self._generation if gen is None else gen
        self._activated_event.emit(g, x, y)
        self.activated.emit()

    def emit_captured(self, button_name: str, x: int = 0, y: int = 0, gen: int | None = None) -> None:
        g = self._generation if gen is None else gen
        self._button_captured_event.emit(g, button_name, x, y)
        self.button_captured.emit(button_name)
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
    ) -> None:
        self.fail_record = fail_record
        self.fail_totals = fail_totals
        self.fail_close = fail_close
        self.fail_mic = fail_mic
        self.fail_history = fail_history
        self.fail_mouse_save = fail_mouse_save
        self.fail_mouse_get = fail_mouse_get
        self.fail_mouse_clear = fail_mouse_clear
        self.records: list[tuple[str, Any, str]] = []
        self.mic_identity: str | None = None
        self.mouse_button: str | None = None
        self.closed = False
        self.close_order_log: list[str] = []
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
    mouse_shortcut_bridge=None,
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
        mouse_shortcut_bridge=mouse_shortcut_bridge,
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
    factory_keys: list[str] = []

    def factory(api_key: str):
        factory_keys.append(api_key)
        return FakeTranscriber()

    window, _ = make_window(qapp, store=store, factory=factory)
    monkeypatch.setattr(window, "_acquire_api_key", lambda: ("  ui-session-token  ", True))

    window._configure_api_key()

    assert factory_keys == ["ui-session-token"]
    assert store.saved == ["ui-session-token"]
    assert window.settings.api_key == "ui-session-token"
    assert window.transcriber is not None
    assert window.record_button.isEnabled()
    assert "ui-session-token" not in window.status_label.text()
    assert "sucesso" in window.status_label.text()
    window.close()


def test_configure_api_key_cancel_or_empty_preserves_state(qapp, monkeypatch) -> None:
    store = FakeStore()
    factory_keys: list[str] = []
    window, _ = make_window(
        qapp,
        store=store,
        factory=lambda api_key: factory_keys.append(api_key) or FakeTranscriber(),
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
    assert factory_keys == []
    assert store.saved == []
    assert window.status_label.text() == original_status
    window.close()


def test_configure_api_key_store_failure_keeps_session_key_only(qapp, monkeypatch) -> None:
    store = FakeStore(fail=True)
    window, _ = make_window(
        qapp,
        store=store,
        factory=lambda api_key: FakeTranscriber(),
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

    def broken_factory(api_key: str):
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
    assert window.configure_key_button.isEnabled()

    window.editor.setPlainText("texto sintético")
    window._update_actions()
    assert window.copy_button.isEnabled()
    assert window.clear_text_button.isEnabled()
    assert window.terminal_button.isEnabled()

    window.state = AppState.RECORDING
    window._update_actions()
    assert not window.configure_key_button.isEnabled()
    assert window.record_button.isEnabled()

    window.state = AppState.TRANSCRIBING
    window._update_actions()
    assert not window.record_button.isEnabled()
    assert not window.copy_button.isEnabled()
    assert not window.clear_text_button.isEnabled()
    assert not window.terminal_button.isEnabled()
    assert not window.configure_key_button.isEnabled()
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


def test_debug_button_toggles_dock_visibility(qapp) -> None:
    window, _ = make_window(qapp)

    assert not window.debug_dock.isVisible()
    window.debug_button.click()
    assert window.debug_dock.isVisible()
    assert window.debug_button.text() == "Ocultar debug"
    window.debug_button.click()
    assert not window.debug_dock.isVisible()
    assert window.debug_button.text() == "Mostrar debug"
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


def test_debug_dock_contains_four_separate_blocks(qapp) -> None:
    window, _ = make_window(qapp)

    assert window.audio_debug is not None
    assert window.payload_debug is not None
    assert window.return_debug is not None
    assert window.usage_debug is not None
    assert window.usage_chart is not None
    assert isinstance(window.usage_chart, TokenUsageChart)
    assert window.debug_dock.widget() is not None
    widgets = {
        window.audio_debug,
        window.payload_debug,
        window.return_debug,
        window.usage_debug,
        window.usage_chart,
    }
    assert len(widgets) == 5
    window.close()


def test_debug_dock_widget_set_and_rendered_with_positive_dimensions(qapp) -> None:
    window, _ = make_window(qapp)

    managed_widget = window.debug_dock.widget()
    assert managed_widget is not None
    assert window.audio_debug.parentWidget() is managed_widget
    assert window.payload_debug.parentWidget() is managed_widget
    assert window.return_debug.parentWidget() is managed_widget
    assert window.usage_debug.parentWidget() is managed_widget
    assert window.usage_chart.parentWidget() is managed_widget

    assert not window.debug_dock.isVisible()
    window.debug_button.click()
    qapp.processEvents()

    assert window.debug_dock.isVisible()
    assert window.debug_dock.width() > 0
    assert managed_widget.width() > 0
    assert managed_widget.height() > 0
    assert window.audio_debug.height() > 0
    assert window.payload_debug.height() > 0
    assert window.return_debug.height() > 0
    assert window.usage_debug.height() > 0
    assert window.usage_chart.height() > 0
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

def test_default_window_mouse_shortcut_disabled(qapp) -> None:
    store = FakeLocalStore()
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
    )
    assert window._active_mouse_button is None
    assert window.shortcut_indicator_label.text() == "Atalho do mouse: Desativado"
    assert window.configure_shortcut_button.isEnabled() is True
    assert window.record_button.isEnabled() is True
    window.close()


def test_restore_mouse_shortcut_from_store_on_startup(qapp) -> None:
    store = FakeLocalStore()
    store.mouse_button = "x1"
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
    )
    assert bridge.started_buttons == ["x1"]
    assert window._active_mouse_button == "x1"
    assert window.shortcut_indicator_label.text() == "Atalho do mouse: Botão 4 (x1)"
    window.close()


def test_restore_mouse_shortcut_backend_failure_keeps_saved_preference(qapp) -> None:
    store = FakeLocalStore()
    store.mouse_button = "x1"
    bridge = FakeMouseShortcutBridge(fail_start=True)
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
    )
    assert window.status_label.text() == "Não foi possível ativar o atalho global do mouse."
    assert store.mouse_button == "x1"
    assert window._active_mouse_button is None
    assert window.shortcut_indicator_label.text() == "Atalho do mouse: Desativado"
    window.close()


def test_restore_mouse_shortcut_unavailable_session(qapp) -> None:
    store = FakeLocalStore()
    store.mouse_button = "x1"
    bridge = FakeMouseShortcutBridge(available=False)
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
    )
    assert window.status_label.text() == "Atalho global do mouse indisponível nesta sessão."
    assert store.mouse_button == "x1"
    assert window._active_mouse_button is None
    window.close()



def test_restore_mouse_shortcut_store_read_failure_displays_diagnostic_and_disables(qapp) -> None:
    store = FakeLocalStore(fail_mouse_get=True)
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
    )
    assert window._active_mouse_button is None
    assert window.shortcut_indicator_label.text() == "Atalho do mouse: Desativado"
    assert bridge.started_buttons == []
    assert window.status_label.text() == "Não foi possível ler preferência de atalho do mouse."
    window.close()


def test_mouse_shortcut_error_messages_whitelisted_and_omit_sensitive_leaks(qapp) -> None:
    store = FakeLocalStore()
    store.mouse_button = "x1"
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
    )
    leak_marker = "SECRET_TOKEN_INJECTED_LEAK_9999"

    # 1. Sinal failed com texto injetado cai no fallback sanitizado
    bridge.failed.emit(leak_marker)
    assert window.status_label.text() == "Não foi possível ativar o atalho global do mouse."
    assert leak_marker not in window.status_label.text()

    # 2. Sinal failed com mensagem canônica de sessão indisponível é aceito pela whitelist
    bridge.failed.emit("Atalho global do mouse indisponível nesta sessão.")
    assert window.status_label.text() == "Atalho global do mouse indisponível nesta sessão."

    # 3. bridge.last_error com texto injetado durante restore cai no fallback sanitizado
    store2 = FakeLocalStore()
    store2.mouse_button = "x1"
    bridge2 = FakeMouseShortcutBridge(fail_start=True)
    bridge2._last_error = leak_marker
    window2, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store2,
        mouse_shortcut_bridge=bridge2,
    )
    assert window2.status_label.text() == "Não foi possível ativar o atalho global do mouse."
    assert leak_marker not in window2.status_label.text()
    window2.close()
    window.close()


def test_configure_recording_shortcut_start_failure_rolls_back_to_previous_binding(
    qapp, monkeypatch
) -> None:
    store = FakeLocalStore()
    store.mouse_button = "x1"

    class SelectiveFailBridge(FakeMouseShortcutBridge):
        def start(self, button_name: str) -> bool:
            if button_name == "x2":
                self._last_error = "Não foi possível ativar o atalho global do mouse."
                self.started_buttons.append("x2")
                return False
            return super().start(button_name)

    bridge = SelectiveFailBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
    )
    assert window._active_mouse_button == "x1"
    assert bridge.started_buttons == ["x1"]
    assert window.shortcut_indicator_label.text() == "Atalho do mouse: Botão 4 (x1)"

    # Diálogo aceito com "x2", mas start("x2") falha
    monkeypatch.setattr(window, "_acquire_recording_mouse_button", lambda: ("x2", True))
    window._configure_recording_shortcut()

    # Rollback verificado: tentou "x2", restaurou "x1", manteve indicador e banco em "x1"
    assert bridge.started_buttons == ["x1", "x2", "x1"]
    assert window._active_mouse_button == "x1"
    assert window.shortcut_indicator_label.text() == "Atalho do mouse: Botão 4 (x1)"
    assert store.mouse_button == "x1"
    assert window.status_label.text() == "Não foi possível ativar o atalho global do mouse."
    window.close()
def test_configure_recording_shortcut_start_failure_with_failed_restoration_clears_indicator_and_preserves_store(
    qapp, monkeypatch
) -> None:
    store = FakeLocalStore()
    store.mouse_button = "x1"

    class BothFailBridge(FakeMouseShortcutBridge):
        def start(self, button_name: str) -> bool:
            if button_name in ("x2", "x1") and len(self.started_buttons) >= 1:
                self._last_error = "Não foi possível ativar o atalho global do mouse."
                self.started_buttons.append(button_name)
                return False
            return super().start(button_name)

    bridge = BothFailBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
    )
    assert window._active_mouse_button == "x1"
    assert bridge.started_buttons == ["x1"]
    assert window.shortcut_indicator_label.text() == "Atalho do mouse: Botão 4 (x1)"

    # Diálogo aceito com "x2", mas start("x2") falha E restauração de "x1" também falha
    monkeypatch.setattr(window, "_acquire_recording_mouse_button", lambda: ("x2", True))
    window._configure_recording_shortcut()

    # Tentou "x2", tentou restaurar "x1"
    assert bridge.started_buttons == ["x1", "x2", "x1"]
    # Indicador e _active_mouse_button foram limpos (não mente que está ativo)
    assert window._active_mouse_button is None
    assert window.shortcut_indicator_label.text() == "Atalho do mouse: Desativado"
    # Preferência persistida no store foi preservada para tentativa futura
    assert store.mouse_button == "x1"
    assert window.status_label.text() == "Não foi possível ativar o atalho global do mouse."
    window.close()


def test_configure_recording_shortcut_dialog_cancel_failed_restoration_clears_indicator_and_preserves_store(
    qapp, monkeypatch
) -> None:
    store = FakeLocalStore()
    store.mouse_button = "x1"

    class FailOnRestartBridge(FakeMouseShortcutBridge):
        def start(self, button_name: str) -> bool:
            if len(self.started_buttons) >= 1:
                self._last_error = "Não foi possível ativar o atalho global do mouse."
                self.started_buttons.append(button_name)
                return False
            return super().start(button_name)

    bridge = FailOnRestartBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
    )
    assert window._active_mouse_button == "x1"
    assert bridge.started_buttons == ["x1"]
    assert window.shortcut_indicator_label.text() == "Atalho do mouse: Botão 4 (x1)"

    # Diálogo cancelado pelo usuário
    monkeypatch.setattr(window, "_acquire_recording_mouse_button", lambda: (None, False))
    window._configure_recording_shortcut()

    # Tentou reiniciar "x1" e falhou
    assert bridge.started_buttons == ["x1", "x1"]
    # Indicador e estado em memória limpos
    assert window._active_mouse_button is None
    assert window.shortcut_indicator_label.text() == "Atalho do mouse: Desativado"
    # Preferência persistida preservada no store
    assert store.mouse_button == "x1"
    assert window.status_label.text() == "Não foi possível ativar o atalho global do mouse."
    window.close()


def test_acquire_recording_mouse_button_queued_relay_from_real_thread(qapp, monkeypatch) -> None:
    class ThreadedCaptureListener:
        def __init__(self, on_click: Any, **kwargs: Any) -> None:
            self.on_click = on_click
            self.thread: threading.Thread | None = None
            self.alive = False
            self.click_event = threading.Event()
            self._button_to_emit = "x2"

        def start(self) -> None:
            self.alive = True
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

        def emit_click_from_thread(self, button: str = "x2") -> None:
            self._button_to_emit = button
            self.click_event.set()

        def _run(self) -> None:
            if self.click_event.wait(timeout=5.0):
                self.on_click(0, 0, self._button_to_emit, True)

        def stop(self) -> None:
            self.alive = False
            self.click_event.set()

        def join(self, timeout: float | None = None) -> None:
            if self.thread is not None:
                self.thread.join(timeout=timeout)

        def is_alive(self) -> bool:
            return self.alive and self.thread is not None and self.thread.is_alive()

    active_listeners: list[ThreadedCaptureListener] = []

    def listener_factory(*args: Any, **kwargs: Any) -> ThreadedCaptureListener:
        listener = ThreadedCaptureListener(*args, **kwargs)
        active_listeners.append(listener)
        return listener

    bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=listener_factory,
    )
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        mouse_shortcut_bridge=bridge,
    )

    relay_handled_thread: QThread | None = None
    relay_captured_button: str | None = None
    relay_confirmed: bool = False

    def fake_exec(self_dialog: QDialog) -> int:
        nonlocal relay_handled_thread, relay_captured_button, relay_confirmed
        assert len(active_listeners) == 1
        listener = active_listeners[0]
        # Emite o clique a partir da thread real do listener durante a execução do diálogo
        listener.emit_click_from_thread("x2")
        listener.join(timeout=2.0)

        # Despacha os eventos postados da thread secundária na fila da thread principal
        QCoreApplication.sendPostedEvents()
        qapp.processEvents()

        # Encontra o relay conectado ao diálogo e registra estado e thread de execução
        relay = next(c for c in self_dialog.children() if isinstance(c, _ShortcutCaptureRelay))
        relay_handled_thread = relay.handled_thread
        relay_captured_button = relay.captured_button
        relay_confirmed = relay.confirmed
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(QDialog, "exec", fake_exec)

    button_name, accepted = window._acquire_recording_mouse_button()
    assert relay_handled_thread == qapp.thread()
    assert relay_captured_button == "x2"
    assert relay_confirmed is True
    assert button_name == "x2"
    assert accepted is True
    assert len(active_listeners) == 1
    assert not active_listeners[0].is_alive()
    window.close()

def test_acquire_recording_mouse_button_real_dialog_capture(qapp, monkeypatch) -> None:
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        mouse_shortcut_bridge=bridge,
    )

    def fake_exec(self_dialog: QDialog) -> int:
        assert isinstance(self_dialog, QDialog)
        assert self_dialog.windowTitle() == "Configurar atalho de gravação"
        bridge.emit_captured("right")
        qapp.processEvents()
        qapp.processEvents()
        return int(QDialog.DialogCode.Accepted)
    monkeypatch.setattr(QDialog, "exec", fake_exec)

    button_name, accepted = window._acquire_recording_mouse_button()
    assert button_name == "right"
    assert accepted is True
    assert bridge.capture_count == 1
    assert bridge.stop_count >= 2

    # Desconexão pós-diálogo verificada: emissão posterior é ignorada
    bridge.emit_captured("left")
    window.close()


def test_acquire_recording_mouse_button_real_dialog_cancel(qapp, monkeypatch) -> None:
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        mouse_shortcut_bridge=bridge,
    )

    def fake_exec(self_dialog: QDialog) -> int:
        cancel_btn = next(
            btn for btn in self_dialog.findChildren(QPushButton) if btn.text() == "Cancelar"
        )
        cancel_btn.click()
        return int(QDialog.DialogCode.Rejected)

    monkeypatch.setattr(QDialog, "exec", fake_exec)

    button_name, accepted = window._acquire_recording_mouse_button()
    assert button_name is None
    assert accepted is False
    assert bridge.capture_count == 1
    assert bridge.stop_count >= 2

    bridge.emit_captured("left")
    window.close()


def test_acquire_recording_mouse_button_real_dialog_disable_action(qapp, monkeypatch) -> None:
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        mouse_shortcut_bridge=bridge,
    )

    def fake_exec(self_dialog: QDialog) -> int:
        disable_btn = next(
            btn for btn in self_dialog.findChildren(QPushButton) if btn.text() == "Desativar atalho"
        )
        disable_btn.click()
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(QDialog, "exec", fake_exec)

    button_name, accepted = window._acquire_recording_mouse_button()
    assert button_name is None
    assert accepted is True
    assert bridge.capture_count == 1
    assert bridge.stop_count >= 2

    bridge.emit_captured("left")
    window.close()


def test_acquire_recording_mouse_button_real_dialog_begin_capture_unavailable(
    qapp, monkeypatch
) -> None:
    bridge = FakeMouseShortcutBridge(fail_capture=True)
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        mouse_shortcut_bridge=bridge,
    )

    exec_called = False

    def fake_exec(self_dialog: QDialog) -> int:
        nonlocal exec_called
        exec_called = True
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(QDialog, "exec", fake_exec)

    button_name, accepted = window._acquire_recording_mouse_button()
    assert button_name is None
    assert accepted is False
    assert exec_called is False
    assert bridge.stop_count == 1
    window.close()


def test_acquire_recording_mouse_button_call_ordering_and_lifecycle(
    qapp, monkeypatch
) -> None:
    call_order: list[str] = []

    class SequenceBridge(FakeMouseShortcutBridge):
        def stop(self) -> None:
            call_order.append("bridge.stop")
            super().stop()

        def begin_capture(self) -> bool:
            call_order.append("bridge.begin_capture")
            return super().begin_capture()

    bridge = SequenceBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        mouse_shortcut_bridge=bridge,
    )
    call_order.clear()

    def fake_exec(self_dialog: QDialog) -> int:
        call_order.append("dialog.exec")
        bridge.emit_captured("x2")
        qapp.processEvents()
        qapp.processEvents()
        return int(QDialog.DialogCode.Accepted)
    monkeypatch.setattr(QDialog, "exec", fake_exec)
    button_name, accepted = window._acquire_recording_mouse_button()
    assert button_name == "x2"
    assert accepted is True
    assert call_order == [
        "bridge.stop",
        "bridge.begin_capture",
        "dialog.exec",
        "bridge.stop",
    ]
    window.close()


def test_configure_recording_shortcut_end_to_end_with_real_dialog(
    qapp, monkeypatch
) -> None:
    store = FakeLocalStore()
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
    )

    def fake_exec(self_dialog: QDialog) -> int:
        bridge.emit_captured("middle")
        qapp.processEvents()
        qapp.processEvents()
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    window._configure_recording_shortcut()
    assert window._active_mouse_button == "middle"
    assert store.mouse_button == "middle"
    assert bridge.started_buttons == ["middle"]
    assert window.shortcut_indicator_label.text() == "Atalho do mouse: Botão do meio"
    assert "Botão do meio" in window.status_label.text()
    window.close()

def test_mouse_shortcut_activated_toggles_recording(qapp) -> None:
    recorder = FakeRecorder()
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        mouse_shortcut_bridge=bridge,
    )
    assert window.state is AppState.IDLE
    assert recorder.recording is False

    # 1ª ativação: inicia gravação
    bridge.emit_activated()
    qapp.processEvents()
    assert recorder.recording is True
    assert window.state is AppState.RECORDING
    assert window.record_button.text() == "Parar e revisar áudio"

    # 2ª ativação: para gravação
    bridge.emit_activated()
    qapp.processEvents()
    assert recorder.recording is False
    assert window.state is AppState.AUDIO_READY
    assert window.record_button.text() == "Gravar"

    # 3ª ativação: inicia nova gravação
    bridge.emit_activated()
    qapp.processEvents()
    assert recorder.recording is True
    assert window.state is AppState.RECORDING
    window.close()


def test_mouse_shortcut_activated_ignored_during_transcribing(qapp) -> None:
    recorder = FakeRecorder()
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        mouse_shortcut_bridge=bridge,
    )
    window.state = AppState.TRANSCRIBING
    bridge.emit_activated()
    qapp.processEvents()
    assert window.state is AppState.TRANSCRIBING
    assert recorder.recording is False
    window.close()

def test_configure_recording_shortcut_dialog_capture_and_persist(qapp, monkeypatch) -> None:
    store = FakeLocalStore()
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
    )
    monkeypatch.setattr(window, "_acquire_recording_mouse_button", lambda: ("x2", True))

    window._configure_recording_shortcut()

    assert bridge.started_buttons == ["x2"]
    assert window._active_mouse_button == "x2"
    assert store.mouse_button == "x2"
    assert window.shortcut_indicator_label.text() == "Atalho do mouse: Botão 5 (x2)"
    assert "Botão 5 (x2)" in window.status_label.text()
    window.close()


def test_configure_recording_shortcut_dialog_cancel_restores_previous(qapp, monkeypatch) -> None:
    store = FakeLocalStore()
    store.mouse_button = "x1"
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
    )
    assert window._active_mouse_button == "x1"

    # Diálogo cancelado
    monkeypatch.setattr(window, "_acquire_recording_mouse_button", lambda: (None, False))
    window._configure_recording_shortcut()

    assert bridge.started_buttons == ["x1", "x1"]
    assert window._active_mouse_button == "x1"
    assert store.mouse_button == "x1"
    window.close()


def test_configure_recording_shortcut_disable_action(qapp, monkeypatch) -> None:
    store = FakeLocalStore()
    store.mouse_button = "x1"
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
    )
    assert window._active_mouse_button == "x1"

    # Desativar atalho
    monkeypatch.setattr(window, "_acquire_recording_mouse_button", lambda: (None, True))
    window._configure_recording_shortcut()

    assert window._active_mouse_button is None
    assert store.mouse_button is None
    assert window.shortcut_indicator_label.text() == "Atalho do mouse: Desativado"
    assert window.status_label.text() == "Atalho de gravação desativado."
    window.close()


def test_configure_recording_shortcut_persistence_failure_retains_session_binding(qapp, monkeypatch) -> None:
    store = FakeLocalStore(fail_mouse_save=True)
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
    )
    monkeypatch.setattr(window, "_acquire_recording_mouse_button", lambda: ("right", True))
    window._configure_recording_shortcut()

    assert bridge.started_buttons == ["right"]
    assert window._active_mouse_button == "right"
    assert window.status_label.text() == "Atalho configurado nesta sessão; não foi possível persistir."
    window.close()


def test_configure_recording_shortcut_disable_persistence_failure(qapp, monkeypatch) -> None:
    store = FakeLocalStore(fail_mouse_clear=True)
    store.mouse_button = "x1"
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
    )
    monkeypatch.setattr(window, "_acquire_recording_mouse_button", lambda: (None, True))
    window._configure_recording_shortcut()

    assert window._active_mouse_button is None
    assert window.status_label.text() == "Atalho desativado nesta sessão; não foi possível persistir."
    window.close()


def test_configure_recording_shortcut_disabled_during_recording_and_transcribing(qapp) -> None:
    recorder = FakeRecorder()
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        mouse_shortcut_bridge=bridge,
    )
    assert window.configure_shortcut_button.isEnabled() is True

    window._start_recording()
    assert window.state is AppState.RECORDING
    assert window.configure_shortcut_button.isEnabled() is False

    # Invocação durante RECORDING é no-op
    window._configure_recording_shortcut()
    assert window.state is AppState.RECORDING

    window._finish_recording()
    assert window.state is AppState.AUDIO_READY
    assert window.configure_shortcut_button.isEnabled() is True

    window.state = AppState.TRANSCRIBING
    window._update_actions()
    assert window.configure_shortcut_button.isEnabled() is False

    window._configure_recording_shortcut()
    assert window.state is AppState.TRANSCRIBING
    window.close()


def test_close_event_stops_bridge_before_closing_store(qapp) -> None:
    call_order: list[str] = []

    class OrderedBridge(FakeMouseShortcutBridge):
        def stop(self) -> None:
            call_order.append("bridge.stop")
            super().stop()

    class OrderedStore(FakeLocalStore):
        def close(self) -> None:
            call_order.append("store.close")
            super().close()

    bridge = OrderedBridge()
    store = OrderedStore()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
    )
    window.close()

    assert "bridge.stop" in call_order
    assert "store.close" in call_order
    assert call_order.index("bridge.stop") < call_order.index("store.close")


def test_late_mouse_shortcut_callback_ignored_after_close(qapp) -> None:
    recorder = FakeRecorder()
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        mouse_shortcut_bridge=bridge,
    )
    window.close()

    bridge.emit_activated()
    qapp.processEvents()
    assert recorder.recording is False
    window.close()


def test_configure_shortcut_button_pressed_prevents_race_with_activated(qapp, monkeypatch) -> None:
    recorder = FakeRecorder()
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        mouse_shortcut_bridge=bridge,
    )
    window._active_mouse_button = "left"

    # Scenario 1: Physical click with coordinates OVER the configure button triggers suppression (zero transitions)
    btn_center = window.configure_shortcut_button.mapToGlobal(
        QPoint(
            window.configure_shortcut_button.width() // 2,
            window.configure_shortcut_button.height() // 2,
        )
    )
    bridge.emit_activated(btn_center.x(), btn_center.y())
    qapp.processEvents()
    assert recorder.recording is False
    assert window.state is AppState.IDLE

    # Scenario 2: Real interaction sequence: position -> pressed -> activated (queued) -> dialog exec -> close
    # During configuration, queued activated signal is consumed/ignored and DOES NOT leak to next click
    dialog_opened = False

    def fake_exec(self_dialog: QDialog) -> int:
        nonlocal dialog_opened
        dialog_opened = True
        return int(QDialog.DialogCode.Rejected)

    monkeypatch.setattr(QDialog, "exec", fake_exec)

    bridge.emit_activated(btn_center.x(), btn_center.y())
    window.configure_shortcut_button.pressed.emit()
    assert window._is_configuring_shortcut is True
    assert bridge.stop_count >= 1

    # Queued activated arrives while configuring
    bridge.emit_activated()
    qapp.processEvents()
    assert recorder.recording is False

    # Button release triggers clicked -> opens and closes dialog
    window.configure_shortcut_button.clicked.emit()
    assert dialog_opened is True
    assert recorder.recording is False
    assert window._is_configuring_shortcut is False

    # Scenario 3: Immediately after dialog closure, user clicks with left button outside
    # The consumed suppression flag ensures the very next click triggers recording without any lost events
    outside_x = btn_center.x() + 2000
    outside_y = btn_center.y() + 2000
    bridge.emit_activated(outside_x, outside_y)
    qapp.processEvents()

    assert recorder.recording is True
    assert window.state is AppState.RECORDING

    # Second activation outside stops recording
    bridge.emit_activated(outside_x, outside_y)
    qapp.processEvents()
    assert recorder.recording is False
    assert window.state is AppState.AUDIO_READY

    # Scenario 4: Non-left active binding (e.g. "x1") over configure button is NOT suppressed
    window._active_mouse_button = "x1"
    bridge.emit_activated(btn_center.x(), btn_center.y())
    qapp.processEvents()
    assert recorder.recording is True
    assert window.state is AppState.RECORDING

    # Scenario 5: When configure button is disabled in RECORDING, left click over it is NOT suppressed and stops recording
    window._active_mouse_button = "left"
    assert window.configure_shortcut_button.isEnabled() is False
    bridge.emit_activated(btn_center.x(), btn_center.y())
    qapp.processEvents()
    assert recorder.recording is False
    assert window.state is AppState.AUDIO_READY

    window.close()


def test_dialog_cancel_and_disable_preemption_and_non_left_candidates(
    qapp, monkeypatch
) -> None:
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        mouse_shortcut_bridge=bridge,
    )

    # Test 1: Lateral button "x1" over Cancel button is captured as candidate (NOT cancelled)
    def fake_exec_cancel_x1(self_dialog: QDialog) -> int:
        cancel_btn = next(
            c for c in self_dialog.children() if isinstance(c, QPushButton) and c.text() == "Cancelar"
        )
        cancel_center = cancel_btn.mapToGlobal(QPoint(cancel_btn.width() // 2, cancel_btn.height() // 2))

        bridge.emit_captured("x1", cancel_center.x(), cancel_center.y())
        qapp.processEvents()
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(QDialog, "exec", fake_exec_cancel_x1)
    button_name, accepted = window._acquire_recording_mouse_button()
    assert button_name == "x1"
    assert accepted is True

    # Test 2: Alias "button8" over Cancel button is normalized to "x1" and captured
    def fake_exec_cancel_button8(self_dialog: QDialog) -> int:
        cancel_btn = next(
            c for c in self_dialog.children() if isinstance(c, QPushButton) and c.text() == "Cancelar"
        )
        cancel_center = cancel_btn.mapToGlobal(QPoint(cancel_btn.width() // 2, cancel_btn.height() // 2))

        bridge.emit_captured("button8", cancel_center.x(), cancel_center.y())
        qapp.processEvents()
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(QDialog, "exec", fake_exec_cancel_button8)
    button_name, accepted = window._acquire_recording_mouse_button()
    assert button_name == "x1"
    assert accepted is True

    # Test 3: Secondary button "right" over Disable button is captured as candidate (NOT disabled)
    def fake_exec_disable_right(self_dialog: QDialog) -> int:
        disable_btn = next(
            c
            for c in self_dialog.children()
            if isinstance(c, QPushButton) and c.text() == "Desativar atalho"
        )
        disable_center = disable_btn.mapToGlobal(QPoint(disable_btn.width() // 2, disable_btn.height() // 2))

        bridge.emit_captured("right", disable_center.x(), disable_center.y())
        qapp.processEvents()
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(QDialog, "exec", fake_exec_disable_right)
    button_name, accepted = window._acquire_recording_mouse_button()
    assert button_name == "right"
    assert accepted is True

    # Test 4: Primary "left" over Cancel button blocks capture candidate and rejects on clicked
    def fake_exec_cancel_left(self_dialog: QDialog) -> int:
        cancel_btn = next(
            c for c in self_dialog.children() if isinstance(c, QPushButton) and c.text() == "Cancelar"
        )
        cancel_center = cancel_btn.mapToGlobal(QPoint(cancel_btn.width() // 2, cancel_btn.height() // 2))

        bridge.emit_captured("left", cancel_center.x(), cancel_center.y())
        cancel_btn.pressed.emit()
        cancel_btn.released.emit()
        cancel_btn.clicked.emit()
        qapp.processEvents()
        return int(QDialog.DialogCode.Rejected)

    monkeypatch.setattr(QDialog, "exec", fake_exec_cancel_left)
    button_name, accepted = window._acquire_recording_mouse_button()
    assert button_name is None
    assert accepted is False

    # Test 5: Primary "left" over Disable button blocks capture candidate and accepts disable on clicked
    def fake_exec_disable_left(self_dialog: QDialog) -> int:
        disable_btn = next(
            c
            for c in self_dialog.children()
            if isinstance(c, QPushButton) and c.text() == "Desativar atalho"
        )
        disable_center = disable_btn.mapToGlobal(QPoint(disable_btn.width() // 2, disable_btn.height() // 2))

        bridge.emit_captured("left", disable_center.x(), disable_center.y())
        disable_btn.pressed.emit()
        disable_btn.released.emit()
        disable_btn.clicked.emit()
        qapp.processEvents()
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(QDialog, "exec", fake_exec_disable_left)
    button_name, accepted = window._acquire_recording_mouse_button()
    assert button_name is None
    assert accepted is True

    window.close()


def test_dialog_drag_out_resumes_capture_and_ignores_terminal(
    qapp, monkeypatch
) -> None:
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        mouse_shortcut_bridge=bridge,
    )

    # Test 1: Drag-out on Cancel button: pressed -> released (no clicked) -> resumes capture -> captures "x2"
    def fake_exec_cancel_drag_out(self_dialog: QDialog) -> int:
        cancel_btn = next(
            c for c in self_dialog.children() if isinstance(c, QPushButton) and c.text() == "Cancelar"
        )
        initial_captures = bridge.capture_count
        cancel_btn.pressed.emit()
        cancel_btn.released.emit()
        qapp.processEvents()

        # Capture was resumed (begin_capture called again)
        assert bridge.capture_count > initial_captures

        # Subsequent click on "x2" is captured successfully
        bridge.emit_captured("x2", 10, 10)
        qapp.processEvents()
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(QDialog, "exec", fake_exec_cancel_drag_out)
    button_name, accepted = window._acquire_recording_mouse_button()
    assert button_name == "x2"
    assert accepted is True

    # Test 2: Drag-out on Disable button: pressed -> released (no clicked) -> resumes capture -> captures "middle"
    def fake_exec_disable_drag_out(self_dialog: QDialog) -> int:
        disable_btn = next(
            c
            for c in self_dialog.children()
            if isinstance(c, QPushButton) and c.text() == "Desativar atalho"
        )
        initial_captures = bridge.capture_count
        disable_btn.pressed.emit()
        disable_btn.released.emit()
        qapp.processEvents()

        # Capture was resumed
        assert bridge.capture_count > initial_captures

        # Subsequent click on "middle" is captured successfully
        bridge.emit_captured("middle", 10, 10)
        qapp.processEvents()
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(QDialog, "exec", fake_exec_disable_drag_out)
    button_name, accepted = window._acquire_recording_mouse_button()
    assert button_name == "middle"
    assert accepted is True

    window.close()

def test_integrated_mainwindow_mouse_shortcut_bridge_recording_transitions_and_wayland_fallback(
    qapp,
    tmp_path,
) -> None:
    # 1. Full integrated test with X11 environment, real LocalStore SQLite database on disk,
    # real MouseShortcutBridge with worker-thread listener fake, MainWindow and FakeRecorder
    db_path = tmp_path / "falafacil_integrated.db"
    store = LocalStore(db_path)
    store.save_recording_mouse_button("x1")
    recorder = FakeRecorder()

    captured_listeners: list[Any] = []

    def threaded_listener_factory(on_click: Any, suppress: bool = False) -> Any:
        class ThreadedIntegratedListener:
            def __init__(self, click_cb: Any) -> None:
                self.on_click = click_cb
                self.started = False
                self.stopped = False
                self._worker_thread: threading.Thread | None = None

            def start(self) -> None:
                self.started = True

            def stop(self) -> None:
                self.stopped = True

            def join(self, timeout: float | None = None) -> None:
                if self._worker_thread is not None and self._worker_thread.is_alive():
                    self._worker_thread.join(timeout=timeout)

            def is_alive(self) -> bool:
                return self.started and not self.stopped

            def emit_click_from_worker_thread(
                self, x: int, y: int, button: str, pressed: bool
            ) -> None:
                def _worker() -> None:
                    self.on_click(x, y, button, pressed)

                t = threading.Thread(target=_worker)
                self._worker_thread = t
                t.start()
                t.join(timeout=1.0)

        listener = ThreadedIntegratedListener(on_click)
        captured_listeners.append(listener)
        return listener

    x11_bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=threaded_listener_factory,
    )

    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        local_store=store,
        mouse_shortcut_bridge=x11_bridge,
    )

    assert window.state is AppState.IDLE
    assert len(captured_listeners) == 1
    active_listener = captured_listeners[0]
    assert active_listener.started is True
    assert window._active_mouse_button == "x1"
    assert window.shortcut_indicator_label.text() == "Atalho do mouse: Botão 4 (x1)"

    # Event 1: First press (pressed=True) on "x1" from secondary thread -> IDLE to RECORDING
    active_listener.emit_click_from_worker_thread(0, 0, "x1", True)
    qapp.processEvents()
    qapp.processEvents()
    assert window.state is AppState.RECORDING
    assert recorder.recording is True

    # Event 2: First release (pressed=False) on "x1" from secondary thread -> Ignored, remains RECORDING
    active_listener.emit_click_from_worker_thread(0, 0, "x1", False)
    qapp.processEvents()
    qapp.processEvents()
    assert window.state is AppState.RECORDING
    assert recorder.recording is True

    # Event 3: Second press (pressed=True) on "x1" from secondary thread -> RECORDING to AUDIO_READY
    active_listener.emit_click_from_worker_thread(0, 0, "x1", True)
    qapp.processEvents()
    qapp.processEvents()
    assert window.state is AppState.AUDIO_READY
    assert recorder.recording is False

    # Event 4: Second release (pressed=False) on "x1" from secondary thread -> Ignored, remains AUDIO_READY
    active_listener.emit_click_from_worker_thread(0, 0, "x1", False)
    qapp.processEvents()
    qapp.processEvents()
    assert window.state is AppState.AUDIO_READY
    assert recorder.recording is False
    window.close()
    store.close()

    # 2. Reopening the real SQLite store recovers "x1" and restores active shortcut in a new window
    reopened_store = LocalStore(db_path)
    assert reopened_store.get_recording_mouse_button() == "x1"

    reopened_bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
        listener_factory=threaded_listener_factory,
    )
    window2, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=FakeRecorder(),
        local_store=reopened_store,
        mouse_shortcut_bridge=reopened_bridge,
    )
    assert window2._active_mouse_button == "x1"
    assert window2.shortcut_indicator_label.text() == "Atalho do mouse: Botão 4 (x1)"
    window2.close()
    reopened_store.close()

    # 3. Wayland session: begin_capture and start return False without creating a listener;
    # UI manual click and Space shortcut remain operational
    wayland_captured_listeners: list[Any] = []

    def wayland_listener_factory(on_click: Any, suppress: bool = False) -> Any:
        listener = threaded_listener_factory(on_click, suppress)
        wayland_captured_listeners.append(listener)
        return listener

    wayland_bridge = MouseShortcutBridge(
        env={"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"},
        listener_factory=wayland_listener_factory,
    )
    assert wayland_bridge.begin_capture() is False
    assert wayland_bridge.start("x1") is False
    assert len(wayland_captured_listeners) == 0

    wayland_recorder = FakeRecorder()
    wayland_db = tmp_path / "falafacil_wayland.db"
    wayland_store = LocalStore(wayland_db)
    wayland_store.save_recording_mouse_button("x1")

    wayland_window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=wayland_recorder,
        local_store=wayland_store,
        mouse_shortcut_bridge=wayland_bridge,
    )

    assert wayland_bridge.available is False
    assert len(wayland_captured_listeners) == 0
    assert wayland_window._active_mouse_button is None
    # Record button has Space shortcut configured and functions in Wayland
    assert wayland_window.record_button.shortcut() == QKeySequence("Space")

    # Manual record button click: IDLE to RECORDING
    wayland_window.record_button.click()
    qapp.processEvents()
    assert wayland_window.state is AppState.RECORDING
    assert wayland_recorder.recording is True

    # Second click (or Space trigger): RECORDING to AUDIO_READY
    wayland_window.record_button.click()
    qapp.processEvents()
    assert wayland_window.state is AppState.AUDIO_READY
    assert wayland_recorder.recording is False

    wayland_window.close()
    wayland_store.close()


def test_left_mouse_binding_record_button_order_and_single_toggle(qapp) -> None:
    recorder = FakeRecorder()
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        mouse_shortcut_bridge=bridge,
    )
    window._active_mouse_button = "left"

    # 1. callback-before-pressed in IDLE:
    # global activated arriving on record button gets suppressed -> does not start recording yet
    btn_center = window.record_button.mapToGlobal(
        QPoint(window.record_button.width() // 2, window.record_button.height() // 2)
    )
    bridge.emit_activated(btn_center.x(), btn_center.y())
    qapp.processEvents()
    assert recorder.recording is False
    assert window.state is AppState.IDLE

    # Qt local click event arrives on record_button -> starts recording with exactly one toggle
    window.record_button.pressed.emit()
    window.record_button.clicked.emit()
    qapp.processEvents()
    assert recorder.recording is True
    assert window.state is AppState.RECORDING

    # 2. pressed-before-callback in RECORDING to stop:
    # Qt local pressed arrives -> global position + activated arrives -> suppressed -> clicked stops
    window.record_button.pressed.emit()
    bridge.emit_activated(btn_center.x(), btn_center.y())
    qapp.processEvents()
    # Before clicked, still recording (global was suppressed)
    assert recorder.recording is True
    assert window.state is AppState.RECORDING

    window.record_button.clicked.emit()
    qapp.processEvents()
    assert recorder.recording is False
    assert window.state is AppState.AUDIO_READY

    # 3. pressed-before-callback in IDLE to start:
    window.state = AppState.IDLE
    window._update_actions()
    window.record_button.pressed.emit()
    bridge.emit_activated(btn_center.x(), btn_center.y())
    qapp.processEvents()
    assert recorder.recording is False

    window.record_button.clicked.emit()
    qapp.processEvents()
    assert recorder.recording is True
    assert window.state is AppState.RECORDING

    window.close()


def test_left_mouse_binding_play_audio_button_order_and_preserves_capture(qapp) -> None:
    recorder = FakeRecorder()
    bridge = FakeMouseShortcutBridge()
    media_player = FakeMediaPlayer()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        media_player=media_player,
        mouse_shortcut_bridge=bridge,
    )
    window._active_mouse_button = "left"

    capture = make_capture()
    window._pending_capture = capture
    window.state = AppState.AUDIO_READY
    window._update_actions()
    assert window.play_audio_button.isEnabled() is True

    btn_center = window.play_audio_button.mapToGlobal(
        QPoint(window.play_audio_button.width() // 2, window.play_audio_button.height() // 2)
    )
    # 1. callback-before-pressed:
    bridge.emit_activated(btn_center.x(), btn_center.y())
    qapp.processEvents()
    # Global toggle is suppressed: pending capture is preserved and state stays AUDIO_READY
    assert window._pending_capture is capture
    assert window.state is AppState.AUDIO_READY
    assert recorder.recording is False

    window.play_audio_button.pressed.emit()
    window.play_audio_button.clicked.emit()
    qapp.processEvents()
    assert window._pending_capture is capture
    assert window.state is AppState.AUDIO_READY
    assert recorder.recording is False
    assert media_player.played_bytes == capture.wav_bytes

    # 2. pressed-before-callback:
    media_player.played_bytes = None
    window.play_audio_button.pressed.emit()
    bridge.emit_activated(btn_center.x(), btn_center.y())
    qapp.processEvents()
    assert window._pending_capture is capture
    assert window.state is AppState.AUDIO_READY
    assert recorder.recording is False

    window.play_audio_button.clicked.emit()
    qapp.processEvents()
    assert window._pending_capture is capture
    assert window.state is AppState.AUDIO_READY
    assert recorder.recording is False
    assert media_player.played_bytes == capture.wav_bytes

    window.close()


def test_left_mouse_binding_send_to_gemini_button_order_and_transcribes_capture(qapp) -> None:
    recorder = FakeRecorder()
    bridge = FakeMouseShortcutBridge()
    transcriber = FakeTranscriber()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        recorder=recorder,
        mouse_shortcut_bridge=bridge,
    )
    window._active_mouse_button = "left"

    capture = make_capture()
    window._pending_capture = capture
    window.state = AppState.AUDIO_READY
    window._update_actions()
    assert window.send_to_gemini_button.isEnabled() is True

    btn_center = window.send_to_gemini_button.mapToGlobal(
        QPoint(window.send_to_gemini_button.width() // 2, window.send_to_gemini_button.height() // 2)
    )
    # 1. callback-before-pressed:
    bridge.emit_activated(btn_center.x(), btn_center.y())
    qapp.processEvents()
    # Before clicked: capture preserved, state stays AUDIO_READY, recording not toggled
    assert window._pending_capture is capture
    assert window.state is AppState.AUDIO_READY
    assert recorder.recording is False

    window.send_to_gemini_button.pressed.emit()
    window.send_to_gemini_button.clicked.emit()
    qapp.processEvents()
    # Clicked initiates transcription of the pending capture
    assert transcriber.calls == [capture.wav_bytes]

    # 2. pressed-before-callback on a fresh capture:
    transcriber2 = FakeTranscriber()
    recorder2 = FakeRecorder()
    bridge2 = FakeMouseShortcutBridge()
    window2, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber2,
        recorder=recorder2,
        mouse_shortcut_bridge=bridge2,
    )
    window2._active_mouse_button = "left"
    capture2 = make_capture()
    window2._pending_capture = capture2
    window2.state = AppState.AUDIO_READY
    window2._update_actions()

    btn_center2 = window2.send_to_gemini_button.mapToGlobal(
        QPoint(window2.send_to_gemini_button.width() // 2, window2.send_to_gemini_button.height() // 2)
    )
    window2.send_to_gemini_button.pressed.emit()
    bridge2.emit_activated(btn_center2.x(), btn_center2.y())
    qapp.processEvents()
    assert window2._pending_capture is capture2
    assert window2.state is AppState.AUDIO_READY
    assert recorder2.recording is False

    window2.send_to_gemini_button.clicked.emit()
    qapp.processEvents()
    assert transcriber2.calls == [capture2.wav_bytes]

    window2.close()
    window.close()


def test_left_mouse_binding_focus_unfocus_and_external_clicks(qapp) -> None:
    recorder = FakeRecorder()
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        mouse_shortcut_bridge=bridge,
    )
    window._active_mouse_button = "left"

    # Scenario A: Unfocused / focus cleared window still suppresses local control click without double toggle
    window.clearFocus()
    btn_center = window.record_button.mapToGlobal(
        QPoint(window.record_button.width() // 2, window.record_button.height() // 2)
    )
    bridge.emit_activated(btn_center.x(), btn_center.y())
    qapp.processEvents()
    assert recorder.recording is False
    assert window.state is AppState.IDLE

    window.record_button.clicked.emit()
    qapp.processEvents()
    assert recorder.recording is True
    assert window.state is AppState.RECORDING

    # Scenario B: External click outside window is NOT suppressed -> toggles recording globally
    outside_x = window.x() + window.width() + 1500
    outside_y = window.y() + window.height() + 1500
    bridge.emit_activated(outside_x, outside_y)
    qapp.processEvents()
    assert recorder.recording is False
    assert window.state is AppState.AUDIO_READY

    bridge.emit_activated(outside_x, outside_y)
    qapp.processEvents()
    assert recorder.recording is True
    assert window.state is AppState.RECORDING

    window._finish_recording()
    assert window.state is AppState.AUDIO_READY

    # Scenario C: Disabled control is NOT treated as an actionable target -> global shortcut toggles
    window.state = AppState.IDLE
    window._update_actions()
    assert window.play_audio_button.isEnabled() is False

    play_center = window.play_audio_button.mapToGlobal(
        QPoint(window.play_audio_button.width() // 2, window.play_audio_button.height() // 2)
    )
    bridge.emit_activated(play_center.x(), play_center.y())
    qapp.processEvents()
    assert recorder.recording is True
    assert window.state is AppState.RECORDING



def test_offscreen_widget_at_none_suppresses_interactive_control(qapp, monkeypatch) -> None:
    recorder = FakeRecorder()
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        mouse_shortcut_bridge=bridge,
    )
    window.show()
    window.activateWindow()
    qapp.processEvents()
    window._active_mouse_button = "left"
    gen = bridge.generation

    # 1. Under offscreen, widgetAt is None, but geometric fallback suppresses active control
    monkeypatch.setattr(QApplication, "platformName", lambda: "offscreen")
    monkeypatch.setattr(QApplication, "widgetAt", lambda *args, **kwargs: None)
    btn_center = window.record_button.mapToGlobal(
        QPoint(window.record_button.width() // 2, window.record_button.height() // 2)
    )
    bridge._activated_event.emit(gen, btn_center.x(), btn_center.y())
    qapp.processEvents()
    assert recorder.recording is False
    assert window.state is AppState.IDLE

    # 2. Under non-offscreen (e.g. xcb/X11), widgetAt None means unknown/external target -> allows global toggle
    monkeypatch.setattr(QApplication, "platformName", lambda: "xcb")
    bridge._activated_event.emit(gen, btn_center.x(), btn_center.y())
    qapp.processEvents()
    assert recorder.recording is True
    assert window.state is AppState.RECORDING

    window.close()


def test_left_mouse_binding_local_press_then_self_disable_suppresses_late_activation(qapp) -> None:
    recorder = FakeRecorder()
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        mouse_shortcut_bridge=bridge,
    )
    window._active_mouse_button = "left"
    window.editor.setPlainText("Texto gravado para apagar")
    window._update_actions()
    assert window.clear_text_button.isEnabled() is True

    # Dispatch local MouseButtonPress on the enabled clear_text_button
    press_event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(window.clear_text_button.width() / 2, window.clear_text_button.height() / 2),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QCoreApplication.sendEvent(window.clear_text_button, press_event)

    # Click handler runs and clears editor, which autodisables clear_text_button
    window.clear_text_button.clicked.emit()
    qapp.processEvents()
    assert window.editor.toPlainText() == ""
    assert window.clear_text_button.isEnabled() is False

    # Late global activation signal arrives after button is already disabled
    btn_center = window.clear_text_button.mapToGlobal(
        QPoint(window.clear_text_button.width() // 2, window.clear_text_button.height() // 2)
    )
    bridge.emit_activated(btn_center.x(), btn_center.y())
    qapp.processEvents()

    # Must be suppressed by the armed flag, not toggling recording
    assert recorder.recording is False
    assert window.state is AppState.IDLE

    window.close()


def test_left_mouse_binding_initially_disabled_target_allows_global_toggle(qapp) -> None:
    recorder = FakeRecorder()
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        mouse_shortcut_bridge=bridge,
    )
    window._active_mouse_button = "left"
    window.editor.setPlainText("")
    window._update_actions()
    assert window.clear_text_button.isEnabled() is False

    # Local MouseButtonPress dispatched on an initially disabled control does not arm suppression
    press_event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(window.clear_text_button.width() / 2, window.clear_text_button.height() / 2),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QCoreApplication.sendEvent(window.clear_text_button, press_event)

    # Global activation arrives -> toggles recording globally
    btn_center = window.clear_text_button.mapToGlobal(
        QPoint(window.clear_text_button.width() // 2, window.clear_text_button.height() // 2)
    )
    bridge.emit_activated(btn_center.x(), btn_center.y())
    qapp.processEvents()

    assert recorder.recording is True
    assert window.state is AppState.RECORDING

    window.close()


def test_shortcut_capture_relay_is_control_at_pos_platform_policy(qapp, monkeypatch) -> None:
    dialog = QDialog()
    dialog.show()
    btn = QPushButton("Cancelar", dialog)
    btn.resize(100, 30)
    btn.show()
    qapp.processEvents()
    relay = _ShortcutCaptureRelay(dialog, cancel_button=btn)

    btn_center = btn.mapToGlobal(QPoint(btn.width() // 2, btn.height() // 2))

    # 1. Quando widgetAt retorna o próprio botão, deve retornar True sob qualquer plataforma
    monkeypatch.setattr(QApplication, "widgetAt", lambda pos: btn)
    monkeypatch.setattr(QApplication, "platformName", lambda: "xcb")
    assert relay._is_control_at_pos(btn, btn_center) is True

    # 2. Quando widgetAt retorna outro widget, deve retornar False sob qualquer plataforma
    other_widget = QWidget()
    monkeypatch.setattr(QApplication, "widgetAt", lambda pos: other_widget)
    assert relay._is_control_at_pos(btn, btn_center) is False

    # 3. Quando widgetAt retorna None em offscreen, fallback geométrico permite True
    monkeypatch.setattr(QApplication, "widgetAt", lambda pos: None)
    monkeypatch.setattr(QApplication, "platformName", lambda: "offscreen")
    assert relay._is_control_at_pos(btn, btn_center) is True

    # 4. Quando widgetAt retorna None em X11/xcb (runtime real), alvo externo/desconhecido retorna False
    monkeypatch.setattr(QApplication, "widgetAt", lambda pos: None)
    monkeypatch.setattr(QApplication, "platformName", lambda: "xcb")
    assert relay._is_control_at_pos(btn, btn_center) is False

    # 5. Ponto fora do rect retorna False sempre
    assert relay._is_control_at_pos(btn, QPoint(-999, -999)) is False

    dialog.close()


def test_configure_button_local_press_dialog_close_allows_subsequent_external_activation(
    qapp, monkeypatch
) -> None:
    recorder = FakeRecorder()
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        mouse_shortcut_bridge=bridge,
    )
    window._active_mouse_button = "left"

    btn = window.configure_shortcut_button
    btn_center = btn.mapToGlobal(QPoint(btn.width() // 2, btn.height() // 2))

    # 1. EventFilter intercepta clique local no botão e arma supressão
    pos_f = QPointF(btn.width() / 2, btn.height() / 2)
    press_event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        pos_f,
        pos_f,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.eventFilter(btn, press_event)

    # 2. Botão entra em modo de configuração: pressed limpa a marca e para bridge
    btn.pressed.emit()
    assert window._is_configuring_shortcut is True
    assert bridge.stop_count >= 1

    # 3. Sinais residuais de ativação chegam durante a transição para configuração
    bridge.emit_activated(btn_center.x(), btn_center.y())
    qapp.processEvents()
    assert recorder.recording is False

    # 4. Diálogo abre e fecha (rejeitado/cancelado)
    def fake_exec(self_dialog: QDialog) -> int:
        return int(QDialog.DialogCode.Rejected)

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    window._configure_recording_shortcut()

    # Garante que o diálogo encerrou e a marca de supressão foi consumida/limpa
    assert window._dialog_open is False
    assert window._is_configuring_shortcut is False

    # 5. Usuário emite uma ativação externa legítima pós-fechamento
    outside_x = btn_center.x() + 2000
    outside_y = btn_center.y() + 2000
    bridge.emit_activated(outside_x, outside_y)
    qapp.processEvents()

    # Deve transicionar exatamente uma vez para gravação
    assert recorder.recording is True
    assert window.state is AppState.RECORDING

    window.close()


def test_stale_activated_during_dialog_does_not_suppress_next_external_activation(qapp) -> None:
    recorder = FakeRecorder()
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        mouse_shortcut_bridge=bridge,
    )
    window._active_mouse_button = "left"

    # Simula estado com diálogo de configuração aberto
    window._is_configuring_shortcut = True
    window._dialog_open = True

    # Sinais residuais chegam durante o diálogo
    bridge.emit_activated(10, 10)
    qapp.processEvents()
    assert recorder.recording is False
    assert window.state is AppState.IDLE

    # Diálogo fecha
    window._dialog_open = False
    window._is_configuring_shortcut = False

    # Ativação externa pós-fechamento
    bridge.emit_activated(-100, -100)
    qapp.processEvents()

    assert recorder.recording is True
    assert window.state is AppState.RECORDING
    window.close()


def test_restore_mouse_shortcut_diagnostic_preserved_when_missing_key_and_zero_microphones(
    qapp,
) -> None:
    # 1. Backend start failure preserva diagnóstico do mouse
    store1 = FakeLocalStore()
    store1.mouse_button = "x1"
    bridge1 = FakeMouseShortcutBridge(fail_start=True)
    window1, _ = make_window(
        qapp,
        settings=Settings(api_key=None),
        transcriber=None,
        local_store=store1,
        mouse_shortcut_bridge=bridge1,
        microphone_provider=lambda: (),
    )
    assert window1.status_label.text() == "Não foi possível ativar o atalho global do mouse."
    assert window1.record_button.isEnabled() is False
    assert window1._microphone_available is False
    window1.close()

    # 2. Wayland / sessão indisponível preserva diagnóstico do mouse
    store2 = FakeLocalStore()
    store2.mouse_button = "x1"
    bridge2 = FakeMouseShortcutBridge(available=False)
    window2, _ = make_window(
        qapp,
        settings=Settings(api_key=None),
        transcriber=None,
        local_store=store2,
        mouse_shortcut_bridge=bridge2,
        microphone_provider=lambda: (),
    )
    assert window2.status_label.text() == "Atalho global do mouse indisponível nesta sessão."
    assert window2.record_button.isEnabled() is False
    assert window2._microphone_available is False
    window2.close()

    # 3. Store read failure preserva diagnóstico do mouse
    store3 = FakeLocalStore(fail_mouse_get=True)
    bridge3 = FakeMouseShortcutBridge()
    window3, _ = make_window(
        qapp,
        settings=Settings(api_key=None),
        transcriber=None,
        local_store=store3,
        mouse_shortcut_bridge=bridge3,
        microphone_provider=lambda: (),
    )
    assert window3.status_label.text() == "Não foi possível ler preferência de atalho do mouse."
    assert window3.record_button.isEnabled() is False
    assert window3._microphone_available is False
    window3.close()


def test_stale_activated_old_generation_after_reconfiguration_zero_toggle_and_first_new_transitions_once(
    qapp,
) -> None:
    recorder = FakeRecorder()
    store = FakeLocalStore()
    store.mouse_button = "x1"
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
        recorder=recorder,
    )
    assert window.state is AppState.IDLE
    old_gen = bridge.generation

    # Start a new binding (increments generation)
    bridge.start("x2")
    new_gen = bridge.generation
    assert new_gen > old_gen

    # 1. Stale activation from old generation must be dropped (zero toggle)
    bridge._activated_event.emit(old_gen, 0, 0)
    bridge.activated.emit()
    qapp.processEvents()

    assert recorder.recording is False
    assert window.state is AppState.IDLE

    # 2. First activation from new generation transitions once
    bridge._activated_event.emit(new_gen, 0, 0)
    bridge.activated.emit()
    qapp.processEvents()

    assert recorder.recording is True
    assert window.state is AppState.RECORDING
    window.close()


def test_stale_activated_old_generation_after_stop_zero_toggle(qapp) -> None:
    recorder = FakeRecorder()
    store = FakeLocalStore()
    store.mouse_button = "x1"
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
        recorder=recorder,
    )
    old_gen = bridge.generation
    bridge.stop()
    assert bridge.generation > old_gen

    # Stale activation after stop is dropped
    bridge._activated_event.emit(old_gen, 0, 0)
    bridge.activated.emit()
    qapp.processEvents()

    assert recorder.recording is False
    assert window.state is AppState.IDLE
    window.close()


def test_api_key_dialog_controls_and_fields_press_suppresses_global_left_shortcut(qapp) -> None:
    recorder = FakeRecorder()
    store = FakeLocalStore()
    store.mouse_button = "left"
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
        recorder=recorder,
    )
    window.show()
    qapp.processEvents()

    dialog = QDialog(window)
    layout = QVBoxLayout(dialog)
    key_input = QLineEdit(dialog)
    layout.addWidget(key_input)
    dialog_buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        parent=dialog,
    )
    layout.addWidget(dialog_buttons)
    dialog.show()
    qapp.processEvents()

    # Press on key_input
    global_input_pos = key_input.mapToGlobal(QPoint(5, 5))
    press_input = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(5, 5),
        QPointF(global_input_pos.x(), global_input_pos.y()),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(key_input, press_input)

    # Shortcut activation arriving after press is suppressed
    bridge.emit_activated(global_input_pos.x(), global_input_pos.y())
    qapp.processEvents()
    assert recorder.recording is False
    assert window.state is AppState.IDLE

    # Press on OK button in dialog_buttons
    ok_button = dialog_buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok_button is not None
    global_ok_pos = ok_button.mapToGlobal(QPoint(5, 5))
    press_btn = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(5, 5),
        QPointF(global_ok_pos.x(), global_ok_pos.y()),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(ok_button, press_btn)

    # Shortcut activation arriving after press is suppressed
    bridge.emit_activated(global_ok_pos.x(), global_ok_pos.y())
    qapp.processEvents()
    assert recorder.recording is False
    assert window.state is AppState.IDLE
    dialog.close()
    window.close()


def test_external_window_mouse_press_does_not_suppress_global_shortcut(qapp) -> None:
    recorder = FakeRecorder()
    store = FakeLocalStore()
    store.mouse_button = "left"
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
        recorder=recorder,
    )
    window.show()
    qapp.processEvents()

    external_button = QPushButton("External Window")
    external_button.show()
    qapp.processEvents()

    press_ext = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(5, 5),
        QPointF(5, 5),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(external_button, press_ext)
    bridge.emit_activated(5, 5)
    qapp.processEvents()
    assert recorder.recording is True
    assert window.state is AppState.RECORDING

    external_button.close()
    window.close()


def test_interleaved_old_generation_paused_event_and_new_generation_transitions_exactly_once(
    qapp,
) -> None:
    recorder = FakeRecorder()
    store = FakeLocalStore()
    store.mouse_button = "x1"
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
        recorder=recorder,
    )
    assert window.state is AppState.IDLE
    old_gen = bridge.generation

    # Reconfigura para nova geração
    bridge.start("x2")
    new_gen = bridge.generation
    assert new_gen > old_gen

    # Evento antigo da geração old_gen chega (deve ser descartado)
    bridge._activated_event.emit(old_gen, 0, 0)
    bridge.activated.emit()
    qapp.processEvents()

    assert recorder.recording is False
    assert window.state is AppState.IDLE

    # Evento novo da geração new_gen chega (deve ser aceito)
    bridge._activated_event.emit(new_gen, 0, 0)
    bridge.activated.emit()
    qapp.processEvents()

    assert recorder.recording is True
    assert window.state is AppState.RECORDING
    window.close()


def test_stale_capture_callback_after_cancel_and_reopen_dialog_does_not_fill_or_close_new_dialog(
    qapp,
    monkeypatch,
) -> None:
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        mouse_shortcut_bridge=bridge,
    )

    dialog_1_gen: int | None = None
    dialog_2_gen: int | None = None
    dialog_count = 0

    def fake_exec_first_dialog(dialog: QDialog) -> int:
        nonlocal dialog_1_gen, dialog_count
        dialog_count += 1
        dialog_1_gen = bridge.generation
        # Diálogo 1 é cancelado (reject)
        dialog.reject()
        return 0

    def fake_exec_second_dialog(dialog: QDialog) -> int:
        nonlocal dialog_2_gen, dialog_count
        dialog_count += 1
        dialog_2_gen = bridge.generation
        assert dialog_1_gen is not None
        assert dialog_2_gen > dialog_1_gen

        # Dispara evento stale da captura anterior (dialog_1_gen)
        bridge._button_captured_event.emit(dialog_1_gen, "x1", 100, 200)
        bridge.button_captured.emit("x1")
        qapp.processEvents()

        # O diálogo não deve ter sido aceito pelo evento antigo
        # Agora dispara evento válido da geração atual (dialog_2_gen)
        bridge._button_captured_event.emit(dialog_2_gen, "x2", 100, 200)
        qapp.processEvents()
        return 1

    monkeypatch.setattr(QDialog, "exec", fake_exec_first_dialog)
    res1, accepted1 = window._acquire_recording_mouse_button()
    assert accepted1 is False
    assert res1 is None

    monkeypatch.setattr(QDialog, "exec", fake_exec_second_dialog)
    res2, accepted2 = window._acquire_recording_mouse_button()
    assert accepted2 is True
    assert res2 == "x2"
    assert dialog_count == 2

    window.close()


def test_local_press_without_global_callback_does_not_lose_subsequent_external_global_press(
    qapp,
) -> None:
    recorder = FakeRecorder()
    store = FakeLocalStore()
    store.mouse_button = "left"
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
        recorder=recorder,
    )
    window.show()
    qapp.processEvents()

    # Pressiona localmente o botão record_button
    press_btn = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(5, 5),
        QPointF(5, 5),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(window.record_button, press_btn)
    assert window._local_press_record is not None

    # Nenhum callback global chega para esse clique local.
    # Em seguida, ocorre uma pressão externa global fora da janela (ex.: coordenadas 9999, 9999)
    cur_gen = bridge.generation
    bridge._activated_event.emit(cur_gen, 9999, 9999)
    bridge.activated.emit()
    qapp.processEvents()

    # A pressão externa deve ser aceita e não perdida
    assert recorder.recording is True
    assert window.state is AppState.RECORDING

    window.close()


def test_local_press_release_preserves_record_until_atomic_consumption(qapp) -> None:
    recorder = FakeRecorder()
    store = FakeLocalStore()
    store.mouse_button = "left"
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
        recorder=recorder,
    )
    window.show()
    window.editor.setPlainText("Texto no editor para botão ficar habilitado")
    window._update_actions()
    qapp.processEvents()

    # 1. Pressão local grava o registro
    press_btn = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(5, 5),
        QPointF(5, 5),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(window.clear_text_button, press_btn)
    assert window._local_press_record is not None

    # 2. Soltura (Release) preserva o registro para correlação com evento atômico
    release_btn = QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        QPointF(5, 5),
        QPointF(5, 5),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(window.clear_text_button, release_btn)
    assert window._local_press_record is not None

    # 3. Evento atômico correspondente consome o registro e suprime o toggle
    origin = window.clear_text_button.mapToGlobal(QPoint(5, 5))
    bridge.emit_activated(origin.x(), origin.y())
    qapp.processEvents()
    assert window._local_press_record is None
    assert recorder.recording is False
    assert window.state is AppState.IDLE

    window.close()
def test_atomic_path_out_of_order_old_event_discarded_new_event_transitions_once(
    qapp,
) -> None:
    recorder = FakeRecorder()
    store = FakeLocalStore()
    store.mouse_button = "x1"
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
        recorder=recorder,
    )
    assert window.state is AppState.IDLE
    old_gen = bridge.generation

    # Nova configuração avança a geração
    bridge.start("x2")
    new_gen = bridge.generation
    assert new_gen > old_gen

    # 1. Evento atômico antigo chega (deve ser descartado)
    bridge._activated_event.emit(old_gen, 0, 0)
    qapp.processEvents()

    # Nenhum toggle deve ocorrer
    assert recorder.recording is False
    assert window.state is AppState.IDLE

    # 2. Evento atômico da nova geração chega
    bridge._activated_event.emit(new_gen, 0, 0)
    qapp.processEvents()

    # Transiciona exatamente uma vez para RECORDING
    assert recorder.recording is True
    assert window.state is AppState.RECORDING

    window.close()


def test_atomic_path_stale_sequence_event_does_not_affect_second_dialog(
    qapp,
    monkeypatch,
) -> None:
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        mouse_shortcut_bridge=bridge,
    )

    dialog_1_gen: int | None = None
    dialog_2_gen: int | None = None

    def fake_exec_first_dialog(dialog: QDialog) -> int:
        nonlocal dialog_1_gen
        dialog_1_gen = bridge.generation
        dialog.reject()
        return 0

    def fake_exec_second_dialog(dialog: QDialog) -> int:
        nonlocal dialog_2_gen
        dialog_2_gen = bridge.generation
        assert dialog_1_gen is not None
        assert dialog_2_gen > dialog_1_gen

        # Dispara evento stale da captura anterior e legado observacional para provar que a UI ignora ambos
        bridge._button_captured_event.emit(dialog_1_gen, "x1", 100, 200)
        bridge.button_captured.emit("x1")
        qapp.processEvents()
        # Diálogo continua aberto e não afetado
        # Agora dispara evento válido da geração atual
        bridge._button_captured_event.emit(dialog_2_gen, "x2", 100, 200)
        qapp.processEvents()
        return 1

    monkeypatch.setattr(QDialog, "exec", fake_exec_first_dialog)
    res1, accepted1 = window._acquire_recording_mouse_button()
    assert accepted1 is False
    assert res1 is None

    monkeypatch.setattr(QDialog, "exec", fake_exec_second_dialog)
    res2, accepted2 = window._acquire_recording_mouse_button()
    assert accepted2 is True
    assert res2 == "x2"

    window.close()


def test_acquire_recording_mouse_button_synchronous_capture_during_begin_capture(
    qapp,
    monkeypatch,
) -> None:
    class FakeSynchronousCaptureBridge(FakeMouseShortcutBridge):
        def begin_capture(self) -> bool:
            started = super().begin_capture()
            if started:
                # Dispara evento síncrono imediatamente durante begin_capture
                self._button_captured_event.emit(self._generation, "x1", 0, 0)
            return started

    bridge = FakeSynchronousCaptureBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        mouse_shortcut_bridge=bridge,
    )

    def fake_exec_sync(dialog: QDialog) -> int:
        qapp.processEvents()
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(QDialog, "exec", fake_exec_sync)
    button, accepted = window._acquire_recording_mouse_button()
    assert accepted is True
    assert button == "x1"

    window.close()

def test_left_mouse_binding_press_release_clicked_self_disable_suppresses_late_activation(qapp) -> None:
    recorder = FakeRecorder()
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        mouse_shortcut_bridge=bridge,
    )
    window._active_mouse_button = "left"
    window.editor.setPlainText("Texto gravado para apagar")
    window._update_actions()
    assert window.clear_text_button.isEnabled() is True

    # 1. Dispatch local MouseButtonPress on the enabled clear_text_button
    press_event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(window.clear_text_button.width() / 2, window.clear_text_button.height() / 2),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QCoreApplication.sendEvent(window.clear_text_button, press_event)
    assert window._local_press_record is not None

    # 2. Dispatch local MouseButtonRelease - record is preserved
    release_event = QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        QPointF(window.clear_text_button.width() / 2, window.clear_text_button.height() / 2),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QCoreApplication.sendEvent(window.clear_text_button, release_event)
    assert window._local_press_record is not None

    # 3. Click handler runs and clears editor, which autodisables clear_text_button
    window.clear_text_button.clicked.emit()
    qapp.processEvents()
    assert window.editor.toPlainText() == ""
    assert window.clear_text_button.isEnabled() is False

    # 4. Late global activation signal arrives after button is already disabled
    btn_center = window.clear_text_button.mapToGlobal(
        QPoint(window.clear_text_button.width() // 2, window.clear_text_button.height() // 2)
    )
    bridge.emit_activated(btn_center.x(), btn_center.y())
    qapp.processEvents()

    # Must be suppressed by preserved record matching gen and rect, resulting in zero toggle
    assert recorder.recording is False
    assert window.state is AppState.IDLE
    assert window._local_press_record is None

    window.close()


def test_left_mouse_binding_press_release_on_initially_disabled_target_allows_toggle(qapp) -> None:
    recorder = FakeRecorder()
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        mouse_shortcut_bridge=bridge,
    )
    window._active_mouse_button = "left"
    window.editor.setPlainText("")
    window._update_actions()
    assert window.clear_text_button.isEnabled() is False

    # 1. Local press on disabled button does not create a press record
    press_event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(window.clear_text_button.width() / 2, window.clear_text_button.height() / 2),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QCoreApplication.sendEvent(window.clear_text_button, press_event)
    assert window._local_press_record is None

    # 2. Local release
    release_event = QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        QPointF(window.clear_text_button.width() / 2, window.clear_text_button.height() / 2),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QCoreApplication.sendEvent(window.clear_text_button, release_event)
    assert window._local_press_record is None

    # 3. Global activation arrives -> toggles recording globally
    btn_center = window.clear_text_button.mapToGlobal(
        QPoint(window.clear_text_button.width() // 2, window.clear_text_button.height() // 2)
    )
    bridge.emit_activated(btn_center.x(), btn_center.y())
    qapp.processEvents()

    assert recorder.recording is True
    assert window.state is AppState.RECORDING

    window.close()


def test_left_mouse_binding_local_press_release_then_outside_event_clears_and_toggles(qapp) -> None:
    recorder = FakeRecorder()
    store = FakeLocalStore()
    store.mouse_button = "left"
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        mouse_shortcut_bridge=bridge,
        recorder=recorder,
    )
    window.show()
    window.editor.setPlainText("Texto no editor para botão ficar habilitado")
    window._update_actions()
    qapp.processEvents()

    # 1. Local press and release on clear_text_button
    press_btn = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(5, 5),
        QPointF(5, 5),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(window.clear_text_button, press_btn)
    assert window._local_press_record is not None

    release_btn = QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        QPointF(5, 5),
        QPointF(5, 5),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(window.clear_text_button, release_btn)
    assert window._local_press_record is not None

    # 2. Outside global activation event arrives (outside window coordinates)
    bridge.emit_activated(9999, 9999)
    qapp.processEvents()

    # 3. Record is cleared and toggle is processed
    assert window._local_press_record is None
    assert recorder.recording is True
    assert window.state is AppState.RECORDING

    window.close()


def test_acquire_recording_mouse_button_connects_atomic_event_signal(
    qapp, monkeypatch
) -> None:
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        mouse_shortcut_bridge=bridge,
    )

    dialog_opened = False

    def fake_exec(dialog: QDialog) -> int:
        nonlocal dialog_opened
        dialog_opened = True
        bridge._button_captured_event.emit(bridge.generation, "x2", 0, 0)
        qapp.processEvents()
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    button, accepted = window._acquire_recording_mouse_button()
    assert dialog_opened is True
    assert accepted is True
    assert button == "x2"

    window.close()

def test_restore_mouse_shortcut_incompatible_schema_version_fail_soft_ui(qapp, tmp_path) -> None:
    db_path = tmp_path / "falafacil_v2.sqlite3"
    raw_conn = sqlite3.connect(str(db_path))
    raw_conn.execute("PRAGMA user_version = 2;")
    raw_conn.execute("CREATE TABLE future_table (data TEXT);")
    raw_conn.commit()
    raw_conn.close()

    # LocalStore recusa abrir versão 2
    with pytest.raises(LocalStoreError, match="Versão de schema incompatível: 2"):
        LocalStore(db_path)

    # App com local_store=None (fail-soft ao falhar LocalStore na inicialização)
    bridge = FakeMouseShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=None,
        mouse_shortcut_bridge=bridge,
    )
    assert window._active_mouse_button is None
    assert window.shortcut_indicator_label.text() == "Atalho do mouse: Desativado"

    window.close()
