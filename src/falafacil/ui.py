from __future__ import annotations

from dataclasses import replace
from enum import Enum, auto
from typing import Callable, Sequence

import numpy as np
from PySide6.QtCore import (
    QByteArray,
    QBuffer,
    QEvent,
    QIODevice,
    QObject,
    QPoint,
    QRect,
    QRectF,
    QSize,
    QThread,
    QTimer,
    QUrl,
    Qt,
    Slot,
)
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QFont,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QShortcut,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractScrollArea,
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
    choose_input_device,
    list_input_devices,
)
from .config import Settings
from .credentials import ApiKeyStore, CredentialStoreError
from .storage import LocalStore, LocalStoreError, TokenTotals, TokenUsageRecord
from .shortcuts import (
    BACKEND_FAILURE_MESSAGE,
    MouseShortcutBridge,
    SESSION_UNAVAILABLE_MESSAGE,
    normalize_button_name,
)
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

def _format_mouse_button_label(button_name: str | None) -> str:
    if not button_name:
        return "Desativado"
    canonical = button_name.lower().strip()
    labels = {
        "left": "Botão esquerdo",
        "right": "Botão direito",
        "middle": "Botão do meio",
        "x1": "Botão 4 (x1)",
        "x2": "Botão 5 (x2)",
    }
    return labels.get(canonical, f"Botão {canonical}")

def _sanitize_mouse_shortcut_error(message: str | None) -> str:
    if message == SESSION_UNAVAILABLE_MESSAGE:
        return SESSION_UNAVAILABLE_MESSAGE
    return BACKEND_FAILURE_MESSAGE



