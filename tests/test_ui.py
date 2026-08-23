from __future__ import annotations

import os
import time

import numpy as np
import pytest
from PySide6.QtCore import QByteArray
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication
from falafacil.audio import AudioCapture, AudioDevice, AudioRecorderError
from falafacil.config import Settings
from falafacil.credentials import CredentialStoreError
from falafacil.storage import LocalStoreError, TokenTotals
from falafacil.terminal import TerminalBridgeError
from falafacil.transcription import TokenUsage, TranscriptionDebug, TranscriptionError
from falafacil.ui import AppState, MainWindow


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
    ) -> None:
        self.fail_record = fail_record
        self.fail_totals = fail_totals
        self.fail_close = fail_close
        self.fail_mic = fail_mic
        self.records: list[tuple[str, Any, str]] = []
        self.mic_identity: str | None = None
        self.closed = False

    def get_last_microphone_identity(self) -> str | None:
        if self.fail_mic:
            raise LocalStoreError("erro ao ler microfone")
        return self.mic_identity

    def save_last_microphone_identity(self, identity: str) -> None:
        if self.fail_mic:
            raise LocalStoreError("erro ao salvar microfone")
        self.mic_identity = identity

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
    def setAudioOutput(self, output) -> None:
        self.audio_output = output

    def setSourceDevice(self, device, url) -> None:
        self.source_device = device
        self.source_url = url

    def play(self) -> None:
        if self.fail_play is not None:
            raise self.fail_play
        self.play_count += 1
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
    widgets = {window.audio_debug, window.payload_debug, window.return_debug, window.usage_debug}
    assert len(widgets) == 4
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
    window.close()
