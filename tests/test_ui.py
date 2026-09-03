from __future__ import annotations

import os
import gc
import sqlite3
import threading
import time
import numpy as np
import pytest
from PySide6.QtCore import QBuffer, QByteArray, QCoreApplication, QEvent, QEventLoop, QIODevice, QObject, QPoint, QPointF, QRect, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent, QCursor, QGuiApplication, QKeyEvent, QKeySequence, QMouseEvent, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMenu,
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
from falafacil.terminal import TerminalBridgeError, TerminalTarget
from falafacil.transcription import TokenUsage, TranscriptionDebug, TranscriptionError
from falafacil.ui import (
    CAPTURE_WAITING_TEXT,
    GLOBAL_SHORTCUT_DEBOUNCE_SECONDS,
    AppState,
    MainWindow,
    SpellSuggestionPopup,
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
        status: str | None = None,
        order_log: list[str] | None = None,
    ) -> None:
        self.recording = False
        self.stop_count = 0
        self.order_log = order_log
        self.selected_devices: list[int | str | None] = []
        if capture is not None:
            self.capture = capture
        elif low_error:
            self.capture = make_capture(rms=0.001)
        else:
            self.capture = make_capture()
        self.low_error = low_error
        self.fail_start = fail_start
        self.fail_stop_error = fail_stop_error
        self.status = status
        self._last_capture: AudioCapture | None = None
    def set_device(self, device: int | str | None) -> None:
        self.selected_devices.append(device)

    def start(self, device: int | str | None = None) -> None:
        if self.fail_start:
            raise AudioRecorderError("Não foi possível acessar o microfone.")
        self.recording = True

    def stop(self) -> AudioCapture:
        self.stop_count += 1
        if self.order_log is not None:
            self.order_log.append("recorder")
        self.recording = False
        self._last_capture = self.capture
        if self.fail_stop_error is not None:
            raise self.fail_stop_error
        if self.low_error:
            raise AudioRecorderError("O áudio capturado está muito baixo.")
        return self.capture

    def last_capture(self) -> AudioCapture | None:
        return self._last_capture if self._last_capture is not None else (self.capture if self.low_error else None)

    def last_status(self) -> str | None:
        return self.status

    def is_recording(self) -> bool:
        return self.recording


class FakeTerminal:
    def __init__(
        self,
        *,
        fail_error: Exception | None = None,
        detected_target: TerminalTarget | None = None,
        detect_fail_error: Exception | None = None,
    ) -> None:
        self.fail_error = fail_error
        self.detected_target = detected_target
        self.detect_fail_error = detect_fail_error
        self.send_calls: list[tuple[str, TerminalTarget | None]] = []
        self.detect_calls: int = 0

    def detect_active_terminal(self) -> TerminalTarget | None:
        self.detect_calls += 1
        if self.detect_fail_error is not None:
            raise self.detect_fail_error
        return self.detected_target

    def send_text(
        self,
        text: str,
        clipboard_setter,
        *,
        target: TerminalTarget | None = None,
    ) -> None:
        self.send_calls.append((text, target))
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
        proofread_text: str = "texto revisado com sucesso",
        proofread_error: str | None = None,
    ) -> None:
        self.text = text
        self.usage = usage
        self.error = error
        self.model = model
        self.proofread_text = proofread_text
        self.proofread_error = proofread_error
        self.calls: list[bytes] = []
        self.proofread_calls: list[str] = []
        self._debug: TranscriptionDebug | None = None

    def transcribe(self, wav_bytes: bytes) -> str:
        self.calls.append(wav_bytes)
        self._debug = make_debug(len(wav_bytes), self.text, usage=self.usage)
        if self.error:
            raise TranscriptionError(self.error)
        return self.text

    def proofread(self, text: str) -> str:
        self.proofread_calls.append(text)
        self._debug = TranscriptionDebug(
            model=self.model,
            prompt="prompt de revisão",
            audio_bytes=len(text.encode("utf-8")),
            audio_mime_type="",
            audio_base64_length=0,
            audio_base64_preview="",
            response_text=self.proofread_text,
            error=self.proofread_error,
            usage=self.usage,
        )
        if self.proofread_error:
            raise TranscriptionError(self.proofread_error)
        return self.proofread_text

    def last_debug(self) -> TranscriptionDebug | None:
        return self._debug


class FakeSpellChecker:
    def __init__(
        self,
        *,
        available: bool = True,
        valid_words: tuple[str, ...] = ("palavra", "aqui", "texto", "correto"),
        suggestions: dict[str, list[str]] | None = None,
        ignored_words: list[str] | None = None,
    ) -> None:
        self._available = available
        self._valid_words = set(valid_words)
        self._suggestions = suggestions or {
            "errado": ["correto", "erado", "errada"],
        }
        self._ignored: set[str] = set(ignored_words or [])

    def is_available(self) -> bool:
        return self._available

    def check(self, word: str) -> bool:
        clean = word.strip().lower()
        if clean in self._ignored:
            return True
        return clean in self._valid_words

    def suggest(self, word: str, limit: int = 5) -> list[str]:
        clean = word.strip().lower()
        sugs = self._suggestions.get(clean, ["sugestão1", "sugestão2"])
        return sugs[:limit]

    def ignore_word(self, word: str) -> None:
        clean = word.strip().lower()
        if clean:
            self._ignored.add(clean)

    def is_ignored(self, word: str) -> bool:
        return word.strip().lower() in self._ignored

    def ignored_words(self) -> set[str]:
        return set(self._ignored)

    def tokenize(self, text: str) -> list[tuple[int, int, str]]:
        import re
        tokens = []
        for m in re.finditer(r"[A-Za-zÀ-ÖØ-öø-ÿ]+(?:-[A-Za-zÀ-ÖØ-öø-ÿ]+)*", text):
            tokens.append((m.start(), m.end(), m.group(0)))
        return tokens
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
        self.spellcheck_enabled: bool = True
        self.spellcheck_ignored_words: list[str] = []
        self.fail_spellcheck_save = False
        self.closed = False
        self.close_count = 0
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
    def get_spellcheck_enabled(self) -> bool:
        return self.spellcheck_enabled

    def save_spellcheck_enabled(self, enabled: bool) -> None:
        if self.fail_spellcheck_save:
            raise LocalStoreError("erro ao salvar corretor")
        self.spellcheck_enabled = bool(enabled)

    def get_spellcheck_ignored_words(self) -> list[str]:
        return list(self.spellcheck_ignored_words)

    def add_spellcheck_ignored_word(self, word: str) -> None:
        clean = word.strip().lower()
        if clean and clean not in self.spellcheck_ignored_words:
            self.spellcheck_ignored_words.append(clean)

    def close(self) -> None:
        self.close_count += 1
        if self.fail_close:
            raise LocalStoreError("erro ao fechar")
        self.closed = True
        self.close_order_log.append("store")
class FakeSignal:
    def connect(self, slot) -> None:
        self.slot = slot


class FakeMediaPlayer(QObject):
    mediaStatusChanged = Signal(object)
    playbackStateChanged = Signal(object)
    errorOccurred = Signal(object, str)

    def __init__(
        self,
        *,
        fail_play: Exception | None = None,
        fail_stop: Exception | None = None,
        fail_detach: Exception | None = None,
        fail_detach_device: Exception | None = None,
        fail_detach_url: Exception | None = None,
        order_log: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.fail_play = fail_play
        self.fail_stop = fail_stop
        self.fail_detach = fail_detach
        self.fail_detach_device = fail_detach_device
        self.fail_detach_url = fail_detach_url
        self.order_log = order_log
        self.audio_output = None
        self.source_device = None
        self.source_url = None
        self.play_calls = 0
        self.play_count = 0
        self.stop_count = 0
        self.cleared_source = None
        self.played_bytes: bytes | None = None
    def setAudioOutput(self, output) -> None:
        self.audio_output = output

    def setSourceDevice(self, device, url=None) -> None:
        err = self.fail_detach_device if self.fail_detach_device is not None else self.fail_detach
        if err is not None and device is None:
            raise err
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
        self.play_calls += 1
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
        if self.order_log is not None:
            self.order_log.append("media_player")
        if self.fail_stop is not None:
            raise self.fail_stop
    def setSource(self, url) -> None:
        err = self.fail_detach_url if self.fail_detach_url is not None else self.fail_detach
        if err is not None:
            raise err
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


def make_capture(
    pcm_bytes: bytes | None = None,
    *,
    wav_bytes: bytes | None = None,
    rms: float = 0.03,
    peak: float | None = None,
) -> AudioCapture:
    if pcm_bytes is None:
        pcm = np.array([[1000], [-1000], [500], [-500]], dtype=np.int16).tobytes()
    else:
        pcm = pcm_bytes
    frames = len(pcm) // 2
    if wav_bytes is None:
        wav = b"RIFF" + pcm
    else:
        wav = wav_bytes
    return AudioCapture(
        wav_bytes=wav,
        pcm_bytes=pcm,
        frames=frames,
        duration_seconds=frames / 16_000,
        rms=rms,
        peak=peak if peak is not None else 1000 / 32768,
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
    spell_checker=None,
    startup_message=None,
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
        spell_checker=spell_checker,
        startup_message=startup_message,
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

def wait_for_proofreading_worker(qapp, window) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and window._proofreading_thread is not None:
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

    assert factory_calls == [("ui-session-token", "gemini-3.5-flash-lite")]
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
    errors: list[BaseException] = []

    def inspect() -> None:
        try:
            dialog = window._settings_dialog
            assert dialog is not None
            assert window.model_combo.count() == 3
            labels = [window.model_combo.itemText(i) for i in range(3)]
            data = [window.model_combo.itemData(i) for i in range(3)]
            assert data == [
                "gemini-3.5-flash-lite",
                "gemini-3.7-flash",
                "gemini-3.8-flash",
            ]
            assert labels == [
                "Econômico e rápido — Gemini 3.5 Flash-Lite",
                "Qualidade — Gemini 3.7 Flash",
                "Mais capaz — Gemini 3.8 Flash",
            ]
            assert window.model_combo.currentData() == "gemini-3.5-flash-lite"
        except BaseException as exc:
            errors.append(exc)
        finally:
            if window._settings_dialog is not None:
                window._settings_dialog.reject()

    QTimer.singleShot(0, inspect)
    window._open_settings_dialog()
    if errors:
        raise errors[0]
    window.close()


def test_apply_model_preference_with_active_key(qapp) -> None:
    local_store = FakeLocalStore()
    factory_calls: list[tuple[str, str]] = []

    def factory(api_key: str, model: str):
        factory_calls.append((api_key, model))
        return FakeTranscriber()

    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-key", model="gemini-3.5-flash-lite"),
        local_store=local_store,
        factory=factory,
    )

    def apply_choice() -> None:
        idx = window.model_combo.findData("gemini-3.7-flash")
        assert idx >= 0
        window.model_combo.setCurrentIndex(idx)
        window.apply_model_button.click()
        window._settings_dialog.accept()

    QTimer.singleShot(0, apply_choice)
    window._open_settings_dialog()

    assert factory_calls == [("active-key", "gemini-3.7-flash")]
    assert window.settings.model == "gemini-3.7-flash"
    assert local_store.get_gemini_model() == "gemini-3.7-flash"
    assert window.transcriber is not None
    assert "Modelo Gemini configurado com sucesso." in window.status_label.text()
    window.close()


def test_apply_model_preference_gemini_3_8_flash_with_active_key(qapp) -> None:
    local_store = FakeLocalStore()
    factory_calls: list[tuple[str, str]] = []

    def factory(api_key: str, model: str):
        factory_calls.append((api_key, model))
        return FakeTranscriber()

    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-key", model="gemini-3.5-flash-lite"),
        local_store=local_store,
        factory=factory,
    )

    def apply_choice() -> None:
        try:
            idx = window.model_combo.findData("gemini-3.8-flash")
            assert idx >= 0
            window.model_combo.setCurrentIndex(idx)
            window.apply_model_button.click()
            window._settings_dialog.accept()
        finally:
            if window._settings_dialog is not None and window._settings_dialog.isVisible():
                window._settings_dialog.reject()
    QTimer.singleShot(0, apply_choice)
    window._open_settings_dialog()

    assert factory_calls == [("active-key", "gemini-3.8-flash")]
    assert window.settings.model == "gemini-3.8-flash"
    assert local_store.get_gemini_model() == "gemini-3.8-flash"
    assert window.transcriber is not None
    assert "Modelo Gemini configurado com sucesso." in window.status_label.text()
    window.close()


def test_apply_model_preference_without_key(qapp) -> None:
    local_store = FakeLocalStore()
    factory_calls: list[tuple[str, str]] = []

    window, _ = make_window(
        qapp,
        settings=Settings(model="gemini-3.5-flash-lite"),
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
    original_transcriber = FakeTranscriber(model="gemini-3.5-flash-lite")

    def failing_factory(api_key: str, model: str):
        raise RuntimeError("factory construction failed")

    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-key", model="gemini-3.5-flash-lite"),
        transcriber=original_transcriber,
        local_store=local_store,
        factory=failing_factory,
    )

    def apply_choice() -> None:
        idx = window.model_combo.findData("gemini-3.7-flash")
        window.model_combo.setCurrentIndex(idx)
        window.apply_model_button.click()
        # Verify visual rollback while dialog is still open
        assert window.model_combo.currentData() == "gemini-3.5-flash-lite"
        assert window.model_combo.currentIndex() == window.model_combo.findData(
            "gemini-3.5-flash-lite"
        )
        window._settings_dialog.reject()

    QTimer.singleShot(0, apply_choice)
    window._open_settings_dialog()

    assert window.settings.model == "gemini-3.5-flash-lite"
    assert window.transcriber is original_transcriber
    assert local_store.get_gemini_model() is None
    assert "Não foi possível configurar o modelo Gemini." in window.status_label.text()
    window.close()


def test_apply_model_preference_store_failure_keeps_session_model_only(qapp) -> None:
    local_store = FakeLocalStore(fail_model_save=True)

    window, _ = make_window(
        qapp,
        settings=Settings(model="gemini-3.5-flash-lite"),
        local_store=local_store,
    )

    def apply_choice() -> None:
        idx = window.model_combo.findData("gemini-3.7-flash")
        window.model_combo.setCurrentIndex(idx)
        window.apply_model_button.click()
        window._settings_dialog.accept()

    QTimer.singleShot(0, apply_choice)
    window._open_settings_dialog()

    assert window.settings.model == "gemini-3.7-flash"
    assert "apenas nesta sessão" in window.status_label.text()
    window.close()


def test_apply_model_preference_store_failure_with_active_key_keeps_session_model_only(
    qapp,
) -> None:
    local_store = FakeLocalStore(fail_model_save=True)
    original_transcriber = FakeTranscriber(model="gemini-3.5-flash-lite")
    factory_calls: list[tuple[str, str]] = []

    def tracking_factory(api_key: str, model: str):
        factory_calls.append((api_key, model))
        return FakeTranscriber(model=model)

    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-key", model="gemini-3.5-flash-lite"),
        transcriber=original_transcriber,
        local_store=local_store,
        factory=tracking_factory,
    )

    def apply_choice() -> None:
        idx = window.model_combo.findData("gemini-3.7-flash")
        window.model_combo.setCurrentIndex(idx)
        window.apply_model_button.click()
        window._settings_dialog.accept()

    QTimer.singleShot(0, apply_choice)
    window._open_settings_dialog()

    assert factory_calls == [("active-key", "gemini-3.7-flash")]
    assert window.settings.model == "gemini-3.7-flash"
    assert window.transcriber is not original_transcriber
    assert window.transcriber.model == "gemini-3.7-flash"
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
    assert window.record_button.text() == "Gravar"
    assert not window.copy_last_button.isEnabled()
    assert not window.clear_last_button.isEnabled()
    assert not window.terminal_button.isEnabled()
    assert not window.copy_and_archive_button.isEnabled()
    assert window.settings_button.isEnabled()

    window.last_message_editor.setPlainText("texto sintético")
    window._update_actions()
    assert window.copy_last_button.isEnabled()
    assert window.clear_last_button.isEnabled()
    assert window.terminal_button.isEnabled()

    window.state = AppState.RECORDING
    window._update_actions()
    assert window.settings_button.isEnabled()
    assert window.record_button.isEnabled()
    assert window.record_button.text() == "Parar e revisar áudio"

    window.state = AppState.TRANSCRIBING
    window._update_actions()
    assert not window.record_button.isEnabled()
    assert window.record_button.text() == "Transcrevendo…"
    assert not window.copy_last_button.isEnabled()
    assert not window.clear_last_button.isEnabled()
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
    assert window.record_button.text() == "Enviar para Gemini"
    window._send_pending_audio()
    wait_for_worker(qapp, window)

    assert window.state is AppState.READY
    assert window.transcription_editor.toPlainText() == ""
    assert window.last_message_editor.toPlainText() == "synthetic transcript"
    window.close()


def test_clear_last_button_removes_last_message_editor_text(qapp) -> None:
    window, _ = make_window(qapp)
    window.last_message_editor.setPlainText("texto para apagar")
    window.state = AppState.READY
    window._update_actions()

    window.clear_last_button.click()

    assert window.last_message_editor.toPlainText() == ""
    assert not window.clear_last_button.isEnabled()
    assert window.status_label.text() == "Texto apagado."
    assert window.state is AppState.IDLE
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
    assert window.record_button.isEnabled()
    assert window.record_button.text() == "Enviar para Gemini"
    assert window.record_again_button.isEnabled()
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
    assert window.play_audio_button.text() == "Parar reprodução"
    assert media_player.play_count == 1
    assert media_player.source_device is window._audio_buffer
    assert bytes(media_player.source_device.data()) == window._pending_capture.wav_bytes
    assert transcriber.calls == []

    saved_wav = window._pending_capture.wav_bytes
    window._send_pending_audio()
    wait_for_worker(qapp, window)
    assert transcriber.calls == [saved_wav]
    assert window.transcription_editor.toPlainText() == ""
    assert window.last_message_editor.toPlainText() == "synthetic transcript"
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
    assert window.record_button.text() == "Enviar para Gemini"
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

    assert media_player.stop_count >= 1
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
    assert window.status_label.text() == "Texto copiado e movido para Última mensagem."
    assert window.transcription_editor.toPlainText() == ""
    assert window.last_message_editor.toPlainText() == "texto ok"
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


class StrictNonblockingThread(QThread):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.quit_called = False
        self._running_flag = True

    def isRunning(self) -> bool:
        return self._running_flag

    def quit(self) -> None:
        self.quit_called = True

    def wait(self, *args: Any, **kwargs: Any) -> bool:
        raise AssertionError("QThread.wait() must never be called in deferred close!")

    def terminate(self) -> None:
        raise AssertionError("QThread.terminate() must never be called in deferred close!")

    def simulate_finish(self) -> None:
        self._running_flag = False
        self.finished.emit()


class SingleShotSpy:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[int, Any]] = []

        def _spy_single_shot(msec: int, callback: Any) -> None:
            self.calls.append((msec, callback))

        monkeypatch.setattr(QTimer, "singleShot", _spy_single_shot)

    def trigger_all(self) -> None:
        for _msec, callback in list(self.calls):
            callback()


class CloseEventRecorder:
    def __init__(self, window: MainWindow, monkeypatch: pytest.MonkeyPatch) -> None:
        self.window = window
        self.events: list[tuple[QCloseEvent, bool]] = []
        orig_close_event = MainWindow.closeEvent

        def _recorded_close_event(w: MainWindow, event: QCloseEvent) -> None:
            orig_close_event(w, event)
            if w is self.window:
                self.events.append((event, event.isAccepted()))

        monkeypatch.setattr(MainWindow, "closeEvent", _recorded_close_event)

    @property
    def accepted_count(self) -> int:
        return sum(1 for _, accepted in self.events if accepted)

    @property
    def ignored_count(self) -> int:
        return sum(1 for _, accepted in self.events if not accepted)

    def clear(self) -> None:
        self.events.clear()
