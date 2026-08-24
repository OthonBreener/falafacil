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
    QIcon,
    QKeySequence,
    QPainter,
    QPaintEvent,
    QPen,
    QShortcut,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStyle,
    QTabWidget,
    QToolButton,
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
from .shortcut_install import ShortcutServiceInstaller
from .shortcuts import (
    BACKEND_FAILURE_MESSAGE,
    InputShortcutBridge,
    normalize_keyboard_shortcut,
    normalize_mouse_button_name,
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
    labels = {
        "middle": "Botão do meio",
        "x1": "Botão 4 (x1)",
        "x2": "Botão 5 (x2)",
        "forward": "Avançar",
        "back": "Voltar",
        "task": "Tarefa",
    }
    canonical = normalize_mouse_button_name(button_name)
    return labels.get(canonical or "", "Desativado")


def _format_keyboard_shortcut_label(shortcut: str | None) -> str:
    canonical = normalize_keyboard_shortcut(shortcut) if shortcut else None
    return canonical.upper() if canonical else "Desativado"



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
        input_shortcut_bridge: InputShortcutBridge | None = None,
        shortcut_service_installer: ShortcutServiceInstaller | None = None,
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
        self._is_closing = False

        self.input_shortcut_bridge = input_shortcut_bridge or InputShortcutBridge(parent=self)
        self.shortcut_service_installer = (
            shortcut_service_installer or ShortcutServiceInstaller(parent=self)
        )
        self._active_mouse_button: str | None = None
        self._active_keyboard_shortcut: str | None = None
        self._pending_bindings: dict[str, tuple[int, str, bool]] = {}
        self._pending_stops: dict[str, int] = {}
        self._capture_dialog: QDialog | None = None
        self._capture_status_label: QLabel | None = None
        self._capture_kind: str | None = None
        self._capture_generation: int | None = None
        self._captured_shortcut: str | None = None
        self._capture_disable = False
        self._pending_authorization_kind: str | None = None
        self._startup_shortcut_diagnostic: str | None = None

        self.input_shortcut_bridge.mouse_binding_ready.connect(
            self._on_mouse_binding_ready
        )
        self.input_shortcut_bridge.keyboard_binding_ready.connect(
            self._on_keyboard_binding_ready
        )
        self.input_shortcut_bridge.mouse_activated.connect(
            self._on_mouse_shortcut_activated
        )
        self.input_shortcut_bridge.keyboard_activated.connect(
            self._on_keyboard_shortcut_activated
        )
        self.input_shortcut_bridge.mouse_captured.connect(
            self._on_mouse_shortcut_captured
        )
        self.input_shortcut_bridge.keyboard_captured.connect(
            self._on_keyboard_shortcut_captured
        )
        self.input_shortcut_bridge.stopped.connect(self._on_shortcut_stopped)
        self.input_shortcut_bridge.failed.connect(self._on_shortcut_failed)
        self.input_shortcut_bridge.ready_changed.connect(
            self._on_shortcut_service_ready
        )
        self.shortcut_service_installer.finished.connect(
            self._on_shortcut_install_finished
        )

        self.setWindowTitle("FalaFácil")
        self.resize(1120, 700)
        self.setMinimumSize(760, 560)
        self._media_player, self._audio_output = self._media_player_factory(self)
        self._connect_media_signals()
        self._build_ui()
        self._restore_shortcuts()
        self._refresh_microphones()
        self._refresh_token_usage_chart()
        self._update_actions()
        if self._startup_shortcut_diagnostic is not None:
            self.status_label.setText(self._startup_shortcut_diagnostic)
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
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        self._settings_dialog: QDialog | None = None

        header = QHBoxLayout()
        title = QLabel("FalaFácil", central)
        title_font = QFont(title.font())
        title_font.setBold(True)
        if title_font.pointSize() > 0:
            title_font.setPointSize(title_font.pointSize() + 4)
        title.setFont(title_font)
        header.addWidget(title)
        header.addStretch(1)
        self.shortcut_indicator_label = QLabel(central)
        header.addWidget(self.shortcut_indicator_label)

        self.settings_button = QToolButton(central)
        self.settings_button.setFixedSize(36, 36)
        settings_icon = QIcon.fromTheme("preferences-system")
        if settings_icon.isNull():
            self.settings_button.setText("⚙")
        else:
            self.settings_button.setIcon(settings_icon)
        self.settings_button.setToolTip("Configurações")
        self.settings_button.setAccessibleName("Configurações")
        self.settings_button.clicked.connect(self._open_settings_dialog)
        header.addWidget(self.settings_button)

        self.fullscreen_button = QToolButton(central)
        self.fullscreen_button.setFixedSize(36, 36)
        self.fullscreen_button.clicked.connect(self._toggle_fullscreen)
        header.addWidget(self.fullscreen_button)
        layout.addLayout(header)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal, central)
        self.main_splitter.setChildrenCollapsible(False)

        left_panel = QWidget(self.main_splitter)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(12)
        microphone_row = QHBoxLayout()
        microphone_row.addWidget(QLabel("Microfone", left_panel))
        self.microphone_combo = QComboBox(left_panel)
        self.microphone_combo.setEditable(False)
        self.microphone_combo.currentIndexChanged.connect(self._select_microphone)
        microphone_row.addWidget(self.microphone_combo, stretch=1)
        self.refresh_microphones_button = QPushButton(
            "Detectar microfones", left_panel
        )
        self.refresh_microphones_button.clicked.connect(self._refresh_microphones)
        microphone_row.addWidget(self.refresh_microphones_button)
        left_layout.addLayout(microphone_row)

        left_layout.addWidget(QLabel("Transcrição", left_panel))
        self.editor = QPlainTextEdit(left_panel)
        self.editor.setPlaceholderText(
            "A transcrição aparecerá aqui. Você também pode corrigir o texto antes de copiar."
        )
        self.editor.setTabChangesFocus(False)
        self.editor.setMinimumHeight(120)
        self.editor.setMaximumHeight(190)
        self.editor.textChanged.connect(self._update_actions)
        left_layout.addWidget(self.editor)

        review_actions = QHBoxLayout()
        self.record_button = QPushButton("Gravar", left_panel)
        self.record_button.setToolTip("Começa ou para a gravação do microfone")
        self.record_button.setShortcut(QKeySequence("Space"))
        self.record_button.clicked.connect(self._toggle_recording)
        review_actions.addWidget(self.record_button)
        self.play_audio_button = QPushButton("Reproduzir áudio", left_panel)
        self.play_audio_button.clicked.connect(self._play_pending_audio)
        review_actions.addWidget(self.play_audio_button)
        self.send_to_gemini_button = QPushButton("Enviar para Gemini", left_panel)
        self.send_to_gemini_button.clicked.connect(self._send_pending_audio)
        review_actions.addWidget(self.send_to_gemini_button)
        left_layout.addLayout(review_actions)

        output_actions = QHBoxLayout()
        self.copy_button = QPushButton("Copiar texto", left_panel)
        self.copy_button.setToolTip("Copia o texto para a área de transferência")
        self.copy_button.clicked.connect(self.copy_text)
        output_actions.addWidget(self.copy_button)
        self.clear_text_button = QPushButton("Apagar texto", left_panel)
        self.clear_text_button.setToolTip("Apaga o texto do editor")
        self.clear_text_button.clicked.connect(self.clear_text)
        output_actions.addWidget(self.clear_text_button)
        self.terminal_button = QPushButton("Enviar ao terminal", left_panel)
        self.terminal_button.setToolTip(
            "Cola o texto no terminal X11 atualmente ativo, sem pressionar Enter"
        )
        self.terminal_button.clicked.connect(self.send_to_terminal)
        output_actions.addWidget(self.terminal_button)
        left_layout.addLayout(output_actions)
        left_layout.addStretch(1)
        self.status_label = QLabel(left_panel)
        self.status_label.setWordWrap(True)
        left_layout.addWidget(self.status_label)

        right_panel = QWidget(self.main_splitter)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)
        diagnostic_title = QLabel("Diagnóstico", right_panel)
        diagnostic_font = QFont(diagnostic_title.font())
        diagnostic_font.setBold(True)
        diagnostic_title.setFont(diagnostic_font)
        right_layout.addWidget(diagnostic_title)
        self.diagnostic_splitter = QSplitter(
            Qt.Orientation.Vertical, right_panel
        )
        self.diagnostic_splitter.setChildrenCollapsible(False)
        self.diagnostic_tabs = QTabWidget(self.diagnostic_splitter)
        self.audio_debug = self._new_debug_editor(self.diagnostic_tabs)
        self.payload_debug = self._new_debug_editor(self.diagnostic_tabs)
        self.return_debug = self._new_debug_editor(self.diagnostic_tabs)
        self.usage_debug = self._new_debug_editor(self.diagnostic_tabs)
        self.diagnostic_tabs.addTab(self.audio_debug, "Áudio")
        self.diagnostic_tabs.addTab(self.payload_debug, "Payload")
        self.diagnostic_tabs.addTab(self.return_debug, "Retorno")
        self.diagnostic_tabs.addTab(self.usage_debug, "Consumo")

        chart_panel = QWidget(self.diagnostic_splitter)
        chart_layout = QVBoxLayout(chart_panel)
        chart_layout.setContentsMargins(0, 0, 0, 0)
        chart_layout.addWidget(QLabel("Gráfico de consumo de tokens", chart_panel))
        self.usage_chart = TokenUsageChart(chart_panel)
        chart_layout.addWidget(self.usage_chart, stretch=1)
        self.diagnostic_splitter.addWidget(self.diagnostic_tabs)
        self.diagnostic_splitter.addWidget(chart_panel)
        self.diagnostic_splitter.setStretchFactor(0, 3)
        self.diagnostic_splitter.setStretchFactor(1, 2)
        right_layout.addWidget(self.diagnostic_splitter, stretch=1)

        self.main_splitter.addWidget(left_panel)
        self.main_splitter.addWidget(right_panel)
        self.main_splitter.setStretchFactor(0, 3)
        self.main_splitter.setStretchFactor(1, 2)
        self.main_splitter.setSizes([660, 440])
        layout.addWidget(self.main_splitter, stretch=1)
        self.setCentralWidget(central)

        self.copy_shortcut = QShortcut(QKeySequence("Ctrl+Shift+C"), self)
        self.copy_shortcut.activated.connect(self.copy_text)
        self.record_button.setFocus()
        self._sync_fullscreen_button()

    def _new_debug_editor(self, parent: QWidget) -> QPlainTextEdit:
        editor = QPlainTextEdit(parent)
        editor.setReadOnly(True)
        editor.setMaximumBlockCount(200)
        return editor

    @Slot()
    def _open_settings_dialog(self) -> None:
        if self._settings_dialog is not None:
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Configurações")
        dialog.setModal(True)
        dialog.setMinimumWidth(460)
        self._settings_dialog = dialog
        layout = QVBoxLayout(dialog)
        layout.setSpacing(12)

        api_group = QGroupBox("Chave API", dialog)
        api_layout = QVBoxLayout(api_group)
        api_layout.addWidget(
            QLabel("A chave é mantida no chaveiro seguro do sistema.", api_group)
        )
        self.configure_key_button = QPushButton("Configurar chave API", api_group)
        self.configure_key_button.clicked.connect(self._configure_api_key)
        api_layout.addWidget(self.configure_key_button)
        layout.addWidget(api_group)

        mouse_group = QGroupBox("Atalho do mouse", dialog)
        mouse_layout = QVBoxLayout(mouse_group)
        self.mouse_settings_status = QLabel(mouse_group)
        mouse_layout.addWidget(self.mouse_settings_status)
        mouse_actions = QHBoxLayout()
        self.configure_mouse_button = QPushButton(mouse_group)
        self.configure_mouse_button.clicked.connect(self._configure_mouse_shortcut)
        self.disable_mouse_button = QPushButton("Desativar", mouse_group)
        self.disable_mouse_button.clicked.connect(
            lambda: self._deactivate_shortcut("mouse")
        )
        mouse_actions.addWidget(self.configure_mouse_button)
        mouse_actions.addWidget(self.disable_mouse_button)
        mouse_layout.addLayout(mouse_actions)
        layout.addWidget(mouse_group)

        keyboard_group = QGroupBox("Atalho do teclado", dialog)
        keyboard_layout = QVBoxLayout(keyboard_group)
        self.keyboard_settings_status = QLabel(keyboard_group)
        keyboard_layout.addWidget(self.keyboard_settings_status)
        keyboard_actions = QHBoxLayout()
        self.configure_keyboard_button = QPushButton(keyboard_group)
        self.configure_keyboard_button.clicked.connect(
            self._configure_keyboard_shortcut
        )
        self.disable_keyboard_button = QPushButton("Desativar", keyboard_group)
        self.disable_keyboard_button.clicked.connect(
            lambda: self._deactivate_shortcut("keyboard")
        )
        keyboard_actions.addWidget(self.configure_keyboard_button)
        keyboard_actions.addWidget(self.disable_keyboard_button)
        keyboard_layout.addLayout(keyboard_actions)
        layout.addWidget(keyboard_group)

        close_buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close, parent=dialog
        )
        close_buttons.rejected.connect(dialog.reject)
        layout.addWidget(close_buttons)
        self._update_settings_dialog()
        try:
            dialog.exec()
        finally:
            self._settings_dialog = None

    def _update_settings_dialog(self) -> None:
        dialog = self._settings_dialog
        if dialog is None:
            return
        busy = self.state in (AppState.RECORDING, AppState.TRANSCRIBING)
        self.mouse_settings_status.setText(
            f"Estado: {_format_mouse_button_label(self._active_mouse_button)}"
        )
        self.keyboard_settings_status.setText(
            f"Estado: {_format_keyboard_shortcut_label(self._active_keyboard_shortcut)}"
        )
        if self.input_shortcut_bridge.ready:
            action_text = "Configurar"
        elif self.input_shortcut_bridge.version_incompatible:
            action_text = "Atualizar integração global"
        else:
            action_text = "Autorizar integração global"
        self.configure_mouse_button.setText(action_text)
        self.configure_keyboard_button.setText(action_text)
        self.configure_key_button.setEnabled(not busy)
        self.configure_mouse_button.setEnabled(not busy)
        self.configure_keyboard_button.setEnabled(not busy)
        self.disable_mouse_button.setEnabled(
            not busy and self._active_mouse_button is not None
        )
        self.disable_keyboard_button.setEnabled(
            not busy and self._active_keyboard_shortcut is not None
        )

    @Slot()
    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()
        self._sync_fullscreen_button()

    def _sync_fullscreen_button(self) -> None:
        fullscreen = self.isFullScreen()
        standard_icon = (
            QStyle.StandardPixmap.SP_TitleBarNormalButton
            if fullscreen
            else QStyle.StandardPixmap.SP_TitleBarMaxButton
        )
        self.fullscreen_button.setIcon(self.style().standardIcon(standard_icon))
        text = "Sair da tela cheia" if fullscreen else "Entrar em tela cheia"
        self.fullscreen_button.setToolTip(text)
        self.fullscreen_button.setAccessibleName(text)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._sync_fullscreen_button()

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
        dialog = QDialog(self._settings_dialog or self)
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
        count = int(self._active_mouse_button is not None) + int(
            self._active_keyboard_shortcut is not None
        )
        self.shortcut_indicator_label.setText(
            f"Atalhos globais: {count} {'ativo' if count == 1 else 'ativos'}"
        )
        self.shortcut_indicator_label.setToolTip(
            f"Mouse: {_format_mouse_button_label(self._active_mouse_button)}\n"
            f"Teclado: {_format_keyboard_shortcut_label(self._active_keyboard_shortcut)}"
        )
        self._update_settings_dialog()

    def _restore_shortcuts(self) -> None:
        self._update_shortcut_indicator()
        if self.local_store is None:
            return
        try:
            mouse = self.local_store.get_recording_mouse_button()
        except Exception:
            mouse = None
            self._startup_shortcut_diagnostic = (
                "Não foi possível ler preferência de atalho do mouse."
            )
        if isinstance(mouse, str) and mouse.strip().lower().removeprefix("button.") in {
            "left",
            "right",
        }:
            try:
                self.local_store.clear_recording_mouse_button()
            except Exception:
                pass
            mouse = None
            self._startup_shortcut_diagnostic = (
                "O atalho anterior usava um botão principal; configure um botão lateral ou central."
            )
        canonical_mouse = normalize_mouse_button_name(mouse)
        if canonical_mouse is not None:
            self._activate_shortcut("mouse", canonical_mouse, persist=False)

        try:
            keyboard = self.local_store.get_recording_keyboard_shortcut()
        except Exception:
            keyboard = None
            if self._startup_shortcut_diagnostic is None:
                self._startup_shortcut_diagnostic = (
                    "Não foi possível ler preferência de atalho do teclado."
                )
        canonical_keyboard = (
            normalize_keyboard_shortcut(keyboard) if keyboard is not None else None
        )
        if canonical_keyboard is not None:
            self._activate_shortcut("keyboard", canonical_keyboard, persist=False)

    def _configure_mouse_shortcut(self) -> None:
        self._request_shortcut_configuration("mouse")

    def _configure_keyboard_shortcut(self) -> None:
        self._request_shortcut_configuration("keyboard")

    def _request_shortcut_configuration(self, kind: str) -> None:
        if self._is_closing or self.state in (AppState.RECORDING, AppState.TRANSCRIBING):
            return
        if not self.input_shortcut_bridge.ready:
            self._pending_authorization_kind = kind
            self._show_shortcut_authorization_dialog()
            return
        self._capture_shortcut(kind)

    def _show_shortcut_authorization_dialog(self) -> None:
        dialog = QDialog(self._settings_dialog or self)
        dialog.setWindowTitle("Integração global")
        layout = QVBoxLayout(dialog)
        explanation = QLabel(
            "A integração local lê eventos de entrada somente para reconhecer os "
            "atalhos globais escolhidos. Ela não armazena texto, teclas ou cliques "
            "e não envia esses dados pela rede.",
            dialog,
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        buttons = QDialogButtonBox(dialog)
        action_text = (
            "Atualizar integração global"
            if self.input_shortcut_bridge.version_incompatible
            else "Autorizar e ativar"
        )
        authorize = buttons.addButton(action_text, QDialogButtonBox.ButtonRole.AcceptRole)
        cancel = buttons.addButton("Cancelar", QDialogButtonBox.ButtonRole.RejectRole)
        authorize.clicked.connect(dialog.accept)
        cancel.clicked.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._pending_authorization_kind = None
            self.status_label.setText("A autorização foi cancelada.")
            return
        if not self.shortcut_service_installer.install():
            self._pending_authorization_kind = None

    @Slot(bool, str)
    def _on_shortcut_install_finished(self, success: bool, message: str) -> None:
        if self._is_closing:
            return
        if not success:
            self._pending_authorization_kind = None
            self.status_label.setText(message)
            return
        self.status_label.setText("Integração global ativada.")
        self.input_shortcut_bridge.reconnect()

    @Slot(bool)
    def _on_shortcut_service_ready(self, ready: bool) -> None:
        if self._is_closing or not ready:
            return
        pending_authorization = self._pending_authorization_kind
        self._pending_authorization_kind = None
        for kind, (_generation, trigger, persist) in tuple(
            self._pending_bindings.items()
        ):
            self._activate_shortcut(kind, trigger, persist=persist)
        if pending_authorization is not None:
            QTimer.singleShot(0, lambda: self._capture_shortcut(pending_authorization))

    def _capture_shortcut(self, kind: str) -> None:
        previous = (
            self._active_mouse_button
            if kind == "mouse"
            else self._active_keyboard_shortcut
        )
        dialog = QDialog(self._settings_dialog or self)
        dialog.setWindowTitle(
            "Configurar atalho do mouse"
            if kind == "mouse"
            else "Configurar atalho do teclado"
        )
        layout = QVBoxLayout(dialog)
        prompt = QLabel(
            "Pressione um botão lateral ou o botão do meio…"
            if kind == "mouse"
            else "Pressione uma combinação ou uma tecla de função…",
            dialog,
        )
        prompt.setWordWrap(True)
        layout.addWidget(prompt)
        status = QLabel("Aguardando entrada…", dialog)
        status.setWordWrap(True)
        layout.addWidget(status)
        buttons = QDialogButtonBox(dialog)
        disable = buttons.addButton(
            "Desativar atalho", QDialogButtonBox.ButtonRole.DestructiveRole
        )
        cancel = buttons.addButton("Cancelar", QDialogButtonBox.ButtonRole.RejectRole)
        disable.clicked.connect(self._mark_capture_disabled)
        cancel.clicked.connect(dialog.reject)
        layout.addWidget(buttons)

        self._capture_dialog = dialog
        self._capture_status_label = status
        self._capture_kind = kind
        self._captured_shortcut = None
        self._capture_disable = False
        self._capture_generation = (
            self.input_shortcut_bridge.begin_mouse_capture()
            if kind == "mouse"
            else self.input_shortcut_bridge.begin_keyboard_capture()
        )
        result = dialog.exec()
        captured = self._captured_shortcut
        disabled = self._capture_disable
        self._capture_dialog = None
        self._capture_status_label = None
        self._capture_kind = None
        self._capture_generation = None

        if result == QDialog.DialogCode.Accepted and captured is not None:
            self._activate_shortcut(kind, captured, persist=True)
        elif result == QDialog.DialogCode.Accepted and disabled:
            self._deactivate_shortcut(kind)
        elif previous is not None:
            self._activate_shortcut(kind, previous, persist=False)
        else:
            if kind == "mouse":
                self.input_shortcut_bridge.stop_mouse()
            else:
                self.input_shortcut_bridge.stop_keyboard()

    @Slot()
    def _mark_capture_disabled(self) -> None:
        self._capture_disable = True
        if self._capture_dialog is not None:
            self._capture_dialog.accept()

    @Slot(int, str)
    def _on_mouse_shortcut_captured(self, generation: int, button: str) -> None:
        self._accept_captured_shortcut("mouse", generation, button)

    @Slot(int, str)
    def _on_keyboard_shortcut_captured(self, generation: int, shortcut: str) -> None:
        self._accept_captured_shortcut("keyboard", generation, shortcut)

    def _accept_captured_shortcut(
        self, kind: str, generation: int, trigger: str
    ) -> None:
        if (
            self._capture_dialog is None
            or self._capture_kind != kind
            or self._capture_generation != generation
        ):
            return
        self._captured_shortcut = trigger
        self._capture_dialog.accept()

    def _activate_shortcut(self, kind: str, trigger: str, *, persist: bool) -> None:
        if kind == "mouse":
            canonical = normalize_mouse_button_name(trigger)
            if canonical is None:
                return
            expected = self.input_shortcut_bridge.mouse_generation + 1
            self._pending_bindings[kind] = (expected, canonical, persist)
            generation = self.input_shortcut_bridge.start_mouse(canonical)
        else:
            canonical = normalize_keyboard_shortcut(trigger)
            if canonical is None:
                return
            expected = self.input_shortcut_bridge.keyboard_generation + 1
            self._pending_bindings[kind] = (expected, canonical, persist)
            generation = self.input_shortcut_bridge.start_keyboard(canonical)
        if generation != expected:
            self._pending_bindings[kind] = (generation, canonical, persist)

    @Slot(int, str)
    def _on_mouse_binding_ready(self, generation: int, button: str) -> None:
        self._commit_shortcut_binding("mouse", generation, button)

    @Slot(int, str)
    def _on_keyboard_binding_ready(self, generation: int, shortcut: str) -> None:
        self._commit_shortcut_binding("keyboard", generation, shortcut)

    def _commit_shortcut_binding(
        self, kind: str, generation: int, trigger: str
    ) -> None:
        pending = self._pending_bindings.get(kind)
        if pending is None or pending[:2] != (generation, trigger):
            return
        del self._pending_bindings[kind]
        persist = pending[2]
        if kind == "mouse":
            self._active_mouse_button = trigger
        else:
            self._active_keyboard_shortcut = trigger
        self._update_shortcut_indicator()
        if not persist:
            return
        persistence_failed = self.local_store is None
        if self.local_store is not None:
            try:
                if kind == "mouse":
                    self.local_store.save_recording_mouse_button(trigger)
                else:
                    self.local_store.save_recording_keyboard_shortcut(trigger)
            except Exception:
                persistence_failed = True
        if persistence_failed:
            self.status_label.setText(
                "Atalho ativo nesta sessão; não foi possível persistir."
            )
        else:
            self.status_label.setText("Atalho global configurado.")

    def _deactivate_shortcut(self, kind: str) -> None:
        expected = (
            self.input_shortcut_bridge.mouse_generation + 1
            if kind == "mouse"
            else self.input_shortcut_bridge.keyboard_generation + 1
        )
        self._pending_stops[kind] = expected
        generation = (
            self.input_shortcut_bridge.stop_mouse()
            if kind == "mouse"
            else self.input_shortcut_bridge.stop_keyboard()
        )
        if generation != expected:
            self._pending_stops[kind] = generation

    @Slot(str, int)
    def _on_shortcut_stopped(self, kind: str, generation: int) -> None:
        if self._pending_stops.get(kind) != generation:
            return
        del self._pending_stops[kind]
        persistence_failed = False
        if kind == "mouse":
            self._active_mouse_button = None
        else:
            self._active_keyboard_shortcut = None
        if self.local_store is not None:
            try:
                if kind == "mouse":
                    self.local_store.clear_recording_mouse_button()
                else:
                    self.local_store.clear_recording_keyboard_shortcut()
            except Exception:
                persistence_failed = True
        self._update_shortcut_indicator()
        self.status_label.setText(
            "Atalho desativado nesta sessão; não foi possível persistir."
            if persistence_failed
            else "Atalho global desativado."
        )

    @Slot(int, str)
    def _on_mouse_shortcut_activated(self, generation: int, button: str) -> None:
        if (
            generation == self.input_shortcut_bridge.mouse_generation
            and button == self._active_mouse_button
        ):
            self._activate_recording_shortcut()

    @Slot(int, str)
    def _on_keyboard_shortcut_activated(
        self, generation: int, shortcut: str
    ) -> None:
        if (
            generation == self.input_shortcut_bridge.keyboard_generation
            and shortcut == self._active_keyboard_shortcut
        ):
            self._activate_recording_shortcut()

    def _activate_recording_shortcut(self) -> None:
        if self._is_closing or self.state is AppState.TRANSCRIBING:
            return
        self._toggle_recording()

    @Slot(str, int, str)
    def _on_shortcut_failed(self, kind: str, generation: int, message: str) -> None:
        if self._is_closing:
            return
        if (
            kind == "keyboard"
            and self._capture_kind == kind
            and self._capture_generation == generation
            and self._capture_status_label is not None
            and message == BACKEND_FAILURE_MESSAGE
        ):
            self._capture_status_label.setText(
                "Uma letra ou dígito isolado não é aceito. Use Ctrl, Alt ou Meta, "
                "ou escolha uma tecla de função/mídia."
            )
            return
        if (
            self._capture_dialog is not None
            and self._capture_kind == kind
            and self._capture_generation == generation
        ):
            self._capture_dialog.reject()
        self.status_label.setText(message or BACKEND_FAILURE_MESSAGE)
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
        configure_key = getattr(self, "configure_key_button", None)
        if configure_key is not None:
            configure_key.setEnabled(not busy and not recording)
        self.settings_button.setEnabled(True)
        self._update_settings_dialog()
        if not self.settings.has_api_key and self.state is AppState.IDLE:
            self.status_label.setText(self.settings.missing_api_key_message)

    def _set_error(self, message: str) -> None:
        self.state = AppState.ERROR
        self.status_label.setText(message)
        self._update_actions()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._is_closing = True
        if self._capture_dialog is not None:
            self._capture_dialog.reject()
        try:
            self.shortcut_service_installer.cancel()
        except Exception:
            pass
        try:
            self.input_shortcut_bridge.close()
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
