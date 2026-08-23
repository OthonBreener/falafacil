from __future__ import annotations

import os
import time

import numpy as np
import pytest
from PySide6.QtCore import QByteArray
from PySide6.QtWidgets import QApplication

from falafacil.audio import AudioCapture, AudioDevice, AudioRecorderError
from falafacil.config import Settings
from falafacil.credentials import CredentialStoreError
from falafacil.transcription import TranscriptionDebug
from falafacil.ui import AppState, MainWindow


class FakeRecorder:
    def __init__(self, capture: AudioCapture | None = None, *, low_error: bool = False) -> None:
        self.recording = False
        self.selected_devices: list[int | str | None] = []
        self.capture = capture or make_capture()
        self.low_error = low_error

    def set_device(self, device: int | str | None) -> None:
        self.selected_devices.append(device)

    def start(self) -> None:
        self.recording = True

    def stop(self) -> AudioCapture:
        self.recording = False
        if self.low_error:
            raise AudioRecorderError("O áudio capturado está muito baixo.")
        return self.capture

    def last_capture(self) -> AudioCapture | None:
        return self.capture if self.low_error else None

    def is_recording(self) -> bool:
        return self.recording


class FakeTerminal:
    def send_text(self, text: str, clipboard_setter) -> None:
        clipboard_setter(text)


class FakeTranscriber:
    def __init__(self, text: str = "synthetic transcript") -> None:
        self.text = text
        self.calls: list[bytes] = []
        self._debug: TranscriptionDebug | None = None

    def transcribe(self, wav_bytes: bytes) -> str:
        self.calls.append(wav_bytes)
        self._debug = make_debug(len(wav_bytes), self.text)
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


class FakeSignal:
    def connect(self, slot) -> None:
        self.slot = slot


class FakeMediaPlayer:
    def __init__(self) -> None:
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


def make_debug(audio_bytes: int = 18, text: str = "synthetic transcript") -> TranscriptionDebug:
    return TranscriptionDebug(
        model="synthetic-model",
        prompt="prompt em português do Brasil",
        audio_bytes=audio_bytes,
        audio_mime_type="audio/wav",
        audio_base64_length=24,
        audio_base64_preview="UklGRHN5bnRoZXRpYw==",
        response_text=text,
        error=None,
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
):
    media_player = media_player or FakeMediaPlayer()
    window = MainWindow(
        settings or Settings(),
        recorder=recorder or FakeRecorder(),
        transcriber=transcriber,
        terminal_bridge=FakeTerminal(),
        api_key_store=store,
        transcriber_factory=factory,
        microphone_provider= microphone_provider or (
            lambda: (AudioDevice(0, "Microfone sintético", 1, True),)
        ),
        media_player_factory=lambda parent: (media_player, FakeAudioOutput()),
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
    assert "synthetic transcript" in window.return_debug.toPlainText()
    assert debug.audio_base64_preview in window.payload_debug.toPlainText()
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
    assert "microfones" in window.status_label.text()
    window.close()