def test_startup_message_wins_after_diagnostics_and_disables_recording_until_recovery(
    qapp,
) -> None:
    msg = "Não foi possível iniciar o Gemini. Revise a chave ou o modelo nas Configurações."
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="valid-key"),
        transcriber=None,
        startup_message=msg,
    )
    assert window.status_label.text() == msg
    assert window.record_button.isEnabled() is False

    started = window._start_recording()
    assert started is True
    assert window.state is not AppState.RECORDING
    assert window.recorder.is_recording() is False

    window.transcriber = FakeTranscriber()
    window._update_actions()
    assert window.record_button.isEnabled() is True
    assert window.record_button.text() == "Gravar"
    window.close()


def test_close_deferred_transcription_thread_nonblocking_and_single_final_close(
    qapp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    store = FakeLocalStore(order_log=order)
    bridge = FakeInputShortcutBridge(order_log=order)
    installer = FakeShortcutInstaller(order_log=order)
    recorder = FakeRecorder()
    window, _ = make_window(
        qapp,
        local_store=store,
        input_shortcut_bridge=bridge,
        shortcut_service_installer=installer,
        recorder=recorder,
        settings=Settings(api_key="active-token"),
    )
    event_rec = CloseEventRecorder(window, monkeypatch)
    window._pending_capture = make_capture(b"pending")
    window._origin_terminal_target = TerminalTarget(
        window_id="123", pid="456", process_name="xterm"
    )

    thread = StrictNonblockingThread(window)
    window._thread = thread  # type: ignore[assignment]
    window._thread.finished.connect(window._on_thread_finished)

    close_event = QCloseEvent()
    window.closeEvent(close_event)

    # 1. Nonblocking: event ignored, window hidden, _close_pending set
    assert close_event.isAccepted() is False
    assert window.isVisible() is False
    assert window._close_pending is True
    assert window._is_closing is True
    assert thread.quit_called is True
    assert event_rec.accepted_count == 0
    assert event_rec.ignored_count == 1

    # 2. Cleanup performed once
    assert store.closed is True
    assert window.local_store is None
    assert bridge.closed is True
    assert installer.cancel_count >= 1
    assert window._pending_capture is None
    assert window._origin_terminal_target is None

    # 3. Repeated close while pending is idempotent and ignores promptly
    second_close = QCloseEvent()
    window.closeEvent(second_close)
    assert second_close.isAccepted() is False
    assert len(order) == 3
    assert event_rec.accepted_count == 0
    assert event_rec.ignored_count == 2

    # 4. Late signals from worker do not mutate editors, diagnostics, clipboard, or status
    window._on_transcription_finished("late result", make_debug("late result"))
    assert window.transcription_editor.toPlainText() == ""
    assert window.last_message_editor.toPlainText() == ""
    assert "late result" not in window.status_label.text()

    window._on_transcription_failed("late error", None)
    assert "late error" not in window.status_label.text()

    # 5. Thread finishes naturally -> schedules exactly one final close
    event_rec.clear()
    thread.simulate_finish()
    assert window._close_pending is False
    assert window._thread is None

    qapp.processEvents()
    assert event_rec.accepted_count == 1
    assert event_rec.ignored_count == 0
    assert len(event_rec.events) == 1
    assert event_rec.events[0][1] is True


def test_close_deferred_proofreading_thread_nonblocking_and_single_final_close(
    qapp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    store = FakeLocalStore(order_log=order)
    window, _ = make_window(
        qapp, local_store=store, settings=Settings(api_key="active-token")
    )
    event_rec = CloseEventRecorder(window, monkeypatch)
    window.last_message_editor.setPlainText("Texto original")

    thread = StrictNonblockingThread(window)
    window._proofreading_thread = thread  # type: ignore[assignment]
    window._proofreading_thread.finished.connect(
        window._on_proofreading_thread_finished
    )
    window._is_reviewing = True

    close_event = QCloseEvent()
    window.closeEvent(close_event)

    assert close_event.isAccepted() is False
    assert window.isVisible() is False
    assert window._close_pending is True
    assert window._is_closing is True
    assert thread.quit_called is True
    assert store.closed is True
    assert window.local_store is None
    assert event_rec.accepted_count == 0
    assert event_rec.ignored_count == 1

    # Late proofreading signals inert
    window._on_proofreading_finished(
        "Texto modificado tardio", make_debug("Texto modificado tardio")
    )
    assert window.last_message_editor.toPlainText() == "Texto original"
    assert "Texto modificado tardio" not in window.status_label.text()

    window._on_proofreading_failed("Erro de revisão tardio", None)
    assert "Erro de revisão tardio" not in window.status_label.text()

    event_rec.clear()
    thread.simulate_finish()
    assert window._close_pending is False
    assert window._proofreading_thread is None

    qapp.processEvents()
    assert event_rec.accepted_count == 1
    assert event_rec.ignored_count == 0
    assert len(event_rec.events) == 1
    assert event_rec.events[0][1] is True


def test_close_deferred_both_threads_running_closes_only_after_last_finishes(
    qapp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeLocalStore()
    window, _ = make_window(
        qapp, local_store=store, settings=Settings(api_key="active-token")
    )
    event_rec = CloseEventRecorder(window, monkeypatch)

    thread1 = StrictNonblockingThread(window)
    window._thread = thread1  # type: ignore[assignment]
    window._thread.finished.connect(window._on_thread_finished)

    thread2 = StrictNonblockingThread(window)
    window._proofreading_thread = thread2  # type: ignore[assignment]
    window._proofreading_thread.finished.connect(
        window._on_proofreading_thread_finished
    )

    close_event = QCloseEvent()
    window.closeEvent(close_event)

    assert close_event.isAccepted() is False
    assert window._close_pending is True
    assert thread1.quit_called is True
    assert thread2.quit_called is True
    assert event_rec.accepted_count == 0
    assert event_rec.ignored_count == 1

    # First thread finishes: close_pending must stay True because thread2 is still running
    thread1.simulate_finish()
    assert window._thread is None
    assert window._close_pending is True
    qapp.processEvents()
    assert window._is_closing is True
    assert event_rec.accepted_count == 0

    # Second thread finishes: now both are done, schedules final close
    event_rec.clear()
    thread2.simulate_finish()
    assert window._proofreading_thread is None
    assert window._close_pending is False

    qapp.processEvents()
    assert event_rec.accepted_count == 1
    assert event_rec.ignored_count == 0
    assert len(event_rec.events) == 1
    assert event_rec.events[0][1] is True


def test_close_when_threads_stopped_accepts_immediately(qapp) -> None:
    store = FakeLocalStore()
    window, _ = make_window(qapp, local_store=store)

    thread = StrictNonblockingThread(window)
    thread._running_flag = False
    window._thread = thread  # type: ignore[assignment]

    proof_thread = StrictNonblockingThread(window)
    proof_thread._running_flag = False
    window._proofreading_thread = proof_thread  # type: ignore[assignment]

    close_event = QCloseEvent()
    window.closeEvent(close_event)

    assert close_event.isAccepted() is True
    assert store.closed is True
    assert window._thread is None
    assert window._proofreading_thread is None


def test_close_repeated_event_when_thread_stopped_before_finished_queued_does_not_directly_accept(
    qapp, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = SingleShotSpy(monkeypatch)
    store = FakeLocalStore()
    window, _ = make_window(qapp, local_store=store, settings=Settings(api_key="active-token"))
    recorder = CloseEventRecorder(window, monkeypatch)

    thread = StrictNonblockingThread(window)
    window._thread = thread  # type: ignore[assignment]
    window._worker = object()  # type: ignore[assignment]
    thread.finished.connect(window._on_thread_finished)

    # 1. First close event: ignores, hides, sets _close_pending
    first_close = QCloseEvent()
    window.closeEvent(first_close)
    assert first_close.isAccepted() is False
    assert window._is_closing is True
    assert window._close_pending is True
    assert len(spy.calls) == 0
    assert recorder.accepted_count == 0
    assert recorder.ignored_count == 1

    # 2. Thread stops running (isRunning becomes False), but finished signal has not run yet
    thread._running_flag = False

    # 3. Repeated close event arrives: immediately clears stopped thread/worker refs,
    # schedules final close callback, and ignores this intermediate event
    second_close = QCloseEvent()
    window.closeEvent(second_close)
    assert second_close.isAccepted() is False
    assert window._thread is None
    assert window._worker is None
    assert window._close_pending is False
    assert len(spy.calls) == 1
    assert spy.calls[0] == (0, window.close)
    assert recorder.accepted_count == 0
    assert recorder.ignored_count == 2

    # 4. Queued finished signal executes completion helper idempotently without scheduling another callback
    thread.finished.emit()
    assert window._thread is None
    assert window._worker is None
    assert len(spy.calls) == 1

    # 5. Clear recorded close events before executing final callback
    recorder.clear()

    # 6. Executing scheduled callback triggers exactly one accepted final close
    spy.trigger_all()
    assert recorder.accepted_count == 1
    assert recorder.ignored_count == 0
    assert len(recorder.events) == 1
    assert recorder.events[0][1] is True

    # 7. Late repeated finished signals schedule nothing additional
    window._on_thread_finished()
    assert len(spy.calls) == 1


def test_close_repeated_event_when_proofreading_thread_stopped_before_finished_queued_does_not_directly_accept(
    qapp, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = SingleShotSpy(monkeypatch)
    store = FakeLocalStore()
    window, _ = make_window(qapp, local_store=store, settings=Settings(api_key="active-token"))
    recorder = CloseEventRecorder(window, monkeypatch)

    thread = StrictNonblockingThread(window)
    window._proofreading_thread = thread  # type: ignore[assignment]
    window._proofreading_worker = object()  # type: ignore[assignment]
    thread.finished.connect(window._on_proofreading_thread_finished)

    # 1. First close event: ignores, hides, sets _close_pending
    first_close = QCloseEvent()
    window.closeEvent(first_close)
    assert first_close.isAccepted() is False
    assert window._is_closing is True
    assert window._close_pending is True
    assert len(spy.calls) == 0
    assert recorder.accepted_count == 0
    assert recorder.ignored_count == 1

    # 2. Thread stops running
    thread._running_flag = False

    # 3. Repeated close event arrives: clears stopped refs, schedules single final close, ignores event
    second_close = QCloseEvent()
    window.closeEvent(second_close)
    assert second_close.isAccepted() is False
    assert window._proofreading_thread is None
    assert window._proofreading_worker is None
    assert window._close_pending is False
    assert len(spy.calls) == 1
    assert spy.calls[0] == (0, window.close)
    assert recorder.accepted_count == 0
    assert recorder.ignored_count == 2

    # 4. Queued finished signal is idempotent
    thread.finished.emit()
    assert window._proofreading_thread is None
    assert window._proofreading_worker is None
    assert len(spy.calls) == 1

    # 5. Clear recorder and execute scheduled callback
    recorder.clear()
    spy.trigger_all()
    assert recorder.accepted_count == 1
    assert recorder.ignored_count == 0
    assert len(recorder.events) == 1
    assert recorder.events[0][1] is True

    # 6. Late signals schedule nothing
    window._on_proofreading_thread_finished()
    assert len(spy.calls) == 1


def test_close_final_scheduling_explicit_timer_spy_transcription_only(
    qapp, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = SingleShotSpy(monkeypatch)
    window, _ = make_window(qapp, settings=Settings(api_key="active-token"))
    recorder = CloseEventRecorder(window, monkeypatch)

    thread = StrictNonblockingThread(window)
    window._thread = thread  # type: ignore[assignment]
    window._worker = object()  # type: ignore[assignment]
    thread.finished.connect(window._on_thread_finished)

    # Initial close -> ignored
    close_event = QCloseEvent()
    window.closeEvent(close_event)
    assert close_event.isAccepted() is False
    assert len(spy.calls) == 0
    assert recorder.accepted_count == 0
    assert recorder.ignored_count == 1

    # Repeated close while running -> ignored
    repeat_event = QCloseEvent()
    window.closeEvent(repeat_event)
    assert repeat_event.isAccepted() is False
    assert len(spy.calls) == 0
    assert recorder.accepted_count == 0
    assert recorder.ignored_count == 2

    # Thread finishes -> exactly one callback scheduled
    thread.simulate_finish()
    assert len(spy.calls) == 1
    assert spy.calls[0] == (0, window.close)
    assert window._thread is None
    assert window._worker is None
    assert window._close_pending is False

    # Repeated finished signal schedules nothing additional
    window._on_thread_finished()
    assert len(spy.calls) == 1

    # Clear prior recorded close events before testing the final callback
    recorder.clear()

    # Execute scheduled callback -> exactly one final close accepted
    spy.trigger_all()
    assert recorder.accepted_count == 1
    assert recorder.ignored_count == 0
    assert len(recorder.events) == 1
    assert recorder.events[0][1] is True

    # Further repeated finished signals or close calls schedule nothing additional
    window._on_thread_finished()
    assert len(spy.calls) == 1


def test_close_final_scheduling_explicit_timer_spy_proofreading_only(
    qapp, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = SingleShotSpy(monkeypatch)
    window, _ = make_window(qapp, settings=Settings(api_key="active-token"))
    recorder = CloseEventRecorder(window, monkeypatch)

    thread = StrictNonblockingThread(window)
    window._proofreading_thread = thread  # type: ignore[assignment]
    window._proofreading_worker = object()  # type: ignore[assignment]
    thread.finished.connect(window._on_proofreading_thread_finished)

    # Initial close -> ignored
    close_event = QCloseEvent()
    window.closeEvent(close_event)
    assert close_event.isAccepted() is False
    assert len(spy.calls) == 0
    assert recorder.accepted_count == 0
    assert recorder.ignored_count == 1

    # Repeated close while running -> ignored
    repeat_event = QCloseEvent()
    window.closeEvent(repeat_event)
    assert repeat_event.isAccepted() is False
    assert len(spy.calls) == 0
    assert recorder.accepted_count == 0
    assert recorder.ignored_count == 2

    # Thread finishes -> exactly one callback scheduled
    thread.simulate_finish()
    assert len(spy.calls) == 1
    assert spy.calls[0] == (0, window.close)
    assert window._proofreading_thread is None
    assert window._proofreading_worker is None
    assert window._close_pending is False

    # Repeated finished signal schedules nothing additional
    window._on_proofreading_thread_finished()
    assert len(spy.calls) == 1

    # Clear prior recorded close events before testing final callback
    recorder.clear()

    # Execute scheduled callback -> exactly one final close accepted
    spy.trigger_all()
    assert recorder.accepted_count == 1
    assert recorder.ignored_count == 0
    assert len(recorder.events) == 1
    assert recorder.events[0][1] is True

    # Further repeated finished signals schedule nothing additional
    window._on_proofreading_thread_finished()
    assert len(spy.calls) == 1


def test_close_final_scheduling_explicit_timer_spy_both_threads(
    qapp, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = SingleShotSpy(monkeypatch)
    window, _ = make_window(qapp, settings=Settings(api_key="active-token"))
    recorder = CloseEventRecorder(window, monkeypatch)

    thread1 = StrictNonblockingThread(window)
    thread2 = StrictNonblockingThread(window)
    window._thread = thread1  # type: ignore[assignment]
    window._worker = object()  # type: ignore[assignment]
    window._proofreading_thread = thread2  # type: ignore[assignment]
    window._proofreading_worker = object()  # type: ignore[assignment]
    thread1.finished.connect(window._on_thread_finished)
    thread2.finished.connect(window._on_proofreading_thread_finished)

    # Initial close -> ignored
    close_event = QCloseEvent()
    window.closeEvent(close_event)
    assert close_event.isAccepted() is False
    assert len(spy.calls) == 0
    assert recorder.accepted_count == 0
    assert recorder.ignored_count == 1

    # First thread finishes: only its own refs cleared, 0 timers scheduled
    thread1.simulate_finish()
    assert window._thread is None
    assert window._worker is None
    assert window._proofreading_thread is thread2
    assert window._proofreading_worker is not None
    assert window._close_pending is True
    assert len(spy.calls) == 0

    # Repeated close while waiting for thread2: 0 timers scheduled, ignored
    repeat_event = QCloseEvent()
    window.closeEvent(repeat_event)
    assert repeat_event.isAccepted() is False
    assert len(spy.calls) == 0
    assert recorder.accepted_count == 0
    assert recorder.ignored_count == 2

    # Second thread finishes: now both cleared -> exactly 1 timer scheduled
    thread2.simulate_finish()
    assert window._proofreading_thread is None
    assert window._proofreading_worker is None
    assert window._close_pending is False
    assert len(spy.calls) == 1
    assert spy.calls[0] == (0, window.close)

    # Repeated finished signals schedule nothing additional
    window._on_thread_finished()
    window._on_proofreading_thread_finished()
    assert len(spy.calls) == 1

    # Clear prior recorded close events before testing final callback
    recorder.clear()

    # Execute scheduled callback -> exactly one final close accepted
    spy.trigger_all()
    assert recorder.accepted_count == 1
    assert recorder.ignored_count == 0
    assert len(recorder.events) == 1
    assert recorder.events[0][1] is True

    # Further repeated finished signals schedule nothing additional
    window._on_thread_finished()
    window._on_proofreading_thread_finished()
    assert len(spy.calls) == 1
def test_close_late_result_and_failure_inertness_preserves_all_widgets_clipboard_diagnostics_chart_storage(
    qapp,
) -> None:
    store = FakeLocalStore()
    window, _ = make_window(
        qapp,
        local_store=store,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
    )

    # 1. Seed initial states across all surfaces
    window.transcription_editor.setPlainText("Seed Transcription Editor Text")
    window.last_message_editor.setPlainText("Seed Last Message Editor Text")
    window.status_label.setText("Seed Status Label Text")
    QApplication.clipboard().setText("Seed Clipboard Text")
    window.audio_debug.setPlainText("Seed Audio Diagnostics")
    window.payload_debug.setPlainText("Seed Payload Diagnostics")
    window.return_debug.setPlainText("Seed Return Diagnostics")
    window.usage_debug.setPlainText("Seed Usage Diagnostics")

    debug_initial = make_debug("Seed Usage", usage=TokenUsage(total_tokens=100))
    window._record_and_render_usage(debug_initial, "success")
    initial_chart_records = tuple(window.usage_chart.records)
    initial_store_records = list(store.records)
    # Snapshot all seeded values
    snapshot_transcription = window.transcription_editor.toPlainText()
    snapshot_last_message = window.last_message_editor.toPlainText()
    snapshot_status = window.status_label.text()
    snapshot_clipboard = QApplication.clipboard().text()
    snapshot_audio_debug = window.audio_debug.toPlainText()
    snapshot_payload_debug = window.payload_debug.toPlainText()
    snapshot_return_debug = window.return_debug.toPlainText()
    snapshot_usage_debug = window.usage_debug.toPlainText()

    # 2. Enter closing state via running thread
    thread = StrictNonblockingThread(window)
    window._thread = thread  # type: ignore[assignment]
    window._worker = object()  # type: ignore[assignment]
    thread.finished.connect(window._on_thread_finished)

    close_event = QCloseEvent()
    window.closeEvent(close_event)
    assert close_event.isAccepted() is False
    assert window._is_closing is True
    assert store.closed is True
    assert window.local_store is None

    # Reset status_label to snapshot value to isolate late-signal inertness
    window.status_label.setText(snapshot_status)

    # 3. Deliver late signals for transcription and proofreading (success and failure)
    late_debug_trans_success = make_debug("Late Trans Success", usage=TokenUsage(total_tokens=200))
    window._on_transcription_finished("Late Trans Success Result", late_debug_trans_success)

    late_debug_trans_failed = make_debug("Late Trans Failed", usage=TokenUsage(total_tokens=50))
    window._on_transcription_failed("Late Trans Failed Message", late_debug_trans_failed)

    late_debug_proof_success = make_debug("Late Proof Success", usage=TokenUsage(total_tokens=300))
    window._on_proofreading_finished("Late Proof Success Result", late_debug_proof_success)

    late_debug_proof_failed = make_debug("Late Proof Failed", usage=TokenUsage(total_tokens=75))
    window._on_proofreading_failed("Late Proof Failed Message", late_debug_proof_failed)

    # 4. Assert byte-for-byte / identity-equivalent NO mutation
    assert window.transcription_editor.toPlainText() == snapshot_transcription
    assert window.last_message_editor.toPlainText() == snapshot_last_message
    assert window.status_label.text() == snapshot_status
    assert QApplication.clipboard().text() == snapshot_clipboard
    assert window.audio_debug.toPlainText() == snapshot_audio_debug
    assert window.payload_debug.toPlainText() == snapshot_payload_debug
    assert window.return_debug.toPlainText() == snapshot_return_debug
    assert window.usage_debug.toPlainText() == snapshot_usage_debug
    assert window.usage_chart.records == initial_chart_records
    assert store.records == initial_store_records

    thread.simulate_finish()
    qapp.processEvents()


def test_close_integrated_deferred_close_with_all_resource_sentinels(
    qapp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    store = FakeLocalStore(order_log=order)
    bridge = FakeInputShortcutBridge(order_log=order)
    installer = FakeShortcutInstaller(order_log=order)
    recorder = FakeRecorder(order_log=order)
    media_player = FakeMediaPlayer(order_log=order)

    window, _ = make_window(
        qapp,
        local_store=store,
        input_shortcut_bridge=bridge,
        shortcut_service_installer=installer,
        recorder=recorder,
        media_player=media_player,
        settings=Settings(api_key="active-token"),
    )
    event_rec = CloseEventRecorder(window, monkeypatch)
    # 1. Install distinct sentinels
    class TranscriptionWorkerSentinel:
        pass

    class ProofreadingWorkerSentinel:
        pass

    trans_sentinel = TranscriptionWorkerSentinel()
    proof_sentinel = ProofreadingWorkerSentinel()
    window._worker = trans_sentinel  # type: ignore[assignment]
    window._proofreading_worker = proof_sentinel  # type: ignore[assignment]

    thread1 = StrictNonblockingThread(window)
    thread2 = StrictNonblockingThread(window)
    window._thread = thread1  # type: ignore[assignment]
    window._proofreading_thread = thread2  # type: ignore[assignment]
    thread1.finished.connect(window._on_thread_finished)
    thread2.finished.connect(window._on_proofreading_thread_finished)

    # 2. Setup active recorder, media player / buffer, timers, popup, dialog, filter, captures, target
    recorder.recording = True
    window.state = AppState.RECORDING

    window._is_playing_audio = True
    buf = QBuffer(window)
    buf.setData(b"test_audio")
    buf.open(QIODevice.OpenModeFlag.ReadOnly)
    window._audio_buffer = buf

    window._hover_spell_timer.start(250)
    window._popup_dismiss_timer.start(200)

    class FakePopup:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    popup = FakePopup()
    window._spell_popup = popup  # type: ignore[assignment]

    class FakeCaptureDialog:
        def __init__(self) -> None:
            self.rejected = False

        def reject(self) -> None:
            self.rejected = True

    capture_dialog = FakeCaptureDialog()
    window._capture_dialog = capture_dialog  # type: ignore[assignment]

    if QApplication.instance() is not None:
        QApplication.instance().installEventFilter(window)

    pending_capture = make_capture(b"pending_pcm")
    preserved_capture = make_capture(b"preserved_pcm")
    window._pending_capture = pending_capture
    window._preserved_capture = preserved_capture

    target = TerminalTarget(window_id="999", pid="1234", process_name="gnome-terminal-server")
    window._origin_terminal_target = target

    # 3. First close attempt: cleans each resource exactly once, closes store once, hides/ignores while workers active
    close_event1 = QCloseEvent()
    window.closeEvent(close_event1)

    assert close_event1.isAccepted() is False
    assert window.isVisible() is False
    assert window._close_pending is True
    assert window._is_closing is True
    assert thread1.quit_called is True
    assert thread2.quit_called is True

    # Workers remain owned while threads are running
    assert window._worker is trans_sentinel
    assert window._proofreading_worker is proof_sentinel

    # Resources cleaned exactly once
    assert recorder.stop_count == 1
    assert window._is_playing_audio is False
    assert window._audio_buffer is None
    assert buf.isOpen() is False
    assert window._hover_spell_timer.isActive() is False
    assert window._popup_dismiss_timer.isActive() is False
    assert popup.closed is True
    assert window._spell_popup is None
    assert capture_dialog.rejected is True
    assert installer.cancel_count == 1
    assert bridge.closed is True
    assert store.closed is True
    assert store.close_count == 1
    assert window.local_store is None
    assert window._pending_capture is None
    assert window._preserved_capture is None
    assert window._origin_terminal_target is None

    # 4. Repeated close does not repeat cleanup
    close_event2 = QCloseEvent()
    window.closeEvent(close_event2)
    assert close_event2.isAccepted() is False
    assert installer.cancel_count == 1
    assert store.close_count == 1

    # 5. Finishing thread 1 clears ONLY its own thread and worker refs
    thread1.simulate_finish()
    assert window._thread is None
    assert window._worker is None
    assert window._proofreading_thread is thread2
    assert window._proofreading_worker is proof_sentinel
    assert window._close_pending is True

    # Repeated close while thread 2 still active
    close_event3 = QCloseEvent()
    window.closeEvent(close_event3)
    assert close_event3.isAccepted() is False

    # 6. Finishing thread 2 clears its own thread and worker refs and schedules final close
    thread2.simulate_finish()
    assert window._proofreading_thread is None
    assert window._proofreading_worker is None
    assert window._close_pending is False

    # 7. Final callback accepts
    event_rec.clear()
    qapp.processEvents()
    assert event_rec.accepted_count == 1
    assert event_rec.ignored_count == 0
    assert len(event_rec.events) == 1
    assert event_rec.events[0][1] is True
    assert window._is_closing is True

def test_on_media_error_ignores_raw_error_string_and_secret(qapp) -> None:
    secret = "secret-token-media-error-3333"
    media_player = FakeMediaPlayer()
    recorder = FakeRecorder(capture=make_capture(b"error_secret_pcm"))
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        media_player=media_player,
    )
    window._start_recording()
    window._finish_recording()
    window._play_pending_audio()
    media_player.errorOccurred.emit(
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
        capture=AudioCapture(
            wav_bytes=b"",
            pcm_bytes=b"",
            frames=0,
            duration_seconds=0.0,
            rms=0.0,
            peak=0.0,
        ),
        fail_stop_error=AudioRecorderError("Não foi possível parar o microfone."),
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
    window.last_message_editor.setPlainText("Texto para colar")
    window.state = AppState.READY
    window.send_to_terminal()

    assert window.status_label.text() == "Não foi possível colar no terminal."
    assert secret not in window.status_label.text()
    assert secret not in window.audio_debug.toPlainText()
    assert secret not in window.payload_debug.toPlainText()
    assert secret not in window.return_debug.toPlainText()
    assert secret not in window.usage_debug.toPlainText()


def test_global_shortcut_detects_origin_terminal_without_raise_on_start_and_retains_target(qapp) -> None:
    origin_target = TerminalTarget(
        window_id="9876",
        pid="5432",
        process_name="kitty",
    )
    terminal = FakeTerminal(detected_target=origin_target)
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        terminal=terminal,
    )
    event_order: list[str] = []
    real_detect = terminal.detect_active_terminal

    def tracked_detect():
        event_order.append("detect_active_terminal")
        return real_detect()

    terminal.detect_active_terminal = tracked_detect

    real_raise = window._raise_to_front

    def tracked_raise():
        event_order.append("raise_to_front")
        return real_raise()

    window._raise_to_front = tracked_raise

    window._activate_recording_shortcut()

    assert event_order == ["detect_active_terminal"]
    assert window.state is AppState.RECORDING
    assert window._origin_terminal_target == origin_target
    window.close()

def test_global_start_then_global_stop_preserves_origin_terminal_target(qapp) -> None:
    origin_target = TerminalTarget(
        window_id="9876",
        pid="5432",
        process_name="kitty",
    )
    terminal = FakeTerminal(detected_target=origin_target)
    recorder = FakeRecorder(capture=make_capture(b"pcm_valid_sample", wav_bytes=b"RIFFwav_sample"))
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        terminal=terminal,
    )
    # 1. Global shortcut starts recording
    window._activate_recording_shortcut()
    assert window.state is AppState.RECORDING
    assert window._origin_terminal_target == origin_target
    assert terminal.detect_calls == 1

    # Reset debounce
    window._last_global_activation_time = 0.0

    # 2. Second global shortcut stops recording
    window._activate_recording_shortcut()
    assert window.state is AppState.AUDIO_READY
    assert window._origin_terminal_target == origin_target
    assert terminal.detect_calls == 1
    # 3. Send to terminal passes origin target and clears it
    window.last_message_editor.setPlainText("comando enviado")
    window.send_to_terminal()
    assert terminal.send_calls == [("comando enviado", origin_target)]
    assert window._origin_terminal_target is None
    window.close()

def test_global_start_then_manual_stop_preserves_origin_terminal_target(qapp) -> None:
    origin_target = TerminalTarget(
        window_id="9876",
        pid="5432",
        process_name="kitty",
    )
    terminal = FakeTerminal(detected_target=origin_target)
    recorder = FakeRecorder(capture=make_capture(b"pcm_valid_sample", wav_bytes=b"RIFFwav_sample"))
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        terminal=terminal,
    )
    # 1. Global shortcut starts recording
    window._activate_recording_shortcut()
    assert window.state is AppState.RECORDING
    assert window._origin_terminal_target == origin_target
    assert terminal.detect_calls == 1

    # 2. Manual stop button stops recording
    window._perform_primary_action()
    assert window.state is AppState.AUDIO_READY
    assert window._origin_terminal_target == origin_target
    assert terminal.detect_calls == 1
    # 3. Send to terminal passes origin target and clears it
    window.last_message_editor.setPlainText("comando manual stop")
    window.send_to_terminal()
    assert terminal.send_calls == [("comando manual stop", origin_target)]
    assert window._origin_terminal_target is None
    window.close()



def test_manual_start_clears_origin_terminal_target(qapp) -> None:
    terminal = FakeTerminal()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        terminal=terminal,
    )
    window._origin_terminal_target = TerminalTarget(
        window_id="111",
        pid="222",
        process_name="konsole",
    )
    window._perform_primary_action()

    assert window.state is AppState.RECORDING
    assert window._origin_terminal_target is None
    assert terminal.detect_calls == 0
    window.close()


def test_global_shortcut_detection_failure_does_not_block_recording(qapp) -> None:
    terminal = FakeTerminal(detect_fail_error=RuntimeError("xdotool crash"))
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        terminal=terminal,
    )
    window._activate_recording_shortcut()

    assert window.state is AppState.RECORDING
    assert window._origin_terminal_target is None
    window.close()


def test_send_to_terminal_passes_origin_target_and_clears_on_success(qapp) -> None:
    origin_target = TerminalTarget(
        window_id="123",
        pid="456",
        process_name="alacritty",
    )
    terminal = FakeTerminal()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        terminal=terminal,
    )
    window._origin_terminal_target = origin_target
    window.last_message_editor.setPlainText("git status")
    window.state = AppState.READY

    window.send_to_terminal()

    assert terminal.send_calls == [("git status", origin_target)]
    assert window._origin_terminal_target is None
    assert window.status_label.text() == "Texto colado no terminal ativo, sem pressionar Enter."
    window.close()


def test_send_to_terminal_failure_preserves_target_for_retry(qapp) -> None:
    origin_target = TerminalTarget(
        window_id="123",
        pid="456",
        process_name="alacritty",
    )
    terminal = FakeTerminal(
        fail_error=TerminalBridgeError("Não foi possível colar no terminal.")
    )
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        terminal=terminal,
    )
    window._origin_terminal_target = origin_target
    window.last_message_editor.setPlainText("git commit")
    window.state = AppState.READY

    window.send_to_terminal()

    assert window.status_label.text() == "Não foi possível colar no terminal."
    assert window._origin_terminal_target == origin_target
    window.close()


def test_close_clears_origin_terminal_target(qapp) -> None:
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
    )
    window._origin_terminal_target = TerminalTarget(
        window_id="555",
        pid="666",
        process_name="xfce4-terminal",
    )
    window.close()
    assert window._origin_terminal_target is None
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
    activations: list[AppState] = []
    real_activate = window._activate_recording_shortcut
    window._activate_recording_shortcut = lambda: (activations.append(window.state), real_activate())[1]
    bridge.mouse_activated.emit(bridge.mouse_generation - 1, "x1")
    bridge.mouse_activated.emit(bridge.mouse_generation, "x2")
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    assert window.state is AppState.RECORDING

    window._last_global_activation_time = 0.0
    bridge.keyboard_activated.emit(bridge.keyboard_generation, "ctrl+alt+r")
    assert window.state is AppState.AUDIO_READY
    assert len(activations) == 2

    window.state = AppState.TRANSCRIBING
    window._last_global_activation_time = 0.0
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    assert window.state is AppState.TRANSCRIBING
    assert len(activations) == 3
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


def test_shortcut_reconnect_reissues_active_bindings_without_persistence_or_duplication(
    qapp,
) -> None:
    store = FakeLocalStore()
    store.mouse_button = "x1"
    store.keyboard_shortcut = "ctrl+alt+r"
    bridge = FakeInputShortcutBridge(auto_ack=True)
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        input_shortcut_bridge=bridge,
    )
    assert window._active_mouse_button == "x1"
    assert window._active_keyboard_shortcut == "ctrl+alt+r"
    assert window.shortcut_indicator_label.text() == "Atalhos globais: 2 ativos"

    save_mouse_calls: list[str] = []
    save_keyboard_calls: list[str] = []
    orig_save_mouse = store.save_recording_mouse_button
    orig_save_keyboard = store.save_recording_keyboard_shortcut
    store.save_recording_mouse_button = lambda b: (save_mouse_calls.append(b), orig_save_mouse(b))[1]
    store.save_recording_keyboard_shortcut = lambda k: (save_keyboard_calls.append(k), orig_save_keyboard(k))[1]

    # Service disconnects unexpectedly
    bridge.ready = False
    bridge.ready_changed.emit(False)
    assert window.shortcut_indicator_label.text() == "Atalhos globais: reconectando (2 configurados)"
    assert window._active_mouse_button == "x1"
    assert window._active_keyboard_shortcut == "ctrl+alt+r"

    # Reconnect occurs; auto_ack=False to verify pending state and indicator
    bridge.auto_ack = False
    bridge.ready = True
    bridge.ready_changed.emit(True)

    assert window.shortcut_indicator_label.text() == "Atalhos globais: ativando…"
    assert "mouse" in window._pending_bindings
    assert "keyboard" in window._pending_bindings
    mouse_pending_gen = window._pending_bindings["mouse"][0]
    keyboard_pending_gen = window._pending_bindings["keyboard"][0]

    # First ACK arrives
    bridge.mouse_binding_ready.emit(mouse_pending_gen, "x1")
    assert window._active_mouse_button == "x1"
    assert window.shortcut_indicator_label.text() == "Atalhos globais: ativando…"

    # Second ACK arrives
    bridge.keyboard_binding_ready.emit(keyboard_pending_gen, "ctrl+alt+r")
    assert window._active_keyboard_shortcut == "ctrl+alt+r"
    assert window.shortcut_indicator_label.text() == "Atalhos globais: 2 ativos"

    # Preferences were not saved again
    assert save_mouse_calls == []
    assert save_keyboard_calls == []
    assert store.mouse_button == "x1"
    assert store.keyboard_shortcut == "ctrl+alt+r"
    window.close()