class TokenUsageChart(QWidget):
    """Widget customizado para renderização gráfica do histórico de consumo de tokens."""

    SUCCESS_FILL_COLOR = QColor(46, 125, 50)
    SUCCESS_BORDER_COLOR = QColor(30, 90, 35)
    ERROR_FILL_COLOR = QColor(198, 40, 40)
    ERROR_BORDER_COLOR = QColor(150, 20, 20)
    UNKNOWN_FILL_COLOR = QColor(120, 120, 120)
    UNKNOWN_BORDER_COLOR = QColor(80, 80, 80)

    OUTCOME_COLORS: dict[str, tuple[QColor, QColor]] = {
        "success": (SUCCESS_FILL_COLOR, SUCCESS_BORDER_COLOR),
        "error": (ERROR_FILL_COLOR, ERROR_BORDER_COLOR),
        "unknown": (UNKNOWN_FILL_COLOR, UNKNOWN_BORDER_COLOR),
    }

    LEGEND_ITEMS: tuple[tuple[str, str, bool], ...] = (
        ("Sucesso", "success", False),
        ("Erro", "error", False),
        ("Indisponível", "unknown", True),
    )

    @classmethod
    def get_outcome_colors(cls, outcome: str | None) -> tuple[QColor, QColor]:
        normalized = (outcome or "").strip().lower()
        if normalized == "success":
            return cls.OUTCOME_COLORS["success"]
        if normalized == "error":
            return cls.OUTCOME_COLORS["error"]
        return cls.OUTCOME_COLORS["unknown"]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.records: tuple[TokenUsageRecord, ...] = ()
        self.status_message: str = ""
        self.last_rendered_legend_rects: dict[str, QRectF] = {}
        self.last_rendered_legend_text_rects: dict[str, QRectF] = {}
        self.last_rendered_bar_rects: tuple[QRectF, ...] = ()
        self.last_rendered_plot_rect: QRectF | None = None
        self.setMinimumHeight(140)

    @property
    def status(self) -> str:
        return self.status_message

    def set_history(
        self,
        records: Sequence[TokenUsageRecord] | None = None,
        status_message: str = "",
    ) -> None:
        self.records = tuple(records) if records is not None else ()
        self.status_message = status_message
        self.update()

    def set_records(
        self,
        records: Sequence[TokenUsageRecord] | None = None,
        status_message: str = "",
    ) -> None:
        self.set_history(records, status_message)

    def set_status_message(self, message: str) -> None:
        self.status_message = message
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(280, 180)

    def minimumSizeHint(self) -> QSize:
        return QSize(200, 140)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect()
        width = rect.width()
        height = rect.height()

        palette = self.palette()
        base_color = palette.color(palette.ColorRole.Base)
        text_color = palette.color(palette.ColorRole.Text)
        mid_color = palette.color(palette.ColorRole.Mid)

        painter.fillRect(rect, base_color)
        painter.setPen(QPen(mid_color, 1))
        painter.drawRect(0, 0, width - 1, height - 1)

        self.last_rendered_legend_rects = {}
        self.last_rendered_legend_text_rects = {}
        self.last_rendered_bar_rects = ()
        self.last_rendered_plot_rect = None

        if self.status_message:
            painter.setPen(QPen(text_color))
            painter.drawText(
                rect.adjusted(10, 10, -10, -10),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                self.status_message,
            )
            return

        if not self.records:
            painter.setPen(QPen(text_color))
            painter.drawText(
                rect.adjusted(10, 10, -10, -10),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                "Nenhum registro de consumo no histórico.",
            )
            return

        small_font = painter.font()
        if small_font.pointSize() > 0:
            small_font.setPointSize(max(8, small_font.pointSize() - 2))
        painter.setFont(small_font)
        fm = painter.fontMetrics()

        margin_left = 10.0
        margin_right = 10.0
        margin_top = 8.0
        line_spacing = 4.0
        icon_size = 10.0
        gap_icon_text = 4.0
        item_gap = 8.0

        line_height = max(icon_size, float(fm.height()))
        max_x = float(width - margin_right)

        legend_data: list[tuple[str, str, bool, float, float]] = []
        total_legend_w = 0.0
        for idx, (label, outcome_key, is_dashed) in enumerate(self.LEGEND_ITEMS):
            text_w = float(fm.horizontalAdvance(label))
            item_w = icon_size + gap_icon_text + text_w
            legend_data.append((label, outcome_key, is_dashed, text_w, item_w))
            total_legend_w += item_w
            if idx < len(self.LEGEND_ITEMS) - 1:
                total_legend_w += item_gap

        title = "Tokens / chamada"
        title_w = float(fm.horizontalAdvance(title))

        if margin_left + title_w + 14.0 + total_legend_w <= max_x:
            title_rect = QRectF(margin_left, margin_top, title_w + 2.0, line_height)
            painter.setPen(QPen(text_color))
            painter.drawText(
                title_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                title,
            )

            cur_x = max(float(margin_left + title_w + 14.0), max_x - total_legend_w)
            cur_y = float(margin_top)
            for label, outcome_key, is_dashed, text_w, item_w in legend_data:
                fill_col, border_col = self.OUTCOME_COLORS.get(
                    outcome_key, self.OUTCOME_COLORS["unknown"]
                )
                icon_rect = QRectF(
                    cur_x,
                    cur_y + (line_height - icon_size) / 2.0,
                    icon_size,
                    icon_size,
                )
                painter.fillRect(icon_rect, fill_col)
                pen_style = Qt.PenStyle.DashLine if is_dashed else Qt.PenStyle.SolidLine
                painter.setPen(QPen(border_col, 1, pen_style))
                painter.drawRect(icon_rect)

                text_rect = QRectF(
                    cur_x + icon_size + gap_icon_text,
                    cur_y,
                    text_w + 2.0,
                    line_height,
                )
                painter.setPen(QPen(text_color))
                painter.drawText(
                    text_rect,
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    label,
                )

                self.last_rendered_legend_rects[outcome_key] = icon_rect
                self.last_rendered_legend_text_rects[outcome_key] = text_rect
                cur_x += item_w + item_gap
            last_y = cur_y + line_height
        else:
            title_rect = QRectF(margin_left, margin_top, title_w + 2.0, line_height)
            painter.setPen(QPen(text_color))
            painter.drawText(
                title_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                title,
            )

            cur_x = float(margin_left)
            cur_y = float(margin_top + line_height + line_spacing)

            for label, outcome_key, is_dashed, text_w, item_w in legend_data:
                if cur_x > margin_left and cur_x + item_w > max_x:
                    cur_x = float(margin_left)
                    cur_y += line_height + line_spacing

                fill_col, border_col = self.OUTCOME_COLORS.get(
                    outcome_key, self.OUTCOME_COLORS["unknown"]
                )
                icon_rect = QRectF(
                    cur_x,
                    cur_y + (line_height - icon_size) / 2.0,
                    icon_size,
                    icon_size,
                )
                painter.fillRect(icon_rect, fill_col)
                pen_style = Qt.PenStyle.DashLine if is_dashed else Qt.PenStyle.SolidLine
                painter.setPen(QPen(border_col, 1, pen_style))
                painter.drawRect(icon_rect)

                text_rect = QRectF(
                    cur_x + icon_size + gap_icon_text,
                    cur_y,
                    text_w + 2.0,
                    line_height,
                )
                painter.setPen(QPen(text_color))
                painter.drawText(
                    text_rect,
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    label,
                )

                self.last_rendered_legend_rects[outcome_key] = icon_rect
                self.last_rendered_legend_text_rects[outcome_key] = text_rect
                cur_x += item_w + item_gap
            last_y = cur_y + line_height

        pad_left = 42
        pad_right = 10
        pad_top = int(last_y + 8.0)
        pad_bottom = 22

        plot_w = width - pad_left - pad_right
        plot_h = height - pad_top - pad_bottom

        if plot_w <= 10 or plot_h <= 10:
            return

        self.last_rendered_plot_rect = QRectF(pad_left, pad_top, plot_w, plot_h)

        valid_totals = [
            r.total_tokens
            for r in self.records
            if r.total_tokens is not None and r.total_tokens >= 0
        ]
        max_tokens = max(valid_totals) if valid_totals else 10
        if max_tokens <= 0:
            max_tokens = 10

        axis_pen = QPen(mid_color, 1)
        grid_pen = QPen(palette.color(palette.ColorRole.Midlight), 1, Qt.PenStyle.DotLine)

        y_zero = pad_top + plot_h
        painter.setPen(axis_pen)
        painter.drawLine(pad_left, int(y_zero), int(pad_left + plot_w), int(y_zero))
        painter.drawLine(pad_left, int(pad_top), pad_left, int(y_zero))

        painter.setPen(QPen(text_color))
        painter.drawText(
            2,
            int(y_zero - 6),
            pad_left - 6,
            12,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            "0",
        )

        y_max = pad_top
        painter.setPen(grid_pen)
        painter.drawLine(pad_left, int(y_max), int(pad_left + plot_w), int(y_max))
        painter.setPen(QPen(text_color))
        painter.drawText(
            2,
            int(y_max - 6),
            pad_left - 6,
            12,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f"{max_tokens}",
        )

        if plot_h >= 40:
            y_mid = pad_top + plot_h / 2.0
            painter.setPen(grid_pen)
            painter.drawLine(pad_left, int(y_mid), pad_left + plot_w, int(y_mid))
            painter.setPen(QPen(text_color))
            painter.drawText(
                2,
                int(y_mid - 6),
                pad_left - 6,
                12,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{max_tokens // 2}",
            )

        n = len(self.records)
        step = plot_w / float(n)
        bar_w = max(1.0, min(24.0, step * 0.75))
        offset = (step - bar_w) / 2.0

        rendered_bars: list[QRectF] = []
        for i, rec in enumerate(self.records):
            bar_x = pad_left + i * step + offset
            total = rec.total_tokens
            outcome = (rec.outcome or "").lower()

            fill_color, border_color = self.get_outcome_colors(outcome)

            if total is not None and total >= 0:
                bar_h = (float(total) / float(max_tokens)) * plot_h
                bar_h = max(1.0, min(plot_h, bar_h)) if total > 0 else 1.0
                bar_y = y_zero - bar_h

                bar_rect = QRectF(bar_x, bar_y, bar_w, bar_h)
                painter.fillRect(bar_rect, fill_color)
                painter.setPen(QPen(border_color, 1))
                painter.drawRect(bar_rect)
                rendered_bars.append(bar_rect)
            else:
                mark_h = min(8.0, plot_h / 4.0)
                mark_y = y_zero - mark_h
                mark_rect = QRectF(bar_x, mark_y, bar_w, mark_h)
                painter.fillRect(mark_rect, self.UNKNOWN_FILL_COLOR)
                painter.setPen(QPen(self.UNKNOWN_BORDER_COLOR, 1, Qt.PenStyle.DashLine))
                painter.drawRect(mark_rect)
                rendered_bars.append(mark_rect)

            if bar_w >= 14 and n <= 15:
                painter.setPen(QPen(text_color))
                painter.drawText(
                    QRectF(bar_x - 4, y_zero + 2, bar_w + 8, 16),
                    Qt.AlignmentFlag.AlignCenter,
                    f"#{rec.id}" if rec.id else f"#{i+1}",
                )

        self.last_rendered_bar_rects = tuple(rendered_bars)
class _ShortcutCaptureRelay(QObject):
    """Receptor seguro de eventos de captura no thread principal da UI."""

    def __init__(
        self,
        dialog: QDialog,
        bridge: MouseShortcutBridge | None = None,
        capture_generation: int | None = None,
        disable_button: QPushButton | None = None,
        cancel_button: QPushButton | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._dialog = dialog
        self._bridge = bridge
        self._capture_generation = capture_generation
        self._disable_button = disable_button
        self._cancel_button = cancel_button
        self._is_terminal = False
        self._pressed_control: str | None = None
        self.captured_button: str | None = None
        self.confirmed = False
        self.is_disable = False
        self.handled_thread: QThread | None = None

    def _is_control_at_pos(self, button: QPushButton | None, pos: QPoint) -> bool:
        if button is None or not button.isVisible() or not button.isEnabled():
            return False
        global_origin = button.mapToGlobal(QPoint(0, 0))
        rect = QRect(global_origin, button.size())
        if not rect.contains(pos):
            return False
        widget_at_pos = QApplication.widgetAt(pos)
        if widget_at_pos is not None:
            w: QWidget | None = widget_at_pos
            while w is not None:
                if w is button:
                    return True
                w = w.parentWidget()
            return False
        return QApplication.platformName() == "offscreen"

    @Slot(int, str, int, int)
    def on_button_captured_event(self, gen: int, button_name: str, x: int, y: int) -> None:
        if self._is_terminal:
            return
        if self._capture_generation is not None and gen != self._capture_generation:
            return
        if self._bridge is not None and getattr(self._bridge, "generation", None) != gen:
            return
        canonical = normalize_button_name(button_name)
        if canonical is None:
            return
        if canonical == "left":
            pos = QPoint(x, y)
            if self._is_control_at_pos(self._cancel_button, pos) or self._is_control_at_pos(self._disable_button, pos):
                return
            if self._pressed_control is not None:
                return
        if self._capture_generation is None:
            self._capture_generation = gen
        self._is_terminal = True
        self.handled_thread = QThread.currentThread()
        self.captured_button = canonical
        self.confirmed = True
        self._dialog.accept()
    def on_disable_pressed(self) -> None:
        if self._is_terminal:
            return
        self._pressed_control = "disable"
        if self._bridge is not None:
            self._bridge.stop()

    @Slot()
    def on_disable_released(self) -> None:
        if self._is_terminal:
            return
        QTimer.singleShot(0, self._cleanup_unclicked_control)

    @Slot()
    def on_disable_clicked(self) -> None:
        if self._is_terminal:
            return
        self._is_terminal = True
        self._pressed_control = None
        if self._bridge is not None:
            self._bridge.stop()
        self.handled_thread = QThread.currentThread()
        self.is_disable = True
        self.confirmed = True
        self._dialog.accept()

    @Slot()
    def on_disable(self) -> None:
        self.on_disable_clicked()

    @Slot()
    def on_cancel_pressed(self) -> None:
        if self._is_terminal:
            return
        self._pressed_control = "cancel"
        if self._bridge is not None:
            self._bridge.stop()

    @Slot()
    def on_cancel_released(self) -> None:
        if self._is_terminal:
            return
        QTimer.singleShot(0, self._cleanup_unclicked_control)

    @Slot()
    def on_cancel_clicked(self) -> None:
        if self._is_terminal:
            return
        self._is_terminal = True
        self._pressed_control = None
        if self._bridge is not None:
            self._bridge.stop()
        self.handled_thread = QThread.currentThread()
        self.confirmed = False
        self._dialog.reject()

    @Slot()
    def on_cancel(self) -> None:
        self.on_cancel_clicked()

    @Slot()
    def _cleanup_unclicked_control(self) -> None:
        if self._is_terminal:
            return
        if self._pressed_control is not None:
            self._pressed_control = None
            if self._bridge is not None and not self._is_terminal:
                started = self._bridge.begin_capture()
                if started:
                    self._capture_generation = getattr(self._bridge, "generation", None)

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
        local_store: LocalStore | None = None,
        mouse_shortcut_bridge: MouseShortcutBridge | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.local_store = local_store
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

        self.mouse_shortcut_bridge = mouse_shortcut_bridge or MouseShortcutBridge(parent=self)
        self._active_mouse_button: str | None = None
        self._is_configuring_shortcut = False
        self._dialog_open = False
        self._is_closing = False
        self._local_press_record: tuple[int, QRect | None, QWidget | None] | None = None
        self._startup_mouse_diagnostic: str | None = None
        self.mouse_shortcut_bridge._activated_event.connect(
            self._on_mouse_shortcut_activated_event
        )
        self.mouse_shortcut_bridge.failed.connect(self._on_mouse_shortcut_failed)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self.setWindowTitle("FalaFácil")
        self.resize(760, 520)
        self._media_player, self._audio_output = self._media_player_factory(self)
        self._connect_media_signals()
        self._build_ui()
        self._restore_mouse_shortcut()
        self._refresh_microphones()
        self._refresh_token_usage_chart()
        self._update_actions()
        if self._startup_mouse_diagnostic is not None:
            self.status_label.setText(self._startup_mouse_diagnostic)
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

        self.configure_shortcut_button = QPushButton("Configurar atalho de gravação", self)
        self.configure_shortcut_button.setToolTip(
            "Configura um botão do mouse como atalho global de gravação"
        )
        self.configure_shortcut_button.pressed.connect(self._on_configure_shortcut_pressed)
        self.configure_shortcut_button.released.connect(self._on_configure_shortcut_released)
        self.configure_shortcut_button.clicked.connect(self._configure_recording_shortcut)
        buttons.addWidget(self.configure_shortcut_button)

        self.shortcut_indicator_label = QLabel(self)
        buttons.addWidget(self.shortcut_indicator_label)

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
        self._install_interactive_event_filters()

    def _build_debug_dock(self) -> None:
        self.debug_dock = QDockWidget("Debug da captura e transcrição", self)
        self.debug_dock.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea)
        debug_widget = QWidget(self.debug_dock)
        debug_layout = QVBoxLayout(debug_widget)
        self.audio_debug = self._debug_text_block(debug_layout, "Áudio recebido")
        self.payload_debug = self._debug_text_block(debug_layout, "Payload enviado ao Gemini")
        self.return_debug = self._debug_text_block(debug_layout, "Retorno")
        self.usage_debug = self._debug_text_block(debug_layout, "Consumo da API Gemini")
        self.usage_chart = self._debug_chart_block(debug_layout, "Gráfico de consumo de tokens")
        self.debug_dock.setWidget(debug_widget)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.debug_dock)
        self.debug_dock.visibilityChanged.connect(self._sync_debug_button)
        self.debug_dock.setVisible(False)
    def _install_interactive_event_filters(self) -> None:
        candidate_widgets: list[QWidget] = []
        for attr_name in (
            "configure_shortcut_button",
            "record_button",
            "play_audio_button",
            "send_to_gemini_button",
            "send_button",
            "copy_button",
            "clear_text_button",
            "clear_button",
            "terminal_button",
            "configure_key_button",
            "api_key_button",
            "refresh_microphones_button",
            "detect_button",
            "debug_button",
            "microphone_combo",
            "editor",
        ):
            widget = getattr(self, attr_name, None)
            if isinstance(widget, QWidget) and widget not in candidate_widgets:
                candidate_widgets.append(widget)

        for widget_type in (QAbstractButton, QComboBox, QLineEdit, QPlainTextEdit, QAbstractScrollArea):
            for child in self.findChildren(widget_type):
                if isinstance(child, QWidget) and child not in candidate_widgets:
                    candidate_widgets.append(child)

        for widget in candidate_widgets:
            widget.installEventFilter(self)
            if isinstance(widget, QAbstractScrollArea):
                viewport = widget.viewport()
                if viewport is not None:
                    viewport.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if getattr(self, "_is_closing", False):
            return super().eventFilter(watched, event)
        if event is None:
            return super().eventFilter(watched, event)
        try:
            event_type = event.type()
        except Exception:
            return super().eventFilter(watched, event)

        if event_type in (QEvent.Type.Close, QEvent.Close):
            self._local_press_record = None
        elif event_type in (QEvent.Type.MouseButtonPress, QEvent.MouseButtonPress):
            if isinstance(event, QMouseEvent) and event.button() == Qt.MouseButton.LeftButton:
                active_button = getattr(self, "_active_mouse_button", None)
                if normalize_button_name(active_button) == "left":
                    if isinstance(watched, QWidget):
                        w: QWidget | None = watched
                        belongs_to_window = False
                        while w is not None:
                            if w is self:
                                belongs_to_window = True
                                break
                            parent_widget = getattr(w, "parentWidget", None)
                            next_w = parent_widget() if callable(parent_widget) else None
                            if next_w is None:
                                parent_obj = getattr(w, "parent", None)
                                parent_val = parent_obj() if callable(parent_obj) else None
                                if isinstance(parent_val, QWidget):
                                    next_w = parent_val
                                elif parent_val is self:
                                    belongs_to_window = True
                                    break
                            w = next_w

                        if belongs_to_window:
                            control: QWidget | None = watched
                            if not isinstance(
                                control,
                                (
                                    QAbstractButton,
                                    QComboBox,
                                    QLineEdit,
                                    QPlainTextEdit,
                                    QDialogButtonBox,
                                    QAbstractScrollArea,
                                ),
                            ):
                                parent_fn = getattr(watched, "parentWidget", None)
                                parent = parent_fn() if callable(parent_fn) else None
                                if isinstance(
                                    parent,
                                    (
                                        QAbstractButton,
                                        QComboBox,
                                        QLineEdit,
                                        QPlainTextEdit,
                                        QDialogButtonBox,
                                        QAbstractScrollArea,
                                    ),
                                ):
                                    control = parent
                            if isinstance(
                                control,
                                (
                                    QAbstractButton,
                                    QComboBox,
                                    QLineEdit,
                                    QPlainTextEdit,
                                    QDialogButtonBox,
                                    QAbstractScrollArea,
                                ),
                            ):
                                is_enabled = getattr(control, "isEnabled", None)
                                enabled = is_enabled() if callable(is_enabled) else True
                                is_visible = getattr(control, "isVisible", None)
                                visible = is_visible() if callable(is_visible) else True
                                if enabled and visible:
                                    current_gen = getattr(self.mouse_shortcut_bridge, "generation", 0)
                                    origin = control.mapToGlobal(QPoint(0, 0))
                                    rect = QRect(origin, control.size())
                                    self._local_press_record = (current_gen, rect, control)
                        else:
                            self._local_press_record = None
        return super().eventFilter(watched, event)
    def _debug_text_block(self, layout: QVBoxLayout, title: str) -> QPlainTextEdit:
        layout.addWidget(QLabel(title, self))
        editor = QPlainTextEdit(self)
        editor.setReadOnly(True)
        editor.setMaximumBlockCount(200)
        layout.addWidget(editor, stretch=1)
        return editor

    def _debug_chart_block(self, layout: QVBoxLayout, title: str) -> TokenUsageChart:
        layout.addWidget(QLabel(title, self))
        chart = TokenUsageChart(self)
        layout.addWidget(chart, stretch=1)
        return chart

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
        current_identity: str | None = None
        current_item_idx = self.microphone_combo.currentIndex()
        if current_item_idx >= 0:
            item_device = self.microphone_combo.itemData(
                current_item_idx, Qt.ItemDataRole.UserRole + 1
            )
            if isinstance(item_device, AudioDevice):
                current_identity = item_device.identity
            elif isinstance(item_device, str):
                current_identity = item_device

        remembered_identity: str | None = None
        if self.local_store is not None:
            try:
                remembered_identity = self.local_store.get_last_microphone_identity()
            except Exception:
                remembered_identity = None

        provider_error = False
        self._microphone_refreshing = True
        try:
            devices = tuple(self._microphone_provider())
        except Exception:
            provider_error = True
            self.microphone_combo.clear()
            self._microphone_available = False
            self.status_label.setText("Não foi possível detectar microfones.")
        else:
            self.microphone_combo.clear()
            chosen_device = choose_input_device(
                devices,
                remembered_identity=remembered_identity,
                current_identity=current_identity,
            )
            target_index = -1
            for i, device in enumerate(devices):
                suffix = " (padrão)" if device.is_default else ""
                self.microphone_combo.addItem(
                    f"{device.name} (índice {device.index}){suffix}",
                    device.index,
                )
                self.microphone_combo.setItemData(
                    i, device, Qt.ItemDataRole.UserRole + 1
                )
                if chosen_device is not None and device == chosen_device and target_index < 0:
                    target_index = i

            self._microphone_available = bool(devices)
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

    def _update_shortcut_indicator(self) -> None:
        button_text = _format_mouse_button_label(self._active_mouse_button)
        self.shortcut_indicator_label.setText(f"Atalho do mouse: {button_text}")
        self.configure_shortcut_button.setToolTip(
            f"Configura um botão do mouse como atalho global de gravação (atual: {button_text})"
        )

    def _restore_or_clear_mouse_button(self, button_name: str | None) -> bool:
        """Tenta ativar um botão de atalho ou limpa o estado ativo em caso de falha.

        Retorna True se o botão foi ativado com sucesso ou se button_name é None.
        Em caso de falha de inicialização, limpa _active_mouse_button e o indicador,
        mantendo a preferência persistida inalterada e exibindo status sanitizado.
        """
        if button_name is None:
            self.mouse_shortcut_bridge.stop()
            self._active_mouse_button = None
            self._update_shortcut_indicator()
            return True

        canonical = normalize_button_name(button_name)
        if canonical is None:
            self._active_mouse_button = None
            self._update_shortcut_indicator()
            error_msg = _sanitize_mouse_shortcut_error(
                self.mouse_shortcut_bridge.last_error
            )
            self.status_label.setText(error_msg)
            return False

        success = self.mouse_shortcut_bridge.start(canonical)
        if success:
            self._active_mouse_button = canonical
            self._update_shortcut_indicator()
            return True

        self._active_mouse_button = None
        self._update_shortcut_indicator()
        error_msg = _sanitize_mouse_shortcut_error(
            self.mouse_shortcut_bridge.last_error
        )
        self.status_label.setText(error_msg)
        return False

    def _restore_mouse_shortcut(self) -> None:
        if self.local_store is None:
            self._update_shortcut_indicator()
            return
        saved_button: str | None = None
        try:
            saved_button = self.local_store.get_recording_mouse_button()
        except Exception:
            self._active_mouse_button = None
            self._update_shortcut_indicator()
            self._startup_mouse_diagnostic = (
                "Não foi possível ler preferência de atalho do mouse."
            )
            self.status_label.setText(self._startup_mouse_diagnostic)
            return

        if saved_button:
            if not self._restore_or_clear_mouse_button(saved_button):
                self._startup_mouse_diagnostic = self.status_label.text()
        else:
            self._active_mouse_button = None
            self._update_shortcut_indicator()
    def _acquire_recording_mouse_button(self) -> tuple[str | None, bool]:
        dialog = QDialog(self)
        dialog.setWindowTitle("Configurar atalho de gravação")
        layout = QVBoxLayout(dialog)

        explanation = QLabel(
            "Pressione o botão do mouse que deseja usar como atalho global de gravação.",
            dialog,
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        status_prompt = QLabel("Aguardando clique do mouse…", dialog)
        layout.addWidget(status_prompt)

        button_row = QHBoxLayout()
        disable_button = QPushButton("Desativar atalho", dialog)
        cancel_button = QPushButton("Cancelar", dialog)
        button_row.addWidget(disable_button)
        button_row.addWidget(cancel_button)
        layout.addLayout(button_row)

        relay = _ShortcutCaptureRelay(
            dialog,
            bridge=self.mouse_shortcut_bridge,
            capture_generation=None,
            disable_button=disable_button,
            cancel_button=cancel_button,
            parent=dialog,
        )
        disable_button.pressed.connect(relay.on_disable_pressed)
        disable_button.released.connect(relay.on_disable_released)
        disable_button.clicked.connect(relay.on_disable)
        cancel_button.pressed.connect(relay.on_cancel_pressed)
        cancel_button.released.connect(relay.on_cancel_released)
        cancel_button.clicked.connect(relay.on_cancel)
        self.mouse_shortcut_bridge._button_captured_event.connect(
            relay.on_button_captured_event, Qt.ConnectionType.QueuedConnection
        )

        self.mouse_shortcut_bridge.stop()
        started = self.mouse_shortcut_bridge.begin_capture()
        if not started:
            try:
                self.mouse_shortcut_bridge._button_captured_event.disconnect(
                    relay.on_button_captured_event
                )
            except Exception:
                pass
            return None, False
        capture_gen = getattr(self.mouse_shortcut_bridge, "generation", None)
        if relay._capture_generation is None:
            relay._capture_generation = capture_gen

        try:
            dialog.exec()
        finally:
            try:
                self.mouse_shortcut_bridge._button_captured_event.disconnect(
                    relay.on_button_captured_event
                )
            except Exception:
                pass
            self.mouse_shortcut_bridge.stop()
        if relay.is_disable:
            return None, True
        if relay.confirmed and relay.captured_button is not None:
            return relay.captured_button, True
        return None, False

    @Slot()
    def _on_configure_shortcut_pressed(self) -> None:
        if getattr(self, "_is_closing", False) or getattr(self, "state", None) in (AppState.RECORDING, AppState.TRANSCRIBING):
            return
        self._is_configuring_shortcut = True
        self._local_press_record = None
        self.mouse_shortcut_bridge.stop()
    @Slot()
    def _on_configure_shortcut_released(self) -> None:
        if getattr(self, "_is_closing", False) or getattr(self, "_dialog_open", False):
            return
        QTimer.singleShot(0, self._cleanup_unclicked_configure_shortcut)

    @Slot()
    def _cleanup_unclicked_configure_shortcut(self) -> None:
        if getattr(self, "_is_closing", False) or getattr(self, "_dialog_open", False):
            return
        if self._is_configuring_shortcut:
            self._is_configuring_shortcut = False
            if self._active_mouse_button is not None:
                self._restore_or_clear_mouse_button(self._active_mouse_button)

    @Slot()
    def _configure_recording_shortcut(self) -> None:
        if getattr(self, "_is_closing", False) or getattr(self, "state", None) in (AppState.RECORDING, AppState.TRANSCRIBING):
            return

        self._is_configuring_shortcut = True
        self._dialog_open = True
        self.mouse_shortcut_bridge.stop()
        try:
            previous_button = self._active_mouse_button
            button_name, accepted = self._acquire_recording_mouse_button()
            if not accepted:
                if previous_button is not None:
                    self._restore_or_clear_mouse_button(previous_button)
                return

            if button_name is None:
                self.mouse_shortcut_bridge.stop()
                self._active_mouse_button = None
                self._update_shortcut_indicator()

                persistence_failed = False
                if self.local_store is not None:
                    try:
                        self.local_store.clear_recording_mouse_button()
                    except Exception:
                        persistence_failed = True

                if persistence_failed:
                    self.status_label.setText(
                        "Atalho desativado nesta sessão; não foi possível persistir."
                    )
                else:
                    self.status_label.setText("Atalho de gravação desativado.")
                return

            started = self.mouse_shortcut_bridge.start(button_name)
            if not started:
                if previous_button is not None:
                    if self._restore_or_clear_mouse_button(previous_button):
                        error_msg = _sanitize_mouse_shortcut_error(
                            self.mouse_shortcut_bridge.last_error
                        )
                        self.status_label.setText(error_msg)
                else:
                    self._active_mouse_button = None
                    self._update_shortcut_indicator()
                    error_msg = _sanitize_mouse_shortcut_error(
                        self.mouse_shortcut_bridge.last_error
                    )
                    self.status_label.setText(error_msg)
                return

            self._active_mouse_button = button_name
            self._update_shortcut_indicator()

            persistence_unavailable = self.local_store is None
            persistence_failed = False
            if self.local_store is not None:
                try:
                    self.local_store.save_recording_mouse_button(button_name)
                except Exception:
                    persistence_failed = True

            if persistence_unavailable or persistence_failed:
                self.status_label.setText(
                    "Atalho configurado nesta sessão; não foi possível persistir."
                )
            else:
                self.status_label.setText(
                    f"Atalho de gravação configurado para {_format_mouse_button_label(button_name)}."
                )
        finally:
            self._dialog_open = False
            self._is_configuring_shortcut = False
            self._local_press_record = None
    @Slot(int, int, int)
    def _on_mouse_shortcut_activated_event(self, gen: int, x: int, y: int) -> None:
        if getattr(self, "_is_closing", False) or getattr(self, "_is_configuring_shortcut", False) or getattr(self, "_dialog_open", False):
            return

        current_gen = getattr(self.mouse_shortcut_bridge, "generation", None)
        if current_gen is None or gen != current_gen:
            return

        if normalize_button_name(self._active_mouse_button) == "left":
            pos = QPoint(x, y)
            if self._local_press_record is not None:
                rec_gen, rec_rect, rec_control = self._local_press_record
                self._local_press_record = None
                if rec_gen == gen:
                    if rec_rect is not None and rec_rect.contains(pos):
                        return
                    if rec_control is not None:
                        is_enabled = getattr(rec_control, "isEnabled", None)
                        enabled = is_enabled() if callable(is_enabled) else True
                        is_visible = getattr(rec_control, "isVisible", None)
                        visible = is_visible() if callable(is_visible) else True
                        if enabled and visible:
                            widget_at_pos = QApplication.widgetAt(pos)
                            if widget_at_pos is not None:
                                w: QWidget | None = widget_at_pos
                                while w is not None:
                                    if w is rec_control:
                                        return
                                    w = w.parentWidget()

            widget_at_pos = QApplication.widgetAt(pos)
            if widget_at_pos is not None:
                w = widget_at_pos
                belongs_to_window = False
                while w is not None:
                    if w is self or (isinstance(w, QDialog) and w.parent() is self):
                        belongs_to_window = True
                        break
                    w = w.parentWidget()
                if belongs_to_window:
                    w = widget_at_pos
                    while w is not None and w is not self:
                        if isinstance(
                            w,
                            (
                                QAbstractButton,
                                QComboBox,
                                QLineEdit,
                                QPlainTextEdit,
                                QDialogButtonBox,
                                QAbstractScrollArea,
                            ),
                        ):
                            if w.isEnabled() and w.isVisible():
                                return
                            break
                        w = w.parentWidget()

            if QApplication.platformName() == "offscreen":
                candidate_widgets: list[QWidget] = []
                for attr_name in (
                    "configure_shortcut_button",
                    "record_button",
                    "play_audio_button",
                    "send_to_gemini_button",
                    "send_button",
                    "copy_button",
                    "clear_text_button",
                    "clear_button",
                    "terminal_button",
                    "configure_key_button",
                    "api_key_button",
                    "refresh_microphones_button",
                    "detect_button",
                    "debug_button",
                    "microphone_combo",
                    "editor",
                ):
                    widget = getattr(self, attr_name, None)
                    if isinstance(widget, QWidget) and widget not in candidate_widgets:
                        candidate_widgets.append(widget)

                for widget_type in (
                    QAbstractButton,
                    QComboBox,
                    QLineEdit,
                    QPlainTextEdit,
                    QDialogButtonBox,
                    QAbstractScrollArea,
                ):
                    for child in self.findChildren(widget_type):
                        if isinstance(child, QWidget) and child not in candidate_widgets:
                            candidate_widgets.append(child)

                for child in candidate_widgets:
                    if not (child.isVisible() and child.isEnabled()):
                        continue
                    origin = child.mapToGlobal(QPoint(0, 0))
                    rect = QRect(origin, child.size())
                    if rect.contains(pos):
                        return
        else:
            self._local_press_record = None

        btn = getattr(self, "configure_shortcut_button", None)
        if btn is not None and btn.isDown():
            return

        self._toggle_recording()

    @Slot(str)
    def _on_mouse_shortcut_failed(self, message: str) -> None:
        if getattr(self, "_is_closing", False):
            return
        self.status_label.setText(_sanitize_mouse_shortcut_error(message))

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
        self.usage_debug.clear()
        try:
            self.recorder.start()
        except AudioRecorderError as exc:
            self._set_error(str(exc))
            return

        current_idx = self.microphone_combo.currentIndex()
        selected_device = self.microphone_combo.itemData(
            current_idx, Qt.ItemDataRole.UserRole + 1
        )
        selected_identity: str | None = None
        if isinstance(selected_device, AudioDevice):
            selected_identity = selected_device.identity
        elif isinstance(selected_device, str):
            selected_identity = selected_device

        persistence_failed = False
        if selected_identity and self.local_store is not None:
            try:
                self.local_store.save_last_microphone_identity(selected_identity)
            except Exception:
                persistence_failed = True

        self.state = AppState.RECORDING
        self.record_button.setText("Parar e revisar áudio")
        if persistence_failed:
            self.status_label.setText(
                "Gravando… não foi possível atualizar a memória do microfone."
            )
        else:
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
        except Exception:
            self.status_label.setText("Não foi possível reproduzir o áudio.")
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
        self._record_and_render_usage(debug, "success")
        self.state = AppState.READY
        self.status_label.setText("Transcrição pronta. Revise, copie ou envie ao terminal.")

    @Slot(str, object)
    def _on_transcription_failed(
        self,
        message: str,
        debug: TranscriptionDebug | None,
    ) -> None:
        self._render_transcription_debug(debug, error=message)
        self._record_and_render_usage(debug, "error")
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
        del error, error_string
        self.status_label.setText("Não foi possível reproduzir o áudio capturado.")

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
                    )
                )
            )
            response = debug.response_text or text
            self.return_debug.setPlainText(
                response if not (debug.error or error) else f"Erro: {debug.error or error}"
            )
        elif error:
            self.return_debug.setPlainText(f"Erro: {error}")

    def _record_and_render_usage(
        self,
        debug: TranscriptionDebug | None,
        outcome: str,
    ) -> None:
        if debug is None or debug.usage is None:
            self.usage_debug.setPlainText("metadados de consumo não fornecidos")
            return

        usage = debug.usage
        totals: TokenTotals | None = None
        if self.local_store is not None:
            try:
                self.local_store.record_token_usage(
                    debug.model, usage, outcome
                )
                totals = self.local_store.get_token_totals()
            except Exception:
                self.usage_debug.setPlainText(
                    "Não foi possível persistir o consumo de tokens."
                )
                self.usage_chart.set_history(
                    self.usage_chart.records,
                    status_message="Não foi possível persistir o consumo de tokens.",
                )
                return

        self._refresh_token_usage_chart()

        def _format_token_count(value: int | None) -> str:
            if value is None:
                return "indisponível"
            return str(value)

        lines = [
            "Chamada atual:",
            f"Modelo: {debug.model}",
            f"Entrada: {_format_token_count(usage.input_tokens)}",
            f"Saída: {_format_token_count(usage.output_tokens)}",
            f"Pensamento: {_format_token_count(usage.thought_tokens)}",
            f"Cache: {_format_token_count(usage.cached_tokens)}",
            f"Ferramentas: {_format_token_count(usage.tool_use_tokens)}",
            f"Total: {_format_token_count(usage.total_tokens)}",
        ]
        if totals is not None:
            lines.extend(
                [
                    "",
                    "Total acumulado:",
                    f"Entrada: {_format_token_count(totals.input_tokens)}",
                    f"Saída: {_format_token_count(totals.output_tokens)}",
                    f"Pensamento: {_format_token_count(totals.thought_tokens)}",
                    f"Cache: {_format_token_count(totals.cached_tokens)}",
                    f"Ferramentas: {_format_token_count(totals.tool_use_tokens)}",
                    f"Total: {_format_token_count(totals.total_tokens)}",
                ]
            )
        elif self.local_store is None:
            lines.extend(
                [
                    "",
                    "Total acumulado: indisponível",
                ]
            )

        self.usage_debug.setPlainText("\n".join(lines))

    def _refresh_token_usage_chart(self) -> None:
        if self.local_store is None:
            self.usage_chart.set_history(
                (), status_message="Histórico local indisponível."
            )
            return

        try:
            history = self.local_store.get_token_usage_history()
        except Exception:
            self.usage_chart.set_history(
                (), status_message="Não foi possível carregar o histórico de consumo."
            )
            return
        if not history:
            self.usage_chart.set_history(
                (), status_message="Nenhum registro de consumo no histórico."
            )
        else:
            self.usage_chart.set_history(history, status_message="")
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
        self.configure_shortcut_button.setEnabled(not busy and not recording)
        self.debug_button.setEnabled(True)
        if not self.settings.has_api_key and self.state is AppState.IDLE:
            self.status_label.setText(self.settings.missing_api_key_message)

    def _set_error(self, message: str) -> None:
        self.state = AppState.ERROR
        self.status_label.setText(message)
        self._update_actions()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._is_closing = True
        self._local_press_record = None
        app = QApplication.instance()
        if app is not None:
            try:
                app.removeEventFilter(self)
            except Exception:
                pass
        try:
            self.mouse_shortcut_bridge.stop()
        except Exception:
            pass
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
        thread = self._thread
        if thread is not None and thread.isRunning():
            thread.quit()
            if not thread.wait(5000):
                thread.wait()
        if self.local_store is not None:
            try:
                self.local_store.close()
            except Exception:
                pass
        event.accept()
