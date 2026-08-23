from __future__ import annotations

from dataclasses import replace
from enum import Enum, auto
from typing import Callable

import numpy as np
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QThread, QUrl, Qt, Slot
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .audio import (
    AudioCapture,
    AudioDevice,
    AudioRecorder,
    AudioRecorderError,
    list_input_devices,
)
from .config import Settings
from .credentials import ApiKeyStore, CredentialStoreError
from .terminal import TerminalBridge, TerminalBridgeError
from .transcription import GeminiTranscriber, TranscriptionDebug, TranscriptionWorker


class AppState(Enum):
    IDLE = auto()
    RECORDING = auto()
    AUDIO_READY = auto()
    TRANSCRIBING = auto()
    READY = auto()
    ERROR = auto()


MediaPlayerFactory = Callable[[QWidget], tuple[QMediaPlayer, QAudioOutput]]


def _default_media_player_factory(parent: QWidget) -> tuple[QMediaPlayer, QAudioOutput]:
    player = QMediaPlayer(parent)
    audio_output = QAudioOutput(parent)
    player.setAudioOutput(audio_output)
    return player, audio_output


class MainWindow(QMainWindow):
    def __init__(
        self,
        settings: Settings,
        recorder: AudioRecorder | None = None,
        transcriber: GeminiTranscriber | None = None,
        terminal_bridge: TerminalBridge | None = None,
        api_key_store: ApiKeyStore | None = None,
        transcriber_factory: Callable[[str], GeminiTranscriber] | None = None,
        microphone_provider: Callable[[], tuple[AudioDevice, ...]] | None = None,
        media_player_factory: MediaPlayerFactory | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.recorder = recorder or AudioRecorder()
        self.transcriber = transcriber
        self.terminal_bridge = terminal_bridge or TerminalBridge()
        self.api_key_store = api_key_store
        self.transcriber_factory = transcriber_factory or (
            lambda api_key: GeminiTranscriber(api_key=api_key, model=self.settings.model)
        )
        self._microphone_provider = microphone_provider or list_input_devices
        self._media_player_factory = media_player_factory or _default_media_player_factory
        self.state = AppState.IDLE
        self._thread: QThread | None = None
        self._worker: TranscriptionWorker | None = None
        self._pending_capture: AudioCapture | None = None
        self._audio_buffer: QBuffer | None = None
        self._audio_byte_array: QByteArray | None = None
        self._microphone_refreshing = False
        self._microphone_available = False

        self.setWindowTitle("FalaFácil")
        self.resize(760, 520)
        self._media_player, self._audio_output = self._media_player_factory(self)
        self._connect_media_signals()
        self._build_ui()
        self._refresh_microphones()
        self._update_actions()

    def _connect_media_signals(self) -> None:
        for signal_name, slot in (
            ("mediaStatusChanged", self._on_media_status_changed),
            ("playbackStateChanged", self._on_playback_state_changed),
            ("errorOccurred", self._on_media_error),
        ):
            signal = getattr(self._media_player, signal_name, None)
            if signal is not None:
                signal.connect(slot)

    def _build_ui(self) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)

        microphone_row = QHBoxLayout()
        microphone_row.addWidget(QLabel("Microfone", self))
        self.microphone_combo = QComboBox(self)
        self.microphone_combo.setEditable(False)
        self.microphone_combo.currentIndexChanged.connect(self._select_microphone)
        microphone_row.addWidget(self.microphone_combo, stretch=1)
        self.refresh_microphones_button = QPushButton("Detectar microfones", self)
        self.refresh_microphones_button.clicked.connect(self._refresh_microphones)
        microphone_row.addWidget(self.refresh_microphones_button)
        layout.addLayout(microphone_row)

        self.editor = QPlainTextEdit(self)
        self.editor.setPlaceholderText(
            "A transcrição aparecerá aqui. Você também pode corrigir o texto antes de copiar."
        )
        self.editor.setTabChangesFocus(False)
        self.editor.textChanged.connect(self._update_actions)
        layout.addWidget(self.editor, stretch=1)

        buttons = QHBoxLayout()
        self.record_button = QPushButton("Gravar", self)
        self.record_button.setToolTip("Começa ou para a gravação do microfone")
        self.record_button.setShortcut(QKeySequence("Space"))
        self.record_button.clicked.connect(self._toggle_recording)
        buttons.addWidget(self.record_button)

        self.play_audio_button = QPushButton("Reproduzir áudio", self)
        self.play_audio_button.clicked.connect(self._play_pending_audio)
        buttons.addWidget(self.play_audio_button)

        self.send_to_gemini_button = QPushButton("Enviar para Gemini", self)
        self.send_to_gemini_button.clicked.connect(self._send_pending_audio)
        buttons.addWidget(self.send_to_gemini_button)

        self.copy_button = QPushButton("Copiar texto", self)
        self.copy_button.setToolTip("Copia o texto para a área de transferência")
        self.copy_button.clicked.connect(self.copy_text)
        buttons.addWidget(self.copy_button)

        self.clear_text_button = QPushButton("Apagar texto", self)
        self.clear_text_button.setToolTip("Apaga o texto do editor")
        self.clear_text_button.clicked.connect(self.clear_text)
        buttons.addWidget(self.clear_text_button)

        self.terminal_button = QPushButton("Enviar ao terminal", self)
        self.terminal_button.setToolTip(
            "Cola o texto no terminal X11 atualmente ativo, sem pressionar Enter"
        )
        self.terminal_button.clicked.connect(self.send_to_terminal)
        buttons.addWidget(self.terminal_button)

        self.configure_key_button = QPushButton("Configurar chave API", self)
        self.configure_key_button.setToolTip("Configura a chave API no chaveiro do sistema")
        self.configure_key_button.clicked.connect(self._configure_api_key)
        buttons.addWidget(self.configure_key_button)

        self.debug_button = QPushButton("Mostrar debug", self)
        self.debug_button.setCheckable(True)
        self.debug_button.clicked.connect(self._toggle_debug_panel)
        buttons.addWidget(self.debug_button)
        layout.addLayout(buttons)

        self.status_label = QLabel(self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.setCentralWidget(central)
        self._build_debug_dock()
        self.copy_shortcut = QShortcut(QKeySequence("Ctrl+Shift+C"), self)
        self.copy_shortcut.activated.connect(self.copy_text)
        self.record_button.setFocus()

    def _build_debug_dock(self) -> None:
        self.debug_dock = QDockWidget("Debug da captura e transcrição", self)
        self.debug_dock.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea)
        debug_widget = QWidget(self.debug_dock)
        debug_layout = QVBoxLayout(debug_widget)
        self.audio_debug = self._debug_text_block(debug_layout, "Áudio recebido")
        self.payload_debug = self._debug_text_block(debug_layout, "Payload enviado ao Gemini")
        self.return_debug = self._debug_text_block(debug_layout, "Retorno")
        self.debug_dock.setWidget(debug_widget)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.debug_dock)
        self.debug_dock.visibilityChanged.connect(self._sync_debug_button)
        self.debug_dock.setVisible(False)

    def _debug_text_block(self, layout: QVBoxLayout, title: str) -> QPlainTextEdit:
        layout.addWidget(QLabel(title, self))
        editor = QPlainTextEdit(self)
        editor.setReadOnly(True)
        editor.setMaximumBlockCount(200)
        layout.addWidget(editor, stretch=1)
        return editor

    @Slot()
    def _toggle_debug_panel(self) -> None:
        self.debug_dock.setVisible(not self.debug_dock.isVisible())
        self._sync_debug_button(self.debug_dock.isVisible())

    @Slot(bool)
    def _sync_debug_button(self, visible: bool) -> None:
        self.debug_button.blockSignals(True)
        self.debug_button.setChecked(visible)
        self.debug_button.setText("Ocultar debug" if visible else "Mostrar debug")
        self.debug_button.blockSignals(False)

    @Slot()
    def _refresh_microphones(self) -> None:
        selected = self.microphone_combo.currentData()
        provider_error = False
        self._microphone_refreshing = True
        try:
            devices = tuple(self._microphone_provider())
        except Exception as exc:
            provider_error = True
            self.microphone_combo.clear()
            self._microphone_available = False
            self.status_label.setText(f"Não foi possível detectar microfones: {exc}")
        else:
            self.microphone_combo.clear()
            for device in devices:
                suffix = " (padrão)" if device.is_default else ""
                self.microphone_combo.addItem(
                    f"{device.name} (índice {device.index}){suffix}",
                    device.index,
                )
            self._microphone_available = bool(devices)
            target_index = -1
            if selected is not None:
                target_index = self.microphone_combo.findData(selected)
            if target_index < 0:
                target_index = next(
                    (
                        index
                        for index, device in enumerate(devices)
                        if device.is_default
                    ),
                    0 if devices else -1,
                )
            if target_index >= 0:
                self.microphone_combo.setCurrentIndex(target_index)
        finally:
            self._microphone_refreshing = False
        if (
            not provider_error
            and not self._microphone_available
            and self.microphone_combo.count() == 0
        ):
            self.status_label.setText("Nenhum microfone de entrada foi detectado.")
        self._select_microphone(self.microphone_combo.currentIndex())
        self._update_actions()

    @Slot(int)
    def _select_microphone(self, index: int) -> None:
        if self._microphone_refreshing or index < 0:
            return
        if self.state in (AppState.RECORDING, AppState.TRANSCRIBING):
            return
        device = self.microphone_combo.itemData(index)
        try:
            self.recorder.set_device(device)
        except AttributeError:
            return
        except AudioRecorderError as exc:
            self._set_error(str(exc))

    def _acquire_api_key(self) -> tuple[str, bool]:
        dialog = QDialog(self)
        dialog.setWindowTitle("Configurar chave API")
        layout = QVBoxLayout(dialog)
        explanation = QLabel(
            "A chave será guardada no chaveiro seguro do sistema. "
            "Ela não será exibida pela interface.",
            dialog,
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        key_input = QLineEdit(dialog)
        key_input.setEchoMode(QLineEdit.EchoMode.Password)
        key_input.setPlaceholderText("Chave API")
        layout.addWidget(key_input)

        dialog_buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=dialog,
        )
        dialog_buttons.accepted.connect(dialog.accept)
        dialog_buttons.rejected.connect(dialog.reject)
        layout.addWidget(dialog_buttons)

        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        return key_input.text(), accepted

    @Slot()
    def _configure_api_key(self) -> None:
        api_key, accepted = self._acquire_api_key()
        if not accepted:
            return

        api_key = api_key.strip()
        if not api_key:
            return

        try:
            new_transcriber = self.transcriber_factory(api_key)
        except Exception:
            self.status_label.setText("Não foi possível configurar a chave API.")
            return

        persistence_unavailable = self.api_key_store is None
        if self.api_key_store is not None:
            try:
                self.api_key_store.set_api_key(api_key)
            except CredentialStoreError:
                persistence_unavailable = True

        self.settings = replace(self.settings, api_key=api_key)
        self.transcriber = new_transcriber
        self._update_actions()
        if persistence_unavailable:
            self.status_label.setText(
                "Chave API configurada apenas nesta sessão; "
                "não foi possível persistir no chaveiro do sistema."
            )
        else:
            self.status_label.setText("Chave API configurada com sucesso.")

    @Slot()
    def _toggle_recording(self) -> None:
        if self.state is AppState.RECORDING:
            self._finish_recording()
        elif self.state is not AppState.TRANSCRIBING:
            self._start_recording()

    def _start_recording(self) -> None:
        if not self.settings.has_api_key or self.transcriber is None:
            self._set_error(self.settings.missing_api_key_message)
            return
        if not self._microphone_available or self.microphone_combo.currentData() is None:
            self._set_error("Nenhum microfone de entrada foi detectado.")
            return
        self._pending_capture = None
        self._release_audio_source()
        self.audio_debug.setPlainText("Capturando áudio…\nO WAV ainda não foi enviado.")
        self.payload_debug.clear()
        self.return_debug.clear()
        try:
            self.recorder.start()
        except AudioRecorderError as exc:
            self._set_error(str(exc))
            return

        self.state = AppState.RECORDING
        self.record_button.setText("Parar e revisar áudio")
        self.status_label.setText("Gravando… fale em português e clique para parar.")
        self._update_actions()

    def _finish_recording(self) -> None:
        try:
            capture = self.recorder.stop()
        except AudioRecorderError as exc:
            capture = self.recorder.last_capture()
            if capture is not None:
                self._render_audio_debug(capture, error=str(exc))
            self._set_error(str(exc))
            return

        self._pending_capture = capture
        self._render_audio_debug(capture)
        self.state = AppState.AUDIO_READY
        self.record_button.setText("Gravar")
        self.status_label.setText(
            "Áudio pronto. Reproduza para revisar ou envie explicitamente ao Gemini."
        )
        self._update_actions()

    def _play_pending_audio(self) -> None:
        capture = self._pending_capture
        if self.state is not AppState.AUDIO_READY or capture is None:
            return
        self._release_audio_source()
        self._audio_byte_array = QByteArray(capture.wav_bytes)
        self._audio_buffer = QBuffer(self)
        self._audio_buffer.setData(self._audio_byte_array)
        if not self._audio_buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            self.status_label.setText("Não foi possível preparar a reprodução do áudio.")
            self._release_audio_source()
            return
        try:
            self._media_player.setAudioOutput(self._audio_output)
            self._media_player.setSourceDevice(self._audio_buffer, QUrl("audio.wav"))
            self._media_player.play()
            self.status_label.setText("Reproduzindo o áudio capturado.")
        except Exception as exc:
            self.status_label.setText(f"Não foi possível reproduzir o áudio: {exc}")
            self._release_audio_source()

    def _send_pending_audio(self) -> None:
        if self.state is not AppState.AUDIO_READY or self._pending_capture is None:
            return
        if self.transcriber is None or not self.settings.has_api_key:
            self._set_error(self.settings.missing_api_key_message)
            return

        self.state = AppState.TRANSCRIBING
        self.status_label.setText("Transcrevendo com Gemini…")
        self._update_actions()

        self._thread = QThread(self)
        self._worker = TranscriptionWorker(
            self.transcriber,
            self._pending_capture.wav_bytes,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_transcription_finished)
        self._worker.failed.connect(self._on_transcription_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.failed.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.start()

    @Slot(str, object)
    def _on_transcription_finished(
        self,
        text: str,
        debug: TranscriptionDebug | None,
    ) -> None:
        self.editor.setPlainText(text)
        self.editor.selectAll()
        self._render_transcription_debug(debug, text=text)
        self.state = AppState.READY
        self.status_label.setText("Transcrição pronta. Revise, copie ou envie ao terminal.")
        self._update_actions()

    @Slot(str, object)
    def _on_transcription_failed(
        self,
        message: str,
        debug: TranscriptionDebug | None,
    ) -> None:
        self._render_transcription_debug(debug, error=message)
        self._set_error(message)

    @Slot()
    def _on_thread_finished(self) -> None:
        if self._thread is not None:
            self._thread.deleteLater()
        self._worker = None
        self._thread = None
        if self.state is AppState.TRANSCRIBING:
            self.state = AppState.IDLE
        self._update_actions()

    @Slot(object)
    def _on_media_status_changed(self, status: object) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._release_audio_source()
            self.status_label.setText("Reprodução concluída.")
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self.status_label.setText("Não foi possível reproduzir o áudio capturado.")

    @Slot(object)
    def _on_playback_state_changed(self, state: object) -> None:
        if state == QMediaPlayer.PlaybackState.StoppedState and self._audio_buffer is not None:
            self._release_audio_source()

    @Slot(object, str)
    def _on_media_error(self, error: object, error_string: str = "") -> None:
        del error
        self.status_label.setText(
            error_string or "Não foi possível reproduzir o áudio capturado."
        )

    def _release_audio_source(self) -> None:
        if self._audio_buffer is not None:
            self._audio_buffer.close()
        self._audio_buffer = None
        self._audio_byte_array = None

    def _render_audio_debug(
        self,
        capture: AudioCapture,
        error: str | None = None,
    ) -> None:
        lines = [
            f"WAV: {len(capture.wav_bytes)} bytes",
            f"PCM: {len(capture.pcm_bytes)} bytes",
            f"Frames: {capture.frames}",
            f"Duração: {capture.duration_seconds:.3f} s",
            f"RMS: {capture.rms:.6f}",
            f"Pico: {capture.peak:.6f}",
            f"Forma de onda: {self._waveform(capture.pcm_bytes)}",
            "WAV aguardando envio ao Gemini." if not error else f"Erro: {error}",
        ]
        self.audio_debug.setPlainText("\n".join(lines))

    @staticmethod
    def _waveform(pcm_bytes: bytes, width: int = 48) -> str:
        if not pcm_bytes:
            return "(vazia)"
        try:
            samples = np.frombuffer(pcm_bytes, dtype=np.int16)
        except ValueError:
            return "(inválida)"
        if samples.size == 0:
            return "(vazia)"
        edges = np.linspace(0, samples.size, num=min(width, samples.size) + 1, dtype=int)
        levels: list[str] = []
        chars = "▁▂▃▄▅▆▇█"
        for start, end in zip(edges, edges[1:]):
            chunk = samples[start:end]
            amplitude = float(np.max(np.abs(chunk))) / 32768.0
            levels.append(chars[min(int(amplitude * len(chars)), len(chars) - 1)])
        return "".join(levels)

    def _render_transcription_debug(
        self,
        debug: TranscriptionDebug | None,
        *,
        text: str = "",
        error: str | None = None,
    ) -> None:
        if debug is not None:
            self.payload_debug.setPlainText(
                "\n".join(
                    (
                        f"Modelo: {debug.model}",
                        f"Prompt: {debug.prompt}",
                        f"MIME: {debug.audio_mime_type}",
                        f"Áudio: {debug.audio_bytes} bytes",
                        f"Base64: {debug.audio_base64_length} caracteres",
                        f"Preview Base64: {debug.audio_base64_preview}",
                    )
                )
            )
            response = debug.response_text or text
            self.return_debug.setPlainText(
                response if not (debug.error or error) else f"Erro: {debug.error or error}"
            )
        elif error:
            self.return_debug.setPlainText(f"Erro: {error}")

    @Slot()
    def copy_text(self) -> None:
        text = self.editor.toPlainText()
        if not text.strip():
            self.status_label.setText("Não há texto para copiar.")
            return
        QApplication.clipboard().setText(text)
        self.status_label.setText("Texto copiado.")

    @Slot()
    def clear_text(self) -> None:
        if not self.editor.toPlainText().strip():
            self.status_label.setText("Não há texto para apagar.")
            return
        self.editor.clear()
        self.status_label.setText("Texto apagado.")

    @Slot()
    def send_to_terminal(self) -> None:
        text = self.editor.toPlainText()
        try:
            self.terminal_bridge.send_text(
                text,
                lambda value: QApplication.clipboard().setText(value),
            )
        except TerminalBridgeError as exc:
            self.status_label.setText(str(exc))
            return
        self.status_label.setText("Texto colado no terminal ativo, sem pressionar Enter.")

    def _update_actions(self) -> None:
        busy = self.state is AppState.TRANSCRIBING
        recording = self.state is AppState.RECORDING
        audio_ready = self.state is AppState.AUDIO_READY
        has_text = bool(self.editor.toPlainText().strip())
        self.record_button.setEnabled(
            not busy and self.settings.has_api_key and self._microphone_available
        )
        self.record_button.setText("Parar e revisar áudio" if recording else "Gravar")
        self.play_audio_button.setEnabled(audio_ready)
        self.send_to_gemini_button.setEnabled(
            audio_ready and self.settings.has_api_key and self.transcriber is not None
        )
        self.microphone_combo.setEnabled(not busy and not recording)
        self.refresh_microphones_button.setEnabled(not busy and not recording)
        self.copy_button.setEnabled(not busy and has_text)
        self.clear_text_button.setEnabled(not busy and has_text)
        self.terminal_button.setEnabled(not busy and has_text)
        self.configure_key_button.setEnabled(not busy and not recording)
        self.debug_button.setEnabled(True)
        if not self.settings.has_api_key and self.state is AppState.IDLE:
            self.status_label.setText(self.settings.missing_api_key_message)

    def _set_error(self, message: str) -> None:
        self.state = AppState.ERROR
        self.status_label.setText(message)
        self._update_actions()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.recorder.is_recording():
            try:
                self.recorder.stop()
            except AudioRecorderError:
                pass
        try:
            self._media_player.stop()
        except Exception:
            pass
        set_source = getattr(self._media_player, "setSource", None)
        if set_source is not None:
            set_source(QUrl())
        self._release_audio_source()
        self._pending_capture = None
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(5000)
        event.accept()