def test_shortcut_service_ready_does_not_duplicate_pending_bind_or_stop(qapp) -> None:
    store = FakeLocalStore()
    store.mouse_button = "x1"
    bridge = FakeInputShortcutBridge(auto_ack=False)
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        input_shortcut_bridge=bridge,
    )
    # Mouse is already pending from startup restore
    # Emit ready=True again
    bridge.ready_changed.emit(True)
    # Mouse should be reissued exactly once (kept as single pending)
    assert len(window._pending_bindings) == 1
    assert "mouse" in window._pending_bindings

    # Now simulate stop pending
    bridge.mouse_binding_ready.emit(window._pending_bindings["mouse"][0], "x1")
    assert window._active_mouse_button == "x1"
    window._deactivate_shortcut("mouse")
    assert "mouse" in window._pending_stops
    stop_gen = window._pending_stops["mouse"]

    # Ready signal arrives while stop is pending -> resumes stop with fresh generation, does NOT reissue watch
    bridge.ready_changed.emit(True)
    assert "mouse" not in window._pending_bindings
    assert "mouse" in window._pending_stops
    fresh_stop_gen = window._pending_stops["mouse"]
    assert fresh_stop_gen > stop_gen
    assert bridge.commands[-1] == ("stop_mouse", fresh_stop_gen, None)
    window.close()


def test_shortcut_disconnect_after_stop_resumes_fresh_stop_and_clears_on_stopped_ack(
    qapp,
) -> None:
    store = FakeLocalStore()
    store.mouse_button = "x1"
    store.keyboard_shortcut = "ctrl+alt+r"
    bridge = FakeInputShortcutBridge(auto_ack=True)
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        local_store=store,
        input_shortcut_bridge=bridge,
    )
    assert window._active_mouse_button == "x1"
    assert window._active_keyboard_shortcut == "ctrl+alt+r"
    assert window.shortcut_indicator_label.text() == "Atalhos globais: 2 ativos"

    # Deactivate mouse; stop command is sent
    bridge.auto_ack = False
    window._deactivate_shortcut("mouse")
    assert "mouse" in window._pending_stops
    stop_gen1 = window._pending_stops["mouse"]
    assert window._active_mouse_button == "x1"
    assert store.mouse_button == "x1"

    # Disconnect occurs before STOPPED arrives
    bridge.ready = False
    bridge.ready_changed.emit(False)
    assert window.shortcut_indicator_label.text() == "Atalhos globais: reconectando (2 configurados)"
    assert window._active_mouse_button == "x1"
    assert window._active_keyboard_shortcut == "ctrl+alt+r"

    # Service reconnects: resumes pending stop with fresh generation, re-watches keyboard, does not watch mouse
    bridge.ready = True
    bridge.ready_changed.emit(True)
    assert "mouse" in window._pending_stops
    stop_gen2 = window._pending_stops["mouse"]
    assert stop_gen2 > stop_gen1
    assert "mouse" not in window._pending_bindings
    assert ("stop_mouse", stop_gen2, None) in bridge.commands
    assert ("watch_mouse", stop_gen2, "x1") not in bridge.commands
    assert "keyboard" in window._pending_bindings
    kb_gen = window._pending_bindings["keyboard"][0]
    assert window._active_mouse_button == "x1"
    assert store.mouse_button == "x1"
    assert window.shortcut_indicator_label.text() == "Atalhos globais: ativando…"

    # Stale STOPPED signal for gen1 is ignored
    bridge.stopped.emit("mouse", stop_gen1)
    assert "mouse" in window._pending_stops
    assert window._active_mouse_button == "x1"
    assert store.mouse_button == "x1"

    # Keyboard ACK arrives
    bridge.keyboard_binding_ready.emit(kb_gen, "ctrl+alt+r")
    assert "keyboard" not in window._pending_bindings
    assert window._active_keyboard_shortcut == "ctrl+alt+r"
    assert window.shortcut_indicator_label.text() == "Atalhos globais: 2 ativos"

    # Matching STOPPED ACK arrives for gen2
    bridge.stopped.emit("mouse", stop_gen2)
    assert "mouse" not in window._pending_stops
    assert window._active_mouse_button is None
    assert store.mouse_button is None
    assert window._active_keyboard_shortcut == "ctrl+alt+r"
    assert window.shortcut_indicator_label.text() == "Atalhos globais: 1 ativo"
    assert window.status_label.text() == "Atalho global desativado."
    window.close()

def test_shortcut_service_ready_schedules_pending_authorization(qapp, monkeypatch) -> None:
    bridge = FakeInputShortcutBridge(ready=False)
    window, _ = make_window(qapp, input_shortcut_bridge=bridge)
    captured: list[str] = []
    monkeypatch.setattr(window, "_capture_shortcut", captured.append)
    window._pending_authorization_kind = "mouse"
    bridge.ready = True
    bridge.ready_changed.emit(True)
    assert window._pending_authorization_kind is None
    qapp.processEvents()
    assert captured == ["mouse"]
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
    assert not hasattr(window, "editor")
    assert not hasattr(window, "send_to_gemini_button")
    assert not window.message_splitter.childrenCollapsible()
    assert not window.main_splitter.childrenCollapsible()
    assert not window.diagnostic_splitter.childrenCollapsible()
    assert window.settings_button.accessibleName() == "Configurações"
    assert window.fullscreen_button.accessibleName() == "Entrar em tela cheia"
    assert not window.grab().isNull()
    window.resize(760, 560)
    qapp.processEvents()
    assert window.transcription_editor.height() > 0
    assert window.last_message_editor.height() > 0
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
            window.spellcheck_checkbox.parent(),
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
        "Corretor ortográfico",
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


def test_global_shortcut_raises_minimized_window_on_stop_and_on_start_error(qapp) -> None:
    bridge = FakeInputShortcutBridge()
    recorder = FakeRecorder()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        input_shortcut_bridge=bridge,
    )
    window._activate_shortcut("mouse", "x1", persist=False)
    order: list[str] = []
    real_raise = window._raise_to_front
    window._raise_to_front = lambda: (order.append("raise"), real_raise())[1]

    # Global start does NOT raise window
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    assert window.state is AppState.RECORDING
    assert order == []

    # Reset debounce timer
    window._last_global_activation_time = 0.0

    # Global stop DOES raise window (brings up review)
    window.showMinimized()
    assert window.isMinimized() is True
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    assert window.state is AppState.AUDIO_READY
    assert order == ["raise"]
    assert window.isMinimized() is False
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
        "Corretor ortográfico",
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


def test_review_button_states_and_enablement(qapp) -> None:
    transcriber = FakeTranscriber()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="valid-key"),
        transcriber=transcriber,
    )
    # Editor vazio -> desabilitado
    window.last_message_editor.setPlainText("")
    assert window.review_button.isEnabled() is False

    # Com texto, chave e transcritor -> habilitado
    window.last_message_editor.setPlainText("Texto a revisar")
    assert window.review_button.isEnabled() is True

    # Sem chave API -> desabilitado
    window.settings = Settings(api_key=None)
    window._update_actions()
    assert window.review_button.isEnabled() is False

    # Sem transcritor -> desabilitado
    window.settings = Settings(api_key="valid-key")
    window.transcriber = None
    window._update_actions()
    assert window.review_button.isEnabled() is False

    # Durante gravação -> desabilitado
    window.transcriber = transcriber
    window.state = AppState.RECORDING
    window._update_actions()
    assert window.review_button.isEnabled() is False

    # Durante transcrição -> desabilitado
    window.state = AppState.TRANSCRIBING
    window._update_actions()
    assert window.review_button.isEnabled() is False

    # Durante revisão -> desabilitado
    window.state = AppState.IDLE
    window._is_reviewing = True
    window._update_actions()
    assert window.review_button.isEnabled() is False

    window._is_reviewing = False
    window._update_actions()
    assert window.review_button.isEnabled() is True
    window.close()


def test_review_button_success_flow(qapp) -> None:
    transcriber = FakeTranscriber(
        proofread_text="Texto corrigido e revisado com sucesso.",
        usage=TokenUsage(input_tokens=15, output_tokens=12, total_tokens=27),
    )
    store = FakeLocalStore()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="valid-key"),
        transcriber=transcriber,
        local_store=store,
    )
    window.last_message_editor.setPlainText("Texto com eror")
    assert window.review_button.isEnabled() is True

    window.review_button.click()
    qapp.processEvents()
    wait_for_proofreading_worker(qapp, window)

    assert window.last_message_editor.toPlainText() == "Texto corrigido e revisado com sucesso."
    assert not window.last_message_editor.textCursor().hasSelection()
    assert QApplication.clipboard().text() == "Texto corrigido e revisado com sucesso."
    assert window.status_label.text() == "Texto revisado e copiado."
    assert window._is_reviewing is False
    assert window.review_button.isEnabled() is True

    # Tokens registrados
    assert len(store.records) == 1
    assert store.records[0][2] == "success"

    # Debug renderizado com bytes de texto e sem MIME de áudio
    expected_bytes = len("Texto com eror".encode("utf-8"))
    payload_debug_text = window.payload_debug.toPlainText()
    assert f"Texto: {expected_bytes} bytes" in payload_debug_text
    assert "MIME:" not in payload_debug_text
    assert "Áudio:" not in payload_debug_text
    window.close()


def test_review_button_error_flow(qapp) -> None:
    transcriber = FakeTranscriber(
        proofread_error="Falha de conexão com a API do Gemini.",
        usage=TokenUsage(input_tokens=10, output_tokens=0, total_tokens=10),
    )
    store = FakeLocalStore()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="valid-key"),
        transcriber=transcriber,
        local_store=store,
    )
    window.last_message_editor.setPlainText("Texto original que deve ser mantido")
    assert window.review_button.isEnabled() is True

    window.review_button.click()
    qapp.processEvents()
    wait_for_proofreading_worker(qapp, window)

    assert window.last_message_editor.toPlainText() == "Texto original que deve ser mantido"
    assert window.status_label.text() == "Falha de conexão com a API do Gemini."
    assert window._is_reviewing is False
    assert window.review_button.isEnabled() is True

    # Registro de erro no store
    assert len(store.records) == 1
    assert store.records[0][2] == "error"
    window.close()


def test_spellcheck_settings_toggle_and_persistence(qapp) -> None:
    store = FakeLocalStore()
    checker = FakeSpellChecker(available=True)
    window, _ = make_window(
        qapp,
        local_store=store,
        spell_checker=checker,
    )
    assert window.highlighter.enabled is True

    def inspect_and_toggle() -> None:
        dialog = window._settings_dialog
        assert dialog is not None
        assert "Instalado" in window.spellcheck_status_label.text()
        assert window.spellcheck_checkbox.isChecked() is True
        assert window.spellcheck_checkbox.isEnabled() is True

        # Desativa o sublinhado
        window.spellcheck_checkbox.setChecked(False)
        assert window.highlighter.enabled is False
        assert store.get_spellcheck_enabled() is False

        # Reativa o sublinhado
        window.spellcheck_checkbox.setChecked(True)
        assert window.highlighter.enabled is True
        assert store.get_spellcheck_enabled() is True
        dialog.reject()

    QTimer.singleShot(0, inspect_and_toggle)
    window._open_settings_dialog()
    window.close()

    # Cenário com corretor indisponível
    checker_unavailable = FakeSpellChecker(available=False)
    window2, _ = make_window(
        qapp,
        local_store=store,
        spell_checker=checker_unavailable,
    )
    assert window2.highlighter.enabled is False

    def inspect_unavailable() -> None:
        dialog = window2._settings_dialog
        assert dialog is not None
        assert "Não instalado" in window2.spellcheck_status_label.text()
        assert window2.spellcheck_checkbox.isEnabled() is False
        dialog.reject()

    QTimer.singleShot(0, inspect_unavailable)
    window2._open_settings_dialog()
    window2.close()


def test_editor_context_menu_suggestions_and_ignore(qapp, monkeypatch) -> None:
    store = FakeLocalStore()
    checker = FakeSpellChecker(
        available=True,
        valid_words=("palavra", "aqui"),
        suggestions={"errado": ["correto", "acertado"]},
    )
    window, _ = make_window(
        qapp,
        local_store=store,
        spell_checker=checker,
    )
    window.last_message_editor.setPlainText("palavra errado aqui")

    # Posiciona o cursor na palavra "errado"
    cursor = window.last_message_editor.textCursor()
    cursor.setPosition(10)  # dentro de 'errado'
    window.last_message_editor.setTextCursor(cursor)
    pos = window.last_message_editor.cursorRect(cursor).center()

    captured_menus: list[QMenu] = []
    orig_create = window.last_message_editor.createStandardContextMenu

    def intercepted_create(*args, **kwargs):
        m = orig_create(*args, **kwargs)
        if m is None:
            m = QMenu(window.last_message_editor)
        m.exec = lambda *a, **kw: captured_menus.append(m)
        m.exec_ = lambda *a, **kw: captured_menus.append(m)
        return m

    monkeypatch.setattr(window.last_message_editor, "createStandardContextMenu", intercepted_create)
    window._show_editor_context_menu(pos)
    assert len(captured_menus) == 1
    menu = captured_menus[0]

    action_texts = [a.text() for a in menu.actions()]
    assert "correto" in action_texts
    assert "acertado" in action_texts
    assert 'Ignorar "errado"' in action_texts

    # Clica na sugestão 'correto'
    suggestion_action = next(a for a in menu.actions() if a.text() == "correto")
    suggestion_action.trigger()
    assert "palavra correto aqui" in window.last_message_editor.toPlainText()

    # Testa 'Ignorar'
    window.last_message_editor.setPlainText("palavra errado aqui")
    cursor.setPosition(10)
    window.last_message_editor.setTextCursor(cursor)
    pos = window.last_message_editor.cursorRect(cursor).center()

    captured_menus.clear()
    window._show_editor_context_menu(pos)
    menu2 = captured_menus[0]
    ignore_action = next(a for a in menu2.actions() if a.text() == 'Ignorar "errado"')
    ignore_action.trigger()

    assert checker.is_ignored("errado") is True
    assert "errado" in store.get_spellcheck_ignored_words()
    window.close()


def test_init_with_store_raising_on_spellcheck_preferences_is_fail_soft(qapp) -> None:
    class FailingSpellcheckStore(FakeLocalStore):
        def get_spellcheck_ignored_words(self) -> list[str]:
            raise LocalStoreError("falha ao ler palavras ignoradas")

        def get_spellcheck_enabled(self) -> bool:
            raise LocalStoreError("falha ao ler estado do corretor")

    store = FailingSpellcheckStore()
    window, _ = make_window(qapp, local_store=store)
    assert window.spell_checker is not None
    assert window.highlighter is not None
    if window.spell_checker.is_available():
        assert window.highlighter.enabled is True
    window.close()


def test_recording_shortcuts_ignored_during_review(qapp) -> None:
    transcriber = FakeTranscriber()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="valid-key"),
        transcriber=transcriber,
    )
    window._is_reviewing = True
    # 1. Ativação via atalho global
    window._activate_recording_shortcut()
    assert window.state is AppState.IDLE
    assert window.recorder.recording is False

    # 2. Ativação via _perform_primary_action (botão ou espaço)
    window._perform_primary_action()
    assert window.state is AppState.IDLE
    assert window.recorder.recording is False

    # 3. Início direto de gravação
    window._start_recording()
    assert window.state is AppState.IDLE
    assert window.recorder.recording is False
    window.close()


def test_editor_is_read_only_during_review_and_restored(qapp) -> None:
    transcriber = FakeTranscriber(
        proofread_text="Texto revisado com IA.",
        usage=TokenUsage(input_tokens=10, output_tokens=8, total_tokens=18),
    )
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="valid-key"),
        transcriber=transcriber,
    )
    window.last_message_editor.setPlainText("Texto original para teste.")
    assert window.last_message_editor.isReadOnly() is False

    # Inicia a revisão com sucesso
    window.review_button.click()
    assert window._is_reviewing is True
    assert window.last_message_editor.isReadOnly() is True

    # Aguarda a conclusão da thread e restauração
    wait_for_proofreading_worker(qapp, window)
    assert window._is_reviewing is False
    assert window.last_message_editor.isReadOnly() is False
    assert window.last_message_editor.toPlainText() == "Texto revisado com IA."

    # Testa com erro na revisão
    window.transcriber = FakeTranscriber(
        proofread_error="Falha simulada na API.",
    )
    window.review_button.click()
    assert window._is_reviewing is True
    assert window.last_message_editor.isReadOnly() is True

    wait_for_proofreading_worker(qapp, window)
    assert window._is_reviewing is False
    assert window.last_message_editor.isReadOnly() is False
    assert window.last_message_editor.toPlainText() == "Texto revisado com IA."
    window.close()


def test_editor_context_menu_with_hyphenated_compound_word(qapp, monkeypatch) -> None:
    checker = FakeSpellChecker(
        available=True,
        valid_words=("comprar", "hoje"),
        suggestions={"guarda-chuva": ["sombrinha", "capa-de-chuva"]},
    )
    window, _ = make_window(
        qapp,
        spell_checker=checker,
    )
    window.last_message_editor.setPlainText("comprar guarda-chuva hoje")

    # Posiciona o cursor no meio do composto hifenizado (offset 12)
    cursor = window.last_message_editor.textCursor()
    cursor.setPosition(12)
    window.last_message_editor.setTextCursor(cursor)
    pos = window.last_message_editor.cursorRect(cursor).center()

    captured_menus: list[QMenu] = []
    orig_create = window.last_message_editor.createStandardContextMenu

    def intercepted_create(*args, **kwargs):
        m = orig_create(*args, **kwargs)
        if m is None:
            m = QMenu(window.last_message_editor)
        m.exec = lambda *a, **kw: captured_menus.append(m)
        m.exec_ = lambda *a, **kw: captured_menus.append(m)
        return m

    monkeypatch.setattr(window.last_message_editor, "createStandardContextMenu", intercepted_create)
    window._show_editor_context_menu(pos)

    assert len(captured_menus) == 1
    menu = captured_menus[0]
    action_texts = [a.text() for a in menu.actions()]
    assert "sombrinha" in action_texts
    assert "capa-de-chuva" in action_texts
    assert 'Ignorar "guarda-chuva"' in action_texts

    # Substituição deve trocar o composto inteiro
    suggestion_action = next(a for a in menu.actions() if a.text() == "sombrinha")
    suggestion_action.trigger()
    assert window.last_message_editor.toPlainText() == "comprar sombrinha hoje"
    window.close()


def test_editor_context_menu_with_emoji_prefix_replaces_accurately(
    qapp, monkeypatch
) -> None:
    checker = FakeSpellChecker(
        available=True,
        valid_words=("palavra", "aqui", "final", "ok"),
        suggestions={"errrrooo": ["correto"]},
    )
    window, _ = make_window(
        qapp,
        spell_checker=checker,
    )

    captured_menus: list[QMenu] = []
    orig_create = window.last_message_editor.createStandardContextMenu

    def intercepted_create(*args, **kwargs):
        m = orig_create(*args, **kwargs)
        if m is None:
            m = QMenu(window.last_message_editor)
        m.exec = lambda *a, **kw: captured_menus.append(m)
        m.exec_ = lambda *a, **kw: captured_menus.append(m)
        return m

    monkeypatch.setattr(window.last_message_editor, "createStandardContextMenu", intercepted_create)

    # Texto com emoji não-BMP antes da palavra incorreta:
    # Em UTF-16: '😀' ocupa índices 0 e 1, ' ' ocupa 2, e 'errrrooo' ocupa 3..10 (tamanho 8).
    # Em Python: '😀' len 1, ' ' len 1, 'errrrooo' índices 2..10.
    # O clique em qualquer posição da palavra (início=3, meio=6, fim=10) deve selecionar
    # exatamente 'errrrooo' e substituir sem resíduos.
    for test_pos in (3, 6, 10):
        window.last_message_editor.setPlainText("😀 errrrooo final")
        cursor = window.last_message_editor.textCursor()
        cursor.setPosition(test_pos)
        window.last_message_editor.setTextCursor(cursor)
        pos = window.last_message_editor.cursorRect(cursor).center()

        captured_menus.clear()
        window._show_editor_context_menu(pos)

        assert len(captured_menus) == 1
        menu = captured_menus[0]
        action_texts = [a.text() for a in menu.actions()]
        assert "correto" in action_texts

        suggestion_action = next(a for a in menu.actions() if a.text() == "correto")
        suggestion_action.trigger()

        # Substituição exata: sem comer espaço antes e sem sobrar letras depois
        assert window.last_message_editor.toPlainText() == "😀 correto final"

    # Teste adicional com múltiplos emojis e caracteres não-BMP:
    # '🚀🎉' (4 unidades UTF-16) + ' ' (1 unidade) -> 'errrrooo' começa na unidade 5
    for test_pos in (5, 8, 12):
        window.last_message_editor.setPlainText("🚀🎉 errrrooo ok")
        cursor = window.last_message_editor.textCursor()
        cursor.setPosition(test_pos)
        window.last_message_editor.setTextCursor(cursor)
        pos = window.last_message_editor.cursorRect(cursor).center()

        captured_menus.clear()
        window._show_editor_context_menu(pos)

        assert len(captured_menus) == 1
        menu = captured_menus[0]
        suggestion_action = next(a for a in menu.actions() if a.text() == "correto")
        suggestion_action.trigger()

        assert window.last_message_editor.toPlainText() == "🚀🎉 correto ok"

    window.close()

def test_editor_context_menu_skips_spellcheck_actions_during_review_and_readonly(
    qapp, monkeypatch
) -> None:
    checker = FakeSpellChecker(
        available=True,
        valid_words=("palavra", "aqui"),
        suggestions={"errado": ["correto"]},
    )
    window, _ = make_window(
        qapp,
        spell_checker=checker,
    )
    window.last_message_editor.setPlainText("palavra errado aqui")

    cursor = window.last_message_editor.textCursor()
    cursor.setPosition(10)
    window.last_message_editor.setTextCursor(cursor)
    pos = window.last_message_editor.cursorRect(cursor).center()

    captured_menus: list[QMenu] = []
    orig_create = window.last_message_editor.createStandardContextMenu

    def intercepted_create(*args, **kwargs):
        m = orig_create(*args, **kwargs)
        if m is None:
            m = QMenu(window.last_message_editor)
        m.exec = lambda *a, **kw: captured_menus.append(m)
        m.exec_ = lambda *a, **kw: captured_menus.append(m)
        return m

    monkeypatch.setattr(window.last_message_editor, "createStandardContextMenu", intercepted_create)

    # 1. Durante _is_reviewing: sem sugestões nem ação de ignorar
    window._is_reviewing = True
    window._show_editor_context_menu(pos)
    assert len(captured_menus) == 1
    actions_during_review = [a.text() for a in captured_menus[0].actions()]
    assert "correto" not in actions_during_review
    assert 'Ignorar "errado"' not in actions_during_review

    # 2. Quando editor isReadOnly(): sem sugestões nem ação de ignorar
    window._is_reviewing = False
    window.last_message_editor.setReadOnly(True)
    captured_menus.clear()
    window._show_editor_context_menu(pos)
    assert len(captured_menus) == 1
    actions_during_readonly = [a.text() for a in captured_menus[0].actions()]
    assert "correto" not in actions_during_readonly
    assert 'Ignorar "errado"' not in actions_during_readonly

    # 3. Em estado normal editável: sugestões e ignorar presentes
    window.last_message_editor.setReadOnly(False)
    captured_menus.clear()
    window._show_editor_context_menu(pos)
    assert len(captured_menus) == 1
    actions_normal = [a.text() for a in captured_menus[0].actions()]
    assert "correto" in actions_normal
    assert 'Ignorar "errado"' in actions_normal

    window.close()


def test_editor_context_menu_at_token_end_boundary_has_no_spellcheck_actions(
    qapp, monkeypatch
) -> None:
    checker = FakeSpellChecker(
        available=True,
        valid_words=("palavra", "aqui"),
        suggestions={"errado": ["correto"]},
    )
    window, _ = make_window(
        qapp,
        spell_checker=checker,
    )
    window.last_message_editor.setPlainText("palavra errado, aqui")

    captured_menus: list[QMenu] = []
    orig_create = window.last_message_editor.createStandardContextMenu

    def intercepted_create(*args, **kwargs):
        m = orig_create(*args, **kwargs)
        if m is None:
            m = QMenu(window.last_message_editor)
        m.exec = lambda *a, **kw: captured_menus.append(m)
        m.exec_ = lambda *a, **kw: captured_menus.append(m)
        return m

    monkeypatch.setattr(window.last_message_editor, "createStandardContextMenu", intercepted_create)

    # Token "errado" vai do índice 8 ao 14.
    # 1. Posição 13 (dentro de "errado"): deve conter sugestões e opção "Ignorar"
    cursor = window.last_message_editor.textCursor()
    cursor.setPosition(13)
    window.last_message_editor.setTextCursor(cursor)
    pos_inside = window.last_message_editor.cursorRect(cursor).center()
    window._show_editor_context_menu(pos_inside)
    assert len(captured_menus) == 1
    actions_inside = [a.text() for a in captured_menus[0].actions()]
    assert "correto" in actions_inside
    assert 'Ignorar "errado"' in actions_inside

    # 2. Posição 14 (exatamente em token_end, sobre a vírgula ','): não deve conter ações de spellcheck
    captured_menus.clear()
    cursor.setPosition(14)
    window.last_message_editor.setTextCursor(cursor)
    pos_at_comma = window.last_message_editor.cursorRect(cursor).center()
    window._show_editor_context_menu(pos_at_comma)
    assert len(captured_menus) == 1
    actions_at_comma = [a.text() for a in captured_menus[0].actions()]
    assert "correto" not in actions_at_comma
    assert 'Ignorar "errado"' not in actions_at_comma

    # 3. Posição 14 em texto com espaço logo após token_end ("palavra errado aqui"): não deve conter ações
    window.last_message_editor.setPlainText("palavra errado aqui")
    captured_menus.clear()
    cursor.setPosition(14)  # sobre o caractere de espaço após "errado"
    window.last_message_editor.setTextCursor(cursor)
    pos_at_space = window.last_message_editor.cursorRect(cursor).center()
    window._show_editor_context_menu(pos_at_space)
    assert len(captured_menus) == 1
    actions_at_space = [a.text() for a in captured_menus[0].actions()]
    assert "correto" not in actions_at_space
    assert 'Ignorar "errado"' not in actions_at_space

    window.close()


def test_editor_context_menu_does_not_leak_qmenu_on_repeated_invocations(
    qapp, monkeypatch
) -> None:
    store = FakeLocalStore()
    checker = FakeSpellChecker(
        available=True,
        valid_words=("palavra", "aqui"),
        suggestions={"errado": ["correto"]},
    )
    window, _ = make_window(
        qapp,
        local_store=store,
        spell_checker=checker,
    )
    window.last_message_editor.setPlainText("palavra errado aqui")

    orig_create = window.last_message_editor.createStandardContextMenu

    def non_blocking_create(*args, **kwargs):
        m = orig_create(*args, **kwargs)
        if m is not None:
            m.exec = lambda *a, **kw: None
            m.exec_ = lambda *a, **kw: None
        return m

    monkeypatch.setattr(window.last_message_editor, "createStandardContextMenu", non_blocking_create)

    cursor = window.last_message_editor.textCursor()
    cursor.setPosition(10)
    window.last_message_editor.setTextCursor(cursor)
    pos = window.last_message_editor.cursorRect(cursor).center()

    # Abre o menu repetidas vezes
    for _ in range(5):
        window._show_editor_context_menu(pos)

    # Processa os eventos DeferredDelete postados por deleteLater()
    for child in window.last_message_editor.findChildren(QMenu):
        QApplication.sendPostedEvents(child, QEvent.Type.DeferredDelete)

    # Nenhum QMenu deve permanecer retido como filho de self.last_message_editor
    assert len(window.last_message_editor.findChildren(QMenu)) == 0
    window.close()


def test_spell_popup_visible_on_cursor_in_misspelled_word_and_hidden_on_valid_or_space(
    qapp,
) -> None:
    checker = FakeSpellChecker(
        available=True,
        valid_words=("palavra", "aqui"),
        suggestions={"errado": ["correto", "erado"]},
    )
    window, _ = make_window(qapp, spell_checker=checker)
    window.last_message_editor.setPlainText("palavra errado aqui")
    qapp.processEvents()

    assert window._spell_popup.isVisible() is False

    # 1. Cursor posicionado dentro de "errado" (posição 10)
    cursor = window.last_message_editor.textCursor()
    cursor.setPosition(10)
    window.last_message_editor.setTextCursor(cursor)
    qapp.processEvents()

    assert window._spell_popup.isVisible() is True
    assert len(window._spell_popup.suggestion_buttons) == 2
    assert window._spell_popup.suggestion_buttons[0].text() == "correto"
    assert window._spell_popup.suggestion_buttons[1].text() == "erado"
    assert window._spell_popup.ignore_button is not None

    # 2. Cursor movido para palavra válida "palavra" (posição 2)
    cursor.setPosition(2)
    window.last_message_editor.setTextCursor(cursor)
    qapp.processEvents()
    assert window._spell_popup.isVisible() is False

    # 3. Cursor movido de volta para "errado" (posição 11)
    cursor.setPosition(11)
    window.last_message_editor.setTextCursor(cursor)
    qapp.processEvents()
    assert window._spell_popup.isVisible() is True

    # 4. Cursor movido para o espaço após "errado" (posição 14)
    cursor.setPosition(14)
    window.last_message_editor.setTextCursor(cursor)
    qapp.processEvents()
    assert window._spell_popup.isVisible() is False

    window.close()


def test_spell_popup_click_suggestion_replaces_word(qapp) -> None:
    checker = FakeSpellChecker(
        available=True,
        valid_words=("palavra", "aqui"),
        suggestions={"errado": ["correto"]},
    )
    window, _ = make_window(qapp, spell_checker=checker)
    window.last_message_editor.setPlainText("palavra errado aqui")

    cursor = window.last_message_editor.textCursor()
    cursor.setPosition(10)
    window.last_message_editor.setTextCursor(cursor)
    qapp.processEvents()

    assert window._spell_popup.isVisible() is True
    assert len(window._spell_popup.suggestion_buttons) == 1

    chip = window._spell_popup.suggestion_buttons[0]
    # Transição do ponteiro e clique real via QTest
    QTest.mouseMove(window._spell_popup, chip.rect().center())
    QTest.mouseClick(chip, Qt.MouseButton.LeftButton)
    qapp.processEvents()

    assert window.last_message_editor.toPlainText() == "palavra correto aqui"
    assert window._spell_popup.isVisible() is False
    assert window.last_message_editor.hasFocus() is True

    # Testa histórico de desfazer (Undo)
    window.last_message_editor.undo()
    assert window.last_message_editor.toPlainText() == "palavra errado aqui"

    window.close()


def test_spell_popup_click_ignore_adds_to_ignored_and_rehighlights(qapp) -> None:
    store = FakeLocalStore()
    checker = FakeSpellChecker(
        available=True,
        valid_words=("palavra", "aqui"),
        suggestions={"errado": ["correto"]},
    )
    window, _ = make_window(qapp, local_store=store, spell_checker=checker)
    window.last_message_editor.setPlainText("palavra errado aqui")

    cursor = window.last_message_editor.textCursor()
    cursor.setPosition(10)
    window.last_message_editor.setTextCursor(cursor)
    qapp.processEvents()

    assert window._spell_popup.isVisible() is True
    assert window._spell_popup.ignore_button is not None

    ignore_btn = window._spell_popup.ignore_button
    # Transição do ponteiro e clique real via QTest
    QTest.mouseMove(window._spell_popup, ignore_btn.rect().center())
    QTest.mouseClick(ignore_btn, Qt.MouseButton.LeftButton)
    qapp.processEvents()

    assert window._spell_popup.isVisible() is False
    assert window.last_message_editor.hasFocus() is True
    assert checker.is_ignored("errado") is True
    assert "errado" in store.get_spellcheck_ignored_words()

    # Ao mover o cursor novamente para a palavra ignorada, o popup não abre
    cursor.setPosition(10)
    window.last_message_editor.setTextCursor(cursor)
    qapp.processEvents()
    assert window._spell_popup.isVisible() is False

    window.close()


def test_spell_popup_hover_trigger_after_timer(qapp) -> None:
    checker = FakeSpellChecker(
        available=True,
        valid_words=("palavra", "aqui"),
        suggestions={"errado": ["correto"]},
    )
    window, _ = make_window(qapp, spell_checker=checker)
    window.last_message_editor.setPlainText("palavra errado aqui")

    cursor = window.last_message_editor.textCursor()
    cursor.setPosition(10)
    pos_errado = window.last_message_editor.cursorRect(cursor).center()

    # Simula hover do mouse sobre "errado"
    hover_event = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(pos_errado),
        QPointF(pos_errado),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.eventFilter(window.last_message_editor.viewport(), hover_event)

    assert window._hover_spell_timer.isActive() is True
    assert window._last_hover_pos == pos_errado

    # Dispara o temporizador de hover
    window._hover_spell_timer.timeout.emit()
    qapp.processEvents()

    assert window._spell_popup.isVisible() is True
    assert len(window._spell_popup.suggestion_buttons) == 1
    assert window._spell_popup.suggestion_buttons[0].text() == "correto"

    window.close()


def test_spell_popup_suppressed_during_review_and_readonly(qapp) -> None:
    checker = FakeSpellChecker(
        available=True,
        valid_words=("palavra", "aqui"),
        suggestions={"errado": ["correto"]},
    )
    window, _ = make_window(qapp, spell_checker=checker)
    window.last_message_editor.setPlainText("palavra errado aqui")

    # 1. Durante revisão com IA
    window._is_reviewing = True
    cursor = window.last_message_editor.textCursor()
    cursor.setPosition(10)
    window.last_message_editor.setTextCursor(cursor)
    qapp.processEvents()
    assert window._spell_popup.isVisible() is False

    # 2. Modo somente leitura
    window._is_reviewing = False
    window.last_message_editor.setReadOnly(True)
    cursor.setPosition(10)
    window.last_message_editor.setTextCursor(cursor)
    qapp.processEvents()
    assert window._spell_popup.isVisible() is False

    # 3. Corretor desabilitado no highlighter
    window.last_message_editor.setReadOnly(False)
    window.highlighter.enabled = False
    cursor.setPosition(10)
    window.last_message_editor.setTextCursor(cursor)
    qapp.processEvents()
    assert window._spell_popup.isVisible() is False

    window.close()


def test_spell_popup_automatic_dismissal_events(qapp) -> None:
    checker = FakeSpellChecker(
        available=True,
        valid_words=("palavra", "aqui"),
        suggestions={"errado": ["correto"]},
    )
    window, _ = make_window(qapp, spell_checker=checker)
    window.last_message_editor.setPlainText("palavra errado aqui")

    # Abre o balão
    cursor = window.last_message_editor.textCursor()
    cursor.setPosition(10)
    window.last_message_editor.setTextCursor(cursor)
    qapp.processEvents()
    assert window._spell_popup.isVisible() is True

    # 1. Tecla Escape fecha o popup e consome o evento
    key_event = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier
    )
    consumed = window.eventFilter(window.last_message_editor, key_event)
    assert consumed is True
    assert window._spell_popup.isVisible() is False

    # Abre novamente
    cursor.setPosition(11)
    window.last_message_editor.setTextCursor(cursor)
    qapp.processEvents()
    assert window._spell_popup.isVisible() is True

    # 2. Evento Leave no viewport inicia timer de tolerância e timeout fecha o popup
    leave_event = QEvent(QEvent.Type.Leave)
    window.eventFilter(window.last_message_editor.viewport(), leave_event)
    assert window._popup_dismiss_timer.isActive() is True
    assert window._spell_popup.isVisible() is True
    QCursor.setPos(QPoint(0, 0))
    window._is_mouse_over_popup = False
    window._on_popup_dismiss_timer_timeout()
    assert window._spell_popup.isVisible() is False
    # Abre novamente
    cursor.setPosition(10)
    window.last_message_editor.setTextCursor(cursor)
    qapp.processEvents()
    assert window._spell_popup.isVisible() is True

    # 3. Evento Wheel fecha o popup
    wheel_event = QEvent(QEvent.Type.Wheel)
    window.eventFilter(window.last_message_editor.viewport(), wheel_event)
    assert window._spell_popup.isVisible() is False

    # Abre novamente
    cursor.setPosition(11)
    window.last_message_editor.setTextCursor(cursor)
    qapp.processEvents()
    assert window._spell_popup.isVisible() is True

    # 4. Redimensionamento fecha o popup
    window.resize(window.width() + 20, window.height() + 20)
    qapp.processEvents()
    assert window._spell_popup.isVisible() is False

    # Abre novamente
    cursor.setPosition(10)
    window.last_message_editor.setTextCursor(cursor)
    qapp.processEvents()
    assert window._spell_popup.isVisible() is True
    # 5. Movimentação da janela fecha o popup
    window.move(window.x() + 30, window.y() + 30)
    qapp.processEvents()
    assert window._spell_popup.isVisible() is False
    window.close()


def test_spell_popup_no_suggestions_displays_label(qapp) -> None:
    checker = FakeSpellChecker(
        available=True,
        valid_words=(),
        suggestions={"xyz": []},
    )
    window, _ = make_window(qapp, spell_checker=checker)
    window.last_message_editor.setPlainText("xyz")

    cursor = window.last_message_editor.textCursor()
    cursor.setPosition(1)
    window.last_message_editor.setTextCursor(cursor)
    qapp.processEvents()

    assert window._spell_popup.isVisible() is True
    assert len(window._spell_popup.suggestion_buttons) == 0
    assert window._spell_popup.no_suggestions_label is not None
    assert window._spell_popup.no_suggestions_label.text() == "Sem sugestões"
    assert window._spell_popup.ignore_button is not None

    window.close()


def test_spell_popup_cleaned_up_on_close(qapp) -> None:
    checker = FakeSpellChecker(
        available=True,
        valid_words=("palavra",),
        suggestions={"errado": ["correto"]},
    )
    window, _ = make_window(qapp, spell_checker=checker)
    window.last_message_editor.setPlainText("errado")
    cursor = window.last_message_editor.textCursor()
    cursor.setPosition(2)
    window.last_message_editor.setTextCursor(cursor)
    qapp.processEvents()
    assert window._spell_popup is not None
    assert window._spell_popup.isVisible() is True

    window.close()
    assert window._spell_popup is None


def test_spell_popup_pointer_transition_and_hover_tolerance(qapp) -> None:
    checker = FakeSpellChecker(
        available=True,
        valid_words=("palavra", "aqui"),
        suggestions={"errado": ["correto"]},
    )
    window, _ = make_window(qapp, spell_checker=checker)
    window.last_message_editor.setPlainText("palavra errado aqui")

    # Posiciona no token com erro para abrir popup
    cursor = window.last_message_editor.textCursor()
    cursor.setPosition(10)
    window.last_message_editor.setTextCursor(cursor)
    qapp.processEvents()
    assert window._spell_popup.isVisible() is True

    # 1. Ponteiro sai do viewport em direção ao popup: timer de 200ms inicia, popup permanece visível
    leave_vp = QEvent(QEvent.Type.Leave)
    window.eventFilter(window.last_message_editor.viewport(), leave_vp)
    assert window._popup_dismiss_timer.isActive() is True
    assert window._spell_popup.isVisible() is True

    # 2. Ponteiro entra no popup antes do timeout: cancela timer de fechamento e registra hover
    enter_popup = QEvent(QEvent.Type.Enter)
    window.eventFilter(window._spell_popup, enter_popup)
    assert window._is_mouse_over_popup is True
    assert window._popup_dismiss_timer.isActive() is False
    assert window._spell_popup.isVisible() is True

    # 3. Movimento sobre o popup mantém o estado de mouse sobre o popup
    move_popup = QEvent(QEvent.Type.MouseMove)
    window.eventFilter(window._spell_popup, move_popup)
    assert window._is_mouse_over_popup is True

    # 4. Timeout do dismiss timer não fecha o popup se mouse estiver sobre ele
    window._on_popup_dismiss_timer_timeout()
    assert window._spell_popup.isVisible() is True

    # 5. Ponteiro sai do popup para fora da área do token: popup fecha
    QCursor.setPos(QPoint(0, 0))
    leave_popup = QEvent(QEvent.Type.Leave)
    window.eventFilter(window._spell_popup, leave_popup)
    assert window._is_mouse_over_popup is False
    assert window._spell_popup.isVisible() is False
    # 6. Se o dismiss timer disparar quando o mouse não estiver no popup, popup fecha
    cursor.setPosition(11)
    window.last_message_editor.setTextCursor(cursor)
    qapp.processEvents()
    assert window._spell_popup.isVisible() is True
    window.eventFilter(window.last_message_editor.viewport(), leave_vp)
    assert window._popup_dismiss_timer.isActive() is True
    window._is_mouse_over_popup = False
    QCursor.setPos(QPoint(0, 0))
    window._on_popup_dismiss_timer_timeout()
    assert window._spell_popup.isVisible() is False

    window.close()


def test_spell_popup_screen_edge_clamping_and_above_positioning(qapp) -> None:
    checker = FakeSpellChecker(
        available=True,
        valid_words=(),
        suggestions={"errado": ["correto", "sugestao2"]},
    )
    window, _ = make_window(qapp, spell_checker=checker)
    popup = window._spell_popup

    screen = QGuiApplication.primaryScreen()
    screen_geom = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)

    # 1. Posição na borda inferior da tela: popup deve ser renderizado ACIMA da palavra
    # e dentro dos limites verticais da tela disponível
    bottom_y = screen_geom.bottom() - 10
    target_rect_bottom = QRect(200, bottom_y, 80, 20)
    popup.show_suggestions("errado", ["correto", "sugestao2"], target_rect_bottom)
    qapp.processEvents()

    assert popup.isVisible() is True
    assert popup.y() < target_rect_bottom.top()
    assert popup.y() >= screen_geom.top() + 4
    assert popup.geometry().bottom() <= screen_geom.bottom() - 4

    # 2. Posição na borda direita da tela: popup deve ser limitado horizontalmente (clamping)
    right_x = screen_geom.right() - 20
    target_rect_right = QRect(right_x, 300, 80, 20)
    popup.show_suggestions("errado", ["correto", "sugestao2"], target_rect_right)
    qapp.processEvents()

    assert popup.isVisible() is True
    assert popup.geometry().right() <= screen_geom.right() - 4
    assert popup.x() >= screen_geom.left() + 4

    # 3. Posição no canto inferior direito extremo
    target_rect_corner = QRect(screen_geom.right() - 10, screen_geom.bottom() - 10, 60, 20)
    popup.show_suggestions("errado", ["correto"], target_rect_corner)
    qapp.processEvents()

    assert popup.isVisible() is True
    assert popup.y() < target_rect_corner.top()
    assert popup.geometry().right() <= screen_geom.right() - 4
    assert popup.geometry().bottom() <= screen_geom.bottom() - 4
    assert popup.x() >= screen_geom.left() + 4
    assert popup.y() >= screen_geom.top() + 4

    window.close()


def test_spell_popup_dismissed_on_scrollbar_scroll(qapp) -> None:
    checker = FakeSpellChecker(
        available=True,
        valid_words=("palavra", "aqui"),
        suggestions={"errado": ["correto"]},
    )
    window, _ = make_window(qapp, spell_checker=checker)
    window.last_message_editor.setPlainText("palavra errado aqui")

    # Abre o popup
    cursor = window.last_message_editor.textCursor()
    cursor.setPosition(10)
    window.last_message_editor.setTextCursor(cursor)
    qapp.processEvents()
    assert window._spell_popup.isVisible() is True

    # 1. Rolagem da barra vertical fecha o popup
    window.last_message_editor.verticalScrollBar().valueChanged.emit(10)
    assert window._spell_popup.isVisible() is False

    # Abre novamente
    cursor.setPosition(11)
    window.last_message_editor.setTextCursor(cursor)
    qapp.processEvents()
    assert window._spell_popup.isVisible() is True

    # 2. Rolagem da barra horizontal fecha o popup
    window.last_message_editor.horizontalScrollBar().valueChanged.emit(5)
    assert window._spell_popup.isVisible() is False

    window.close()

def test_usable_cleanup_error_enters_audio_ready_and_can_be_sent(qapp) -> None:
    capture = make_capture(b"usable-audio-pcm-cleanup-error")
    recorder = FakeRecorder(
        capture=capture,
        fail_stop_error=AudioRecorderError("Não foi possível fechar o microfone."),
    )
    transcriber = FakeTranscriber()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        recorder=recorder,
    )

    window._start_recording()
    window._finish_recording()

    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture
    assert window.play_audio_button.isEnabled()
    assert window.record_button.isEnabled()
    assert window.record_button.text() == "Enviar para Gemini"
    assert "Não foi possível fechar o microfone." in window.status_label.text()
    assert "Forma de onda" in window.audio_debug.toPlainText()

    window._send_pending_audio()
    wait_for_worker(qapp, window)
    assert transcriber.calls == [capture.wav_bytes]
    assert window.transcription_editor.toPlainText() == ""
    assert window.last_message_editor.toPlainText() == "synthetic transcript"
    window.close()


def test_status_compromised_capture_enters_error_and_cannot_be_sent(qapp) -> None:
    capture = make_capture(b"compromised-audio-pcm")
    recorder = FakeRecorder(
        capture=capture,
        fail_stop_error=AudioRecorderError(
            "O áudio perdeu trechos durante a captura. Grave novamente."
        ),
        status="input-overflow",
    )
    transcriber = FakeTranscriber()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        recorder=recorder,
    )

    window._start_recording()
    window._finish_recording()

    assert window.state is AppState.ERROR
    assert window._pending_capture is None
    assert not window.play_audio_button.isEnabled()
    assert window.record_button.text() == "Gravar"
    assert "perdeu trechos" in window.status_label.text()
    assert transcriber.calls == []
    window.close()


def test_replacement_recording_start_failure_preserves_old_capture(qapp) -> None:
    capture1 = make_capture(b"first-valid-audio-capture-1111")
    recorder = FakeRecorder(capture=capture1)
    transcriber = FakeTranscriber()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        recorder=recorder,
    )

    # 1. First recording succeeds -> AUDIO_READY
    window._start_recording()
    window._finish_recording()
    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture1

    # 2. Replacement recording starts with failure
    recorder.fail_start = True
    window._start_replacement_recording()

    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture1
    assert window.play_audio_button.isEnabled()
    assert window.record_button.isEnabled()
    assert window.record_button.text() == "Enviar para Gemini"
    assert window.record_again_button.isEnabled()
    assert "Não foi possível acessar o microfone." in window.status_label.text()

    # Can still send preserved audio
    window._send_pending_audio()
    wait_for_worker(qapp, window)
    assert transcriber.calls == [capture1.wav_bytes]
    window.close()


def test_replacement_recording_low_rms_failure_preserves_old_capture(qapp) -> None:
    capture1 = make_capture(b"first-valid-audio-capture-2222")
    recorder = FakeRecorder(capture=capture1)
    transcriber = FakeTranscriber()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        recorder=recorder,
    )

    # 1. First recording succeeds -> AUDIO_READY
    window._start_recording()
    window._finish_recording()
    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture1

    # 2. Replacement recording fails with low RMS
    recorder.capture = make_capture(b"low-audio-pcm", rms=0.001)
    recorder.low_error = True
    window._start_replacement_recording()
    assert window.state is AppState.RECORDING
    window._finish_recording()

    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture1
    assert window.play_audio_button.isEnabled()
    assert window.record_button.isEnabled()
    assert window.record_button.text() == "Enviar para Gemini"
    assert window.record_again_button.isEnabled()
    assert "baixo" in window.status_label.text()

    # Can still send preserved audio
    window._send_pending_audio()
    wait_for_worker(qapp, window)
    assert transcriber.calls == [capture1.wav_bytes]
    window.close()


def test_replacement_recording_status_compromised_preserves_old_capture(qapp) -> None:
    capture1 = make_capture(b"first-valid-audio-capture-3333")
    recorder = FakeRecorder(capture=capture1)
    transcriber = FakeTranscriber()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        recorder=recorder,
    )

    # 1. First recording succeeds -> AUDIO_READY
    window._start_recording()
    window._finish_recording()
    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture1

    # 2. Replacement recording fails with lost segments
    recorder.capture = make_capture(b"overflow-audio-pcm")
    recorder.status = "input-overflow"
    recorder.fail_stop_error = AudioRecorderError(
        "O áudio perdeu trechos durante a captura. Grave novamente."
    )
    window._start_replacement_recording()
    assert window.state is AppState.RECORDING
    window._finish_recording()

    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture1
    assert window.play_audio_button.isEnabled()
    assert window.record_button.isEnabled()
    assert window.record_button.text() == "Enviar para Gemini"
    assert window.record_again_button.isEnabled()
    assert "perdeu trechos" in window.status_label.text()

    # Can still send preserved audio
    window._send_pending_audio()
    wait_for_worker(qapp, window)
    assert transcriber.calls == [capture1.wav_bytes]
    window.close()


def test_replacement_recording_stop_cleanup_failure_preserves_old_capture(qapp) -> None:
    capture1 = make_capture(b"first-valid-audio-capture-stop-fail-1111")
    capture2 = make_capture(b"second-audible-audio-capture-stop-fail-2222")
    recorder = FakeRecorder(capture=capture1)
    transcriber = FakeTranscriber()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        recorder=recorder,
    )

    # 1. First recording succeeds -> AUDIO_READY
    window._start_recording()
    window._finish_recording()
    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture1

    # 2. Replacement recording produces audible capture2, but stop() raises cleanup error
    recorder.capture = capture2
    recorder.status = None
    recorder.low_error = False
    recorder.fail_stop_error = AudioRecorderError("Não foi possível parar o microfone.")
    window._start_replacement_recording()
    assert window.state is AppState.RECORDING
    window._finish_recording()

    # Must preserve old capture object identity and bytes
    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture1
    assert window._pending_capture.pcm_bytes == capture1.pcm_bytes
    assert window._pending_capture.wav_bytes == capture1.wav_bytes
    assert window.play_audio_button.isEnabled()
    assert window.record_button.isEnabled()
    assert window.record_button.text() == "Enviar para Gemini"
    assert window.record_again_button.isEnabled()
    assert "Não foi possível parar o microfone." in window.status_label.text()

    # Can still send preserved old audio
    window._send_pending_audio()
    wait_for_worker(qapp, window)
    assert transcriber.calls == [capture1.wav_bytes]
    window.close()


def test_replacement_recording_close_cleanup_failure_preserves_old_capture(qapp) -> None:
    capture1 = make_capture(b"first-valid-audio-capture-close-fail-1111")
    capture2 = make_capture(b"second-audible-audio-capture-close-fail-2222")
    recorder = FakeRecorder(capture=capture1)
    transcriber = FakeTranscriber()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        recorder=recorder,
    )

    # 1. First recording succeeds -> AUDIO_READY
    window._start_recording()
    window._finish_recording()
    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture1

    # 2. Replacement recording produces audible capture2, but close() raises cleanup error
    recorder.capture = capture2
    recorder.status = None
    recorder.low_error = False
    recorder.fail_stop_error = AudioRecorderError("Não foi possível fechar o microfone.")
    window._start_replacement_recording()
    assert window.state is AppState.RECORDING
    window._finish_recording()

    # Must preserve old capture object identity and bytes
    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture1
    assert window._pending_capture.pcm_bytes == capture1.pcm_bytes
    assert window._pending_capture.wav_bytes == capture1.wav_bytes
    assert window.play_audio_button.isEnabled()
    assert window.record_button.isEnabled()
    assert window.record_button.text() == "Enviar para Gemini"
    assert window.record_again_button.isEnabled()
    assert "Não foi possível fechar o microfone." in window.status_label.text()

    # Can still send preserved old audio
    window._send_pending_audio()
    wait_for_worker(qapp, window)
    assert transcriber.calls == [capture1.wav_bytes]
    window.close()

def test_replacement_recording_success_replaces_old_capture(qapp) -> None:
    capture1 = make_capture(b"first-valid-audio-capture-4444")
    capture2 = make_capture(b"second-valid-audio-capture-5555")
    recorder = FakeRecorder(capture=capture1)
    transcriber = FakeTranscriber()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        recorder=recorder,
    )

    # 1. First recording succeeds -> AUDIO_READY
    window._start_recording()
    window._finish_recording()
    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture1

    # 2. Replacement recording succeeds -> replaces capture
    recorder.capture = capture2
    recorder.status = None
    recorder.fail_stop_error = None
    recorder.low_error = False
    window._start_replacement_recording()
    assert window.state is AppState.RECORDING
    window._finish_recording()

    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture2

    window._send_pending_audio()
    wait_for_worker(qapp, window)
    assert transcriber.calls == [capture2.wav_bytes]
    window.close()


def test_ordinary_new_recording_clears_stale_audio(qapp) -> None:
    capture1 = make_capture(b"first-valid-audio-capture-6666")
    recorder = FakeRecorder(capture=capture1)
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
    )

    # 1. First recording succeeds -> AUDIO_READY
    window._start_recording()
    window._finish_recording()
    assert window._pending_capture is capture1

    # 2. Ordinary start_recording (preserve_pending=False) clears pending capture
    window._start_recording()
    assert window._pending_capture is None
    assert window.state is AppState.RECORDING
    window.close()


def test_transcription_failure_preserves_capture_and_allows_byte_identical_retry(qapp) -> None:
    capture = make_capture(b"retryable-valid-audio-capture-7777")
    recorder = FakeRecorder(capture=capture)
    transcriber = FakeTranscriber(
        text="sucesso na segunda tentativa",
        error="Erro temporário de conexão com a API.",
    )
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        recorder=recorder,
    )

    window._start_recording()
    window._finish_recording()
    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture

    # 1. First send fails with transient API error
    window._send_pending_audio()
    wait_for_worker(qapp, window)

    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture
    assert window.record_button.isEnabled()
    assert window.record_button.text() == "Enviar para Gemini"
    assert window.record_again_button.isEnabled()
    assert window.play_audio_button.isEnabled()
    assert "Erro temporário" in window.status_label.text()
    assert transcriber.calls == [capture.wav_bytes]

    # 2. Second send (retry) succeeds with byte-identical WAV
    transcriber.error = None
    window._send_pending_audio()
    wait_for_worker(qapp, window)

    assert window.state is AppState.READY
    assert window.transcription_editor.toPlainText() == ""
    assert window.last_message_editor.toPlainText() == "sucesso na segunda tentativa"
    assert len(transcriber.calls) == 2
    assert transcriber.calls[0] == transcriber.calls[1] == capture.wav_bytes
    window.close()


def test_transcription_failure_teardown_race_blocks_retry_until_thread_finished(
    qapp, monkeypatch
) -> None:
    spawned_threads: list[ControlledThread] = []

    class ControlledThread(QThread):
        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self.auto_finish = len(spawned_threads) > 0
            self.quit_called = False
            spawned_threads.append(self)

        def start(self, priority=QThread.Priority.InheritPriority) -> None:
            self.started.emit()
            if self.auto_finish and self.quit_called:
                self.finished.emit()

        def quit(self) -> None:
            self.quit_called = True
            if self.auto_finish:
                self.finished.emit()

    monkeypatch.setattr("falafacil.ui.QThread", ControlledThread)
    monkeypatch.setattr(
        "falafacil.ui.TranscriptionWorker.moveToThread", lambda self, target: None
    )
    capture = make_capture(b"retryable-race-audio-capture-8888")
    recorder = FakeRecorder(capture=capture)
    transcriber = FakeTranscriber(
        text="sucesso na segunda tentativa",
        error="Erro temporário de conexão com a API.",
    )
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        recorder=recorder,
    )

    window._start_recording()
    window._finish_recording()
    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture

    # 1. First send attempt: worker runs and fails, thread quits without emitting finished yet
    window._send_pending_audio()
    assert len(spawned_threads) == 1
    thread = window._thread
    worker = window._worker
    assert thread is spawned_threads[0]
    assert worker is not None
    assert thread.quit_called is True
    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture
    assert "Erro temporário" in window.status_label.text()
    assert window.record_button.isEnabled() is False
    assert window.play_audio_button.isEnabled() is False
    assert len(transcriber.calls) == 1
    assert transcriber.calls[0] == capture.wav_bytes

    # 2. Defensive guard: retry during teardown must not spawn new thread/worker or call API
    window._send_pending_audio()
    assert len(spawned_threads) == 1
    assert window._thread is thread
    assert window._worker is worker
    assert len(transcriber.calls) == 1
    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture

    # 3. Controlled signal: emitting QThread.finished unlocks retry via _on_thread_finished
    thread.finished.emit()
    assert window._thread is None
    assert window._worker is None
    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture
    assert window.record_button.isEnabled() is True
    assert window.record_button.text() == "Enviar para Gemini"
    assert window.play_audio_button.isEnabled() is True

    # 4. Retry proceeds successfully and produces exactly two identical WAV submissions
    transcriber.error = None
    window._send_pending_audio()
    assert len(spawned_threads) == 2
    assert window._thread is None
    assert window._worker is None
    assert window.state is AppState.READY
    assert window.transcription_editor.toPlainText() == ""
    assert window.last_message_editor.toPlainText() == "sucesso na segunda tentativa"
    assert len(transcriber.calls) == 2
    assert transcriber.calls[0] == transcriber.calls[1] == capture.wav_bytes
    window.close()


def test_three_global_activations_start_stop_send_and_no_network_after_stop(qapp, monkeypatch) -> None:
    bridge = FakeInputShortcutBridge()
    recorder = FakeRecorder(capture=make_capture(b"three_activations_pcm"))
    transcriber = FakeTranscriber(text="Texto transcrito com sucesso")
    terminal = FakeTerminal(
        detected_target=TerminalTarget(window_id="111", pid="222", process_name="kitty")
    )
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        recorder=recorder,
        terminal=terminal,
        input_shortcut_bridge=bridge,
    )
    window._activate_shortcut("mouse", "x1", persist=False)

    raises: list[str] = []
    real_raise = window._raise_to_front
    window._raise_to_front = lambda: (raises.append("raise"), real_raise())[1]

    current_time = 100.0
    monkeypatch.setattr(time, "monotonic", lambda: current_time)

    # 1. First global trigger: starts recording, captures origin terminal, NO window raise
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    assert window.state is AppState.RECORDING
    assert window._origin_terminal_target == TerminalTarget(window_id="111", pid="222", process_name="kitty")
    assert raises == []
    assert transcriber.calls == []

    # 2. Second global trigger: stops recording into AUDIO_READY, raises window once, ZERO network calls
    current_time += 1.0
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    assert window.state is AppState.AUDIO_READY
    assert window.record_button.text() == "Enviar para Gemini"
    assert raises == ["raise"]
    assert transcriber.calls == []
    assert window._pending_capture is recorder.capture

    # 3. Third global trigger: sends pending audio to Gemini (no extra window raise)
    current_time += 1.0
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    wait_for_worker(qapp, window)
    assert window.state is AppState.READY
    assert raises == ["raise"]  # Still only 1 raise from stop
    assert transcriber.calls == [recorder.capture.wav_bytes]
    assert window.transcription_editor.toPlainText() == ""
    assert window.last_message_editor.toPlainText() == "Texto transcrito com sucesso"
    assert QApplication.clipboard().text() == "Texto transcrito com sucesso"
    assert window.status_label.text() == "Texto copiado e movido para Última mensagem."

    window.close()


def test_cross_trigger_debounce_with_controlled_monotonic(qapp, monkeypatch) -> None:
    bridge = FakeInputShortcutBridge()
    recorder = FakeRecorder()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        input_shortcut_bridge=bridge,
    )
    window._activate_shortcut("mouse", "x1", persist=False)
    window._activate_shortcut("keyboard", "ctrl+alt+r", persist=False)

    current_time = 50.0
    monkeypatch.setattr(time, "monotonic", lambda: current_time)

    # 1. Mouse trigger at t=50.00 starts recording
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    assert window.state is AppState.RECORDING

    # 2. Immediate keyboard trigger at t=50.10 (dt=0.10 < 0.35s) is debounced (ignored)
    current_time = 50.10
    bridge.keyboard_activated.emit(bridge.keyboard_generation, "ctrl+alt+r")
    assert window.state is AppState.RECORDING

    # 3. Later keyboard trigger at t=50.40 (dt=0.40 >= 0.35s) stops recording
    current_time = 50.40
    bridge.keyboard_activated.emit(bridge.keyboard_generation, "ctrl+alt+r")
    assert window.state is AppState.AUDIO_READY

    # 4. Manual record_button click is NOT debounced (works even with dt=0)
    window.record_button.click()
    assert window.state is AppState.TRANSCRIBING
    wait_for_worker(qapp, window)
    assert window.state is AppState.READY

    window.close()


def test_space_shortcut_inserts_in_text_inputs_and_triggers_primary_outside(qapp) -> None:
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=FakeRecorder(),
    )
    window.show()
    qapp.processEvents()

    # 1. Focus in transcription_editor -> Space inserts a space character and does NOT toggle recording
    window.transcription_editor.setFocus()
    assert window.transcription_editor.hasFocus() is True
    window.transcription_editor.setPlainText("hello")
    cursor = window.transcription_editor.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    window.transcription_editor.setTextCursor(cursor)

    QTest.keyClick(window.transcription_editor, Qt.Key.Key_Space)
    qapp.processEvents()
    assert window.state is AppState.IDLE
    assert window.transcription_editor.toPlainText() == "hello "

    # 2. Focus in last_message_editor -> Space inserts a space character and does NOT toggle recording
    window.last_message_editor.setFocus()
    assert window.last_message_editor.hasFocus() is True
    window.last_message_editor.setPlainText("world")
    cursor = window.last_message_editor.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    window.last_message_editor.setTextCursor(cursor)

    QTest.keyClick(window.last_message_editor, Qt.Key.Key_Space)
    qapp.processEvents()
    assert window.state is AppState.IDLE
    assert window.last_message_editor.toPlainText() == "world "

    # 3. Focus in real API-key QLineEdit (without caller-installed event filter) -> Space inserts a space
    api_dialog, key_input = window._create_api_key_dialog()
    api_dialog.show()
    qapp.processEvents()
    key_input.setText("minha-chave")
    key_input.setFocus()
    qapp.processEvents()
    assert key_input.hasFocus() is True
    key_input.setCursorPosition(len(key_input.text()))

    QTest.keyClick(key_input, Qt.Key.Key_Space)
    qapp.processEvents()
    assert window.state is AppState.IDLE
    assert key_input.text() == "minha-chave "
    api_dialog.close()
    qapp.processEvents()

    # 4. Focus in an editable QComboBox line edit (without caller-installed event filter) -> Space inserts a space
    combo = QComboBox(window)
    combo.setEditable(True)
    combo.lineEdit().setText("opcao")
    combo.show()
    qapp.processEvents()
    combo.lineEdit().setFocus()
    qapp.processEvents()
    combo.lineEdit().setCursorPosition(len(combo.lineEdit().text()))

    QTest.keyClick(combo.lineEdit(), Qt.Key.Key_Space)
    qapp.processEvents()
    assert window.state is AppState.IDLE
    assert combo.lineEdit().text() == "opcao "

    # 5. Focus a non-text control (e.g. record_button) -> Space triggers primary action
    window.activateWindow()
    window.record_button.setFocus()
    qapp.processEvents()
    assert window.record_button.hasFocus() is True
    QTest.keyClick(window.record_button, Qt.Key.Key_Space)
    assert window.state is AppState.RECORDING

    QTest.keyClick(window, Qt.Key.Key_Space)
    qapp.processEvents()
    assert window.state is AppState.AUDIO_READY

    window.close()

def test_current_nonblank_blocks_audio_send_with_exact_status_and_preserves_wav(qapp) -> None:
    capture = make_capture(b"test_nonblank_wav")
    transcriber = FakeTranscriber()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        recorder=FakeRecorder(capture=capture),
    )

    # 1. Record audio -> AUDIO_READY
    window._start_recording()
    window._finish_recording()
    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture

    # 2. Current transcription contains nonblank text
    window.transcription_editor.setPlainText("Texto não arquivado")

    # 3. Attempt to send -> blocked, WAV preserved, exact status
    window._send_pending_audio()
    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture
    assert transcriber.calls == []
    assert window.status_label.text() == "Copie e arquive a transcrição atual antes de enviar outro áudio."

    # 4. User copies and archives current text
    window.copy_and_archive_button.click()
    assert window.transcription_editor.toPlainText() == ""
    assert window.last_message_editor.toPlainText() == "Texto não arquivado"

    window.close()


def test_automatic_clipboard_and_archive_flow(qapp) -> None:
    transcriber = FakeTranscriber(text="Mensagem 1")
    recorder = FakeRecorder(capture=make_capture(b"wav_1"))
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        recorder=recorder,
    )

    # 1. Record and send message 1
    window._start_recording()
    window._finish_recording()
    window._send_pending_audio()
    wait_for_worker(qapp, window)

    assert window.state is AppState.READY
    assert window.transcription_editor.toPlainText() == ""
    assert window.last_message_editor.toPlainText() == "Mensagem 1"
    assert QApplication.clipboard().text() == "Mensagem 1"
    assert window._pending_capture is None
    assert not window.last_message_editor.textCursor().hasSelection()
    assert window.status_label.text() == "Texto copiado e movido para Última mensagem."

    # 2. Record and send message 2 -> overwrites single in-memory last message
    transcriber.text = "Mensagem 2"
    recorder.capture = make_capture(b"wav_2")
    window._start_recording()
    window._finish_recording()
    window._send_pending_audio()
    wait_for_worker(qapp, window)

    assert window.state is AppState.READY
    assert window.transcription_editor.toPlainText() == ""
    assert window.last_message_editor.toPlainText() == "Mensagem 2"
    assert QApplication.clipboard().text() == "Mensagem 2"

    window.close()


def test_per_block_actions_affect_only_intended_editor(qapp) -> None:
    terminal = FakeTerminal()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        terminal=terminal,
    )

    window.transcription_editor.setPlainText("Texto no bloco atual")
    window.last_message_editor.setPlainText("Texto no bloco anterior")
    window.state = AppState.READY
    window._update_actions()

    assert window.copy_and_archive_button.isEnabled() is True
    assert window.copy_last_button.isEnabled() is True
    assert window.clear_last_button.isEnabled() is True
    assert window.terminal_button.isEnabled() is True

    # 1. Copy last message copies only last_message_editor
    QApplication.clipboard().clear()
    window.copy_last_button.click()
    assert QApplication.clipboard().text() == "Texto no bloco anterior"
    assert window.transcription_editor.toPlainText() == "Texto no bloco atual"

    # 2. Clear last message clears only last_message_editor
    window.clear_last_button.click()
    assert window.last_message_editor.toPlainText() == ""
    assert window.transcription_editor.toPlainText() == "Texto no bloco atual"
    assert window.state is AppState.IDLE

    # 3. Copy and archive moves current to last and clears current
    window.copy_and_archive_button.click()
    assert window.transcription_editor.toPlainText() == ""
    assert window.last_message_editor.toPlainText() == "Texto no bloco atual"
    assert QApplication.clipboard().text() == "Texto no bloco atual"
    assert window.state is AppState.READY

    # 4. Terminal send uses only last_message_editor
    window.send_to_terminal()
    assert terminal.send_calls == [("Texto no bloco atual", None)]

    window.close()


def test_proofreading_exact_whitespace_and_one_step_undo(qapp) -> None:
    original_text = "   Primeira linha com erro\nSegunda linha 😀   "
    revised_text = "   Primeira linha corrigida\nSegunda linha 😀   "
    transcriber = FakeTranscriber(proofread_text=revised_text)
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="valid-key"),
        transcriber=transcriber,
    )

    window.last_message_editor.setPlainText(original_text)
    assert window.review_button.isEnabled() is True

    # 1. Proofreading sends untrimmed original text
    window.review_button.click()
    assert window._is_reviewing is True
    assert window.last_message_editor.isReadOnly() is True

    wait_for_proofreading_worker(qapp, window)
    assert transcriber.proofread_calls == [original_text]

    # 2. Success replaces document, auto-copies to clipboard, sets cursor at end
    assert window.last_message_editor.toPlainText() == revised_text
    assert QApplication.clipboard().text() == revised_text
    assert window.status_label.text() == "Texto revisado e copiado."
    assert not window.last_message_editor.textCursor().hasSelection()
    assert window.last_message_editor.isReadOnly() is False

    # 3. One single Undo restores exact byte-for-byte original
    window.last_message_editor.document().undo()
    assert window.last_message_editor.toPlainText() == original_text

    window.close()


def test_playback_toggle_and_resource_safety(qapp) -> None:
    capture = make_capture(b"playback_toggle_wav")
    media_player = FakeMediaPlayer()
    recorder = FakeRecorder(capture=capture)
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        media_player=media_player,
    )

    window._start_recording()
    window._finish_recording()
    assert window.state is AppState.AUDIO_READY
    assert window.play_audio_button.text() == "Reproduzir áudio"

    # 1. Click starts playback -> text becomes "Parar reprodução"
    window.play_audio_button.click()
    assert media_player.play_count == 1
    assert window.play_audio_button.text() == "Parar reprodução"
    assert window._audio_buffer is not None

    # 2. Second click stops playback -> text restored, capture kept
    stops_before = media_player.stop_count
    window.play_audio_button.click()
    assert media_player.stop_count > stops_before
    assert window.play_audio_button.text() == "Reproduzir áudio"
    assert window._audio_buffer is None
    assert window._pending_capture is capture
    # 3. Starting new recording stops player if active
    window.play_audio_button.click()
    assert window._audio_buffer is not None
    window._start_recording()
    assert window._audio_buffer is None
    assert window.play_audio_button.text() == "Reproduzir áudio"

    window.close()


def test_no_legacy_aliases(qapp) -> None:
    window, _ = make_window(qapp)
    assert not hasattr(window, "editor")
    assert not hasattr(window, "send_to_gemini_button")
    assert not hasattr(window, "copy_button")
    assert not hasattr(window, "clear_text_button")
    assert not hasattr(window, "_toggle_recording")
    assert hasattr(window, "transcription_editor")
    assert hasattr(window, "last_message_editor")
    assert hasattr(window, "record_button")
    assert hasattr(window, "record_again_button")
    assert hasattr(window, "play_audio_button")
    assert hasattr(window, "copy_and_archive_button")
    assert hasattr(window, "copy_last_button")
    assert hasattr(window, "clear_last_button")
    assert hasattr(window, "terminal_button")
    assert hasattr(window, "review_button")
    window.close()

def test_spelling_operates_only_on_last_message_editor(qapp) -> None:
    checker = FakeSpellChecker(
        available=True,
        valid_words=("palavra", "aqui"),
    )
    window, _ = make_window(qapp, spell_checker=checker)

    # 1. Misspelled word in last_message_editor triggers popup
    window.last_message_editor.setPlainText("palavra errado aqui")
    cursor = window.last_message_editor.textCursor()
    cursor.setPosition(10)
    window.last_message_editor.setTextCursor(cursor)
    qapp.processEvents()
    assert window._spell_popup.isVisible() is True

    # 2. Typing in transcription_editor does not use spell popup
    window.last_message_editor.clear()
    window._hide_spell_popup()
    window.transcription_editor.setPlainText("palavra errado aqui")
    cursor_trans = window.transcription_editor.textCursor()
    cursor_trans.setPosition(10)
    window.transcription_editor.setTextCursor(cursor_trans)
    qapp.processEvents()
    assert window._spell_popup.isVisible() is False

    window.close()


def test_focus_if_workflow_active_conditions(qapp) -> None:
    window, _ = make_window(qapp, settings=Settings(api_key="active-token"), transcriber=FakeTranscriber())
    target = window.record_button
    assert target.isEnabled() is True

    # 1. Normal active workflow -> sets focus
    target.clearFocus()
    assert target.hasFocus() is False
    window._focus_if_workflow_active(target)
    assert target.hasFocus() is True

    # 2. When settings dialog is open -> no focus change
    target.clearFocus()
    dialog = QDialog(window)
    dialog.show()
    window._settings_dialog = dialog
    qapp.processEvents()
    window._focus_if_workflow_active(target)
    assert target.hasFocus() is False
    dialog.reject()
    window._settings_dialog = None

    # 3. When closing -> no focus change
    target.clearFocus()
    window._is_closing = True
    window._focus_if_workflow_active(target)
    assert target.hasFocus() is False

    window.close()

def test_manual_archive_during_audio_ready_preserves_capture_and_state(qapp) -> None:
    capture = make_capture(b"preserved_audio_ready_pcm")
    transcriber = FakeTranscriber(text="Texto transcrito do audio preservado")
    recorder = FakeRecorder(capture=capture)
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        recorder=recorder,
    )

    # 1. Record audio -> enters AUDIO_READY with pending capture
    window._start_recording()
    window._finish_recording()
    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture

    # 2. Put text in transcription_editor
    window.transcription_editor.setPlainText("Texto rascunho anterior")

    # 3. User clicks manual "Copiar e arquivar" while in AUDIO_READY
    window.copy_and_archive_button.click()
    assert QApplication.clipboard().text() == "Texto rascunho anterior"
    assert window.last_message_editor.toPlainText() == "Texto rascunho anterior"
    assert window.transcription_editor.toPlainText() == ""

    # State MUST remain AUDIO_READY and pending capture MUST be preserved!
    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture

    # 4. Now user clicks "Enviar para Gemini" -> sends the preserved WAV!
    window.record_button.click()
    assert window.state is AppState.TRANSCRIBING
    wait_for_worker(qapp, window)

    assert window.state is AppState.READY
    assert window.transcription_editor.toPlainText() == ""
    assert window.last_message_editor.toPlainText() == "Texto transcrito do audio preservado"
    assert QApplication.clipboard().text() == "Texto transcrito do audio preservado"
    assert window._pending_capture is None

    window.close()


def test_manual_archive_during_recording_preserves_recording_state_and_stream(qapp) -> None:
    capture = make_capture(b"recording_capture_pcm")
    recorder = FakeRecorder(capture=capture)
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
    )

    # 1. Start recording -> enters RECORDING
    window._start_recording()
    assert window.state is AppState.RECORDING
    assert recorder.recording is True

    # 2. Put text in transcription_editor
    window.transcription_editor.setPlainText("Texto rascunho durante gravacao")

    # 3. User clicks manual "Copiar e arquivar" while RECORDING
    window.copy_and_archive_button.click()
    assert QApplication.clipboard().text() == "Texto rascunho durante gravacao"
    assert window.last_message_editor.toPlainText() == "Texto rascunho durante gravacao"
    assert window.transcription_editor.toPlainText() == ""

    # State MUST remain RECORDING and stream MUST still be recording!
    assert window.state is AppState.RECORDING
    assert recorder.recording is True

    # 4. Finish recording -> enters AUDIO_READY
    window._finish_recording()
    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is capture

    window.close()


def test_debounce_sentinel_allows_first_activation_at_t_zero(qapp, monkeypatch) -> None:
    bridge = FakeInputShortcutBridge()
    recorder = FakeRecorder()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        input_shortcut_bridge=bridge,
    )
    window._activate_shortcut("mouse", "x1", persist=False)

    # First activation at monotonic time 0.0 MUST succeed and not be debounced
    monkeypatch.setattr(time, "monotonic", lambda: 0.0)
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    assert window.state is AppState.RECORDING

    window.close()


def test_debounce_exact_boundary_and_reject(qapp, monkeypatch) -> None:
    bridge = FakeInputShortcutBridge()
    recorder = FakeRecorder()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        input_shortcut_bridge=bridge,
    )
    window._activate_shortcut("mouse", "x1", persist=False)

    current_time = 0.00
    monkeypatch.setattr(time, "monotonic", lambda: current_time)

    # 1. First trigger at t=0.00 starts recording
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    assert window.state is AppState.RECORDING

    # 2. Trigger at t=0.3499996 (raw dt = 0.3499996 < 0.350) is REJECTED without rounding
    current_time = 0.3499996
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    assert window.state is AppState.RECORDING

    # 3. Trigger at exact t=0.350 (raw dt = 0.350 >= 0.350) is ACCEPTED
    current_time = 0.350
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    assert window.state is AppState.AUDIO_READY

    window.close()


def test_debounce_blocked_send_by_nonblank_text_does_not_consume_debounce(
    qapp, monkeypatch
) -> None:
    bridge = FakeInputShortcutBridge()
    recorder = FakeRecorder(capture=make_capture(b"blocked_send_pcm"))
    transcriber = FakeTranscriber(text="Texto transcrito apos desbloqueio")
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        recorder=recorder,
        input_shortcut_bridge=bridge,
    )
    window._activate_shortcut("mouse", "x1", persist=False)

    # 1. Record and enter AUDIO_READY
    current_time = 10.0
    monkeypatch.setattr(time, "monotonic", lambda: current_time)
    window._start_recording()
    window._finish_recording()
    assert window.state is AppState.AUDIO_READY

    # 2. Current editor has nonblank text
    window.transcription_editor.setPlainText("texto não arquivado")

    # 3. Global shortcut arrives at t=50.00 -> send blocked by nonblank text, debounce timestamp NOT consumed
    current_time = 50.00
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    assert window.state is AppState.AUDIO_READY
    assert "Copie e arquive a transcrição atual antes de enviar outro áudio." in window.status_label.text()

    # 4. User archives current text
    window._copy_and_archive_current_transcription()
    assert window.transcription_editor.toPlainText() == ""
    assert window.last_message_editor.toPlainText() == "texto não arquivado"
    assert window.state is AppState.AUDIO_READY

    # 5. Global shortcut arrives at t=50.10 (only 0.10s after blocked attempt at 50.00).
    # Because the blocked attempt did NOT consume debounce, this send is ACCEPTED immediately!
    current_time = 50.10
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    assert window.state is AppState.TRANSCRIBING
    wait_for_worker(qapp, window)
    assert window.state is AppState.READY
    assert window.last_message_editor.toPlainText() == "Texto transcrito apos desbloqueio"

    window.close()


def test_debounce_no_op_during_transcribing_proofreading_closing_or_teardown_does_not_consume_debounce(
    qapp, monkeypatch
) -> None:
    bridge = FakeInputShortcutBridge()
    recorder = FakeRecorder(capture=make_capture(b"teardown_wav"))
    transcriber = FakeTranscriber(text="Resultado")
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        recorder=recorder,
        input_shortcut_bridge=bridge,
    )
    window._activate_shortcut("mouse", "x1", persist=False)

    # 1. Start recording and finish into AUDIO_READY
    current_time = 10.0
    monkeypatch.setattr(time, "monotonic", lambda: current_time)
    window._start_recording()
    window._finish_recording()
    assert window.state is AppState.AUDIO_READY

    # 2. Send pending audio -> TRANSCRIBING
    window._send_pending_audio()
    assert window.state is AppState.TRANSCRIBING
    assert window._thread is not None

    # 3. Global shortcut arrives while TRANSCRIBING at t=20.00 -> no-op, debounce timestamp NOT set
    current_time = 20.00
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    assert window.state is AppState.TRANSCRIBING

    # 4. Wait for worker to finish
    wait_for_worker(qapp, window)
    assert window.state is AppState.READY
    assert window._thread is None

    # 5. At t=20.10 (only 0.10s after the ignored trigger at 20.00), global trigger is ACCEPTED because
    # the no-op at t=20.00 did not update _last_global_activation_time!
    current_time = 20.10
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    assert window.state is AppState.RECORDING

    window.close()


def test_global_stop_from_initially_inactive_window_raises_and_focuses_play(
    qapp, monkeypatch
) -> None:
    bridge = FakeInputShortcutBridge()
    recorder = FakeRecorder(capture=make_capture(b"focus_play_pcm"))
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        input_shortcut_bridge=bridge,
    )
    window._activate_shortcut("mouse", "x1", persist=False)
    window.show()
    window.showFullScreen()
    qapp.processEvents()
    assert window.isFullScreen() is True

    # Create another active top-level window so FalaFácil is genuinely inactive
    other_window = QWidget()
    other_window.setWindowTitle("Janela Ativa de Origem")
    other_window.resize(300, 200)
    other_window.show()
    other_window.activateWindow()
    qapp.processEvents()

    current_time = 100.0
    monkeypatch.setattr(time, "monotonic", lambda: current_time)

    # 1. Global start does NOT raise window
    raises: list[str] = []
    real_raise = window._raise_to_front
    window._raise_to_front = lambda: (raises.append("raise"), real_raise())[1]

    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    assert window.state is AppState.RECORDING
    assert raises == []

    # 2. Global stop raises window once, preserves fullscreen, and focuses play_audio_button
    current_time += 1.0
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    qapp.processEvents()
    assert window.state is AppState.AUDIO_READY
    assert raises == ["raise"]
    assert window.isFullScreen() is True
    assert window.play_audio_button.hasFocus() is True

    other_window.close()
    window.close()


def test_global_stop_with_usable_cleanup_error_audio_raises_and_focuses_play(
    qapp, monkeypatch
) -> None:
    bridge = FakeInputShortcutBridge()
    capture = make_capture(b"cleanup_error_pcm")
    recorder = FakeRecorder(
        capture=capture,
        fail_stop_error=AudioRecorderError("Falha ao fechar microfone"),
    )
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        input_shortcut_bridge=bridge,
    )
    window._activate_shortcut("mouse", "x1", persist=False)
    window.show()
    qapp.processEvents()

    current_time = 100.0
    monkeypatch.setattr(time, "monotonic", lambda: current_time)

    raises: list[str] = []
    real_raise = window._raise_to_front
    window._raise_to_front = lambda: (raises.append("raise"), real_raise())[1]

    # 1. Global start
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    assert window.state is AppState.RECORDING
    assert raises == []

    # 2. Global stop with cleanup error enters AUDIO_READY, raises window once, and focuses play button
    current_time += 1.0
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    assert window.state is AppState.AUDIO_READY
    assert raises == ["raise"]
    assert window.play_audio_button.hasFocus() is True
    assert "Falha ao fechar microfone" in window.status_label.text()

    window.close()


def test_global_start_failure_raises_once(qapp, monkeypatch) -> None:
    bridge = FakeInputShortcutBridge()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key=""),
        transcriber=None,
        input_shortcut_bridge=bridge,
    )
    window._activate_shortcut("mouse", "x1", persist=False)
    window.show()
    qapp.processEvents()

    raises: list[str] = []
    real_raise = window._raise_to_front
    window._raise_to_front = lambda: (raises.append("raise"), real_raise())[1]

    # Global start with missing API key fails into ERROR and raises window once
    bridge.mouse_activated.emit(bridge.mouse_generation, "x1")
    assert window.state is AppState.ERROR
    assert raises == ["raise"]

    window.close()


def test_worker_completion_does_not_raise_or_activate_window(qapp) -> None:
    transcriber = FakeTranscriber(text="Texto worker completion")
    recorder = FakeRecorder(capture=make_capture(b"worker_completion_wav"))
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        recorder=recorder,
    )
    window.show()
    qapp.processEvents()

    raises: list[str] = []
    real_raise = window._raise_to_front
    window._raise_to_front = lambda: (raises.append("raise"), real_raise())[1]

    # 1. Start and send
    window._start_recording()
    window._finish_recording()
    window._send_pending_audio()
    assert window.state is AppState.TRANSCRIBING

    # 2. Worker completes
    wait_for_worker(qapp, window)
    assert window.state is AppState.READY
    assert raises == []

    window.close()


def test_media_lifecycle_detach_and_buffer_cleanup_on_all_paths(qapp) -> None:
    from PySide6.QtCore import QBuffer
    capture = make_capture(b"media_lifecycle_wav")
    media_player = FakeMediaPlayer()
    recorder = FakeRecorder(capture=capture)
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        media_player=media_player,
    )

    def assert_buffers_deleted() -> None:
        qapp.processEvents()
        qapp.sendPostedEvents()
        qapp.processEvents()
        assert window._audio_buffer is None
        assert window.findChildren(QBuffer) == []
    # Path 1: Manual stop
    window._start_recording()
    window._finish_recording()
    window._play_pending_audio()
    assert window._audio_buffer is not None
    assert len(window.findChildren(QBuffer)) > 0
    assert media_player.play_count == 1
    window._stop_playback()
    assert_buffers_deleted()

    # Path 2: Send pending audio
    window._play_pending_audio()
    assert window._audio_buffer is not None
    assert len(window.findChildren(QBuffer)) > 0
    window._send_pending_audio()
    assert_buffers_deleted()
    wait_for_worker(qapp, window)

    # Path 3: Ordinary new recording
    window._start_recording()
    window._finish_recording()
    window._play_pending_audio()
    assert window._audio_buffer is not None
    assert len(window.findChildren(QBuffer)) > 0
    window._start_recording()
    assert_buffers_deleted()
    window._finish_recording()

    # Path 4: Replacement recording
    window._play_pending_audio()
    assert window._audio_buffer is not None
    assert len(window.findChildren(QBuffer)) > 0
    window._start_replacement_recording()
    assert_buffers_deleted()
    window._finish_recording()

    # Path 5: Media error
    window._play_pending_audio()
    assert window._audio_buffer is not None
    assert len(window.findChildren(QBuffer)) > 0
    media_player.errorOccurred.emit(None, "erro simulado")
    assert_buffers_deleted()

    # Path 6: End of media
    from PySide6.QtMultimedia import QMediaPlayer
    window._play_pending_audio()
    assert window._audio_buffer is not None
    assert len(window.findChildren(QBuffer)) > 0
    media_player.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.EndOfMedia)
    assert_buffers_deleted()
    # Path 7: Window close
    window._play_pending_audio()
    assert window._audio_buffer is not None
    assert len(window.findChildren(QBuffer)) > 0
    window.close()
    assert_buffers_deleted()


def test_media_stop_and_detach_failure_never_closes_attached_buffer(qapp) -> None:
    from PySide6.QtMultimedia import QMediaPlayer

    # Scenario 1: Stop fails, but source detach succeeds -> buffer released, returns True
    capture1 = make_capture(b"detach_ok_pcm")
    media_player1 = FakeMediaPlayer(
        fail_stop=RuntimeError("stop failed"),
    )
    window1, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=FakeRecorder(capture=capture1),
        media_player=media_player1,
    )
    window1._start_recording()
    window1._finish_recording()
    window1._play_pending_audio()
    assert window1._audio_buffer is not None
    assert window1._stop_playback() is True
    assert window1._is_playing_audio is False
    assert window1._audio_buffer is None
    assert window1.play_audio_button.text() == "Reproduzir áudio"
    window1.close()

    # Scenario 2: First detach (device) succeeds, second detach (url) fails -> detached is True, buffer released
    media_player2 = FakeMediaPlayer(
        fail_detach_device=None,
        fail_detach_url=RuntimeError("url detach failed"),
    )
    window2, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=FakeRecorder(capture=capture1),
        media_player=media_player2,
    )
    window2._start_recording()
    window2._finish_recording()
    window2._play_pending_audio()
    assert window2._audio_buffer is not None
    assert window2._stop_playback() is True
    assert window2._is_playing_audio is False
    assert window2._audio_buffer is None
    assert window2.play_audio_button.text() == "Reproduzir áudio"
    window2.close()

    # Scenario 3: Both device and URL detach fail -> buffer retained open, UI truthful, callers respect failure
    capture = make_capture(b"detach_failure_pcm")
    media_player = FakeMediaPlayer(
        fail_stop=RuntimeError("stop failed"),
        fail_detach_device=RuntimeError("device detach failed"),
        fail_detach_url=RuntimeError("url detach failed"),
    )
    recorder = FakeRecorder(capture=capture)
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        media_player=media_player,
    )
    window._start_recording()
    window._finish_recording()
    window._play_pending_audio()
    assert window._audio_buffer is not None
    assert window._audio_buffer.isOpen() is True

    # When stop and both detaches fail, _stop_playback returns False, leaves buffer open and UI truthful
    stopped = window._stop_playback()
    assert stopped is False
    assert window._is_playing_audio is True
    assert window._audio_buffer is not None
    assert window._audio_buffer.isOpen() is True
    assert window.play_audio_button.text() == "Parar reprodução"
    assert window.status_label.text() == "Não foi possível parar a reprodução do áudio."

    # Caller 1: _toggle_playback respects failure
    window._toggle_playback()
    assert window._is_playing_audio is True
    assert window.play_audio_button.text() == "Parar reprodução"
    assert window.status_label.text() == "Não foi possível parar a reprodução do áudio."

    # Caller 2: EndOfMedia signal respects failure
    media_player.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.EndOfMedia)
    assert window._is_playing_audio is True
    assert window.play_audio_button.text() == "Parar reprodução"
    assert window.status_label.text() == "Não foi possível parar a reprodução do áudio."
    assert window.record_button.hasFocus() is False

    # Caller 3: InvalidMedia signal respects failure
    media_player.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.InvalidMedia)
    assert window._is_playing_audio is True
    assert window.play_audio_button.text() == "Parar reprodução"
    assert window.status_label.text() == "Não foi possível parar a reprodução do áudio."

    # Caller 4: errorOccurred signal respects failure
    media_player.errorOccurred.emit(object(), "simulated media error")
    assert window._is_playing_audio is True
    assert window.play_audio_button.text() == "Parar reprodução"
    assert window.status_label.text() == "Não foi possível parar a reprodução do áudio."

    # Caller 5: _play_pending_audio while prior playback unreleased respects failure
    media_player.fail_play = RuntimeError("play error")
    window._play_pending_audio()
    assert window._is_playing_audio is True
    assert window.play_audio_button.text() == "Parar reprodução"
    assert window.status_label.text() == "Não foi possível parar a reprodução do áudio."
    media_player.fail_play = None

    # Incompatible workflow actions are blocked while release has not succeeded
    assert window._send_pending_audio() is False
    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is not None

    assert window._start_recording() is False
    assert window.state is AppState.AUDIO_READY

    window._start_replacement_recording()
    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is not None

    # When player detaches cleanly on retry, release succeeds and unblocks workflow
    media_player.fail_stop = None
    media_player.fail_detach = None
    media_player.fail_detach_device = None
    media_player.fail_detach_url = None

    assert window._stop_playback() is True
    assert window._is_playing_audio is False
    assert window._audio_buffer is None
    assert window.play_audio_button.text() == "Reproduzir áudio"

    assert window._send_pending_audio() is True
    assert window.state is AppState.TRANSCRIBING
    wait_for_worker(qapp, window)
    assert window.state is AppState.READY

    window.close()
def test_media_play_exception_with_detach_failure_keeps_buffer_attached_and_blocks_workflow(qapp) -> None:
    capture = make_capture(b"play_exc_detach_failure_pcm")
    media_player = FakeMediaPlayer(
        fail_play=RuntimeError("play device failed"),
        fail_stop=RuntimeError("stop failed"),
        fail_detach_device=RuntimeError("device detach failed"),
        fail_detach_url=RuntimeError("url detach failed"),
    )
    recorder = FakeRecorder(capture=capture)
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        media_player=media_player,
    )
    window._start_recording()
    window._finish_recording()
    assert window.state is AppState.AUDIO_READY
    assert window._is_playing_audio is False
    assert window._audio_buffer is None
    assert media_player.play_calls == 0
    assert media_player.play_count == 0

    # Trigger playback from clean state with play and detach failure preconfigured
    window._play_pending_audio()

    assert media_player.play_calls == 1
    assert media_player.play_count == 0
    assert window._audio_buffer is not None
    assert window._audio_buffer.isOpen() is True
    assert media_player.source_device is window._audio_buffer
    assert window._is_playing_audio is True
    assert window.play_audio_button.text() == "Parar reprodução"
    assert window.status_label.text() == "Não foi possível parar a reprodução do áudio."

    # Incompatible workflow actions are blocked while release has not succeeded
    assert window._send_pending_audio() is False
    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is not None

    assert window._start_recording() is False
    assert window.state is AppState.AUDIO_READY

    window._start_replacement_recording()
    assert window.state is AppState.AUDIO_READY
    assert window._pending_capture is not None

    # Retry release succeeds when backend failure is resolved
    media_player.fail_play = None
    media_player.fail_stop = None
    media_player.fail_detach = None
    media_player.fail_detach_device = None
    media_player.fail_detach_url = None

    assert window._stop_playback() is True
    assert window._is_playing_audio is False
    assert window._audio_buffer is None
    assert window.play_audio_button.text() == "Reproduzir áudio"

    assert window._send_pending_audio() is True
    assert window.state is AppState.TRANSCRIBING
    wait_for_worker(qapp, window)
    assert window.state is AppState.READY

    window.close()




def test_media_generations_isolate_stale_callbacks_and_prevent_corruption(qapp) -> None:
    from PySide6.QtMultimedia import QMediaPlayer
    capture = make_capture(b"gen_isolation_wav")
    media_player = FakeMediaPlayer()
    recorder = FakeRecorder(capture=capture)
    transcriber = FakeTranscriber(text="Texto final de teste de geracao")
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        recorder=recorder,
        media_player=media_player,
    )
    window._start_recording()
    window._finish_recording()
    assert window.state is AppState.AUDIO_READY

    # 1. Start generation 1
    window._play_pending_audio()
    gen1 = window._active_playback_generation
    assert gen1 is not None
    assert window._is_playing_audio is True
    # Retain all three generation 1 adapters to simulate delayed callback delivery
    gen1_status_adapter, gen1_state_adapter, gen1_error_adapter = window._media_adapters

    # 2. Stop and start generation 2
    window._stop_playback()
    assert window._active_playback_generation is None
    window._play_pending_audio()
    gen2 = window._active_playback_generation
    assert gen2 is not None and gen2 != gen1
    # Focus explicit sentinel widget before generation 1 stale batch during generation 2
    sentinel1 = window.last_message_editor
    sentinel1.setFocus()
    assert QApplication.focusWidget() is sentinel1

    # 3. Delayed EndOfMedia and InvalidMedia from generation 1 arrive -> must NOT stop generation 2 or change focus
    gen1_status_adapter(QMediaPlayer.MediaStatus.EndOfMedia)
    assert QApplication.focusWidget() is sentinel1
    assert window._is_playing_audio is True
    assert window.play_audio_button.text() == "Parar reprodução"
    assert window.status_label.text() == "Reproduzindo o áudio capturado."
    assert window._audio_buffer is not None
    assert window._audio_buffer.isOpen() is True

    gen1_status_adapter(QMediaPlayer.MediaStatus.InvalidMedia)
    assert QApplication.focusWidget() is sentinel1
    assert window._is_playing_audio is True
    assert window.play_audio_button.text() == "Parar reprodução"
    assert window.status_label.text() == "Reproduzindo o áudio capturado."
    assert window._audio_buffer is not None
    assert window._audio_buffer.isOpen() is True

    # 4. Delayed playbackStateChanged (StoppedState) from generation 1 arrives -> must NOT stop generation 2 or change focus
    gen1_state_adapter(QMediaPlayer.PlaybackState.StoppedState)
    assert QApplication.focusWidget() is sentinel1
    assert window._is_playing_audio is True
    assert window.play_audio_button.text() == "Parar reprodução"
    assert window.status_label.text() == "Reproduzindo o áudio capturado."
    assert window._audio_buffer is not None
    assert window._audio_buffer.isOpen() is True

    # 5. Delayed errorOccurred from generation 1 arrives -> must NOT stop generation 2 or change focus
    gen1_error_adapter(None, "erro antigo gen1")
    assert QApplication.focusWidget() is sentinel1
    assert window._is_playing_audio is True
    assert window.play_audio_button.text() == "Parar reprodução"
    assert window.status_label.text() == "Reproduzindo o áudio capturado."
    assert window._audio_buffer is not None
    assert window._audio_buffer.isOpen() is True

    # 6. Valid signals for generation 2 emitted via connected signals on FakeMediaPlayer
    media_player.playbackStateChanged.emit(QMediaPlayer.PlaybackState.PlayingState)
    assert window._is_playing_audio is True

    media_player.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.BufferedMedia)
    assert window._is_playing_audio is True

    media_player.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.EndOfMedia)
    assert window._is_playing_audio is False
    assert window.play_audio_button.text() == "Reproduzir áudio"
    assert window.status_label.text() == "Reprodução concluída."
    assert window.record_button.hasFocus() is True

    # 7. Advance workflow to READY via send + transcription
    window._send_pending_audio()
    assert window.state is AppState.TRANSCRIBING
    wait_for_worker(qapp, window)
    assert window.state is AppState.READY
    assert window.status_label.text() == "Texto copiado e movido para Última mensagem."

    # 8. Delayed callbacks from all 3 adapters arriving in READY must NOT overwrite status, state, or focus
    # Focus distinct sentinel widget before stale callbacks in READY
    sentinel2 = window.transcription_editor
    sentinel2.setFocus()
    assert QApplication.focusWidget() is sentinel2

    gen1_status_adapter(QMediaPlayer.MediaStatus.EndOfMedia)
    assert QApplication.focusWidget() is sentinel2
    gen1_status_adapter(QMediaPlayer.MediaStatus.InvalidMedia)
    assert QApplication.focusWidget() is sentinel2
    gen1_state_adapter(QMediaPlayer.PlaybackState.StoppedState)
    assert QApplication.focusWidget() is sentinel2
    gen1_error_adapter(None, "erro tardio em READY")
    assert QApplication.focusWidget() is sentinel2
    assert window.status_label.text() == "Texto copiado e movido para Última mensagem."
    assert window.state is AppState.READY
    assert window._audio_buffer is None
    assert window._is_playing_audio is False

    media_player.mediaStatusChanged.emit(QMediaPlayer.MediaStatus.EndOfMedia)
    assert QApplication.focusWidget() is sentinel2
    media_player.playbackStateChanged.emit(QMediaPlayer.PlaybackState.StoppedState)
    assert QApplication.focusWidget() is sentinel2
    media_player.errorOccurred.emit(object(), "erro em READY")
    assert QApplication.focusWidget() is sentinel2
    assert window.status_label.text() == "Texto copiado e movido para Última mensagem."
    assert window.state is AppState.READY
    assert window._audio_buffer is None
    assert window._is_playing_audio is False

    window.close()

def test_close_removes_application_event_filter_unconditionally_in_all_states(qapp) -> None:
    # 1. Close from IDLE removes event filter and lets Space work in external QLineEdit
    window_idle, _ = make_window(qapp)
    assert window_idle.state is AppState.IDLE
    window_idle.close()
    external_line_edit = QLineEdit()
    external_line_edit.show()
    QTest.keyClicks(external_line_edit, "hello world")
    assert external_line_edit.text() == "hello world"
    external_line_edit.close()

    # 2. Close from READY removes event filter
    transcriber = FakeTranscriber(text="Texto pronto")
    window_ready, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        recorder=FakeRecorder(capture=make_capture(b"ready_close_pcm")),
    )
    window_ready._start_recording()
    window_ready._finish_recording()
    window_ready._send_pending_audio()
    wait_for_worker(qapp, window_ready)
    assert window_ready.state is AppState.READY
    window_ready.close()

    # 3. Close from RECORDING removes event filter
    window_rec, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=transcriber,
        recorder=FakeRecorder(capture=make_capture(b"rec_close_pcm")),
    )
    window_rec._start_recording()
    assert window_rec.state is AppState.RECORDING
    window_rec.close()
def test_close_event_ignored_and_state_preserved_when_playback_release_fails(qapp) -> None:
    from falafacil.terminal import TerminalTarget
    capture = make_capture(b"close_fail_detach_pcm")
    media_player = FakeMediaPlayer(
        fail_stop=RuntimeError("stop failed on close"),
        fail_detach_device=RuntimeError("device detach failed on close"),
        fail_detach_url=RuntimeError("url detach failed on close"),
    )
    store = FakeLocalStore()
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=FakeRecorder(capture=capture),
        media_player=media_player,
        local_store=store,
    )
    window._start_recording()
    window._finish_recording()
    target = TerminalTarget(window_id="999", pid="1234", process_name="gnome-terminal-server")
    window._origin_terminal_target = target
    window._preserved_capture = capture

    window._play_pending_audio()
    assert window._is_playing_audio is True
    assert window._audio_buffer is not None
    assert window._audio_buffer.isOpen() is True

    # 1. Close attempt when release fails: event is ignored, state & resources are preserved
    window.close()
    assert window._is_closing is False
    assert window.status_label.text() == "Não foi possível parar a reprodução do áudio."
    assert window._is_playing_audio is True
    assert window._audio_buffer is not None
    assert window._audio_buffer.isOpen() is True
    assert window._pending_capture is capture
    assert window._preserved_capture is capture
    assert window._origin_terminal_target is target
    assert store.closed is False
    assert window.state is AppState.AUDIO_READY

    # Space filter remains functional
    external_line_edit = QLineEdit()
    external_line_edit.show()
    QTest.keyClicks(external_line_edit, "abc")
    assert external_line_edit.text() == "abc"
    external_line_edit.close()

    # 2. Second close after backend detachment succeeds performs normal cleanup
    media_player.fail_stop = None
    media_player.fail_detach = None
    media_player.fail_detach_device = None
    media_player.fail_detach_url = None

    window.close()
    assert window._is_closing is True
    assert window._is_playing_audio is False
    assert window._audio_buffer is None
    assert window._pending_capture is None
    assert window._preserved_capture is None
    assert window._origin_terminal_target is None
    assert store.closed is True



def test_microphone_persistence_failure_shows_warning(qapp) -> None:
    store_fail = FakeLocalStore(fail_mic=True)
    recorder = FakeRecorder(capture=make_capture(b"mic_fail_pcm"))
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        local_store=store_fail,
    )
    window._start_recording()
    assert window.state is AppState.RECORDING
    assert window.status_label.text() == "Gravando… não foi possível atualizar a memória do microfone."
    window._finish_recording()

    # Normal store shows "Gravando áudio…"
    store_ok = FakeLocalStore()
    window_ok, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
        recorder=recorder,
        local_store=store_ok,
    )
    window_ok._start_recording()
    assert window_ok.state is AppState.RECORDING
    assert window_ok.status_label.text() == "Gravando áudio…"
    assert hasattr(window_ok, "_active_recording_device") is False
    window_ok._finish_recording()
    window.close()
    window_ok.close()

def test_minimum_size_layout_and_splitter_visibility(qapp) -> None:
    window, _ = make_window(
        qapp,
        settings=Settings(api_key="active-token"),
        transcriber=FakeTranscriber(),
    )
    window.resize(760, 560)
    window.show()
    qapp.processEvents()

    assert window.width() >= 760
    assert window.height() >= 560

    # message_splitter has deterministic positive sizes matching UI configuration
    sizes = window.message_splitter.sizes()
    assert len(sizes) == 2
    assert sizes[0] >= 100
    assert sizes[1] >= 100
    # 1:1 ratio within documented small pixel tolerance matching setSizes([200, 200])
    assert abs(sizes[0] - sizes[1]) <= 10

    # Both editor viewports have meaningful minimum height and width
    assert window.transcription_editor.isVisible()
    assert window.transcription_editor.viewport().height() >= 50
    assert window.transcription_editor.viewport().width() >= 150

    assert window.last_message_editor.isVisible()
    assert window.last_message_editor.viewport().height() >= 50
    assert window.last_message_editor.viewport().width() >= 150

    # Diagnostics panel visible with meaningful dimensions
    assert window.diagnostic_tabs.isVisible()
    assert window.diagnostic_tabs.width() >= 150
    assert window.diagnostic_tabs.height() >= 100
    assert window.usage_chart.isVisible()
    assert window.usage_chart.width() >= 150
    assert window.usage_chart.height() >= 60

    # Stability across multiple event processing cycles
    for _ in range(3):
        qapp.processEvents()
    assert window.message_splitter.sizes() == sizes
    window.close()
