from __future__ import annotations

from dataclasses import replace
from enum import Enum, auto
import time
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
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QIcon,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPaintEvent,
    QPen,
    QShortcut,
    QTextCursor,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStyle,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from . import __version__
from .audio import (
    MIN_RMS_LEVEL,
    AudioCapture,
    AudioDevice,
    AudioRecorder,
    AudioRecorderError,
    choose_input_device,
    list_input_devices,
)
from .config import MODEL_CHOICES, Settings
from .credentials import ApiKeyStore, CredentialStoreError
from .storage import LocalStore, LocalStoreError, TokenTotals, TokenUsageRecord
from .homebrew_update import HomebrewUpdateController
from .shortcut_install import ShortcutServiceInstaller
from .shortcuts import (
    BACKEND_FAILURE_MESSAGE,
    MOUSE_CAPTURE_HINT_MESSAGES,
    InputShortcutBridge,
    normalize_keyboard_shortcut,
    normalize_mouse_button_name,
)
from .terminal import TerminalBridge, TerminalBridgeError, TerminalTarget
from .spell_highlighter import SpellHighlighter
from .spellcheck import LocalSpellChecker, utf16_code_unit_offsets
from .transcription import (
    GeminiTranscriber,
    ProofreadingWorker,
    TranscriptionDebug,
    TranscriptionWorker,
)


class AppState(Enum):
    IDLE = auto()
    RECORDING = auto()
    AUDIO_READY = auto()
    TRANSCRIBING = auto()
    READY = auto()
    ERROR = auto()


MediaPlayerFactory = Callable[[QWidget], tuple[QMediaPlayer, QAudioOutput]]

CAPTURE_WAITING_TEXT = "Aguardando entrada…"
CAPTURE_HINT_DELAY_MS = 8000
GLOBAL_SHORTCUT_DEBOUNCE_SECONDS = 0.35


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

class SpellSuggestionPopup(QFrame):
    """Balão flutuante moderno de sugestões ortográficas para palavras com erro."""

    suggestion_selected = Signal(str)
    ignore_selected = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            parent,
            Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self._current_word: str = ""
        self.suggestion_buttons: list[QPushButton] = []
        self.ignore_button: QPushButton | None = None
        self.no_suggestions_label: QLabel | None = None
        self._setup_style()
        self._init_layout()

    def _setup_style(self) -> None:
        self.setStyleSheet(
            """
            SpellSuggestionPopup {
                background-color: #1E293B;
                border: 1px solid #475569;
                border-radius: 6px;
            }
            QPushButton#suggestion_chip {
                background-color: #334155;
                color: #38BDF8;
                border: 1px solid #334155;
                border-radius: 4px;
                padding: 3px 8px;
                font-weight: 500;
                font-size: 12px;
            }
            QPushButton#suggestion_chip:hover {
                background-color: #0284C7;
                color: #FFFFFF;
                border-color: #0284C7;
            }
            QPushButton#ignore_button {
                background-color: transparent;
                color: #94A3B8;
                border: none;
                border-radius: 4px;
                padding: 3px 6px;
                font-size: 11px;
            }
            QPushButton#ignore_button:hover {
                background-color: #334155;
                color: #E2E8F0;
            }
            QLabel {
                color: #94A3B8;
                font-size: 12px;
                border: none;
                padding: 2px 4px;
            }
            """
        )

    def _init_layout(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)

    def show_suggestions(
        self, word: str, suggestions: list[str], target_rect: QRect
    ) -> None:
        self._current_word = word
        self.suggestion_buttons.clear()
        self.ignore_button = None
        self.no_suggestions_label = None

        layout = self.layout()
        if layout is None:
            return

        while layout.count() > 0:
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        top_suggestions = suggestions[:3]
        if top_suggestions:
            for sug in top_suggestions:
                btn = QPushButton(sug, self)
                btn.setObjectName("suggestion_chip")
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.clicked.connect(
                    lambda checked=False, s=sug: self._on_suggestion_clicked(s)
                )
                btn.show()
                layout.addWidget(btn)
                self.suggestion_buttons.append(btn)
        else:
            lbl = QLabel("Sem sugestões", self)
            lbl.show()
            layout.addWidget(lbl)
            self.no_suggestions_label = lbl

        ignore_btn = QPushButton("Ignorar", self)
        ignore_btn.setObjectName("ignore_button")
        ignore_btn.setToolTip(f'Ignorar "{word}"')
        ignore_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ignore_btn.clicked.connect(self._on_ignore_clicked)
        ignore_btn.show()
        layout.addWidget(ignore_btn)
        self.ignore_button = ignore_btn

        self.adjustSize()
        screen = (
            QGuiApplication.screenAt(target_rect.center())
            or QGuiApplication.primaryScreen()
        )
        screen_geom = (
            screen.availableGeometry()
            if screen is not None
            else QRect(0, 0, 1920, 1080)
        )
        popup_w = self.sizeHint().width()
        popup_h = self.sizeHint().height()
        x = target_rect.left()
        y = target_rect.bottom() + 4
        if y + popup_h > screen_geom.bottom() - 6:
            y = target_rect.top() - popup_h - 4
        x = max(screen_geom.left() + 4, min(x, screen_geom.right() - popup_w - 4))
        y = max(screen_geom.top() + 4, min(y, screen_geom.bottom() - popup_h - 4))
        self.move(QPoint(x, y))
        self.show()
        self.raise_()

    def _on_suggestion_clicked(self, suggestion: str) -> None:
        self.hide()
        self.suggestion_selected.emit(suggestion)

    def _on_ignore_clicked(self) -> None:
        word = self._current_word
        self.hide()
        self.ignore_selected.emit(word)


class MainWindow(QMainWindow):
    def __init__(
        self,
        settings: Settings,
        recorder: AudioRecorder | None = None,
        transcriber: GeminiTranscriber | None = None,
        terminal_bridge: TerminalBridge | None = None,
        api_key_store: ApiKeyStore | None = None,
        transcriber_factory: Callable[[str, str], GeminiTranscriber] | None = None,
        microphone_provider: Callable[[], tuple[AudioDevice, ...]] | None = None,
        media_player_factory: MediaPlayerFactory | None = None,
        local_store: LocalStore | None = None,
        input_shortcut_bridge: InputShortcutBridge | None = None,
        shortcut_service_installer: ShortcutServiceInstaller | None = None,
        homebrew_update_controller: HomebrewUpdateController | None = None,
        spell_checker: LocalSpellChecker | None = None,
        startup_message: str | None = None,
    ) -> None:
        super().__init__()
        self.homebrew_update_controller = homebrew_update_controller
        self._update_status: str = (
            ""
            if self.homebrew_update_controller is not None
            else "Instale o FalaFácil com: brew install OthonBreener/falafacil/falafacil"
        )
        self.settings = settings
        self.local_store = local_store
        self.recorder = recorder or AudioRecorder()
        self.transcriber = transcriber
        self.terminal_bridge = terminal_bridge or TerminalBridge()
        self.api_key_store = api_key_store
        self.transcriber_factory = transcriber_factory or (
            lambda api_key, model: GeminiTranscriber(api_key=api_key, model=model)
        )
        self._microphone_provider = microphone_provider or list_input_devices
        self._media_player_factory = media_player_factory or _default_media_player_factory
        self.state = AppState.IDLE
        self._thread: QThread | None = None
        self._worker: TranscriptionWorker | None = None
        self._pending_capture: AudioCapture | None = None
        self._preserved_capture: AudioCapture | None = None
        self._audio_buffer: QBuffer | None = None
        self._audio_byte_array: QByteArray | None = None
        self._microphone_refreshing = False
        self._microphone_available = False
        self._is_closing = False
        self._close_pending = False
        self._final_close_scheduled = False
        self._origin_terminal_target: TerminalTarget | None = None
        self._last_global_activation_time: float | None = None
        self._active_recording_globally_initiated: bool = False
        self._is_playing_audio: bool = False
        self._playback_generation: int = 0
        self._active_playback_generation: int | None = None
        self._media_adapters: tuple[Callable, ...] = ()
        self._spell_popup: SpellSuggestionPopup | None = None
        if spell_checker is not None:
            self.spell_checker = spell_checker
        else:
            ignored = None
            if self.local_store:
                try:
                    ignored = self.local_store.get_spellcheck_ignored_words()
                except Exception:
                    ignored = None
            self.spell_checker = LocalSpellChecker(ignored_words=ignored)
        self._is_reviewing = False
        self._proofreading_thread: QThread | None = None
        self._proofreading_worker: ProofreadingWorker | None = None
        self.spellcheck_status_label: QLabel | None = None
        self.spellcheck_checkbox: QCheckBox | None = None

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
        if self.homebrew_update_controller is not None:
            self.homebrew_update_controller.status_changed.connect(
                self._on_homebrew_status_changed
            )
            self.homebrew_update_controller.up_to_date.connect(
                self._on_homebrew_up_to_date
            )
            self.homebrew_update_controller.ready_to_restart.connect(
                self._on_homebrew_ready_to_restart
            )
            self.homebrew_update_controller.failed.connect(
                self._on_homebrew_failed
            )

        self.setWindowTitle("FalaFácil")
        self.resize(1120, 700)
        self.setMinimumSize(760, 560)
        self._media_player, self._audio_output = self._media_player_factory(self)
        self._build_ui()
        spellcheck_enabled = True
        if self.local_store:
            try:
                spellcheck_enabled = self.local_store.get_spellcheck_enabled()
            except Exception:
                spellcheck_enabled = True
        self.highlighter = SpellHighlighter(
            self.last_message_editor.document(),
            spell_checker=self.spell_checker,
            enabled=bool(spellcheck_enabled and self.spell_checker.is_available()),
        )
        self.last_message_editor.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.last_message_editor.customContextMenuRequested.connect(self._show_editor_context_menu)
        self._spell_popup = SpellSuggestionPopup(self.last_message_editor)
        self._spell_popup.suggestion_selected.connect(
            self._on_popup_suggestion_selected
        )
        self._spell_popup.ignore_selected.connect(self._on_popup_ignore_selected)
        self._spell_popup.installEventFilter(self)
        self._is_mouse_over_popup: bool = False
        self._active_spell_token: tuple[int, int, str] | None = None
        self._hover_spell_timer = QTimer(self)
        self._hover_spell_timer.setSingleShot(True)
        self._hover_spell_timer.timeout.connect(self._on_hover_spell_timer_timeout)
        self._popup_dismiss_timer = QTimer(self)
        self._popup_dismiss_timer.setSingleShot(True)
        self._popup_dismiss_timer.timeout.connect(
            self._on_popup_dismiss_timer_timeout
        )
        self._last_hover_pos: QPoint | None = None
        self.last_message_editor.cursorPositionChanged.connect(
            self._on_editor_cursor_position_changed
        )
        self.transcription_editor.viewport().installEventFilter(self)
        self.transcription_editor.installEventFilter(self)
        self.last_message_editor.viewport().installEventFilter(self)
        self.last_message_editor.installEventFilter(self)
        self.last_message_editor.viewport().setMouseTracking(True)
        self.last_message_editor.verticalScrollBar().valueChanged.connect(self._hide_spell_popup)
        self.last_message_editor.horizontalScrollBar().valueChanged.connect(self._hide_spell_popup)
        self._restore_shortcuts()
        self._refresh_microphones()
        self._refresh_token_usage_chart()
        self._update_actions()
        if self._startup_shortcut_diagnostic is not None:
            self.status_label.setText(self._startup_shortcut_diagnostic)
        if startup_message is not None:
            self.status_label.setText(startup_message)
        if QApplication.instance() is not None:
            QApplication.instance().installEventFilter(self)
    def _connect_media_adapters(self, generation: int) -> None:
        self._disconnect_media_adapters()

        def on_status(status: object, gen: int = generation) -> None:
            self._on_media_status_changed(status, gen)

        def on_state(state: object, gen: int = generation) -> None:
            self._on_playback_state_changed(state, gen)

        def on_error(
            error: object, error_string: str = "", gen: int = generation
        ) -> None:
            self._on_media_error(error, error_string, gen)

        self._media_adapters = (on_status, on_state, on_error)

        status_sig = getattr(self._media_player, "mediaStatusChanged", None)
        if status_sig is not None:
            try:
                status_sig.connect(on_status)
            except Exception:
                pass
        state_sig = getattr(self._media_player, "playbackStateChanged", None)
        if state_sig is not None:
            try:
                state_sig.connect(on_state)
            except Exception:
                pass
        error_sig = getattr(self._media_player, "errorOccurred", None)
        if error_sig is not None:
            try:
                error_sig.connect(on_error)
            except Exception:
                pass

    def _disconnect_media_adapters(self) -> None:
        if not hasattr(self, "_media_adapters") or not self._media_adapters:
            return
        on_status, on_state, on_error = self._media_adapters
        status_sig = getattr(self._media_player, "mediaStatusChanged", None)
        if status_sig is not None:
            try:
                status_sig.disconnect(on_status)
            except Exception:
                pass
        state_sig = getattr(self._media_player, "playbackStateChanged", None)
        if state_sig is not None:
            try:
                state_sig.disconnect(on_state)
            except Exception:
                pass
        error_sig = getattr(self._media_player, "errorOccurred", None)
        if error_sig is not None:
            try:
                error_sig.disconnect(on_error)
            except Exception:
                pass
        self._media_adapters = ()

    def _build_ui(self) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        self._settings_dialog: QDialog | None = None
        # These controls belong to the transient settings dialog.  Keep the
        # references explicit so they can be invalidated when Qt destroys the
        # dialog instead of retaining wrappers around deleted C++ objects.
        self.configure_key_button: QPushButton | None = None
        self.model_combo: QComboBox | None = None
        self.apply_model_button: QPushButton | None = None
        self.mouse_settings_status: QLabel | None = None
        self.keyboard_settings_status: QLabel | None = None
        self.configure_mouse_button: QPushButton | None = None
        self.disable_mouse_button: QPushButton | None = None
        self.configure_keyboard_button: QPushButton | None = None
        self.disable_keyboard_button: QPushButton | None = None
        self.installed_version_label: QLabel | None = None
        self.update_status_label: QLabel | None = None
        self.update_progress_bar: QProgressBar | None = None
        self.install_update_button: QPushButton | None = None
        self.spellcheck_status_label = None
        self.spellcheck_checkbox = None

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

        self.message_splitter = QSplitter(Qt.Orientation.Vertical, left_panel)
        self.message_splitter.setChildrenCollapsible(False)

        # Bloco superior: Transcrição atual
        current_block = QWidget(self.message_splitter)
        current_layout = QVBoxLayout(current_block)
        current_layout.setContentsMargins(0, 0, 0, 0)
        current_layout.setSpacing(8)

        current_layout.addWidget(QLabel("Transcrição atual", current_block))
        self.transcription_editor = QPlainTextEdit(current_block)
        self.transcription_editor.setPlaceholderText(
            "A transcrição aparecerá aqui antes de ser copiada e movida para Última mensagem."
        )
        self.transcription_editor.setTabChangesFocus(False)
        self.transcription_editor.textChanged.connect(self._update_actions)
        current_layout.addWidget(self.transcription_editor, stretch=1)

        current_actions = QHBoxLayout()
        self.record_button = QPushButton("Gravar", current_block)
        self.record_button.setToolTip("Ação principal: grava, pausa para revisar ou envia áudio")
        self.record_button.clicked.connect(self._perform_primary_action)
        current_actions.addWidget(self.record_button)

        self.record_again_button = QPushButton("Descartar e gravar novamente", current_block)
        self.record_again_button.setToolTip("Descarta o áudio gravado e inicia uma nova gravação")
        self.record_again_button.clicked.connect(self._start_replacement_recording)
        current_actions.addWidget(self.record_again_button)

        self.play_audio_button = QPushButton("Reproduzir áudio", current_block)
        self.play_audio_button.setToolTip("Reproduz ou para o áudio capturado para revisão")
        self.play_audio_button.clicked.connect(self._toggle_playback)
        current_actions.addWidget(self.play_audio_button)

        self.copy_and_archive_button = QPushButton("Copiar e arquivar", current_block)
        self.copy_and_archive_button.setToolTip("Copia o texto atual e move para Última mensagem")
        self.copy_and_archive_button.clicked.connect(self._copy_and_archive_current_transcription)
        current_actions.addWidget(self.copy_and_archive_button)

        current_layout.addLayout(current_actions)
        self.message_splitter.addWidget(current_block)

        # Bloco inferior: Última mensagem — somente nesta sessão
        last_block = QWidget(self.message_splitter)
        last_layout = QVBoxLayout(last_block)
        last_layout.setContentsMargins(0, 0, 0, 0)
        last_layout.setSpacing(8)

        last_layout.addWidget(QLabel("Última mensagem — somente nesta sessão", last_block))
        self.last_message_editor = QPlainTextEdit(last_block)
        self.last_message_editor.setPlaceholderText(
            "O último texto copiado fica aqui para revisão com IA, nova cópia ou envio ao terminal."
        )
        self.last_message_editor.setTabChangesFocus(False)
        self.last_message_editor.textChanged.connect(self._update_actions)
        last_layout.addWidget(self.last_message_editor, stretch=1)

        last_actions = QHBoxLayout()
        self.review_button = QPushButton("Revisar com IA", last_block)
        self.review_button.setToolTip(
            "Revisa gramática, concordância, crase e pontuação com o Gemini"
        )
        self.review_button.clicked.connect(self._review_text_with_ai)
        last_actions.addWidget(self.review_button)

        self.copy_last_button = QPushButton("Copiar novamente", last_block)
        self.copy_last_button.setToolTip("Copia novamente o texto para a área de transferência")
        self.copy_last_button.clicked.connect(self.copy_last_message)
        last_actions.addWidget(self.copy_last_button)

        self.terminal_button = QPushButton("Enviar ao terminal", last_block)
        self.terminal_button.setToolTip(
            "Cola o texto no terminal X11 atualmente ativo, sem pressionar Enter"
        )
        self.terminal_button.clicked.connect(self.send_to_terminal)
        last_actions.addWidget(self.terminal_button)

        self.clear_last_button = QPushButton("Apagar", last_block)
        self.clear_last_button.setToolTip("Apaga o texto da última mensagem")
        self.clear_last_button.clicked.connect(self.clear_last_message)
        last_actions.addWidget(self.clear_last_button)

        last_layout.addLayout(last_actions)
        self.message_splitter.addWidget(last_block)

        self.message_splitter.setSizes([200, 200])
        left_layout.addWidget(self.message_splitter, stretch=1)

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
        self.copy_shortcut.activated.connect(self.copy_last_message)

        self.space_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        self.space_shortcut.activated.connect(self._on_space_shortcut_activated)

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

        model_group = QGroupBox("Modelo Gemini", dialog)
        model_layout = QVBoxLayout(model_group)
        self.model_combo = QComboBox(model_group)
        for model_id, label in MODEL_CHOICES:
            self.model_combo.addItem(label, model_id)
        current_model_idx = self.model_combo.findData(self.settings.model)
        self.model_combo.setCurrentIndex(current_model_idx)
        model_layout.addWidget(self.model_combo)
        self.apply_model_button = QPushButton("Aplicar modelo", model_group)
        self.apply_model_button.clicked.connect(self._apply_model_preference)
        model_layout.addWidget(self.apply_model_button)
        layout.addWidget(model_group)

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
        spell_group = QGroupBox("Corretor ortográfico", dialog)
        spell_layout = QVBoxLayout(spell_group)
        self.spellcheck_status_label = QLabel(spell_group)
        self.spellcheck_status_label.setWordWrap(True)
        if self.spell_checker.is_available():
            self.spellcheck_status_label.setText(
                "Dicionário local: Instalado (pt_BR via libenchant)"
            )
        else:
            self.spellcheck_status_label.setText(
                "Dicionário local: Não instalado (opcional — instale com: sudo apt install hunspell-pt-br)"
            )
        spell_layout.addWidget(self.spellcheck_status_label)

        self.spellcheck_checkbox = QCheckBox(
            "Sublinhar palavras desconhecidas no editor", spell_group
        )
        self.spellcheck_checkbox.setChecked(bool(self.highlighter.enabled))
        self.spellcheck_checkbox.setEnabled(self.spell_checker.is_available())
        self.spellcheck_checkbox.toggled.connect(self._on_spellcheck_toggled)
        spell_layout.addWidget(self.spellcheck_checkbox)
        layout.addWidget(spell_group)


        update_group = QGroupBox("Atualizações", dialog)
        update_layout = QVBoxLayout(update_group)
        self.installed_version_label = QLabel(
            f"Versão instalada: {__version__}", update_group
        )
        update_layout.addWidget(self.installed_version_label)
        self.update_status_label = QLabel(update_group)
        self.update_status_label.setWordWrap(True)
        update_layout.addWidget(self.update_status_label)
        self.update_progress_bar = QProgressBar(update_group)
        self.update_progress_bar.setRange(0, 0)
        update_layout.addWidget(self.update_progress_bar)
        self.install_update_button = QPushButton("Instalar atualizações", update_group)
        self.install_update_button.clicked.connect(self._on_install_updates_clicked)
        update_layout.addWidget(self.install_update_button)
        layout.addWidget(update_group)

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
            # QDialog::exec() may have destroyed the child widgets already.
            # Drop every Python wrapper before a later state update can touch
            # one of them (for example after sending a pending recording).
            self.configure_key_button = None
            self.model_combo = None
            self.apply_model_button = None
            self.mouse_settings_status = None
            self.keyboard_settings_status = None
            self.configure_mouse_button = None
            self.disable_mouse_button = None
            self.configure_keyboard_button = None
            self.disable_keyboard_button = None
            self.installed_version_label = None
            self.update_status_label = None
            self.update_progress_bar = None
            self.install_update_button = None
            self.spellcheck_status_label = None
            self.spellcheck_checkbox = None

    def _update_settings_dialog(self) -> None:
        dialog = self._settings_dialog
        if dialog is None:
            return
        # The dialog can be in the process of closing while queued UI signals
        # are being delivered.  Its child wrappers are only valid while the
        # dialog is active.
        if self.configure_key_button is None:
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
        model_combo = self.model_combo
        apply_model_button = self.apply_model_button
        if model_combo is not None:
            model_combo.setEnabled(not busy and not self.settings.model_from_environment)
        if apply_model_button is not None:
            apply_model_button.setEnabled(
                not busy and not self.settings.model_from_environment
            )
        update_status_label = self.update_status_label
        update_progress_bar = self.update_progress_bar
        install_update_button = self.install_update_button
        if self.homebrew_update_controller is None:
            if update_status_label is not None:
                update_status_label.setText(
                    "Instale o FalaFácil com: brew install OthonBreener/falafacil/falafacil"
                )
            if update_progress_bar is not None:
                update_progress_bar.setVisible(False)
            if install_update_button is not None:
                install_update_button.setEnabled(False)
        else:
            is_running = self.homebrew_update_controller.running
            if update_status_label is not None:
                update_status_label.setText(self._update_status)
            if update_progress_bar is not None:
                update_progress_bar.setVisible(is_running)
            if install_update_button is not None:
                install_update_button.setEnabled(not busy and not is_running)
        if self.spellcheck_status_label is not None:
            if self.spell_checker.is_available():
                self.spellcheck_status_label.setText(
                    "Dicionário local: Instalado (pt_BR via libenchant)"
                )
            else:
                self.spellcheck_status_label.setText(
                    "Dicionário local: Não instalado (opcional — instale com: sudo apt install hunspell-pt-br)"
                )
        if self.spellcheck_checkbox is not None:
            self.spellcheck_checkbox.setEnabled(self.spell_checker.is_available())

    @Slot(bool)
    def _on_spellcheck_toggled(self, checked: bool) -> None:
        self.highlighter.enabled = checked
        if not checked:
            self._hide_spell_popup()
        if self.local_store is not None:
            try:
                self.local_store.save_spellcheck_enabled(checked)
            except Exception:
                pass

    @Slot()
    def _on_install_updates_clicked(self) -> None:
        if self._is_closing or self.state in (AppState.RECORDING, AppState.TRANSCRIBING):
            return
        if self.homebrew_update_controller is None:
            return
        if self.homebrew_update_controller.running:
            return
        self.homebrew_update_controller.install_latest()
        self._update_actions()

    @Slot(str)
    def _on_homebrew_status_changed(self, message: str) -> None:
        if self._is_closing:
            return
        self._update_status = message
        self._update_settings_dialog()

    @Slot(str)
    def _on_homebrew_up_to_date(self, message: str) -> None:
        if self._is_closing:
            return
        self._update_status = message
        self._update_actions()

    @Slot(str)
    def _on_homebrew_failed(self, message: str) -> None:
        if self._is_closing:
            return
        self._update_status = message
        self._update_actions()

    @Slot(str)
    def _on_homebrew_ready_to_restart(self, message: str) -> None:
        if self._is_closing:
            return
        self._update_status = message
        self._update_actions()
        self._show_restart_dialog(message)

    def _show_restart_dialog(self, message: str) -> None:
        parent = self._settings_dialog if self._settings_dialog is not None else self
        dialog = QDialog(parent)
        dialog.setWindowTitle("Atualização concluída")
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)
        label = QLabel(message, dialog)
        label.setWordWrap(True)
        layout.addWidget(label)

        buttons = QDialogButtonBox(dialog)
        restart_btn = buttons.addButton(
            "Reiniciar agora", QDialogButtonBox.ButtonRole.AcceptRole
        )
        later_btn = buttons.addButton(
            "Mais tarde", QDialogButtonBox.ButtonRole.RejectRole
        )
        restart_btn.clicked.connect(dialog.accept)
        later_btn.clicked.connect(dialog.reject)
        layout.addWidget(buttons)

        result = dialog.exec()
        if result == QDialog.DialogCode.Accepted:
            if self.homebrew_update_controller is not None:
                started = self.homebrew_update_controller.restart()
                if started:
                    self.close()
                else:
                    self._update_status = (
                        "O Homebrew não conseguiu concluir a atualização. Tente novamente."
                    )
                    self._update_actions()

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

    def _create_api_key_dialog(self) -> tuple[QDialog, QLineEdit]:
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
        return dialog, key_input

    def _acquire_api_key(self) -> tuple[str, bool]:
        dialog, key_input = self._create_api_key_dialog()
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
            new_transcriber = self.transcriber_factory(api_key, self.settings.model)
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
    def _apply_model_preference(self) -> None:
        if self.state in (AppState.RECORDING, AppState.TRANSCRIBING):
            return
        if self.settings.model_from_environment:
            return
        selected_model = self.model_combo.currentData()
        if not isinstance(selected_model, str) or not selected_model:
            return
        valid_choices = {choice[0] for choice in MODEL_CHOICES}
        if selected_model not in valid_choices:
            return

        new_transcriber = None
        if self.settings.has_api_key and self.settings.api_key is not None:
            try:
                new_transcriber = self.transcriber_factory(
                    self.settings.api_key, selected_model
                )
            except Exception:
                current_model_idx = self.model_combo.findData(self.settings.model)
                if current_model_idx >= 0:
                    self.model_combo.setCurrentIndex(current_model_idx)
                self.status_label.setText("Não foi possível configurar o modelo Gemini.")
                return

        persistence_unavailable = self.local_store is None
        if self.local_store is not None:
            try:
                self.local_store.save_gemini_model(selected_model)
            except Exception:
                persistence_unavailable = True

        self.settings = replace(self.settings, model=selected_model)
        if new_transcriber is not None:
            self.transcriber = new_transcriber
        self._update_actions()
        if persistence_unavailable:
            self.status_label.setText(
                "Modelo Gemini configurado apenas nesta sessão; "
                "não foi possível persistir no banco local."
            )
        else:
            self.status_label.setText("Modelo Gemini configurado com sucesso.")
    def _update_shortcut_indicator(self) -> None:
        if not self.input_shortcut_bridge.ready:
            count = int(self._active_mouse_button is not None) + int(
                self._active_keyboard_shortcut is not None
            )
            self.shortcut_indicator_label.setText(
                f"Atalhos globais: reconectando ({count} configurados)"
            )
        elif self._pending_bindings:
            self.shortcut_indicator_label.setText("Atalhos globais: ativando…")
        else:
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
        if self._is_closing:
            return
        if not ready:
            self._update_shortcut_indicator()
            return
        pending_authorization = self._pending_authorization_kind
        self._pending_authorization_kind = None
        for kind in tuple(self._pending_stops.keys()):
            self._deactivate_shortcut(kind)
        for kind, (_generation, trigger, persist) in tuple(
            self._pending_bindings.items()
        ):
            self._activate_shortcut(kind, trigger, persist=persist)
        if (
            self._active_mouse_button is not None
            and "mouse" not in self._pending_bindings
            and "mouse" not in self._pending_stops
        ):
            self._activate_shortcut("mouse", self._active_mouse_button, persist=False)
        if (
            self._active_keyboard_shortcut is not None
            and "keyboard" not in self._pending_bindings
            and "keyboard" not in self._pending_stops
        ):
            self._activate_shortcut(
                "keyboard", self._active_keyboard_shortcut, persist=False
            )
        self._update_shortcut_indicator()
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
        status = QLabel(CAPTURE_WAITING_TEXT, dialog)
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
        pending = self._capture_generation
        QTimer.singleShot(
            CAPTURE_HINT_DELAY_MS, lambda: self._hint_capture_timeout(kind, pending)
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

    def _hint_capture_timeout(self, kind: str, generation: int) -> None:
        """Explain a silent capture instead of waiting on input that never comes."""
        if (
            self._is_closing
            or self._capture_status_label is None
            or self._capture_kind != kind
            or self._capture_generation != generation
        ):
            return
        if self._capture_status_label.text() != CAPTURE_WAITING_TEXT:
            return
        self._capture_status_label.setText(
            "Nenhuma entrada foi reconhecida. Alguns botões existem apenas no firmware "
            "do dispositivo e não chegam ao sistema; remapeie-o no software do "
            "fabricante ou escolha outro botão."
        )

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
        self._pending_stops.pop(kind, None)
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
        self._pending_bindings.pop(kind, None)
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
        if (
            self._is_closing
            or self._capture_dialog is not None
            or self.state is AppState.TRANSCRIBING
            or self._is_reviewing
            or self._thread is not None
            or self._worker is not None
            or self._proofreading_thread is not None
            or self._proofreading_worker is not None
        ):
            return

        now = time.monotonic()
        if self._last_global_activation_time is not None:
            if (now - self._last_global_activation_time) < GLOBAL_SHORTCUT_DEBOUNCE_SECONDS:
                return

        if self.state is AppState.RECORDING:
            self._last_global_activation_time = now
            globally_initiated = self._active_recording_globally_initiated
            self._finish_recording()
            if globally_initiated:
                self._raise_to_front()
                if self.state is AppState.AUDIO_READY:
                    self._focus_if_workflow_active(self.play_audio_button)
        elif self.state is AppState.AUDIO_READY:
            if self._send_pending_audio():
                self._last_global_activation_time = now
        else:
            try:
                origin_target = self.terminal_bridge.detect_active_terminal()
            except Exception:
                origin_target = None
            self._origin_terminal_target = origin_target
            self._active_recording_globally_initiated = True
            if self._start_recording(initiated_globally=True):
                self._last_global_activation_time = now
            if self.state is AppState.ERROR:
                self._active_recording_globally_initiated = False
                self._raise_to_front()

    def _raise_to_front(self) -> None:
        """Show the window above other applications, restoring it if minimized."""
        if not self.isVisible():
            self.show()
        current_state = self.windowState()
        is_fullscreen = bool(current_state & Qt.WindowState.WindowFullScreen)
        new_state = (current_state & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive
        if is_fullscreen:
            new_state |= Qt.WindowState.WindowFullScreen
        self.setWindowState(new_state)
        self.raise_()
        self.activateWindow()
    @Slot()
    def _on_space_shortcut_activated(self) -> None:
        if self._is_text_input_focused():
            return
        self._perform_primary_action()

    def _is_text_input_widget(self, watched: QObject | None) -> bool:
        if watched is None:
            return False
        try:
            if isinstance(watched, (QPlainTextEdit, QTextEdit, QLineEdit)):
                return True
            parent = watched.parent()
            if parent is not None and isinstance(parent, (QPlainTextEdit, QTextEdit, QLineEdit)):
                return True
        except (RuntimeError, SystemError):
            return False
        return False

    def _is_text_input_focused(self) -> bool:
        focus_widget = QApplication.focusWidget()
        return self._is_text_input_widget(focus_widget)

    def _focus_if_workflow_active(self, widget: QWidget) -> None:
        if self._is_closing:
            return
        if not (self.isActiveWindow() or QApplication.activeWindow() is self or bool(self.windowState() & Qt.WindowState.WindowActive)):
            return
        if self._settings_dialog is not None and self._settings_dialog.isVisible():
            return
        if self._capture_dialog is not None and self._capture_dialog.isVisible():
            return
        widget.setFocus()

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
            kind == "mouse"
            and self._capture_kind == kind
            and self._capture_generation == generation
            and self._capture_status_label is not None
            and message in MOUSE_CAPTURE_HINT_MESSAGES
        ):
            self._capture_status_label.setText(message)
            return
        if (
            self._capture_dialog is not None
            and self._capture_kind == kind
            and self._capture_generation == generation
        ):
            self._capture_dialog.reject()
        self.status_label.setText(message or BACKEND_FAILURE_MESSAGE)

    @Slot()
    def _perform_primary_action(self) -> None:
        if self._is_reviewing or self.state is AppState.TRANSCRIBING:
            return
        if self.state is AppState.RECORDING:
            self._finish_recording()
        elif self.state is AppState.AUDIO_READY:
            self._send_pending_audio()
        else:
            self._active_recording_globally_initiated = False
            self._start_recording(initiated_globally=False)

    def _start_replacement_recording(self) -> None:
        self._active_recording_globally_initiated = False
        self._start_recording(preserve_pending=True)
    def _persist_selected_microphone(self) -> bool:
        if self.local_store is None:
            return True
        current_idx = self.microphone_combo.currentIndex()
        if current_idx < 0:
            return True
        device = self.microphone_combo.itemData(
            current_idx, Qt.ItemDataRole.UserRole + 1
        )
        if isinstance(device, AudioDevice):
            identity = device.identity
        elif isinstance(device, str):
            identity = device
        else:
            return True
        try:
            self.local_store.save_last_microphone_identity(identity)
            return True
        except Exception:
            return False
    def _start_recording(
        self,
        initiated_globally: bool = False,
        preserve_pending: bool = False,
    ) -> bool:
        if self._is_reviewing or self._thread is not None or self._worker is not None:
            return False
        if not self.settings.has_api_key or self.transcriber is None:
            if preserve_pending and self._pending_capture is not None:
                self.state = AppState.AUDIO_READY
                self.status_label.setText(self.settings.missing_api_key_message)
                self._update_actions()
                return True
            self._set_error(self.settings.missing_api_key_message)
            return True
        if not self._microphone_available or self.microphone_combo.currentData() is None:
            if preserve_pending and self._pending_capture is not None:
                self.state = AppState.AUDIO_READY
                self.status_label.setText("Nenhum microfone de entrada foi detectado.")
                self._update_actions()
                return True
            self._set_error("Nenhum microfone de entrada foi detectado.")
            return True

        if self._is_playing_audio or self._audio_buffer is not None:
            if not self._stop_playback():
                return False

        if not initiated_globally:
            self._origin_terminal_target = None
            self._active_recording_globally_initiated = False

        if not preserve_pending:
            self._pending_capture = None
            self._preserved_capture = None
        else:
            self._preserved_capture = self._pending_capture
        self.payload_debug.clear()
        self.return_debug.clear()
        self.usage_debug.clear()
        try:
            self.recorder.start()
            self.state = AppState.RECORDING
            if self._persist_selected_microphone():
                self.status_label.setText("Gravando áudio…")
            else:
                self.status_label.setText(
                    "Gravando… não foi possível atualizar a memória do microfone."
                )
        except AudioRecorderError as exc:
            if preserve_pending and self._preserved_capture is not None:
                self._pending_capture = self._preserved_capture
                self.state = AppState.AUDIO_READY
                self.status_label.setText(str(exc))
                self._update_actions()
                return True
            self._set_error(str(exc))
            return True
        except Exception:
            if preserve_pending and self._preserved_capture is not None:
                self._pending_capture = self._preserved_capture
                self.state = AppState.AUDIO_READY
                self.status_label.setText("Não foi possível iniciar a gravação do áudio.")
                self._update_actions()
                return True
            self._set_error("Não foi possível iniciar a gravação do áudio.")
            return True
        self._update_actions()
        return True

    def _finish_recording(self) -> None:
        stop_exc: AudioRecorderError | None = None
        try:
            capture = self.recorder.stop()
        except AudioRecorderError as exc:
            stop_exc = exc
            capture = self.recorder.last_capture()

        has_callback_status = bool(self.recorder.last_status())
        is_usable = (
            capture is not None
            and bool(capture.wav_bytes)
            and bool(capture.pcm_bytes)
            and capture.rms >= MIN_RMS_LEVEL
            and not has_callback_status
        )

        if self._preserved_capture is not None and (stop_exc is not None or not is_usable):
            self._pending_capture = self._preserved_capture
            self._preserved_capture = None
            if capture is not None:
                self._render_audio_debug(capture, error=str(stop_exc))
            self.state = AppState.AUDIO_READY
            self.status_label.setText(str(stop_exc))
            self._update_actions()
            self._focus_if_workflow_active(self.play_audio_button)
            return

        if is_usable:
            assert capture is not None
            self._preserved_capture = None
            self._pending_capture = capture
            self._render_audio_debug(capture, error=str(stop_exc) if stop_exc else None)
            self.state = AppState.AUDIO_READY
            if stop_exc is not None:
                self.status_label.setText(
                    f"{stop_exc} Áudio pronto para envio ou reprodução."
                )
            else:
                self.status_label.setText(
                    "Áudio pronto. Reproduza para revisar ou envie explicitamente ao Gemini."
                )
            self._update_actions()
            self._focus_if_workflow_active(self.play_audio_button)
            return

        self._pending_capture = None
        if capture is not None:
            self._render_audio_debug(capture, error=str(stop_exc))
        self._set_error(str(stop_exc))

    @Slot()
    def _toggle_playback(self) -> None:
        if self._is_playing_audio:
            if self._stop_playback():
                self.status_label.setText("Reprodução parada.")
        else:
            self._play_pending_audio()

    def _play_pending_audio(self) -> None:
        capture = self._pending_capture
        if (
            self.state is not AppState.AUDIO_READY
            or capture is None
            or self._thread is not None
            or self._worker is not None
        ):
            return
        if self._is_playing_audio or self._audio_buffer is not None:
            if not self._stop_playback():
                return
        self._audio_byte_array = QByteArray(capture.wav_bytes)
        self._audio_buffer = QBuffer(self)
        self._audio_buffer.setData(self._audio_byte_array)
        if not self._audio_buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            self.status_label.setText("Não foi possível preparar a reprodução do áudio.")
            self._release_audio_source()
            return
        self._playback_generation += 1
        current_gen = self._playback_generation
        self._active_playback_generation = current_gen
        self._is_playing_audio = True
        self.play_audio_button.setText("Parar reprodução")
        self.status_label.setText("Reproduzindo o áudio capturado.")
        self._connect_media_adapters(current_gen)
        try:
            self._media_player.setAudioOutput(self._audio_output)
            self._media_player.setSourceDevice(self._audio_buffer, QUrl("audio.wav"))
            self._media_player.play()
        except Exception:
            if not self._stop_playback():
                return
            self.status_label.setText("Não foi possível reproduzir o áudio.")
    def _stop_playback(self) -> bool:
        if (
            not self._is_playing_audio
            and self._audio_buffer is None
            and not self._media_adapters
        ):
            return True
        if not self._release_audio_source():
            self._is_playing_audio = True
            if hasattr(self, "play_audio_button") and self.play_audio_button is not None:
                self.play_audio_button.setText("Parar reprodução")
            self.status_label.setText("Não foi possível parar a reprodução do áudio.")
            self._update_actions()
            return False
        self._playback_generation += 1
        self._active_playback_generation = None
        self._is_playing_audio = False
        if hasattr(self, "play_audio_button") and self.play_audio_button is not None:
            self.play_audio_button.setText("Reproduzir áudio")
        self._update_actions()
        return True

    def _release_audio_source(self) -> bool:
        if (
            not self._is_playing_audio
            and self._audio_buffer is None
            and not self._media_adapters
        ):
            return True
        detached = False
        if self._media_player is not None:
            try:
                self._media_player.stop()
            except Exception:
                pass
            if hasattr(self._media_player, "setSourceDevice"):
                try:
                    self._media_player.setSourceDevice(None)
                    detached = True
                except Exception:
                    pass
            if hasattr(self._media_player, "setSource"):
                try:
                    self._media_player.setSource(QUrl())
                    detached = True
                except Exception:
                    pass
        else:
            detached = True
        if detached:
            self._disconnect_media_adapters()
            if self._audio_buffer is not None:
                try:
                    self._audio_buffer.close()
                except Exception:
                    pass
                self._audio_buffer.setParent(None)
                self._audio_buffer.deleteLater()
                self._audio_buffer = None
            self._audio_byte_array = None
            return True
        return False

    def _send_pending_audio(self) -> bool:
        if self.state is not AppState.AUDIO_READY or self._pending_capture is None:
            return False
        if self._thread is not None or self._worker is not None:
            return False
        if self.transcriber is None or not self.settings.has_api_key:
            self._set_error(self.settings.missing_api_key_message)
            return False

        if bool(self.transcription_editor.toPlainText().strip()):
            self.status_label.setText(
                "Copie e arquive a transcrição atual antes de enviar outro áudio."
            )
            return False

        if self._is_playing_audio or self._audio_buffer is not None:
            if not self._stop_playback():
                return False
        self.state = AppState.TRANSCRIBING
        self.transcription_editor.setReadOnly(True)
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
        return True

    @Slot(str, object)
    def _on_transcription_finished(
        self,
        text: str,
        debug: TranscriptionDebug | None,
    ) -> None:
        if self._is_closing:
            return
        self.transcription_editor.setPlainText(text)
        self._render_transcription_debug(debug, text=text)
        self._record_and_render_usage(debug, "success")
        self._copy_and_archive_current_transcription(from_transcription=True)

    @Slot()
    def _copy_and_archive_current_transcription(
        self,
        *,
        from_transcription: bool = False,
    ) -> None:
        text = self.transcription_editor.toPlainText()
        if not text.strip():
            self.status_label.setText("Não há texto para copiar e arquivar.")
            return
        QApplication.clipboard().setText(text)
        self.last_message_editor.setPlainText(text)
        self.transcription_editor.clear()

        if from_transcription:
            self._pending_capture = None
            self._stop_playback()
            self.state = AppState.READY
        elif self.state not in (AppState.AUDIO_READY, AppState.RECORDING):
            self.state = AppState.READY

        self.status_label.setText("Texto copiado e movido para Última mensagem.")
        cursor = self.last_message_editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.last_message_editor.setTextCursor(cursor)
        self._focus_if_workflow_active(self.last_message_editor)
        self._update_actions()
    @Slot(str, object)
    def _on_transcription_failed(
        self,
        message: str,
        debug: TranscriptionDebug | None,
    ) -> None:
        if self._is_closing:
            return
        self._render_transcription_debug(debug, error=message)
        self._record_and_render_usage(debug, "error")
        if self._pending_capture is not None:
            self.state = AppState.AUDIO_READY
            self.status_label.setText(message)
        else:
            self._set_error(message)

    def _clear_transcription_worker(self) -> None:
        if self._thread is not None:
            try:
                self._thread.deleteLater()
            except Exception:
                pass
            self._thread = None
        self._worker = None

    @Slot()
    def _on_thread_finished(self) -> None:
        self._clear_transcription_worker()

        if self._is_closing:
            self._finish_deferred_close_if_ready()
            return
        self.transcription_editor.setReadOnly(False)
        if self.state is AppState.TRANSCRIBING:
            self.state = AppState.IDLE
        self._update_actions()

    @Slot(object)
    def _on_media_status_changed(
        self, status: object, generation: int | None = None
    ) -> None:
        if self._is_closing or self.state is not AppState.AUDIO_READY or not self._is_playing_audio:
            return
        if self._active_playback_generation is None:
            return
        if generation is None or generation != self._active_playback_generation:
            return
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self._stop_playback():
                self.status_label.setText("Reprodução concluída.")
                self._focus_if_workflow_active(self.record_button)
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            if self._stop_playback():
                self.status_label.setText("Não foi possível reproduzir o áudio capturado.")
    @Slot(object)
    def _on_playback_state_changed(
        self, state: object, generation: int | None = None
    ) -> None:
        if self._is_closing or self.state is not AppState.AUDIO_READY:
            return
        if self._active_playback_generation is None:
            return
        if generation is None or generation != self._active_playback_generation:
            return
        if state == QMediaPlayer.PlaybackState.StoppedState:
            if self._is_playing_audio:
                self._stop_playback()

    @Slot(object, str)
    def _on_media_error(
        self, error: object, error_string: str = "", generation: int | None = None
    ) -> None:
        del error, error_string
        if self._is_closing or self.state in (
            AppState.RECORDING,
            AppState.TRANSCRIBING,
            AppState.READY,
        ):
            return
        if self._active_playback_generation is None:
            return
        if generation is None or generation != self._active_playback_generation:
            return
        if self._stop_playback():
            self.status_label.setText("Não foi possível reproduzir o áudio capturado.")

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
            payload_lines = [
                f"Modelo: {debug.model}",
                f"Prompt: {debug.prompt}",
            ]
            if debug.audio_mime_type:
                payload_lines.extend(
                    (
                        f"MIME: {debug.audio_mime_type}",
                        f"Áudio: {debug.audio_bytes} bytes",
                        f"Base64: {debug.audio_base64_length} caracteres",
                    )
                )
            else:
                payload_lines.append(f"Texto: {debug.audio_bytes} bytes")

            self.payload_debug.setPlainText("\n".join(payload_lines))
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
    def copy_last_message(self) -> None:
        text = self.last_message_editor.toPlainText()
        if not text.strip():
            self.status_label.setText("Não há texto para copiar.")
            return
        QApplication.clipboard().setText(text)
        self.status_label.setText("Texto copiado.")

    @Slot()
    def clear_last_message(self) -> None:
        if not self.last_message_editor.toPlainText().strip():
            self.status_label.setText("Não há texto para apagar.")
            return
        self._hide_spell_popup()
        self.last_message_editor.clear()
        if self.state is AppState.READY and self._pending_capture is None:
            self.state = AppState.IDLE
        self._update_actions()
        self.status_label.setText("Texto apagado.")

    @Slot()
    def send_to_terminal(self) -> None:
        text = self.last_message_editor.toPlainText()
        try:
            self.terminal_bridge.send_text(
                text,
                lambda value: QApplication.clipboard().setText(value),
                target=self._origin_terminal_target,
            )
        except TerminalBridgeError as exc:
            self.status_label.setText(str(exc))
            return
        self._origin_terminal_target = None
        self.status_label.setText("Texto colado no terminal ativo, sem pressionar Enter.")

    def _update_actions(self) -> None:
        worker_busy = self._thread is not None or self._worker is not None
        busy = self.state is AppState.TRANSCRIBING or worker_busy
        recording = self.state is AppState.RECORDING
        audio_ready = self.state is AppState.AUDIO_READY and not worker_busy
        has_transcription = bool(self.transcription_editor.toPlainText().strip())
        has_last_message = bool(self.last_message_editor.toPlainText().strip())
        reviewing = self._is_reviewing

        if recording:
            primary_text = "Parar e revisar áudio"
        elif audio_ready:
            primary_text = "Enviar para Gemini"
        elif busy:
            primary_text = "Transcrevendo…"
        else:
            primary_text = "Gravar"
        self.record_button.setText(primary_text)
        self.record_button.setEnabled(
            not busy
            and not reviewing
            and self.settings.has_api_key
            and self.transcriber is not None
            and (self._microphone_available or audio_ready)
        )

        self.record_again_button.setEnabled(
            audio_ready
            and not reviewing
            and self._microphone_available
            and self.settings.has_api_key
            and self.transcriber is not None
        )
        self.play_audio_button.setEnabled(audio_ready and not reviewing)
        self.copy_and_archive_button.setEnabled(
            not busy and not reviewing and has_transcription
        )
        self.copy_last_button.setEnabled(
            not busy and not reviewing and has_last_message
        )
        self.review_button.setEnabled(
            not busy
            and not recording
            and self.settings.has_api_key
            and self.transcriber is not None
            and has_last_message
            and not reviewing
        )
        self.clear_last_button.setEnabled(
            not busy and not reviewing and has_last_message
        )
        self.terminal_button.setEnabled(
            not busy and not reviewing and has_last_message
        )
        self.microphone_combo.setEnabled(not busy and not recording and not reviewing)
        self.refresh_microphones_button.setEnabled(not busy and not recording and not reviewing)
        self.settings_button.setEnabled(True)
        self._update_settings_dialog()
        if not self.settings.has_api_key and self.state is AppState.IDLE:
            self.status_label.setText(self.settings.missing_api_key_message)

    def _show_editor_context_menu(self, pos: QPoint) -> None:
        menu = self.last_message_editor.createStandardContextMenu()
        if menu is None:
            menu = QMenu(self.last_message_editor)
        try:
            cursor = self.last_message_editor.cursorForPosition(pos)

            if (
                not self._is_reviewing
                and not self.last_message_editor.isReadOnly()
                and self.highlighter.enabled
                and self.spell_checker.is_available()
            ):
                block = cursor.block()
                block_text = block.text()
                pos_in_block = cursor.positionInBlock()
                tokens = self.spell_checker.tokenize(block_text)
                utf16_map = utf16_code_unit_offsets(block_text)
                matched_token: tuple[int, int, str] | None = None
                for start, end, t_word in tokens:
                    qt_start = utf16_map[start]
                    qt_end = utf16_map[end]
                    if qt_start <= pos_in_block < qt_end:
                        matched_token = (qt_start, qt_end, t_word)
                        break

                if matched_token is not None:
                    qt_start, qt_end, word = matched_token
                    block_pos = block.position()
                    cursor.setPosition(block_pos + qt_start)
                    cursor.setPosition(block_pos + qt_end, QTextCursor.MoveMode.KeepAnchor)

                    if not self.spell_checker.check(word):
                        first_action = menu.actions()[0] if menu.actions() else None
                        suggestions = self.spell_checker.suggest(word, limit=5)
                        for sug in suggestions:
                            sug_act = QAction(sug, menu)
                            sug_act.triggered.connect(
                                lambda checked=False, s=sug, c=cursor: c.insertText(s)
                            )
                            menu.insertAction(first_action, sug_act)

                        if not suggestions:
                            no_sug_act = QAction("Nenhuma sugestão encontrada", menu)
                            no_sug_act.setEnabled(False)
                            menu.insertAction(first_action, no_sug_act)

                        ignore_act = QAction(f'Ignorar "{word}"', menu)
                        ignore_act.triggered.connect(
                            lambda checked=False, w=word: self._ignore_spellcheck_word(w)
                        )
                        menu.insertAction(first_action, ignore_act)
                        menu.insertSeparator(first_action)

            menu.exec(self.last_message_editor.mapToGlobal(pos))
        finally:
            menu.deleteLater()

    def _ignore_spellcheck_word(self, word: str) -> None:
        self.spell_checker.ignore_word(word)
        if self.local_store is not None:
            try:
                self.local_store.add_spellcheck_ignored_word(word)
            except Exception:
                pass
        self.highlighter.rehighlight()

    def _hide_spell_popup(self) -> None:
        if hasattr(self, "_hover_spell_timer") and self._hover_spell_timer.isActive():
            self._hover_spell_timer.stop()
        if (
            hasattr(self, "_popup_dismiss_timer")
            and self._popup_dismiss_timer.isActive()
        ):
            self._popup_dismiss_timer.stop()
        self._is_mouse_over_popup = False
        if hasattr(self, "_spell_popup") and self._spell_popup is not None:
            self._spell_popup.hide()
        self._active_spell_token = None

    def _on_popup_dismiss_timer_timeout(self) -> None:
        if self._spell_popup is None or not self._spell_popup.isVisible():
            return
        cursor_pos = QCursor.pos()
        if self._is_mouse_over_popup or self._spell_popup.geometry().contains(cursor_pos):
            return
        self._hide_spell_popup()

    def _on_editor_cursor_position_changed(self) -> None:
        self._check_spell_under_cursor(self.last_message_editor.textCursor(), source="cursor")

    def _on_hover_spell_timer_timeout(self) -> None:
        if self._last_hover_pos is None:
            return
        cursor = self.last_message_editor.cursorForPosition(self._last_hover_pos)
        self._check_spell_under_cursor(cursor, source="hover")

    def _check_spell_under_cursor(
        self, cursor: QTextCursor, source: str = "cursor"
    ) -> None:
        del source
        if (
            self._is_reviewing
            or self.last_message_editor.isReadOnly()
            or not self.highlighter.enabled
            or not self.spell_checker.is_available()
            or self._spell_popup is None
        ):
            self._hide_spell_popup()
            return

        block = cursor.block()
        block_text = block.text()
        pos_in_block = cursor.positionInBlock()

        tokens = self.spell_checker.tokenize(block_text)
        utf16_map = utf16_code_unit_offsets(block_text)
        matched_token: tuple[int, int, str] | None = None
        for start, end, t_word in tokens:
            qt_start = utf16_map[start]
            qt_end = utf16_map[end]
            if qt_start <= pos_in_block < qt_end:
                matched_token = (qt_start, qt_end, t_word)
                break

        if matched_token is None:
            self._hide_spell_popup()
            return

        qt_start, qt_end, word = matched_token
        if self.spell_checker.check(word):
            self._hide_spell_popup()
            return

        block_pos = block.position()
        global_start = block_pos + qt_start
        global_end = block_pos + qt_end

        if (
            self._spell_popup.isVisible()
            and self._active_spell_token == (global_start, global_end, word)
        ):
            return

        self._active_spell_token = (global_start, global_end, word)
        suggestions = self.spell_checker.suggest(word, limit=3)

        word_cursor = QTextCursor(cursor)
        word_cursor.setPosition(global_start)
        word_cursor.setPosition(global_end, QTextCursor.MoveMode.KeepAnchor)
        cursor_rect = self.last_message_editor.cursorRect(word_cursor)
        global_top_left = self.last_message_editor.viewport().mapToGlobal(cursor_rect.topLeft())
        target_rect = QRect(global_top_left, cursor_rect.size())

        self._spell_popup.show_suggestions(word, suggestions, target_rect)

    def _on_popup_suggestion_selected(self, replacement: str) -> None:
        if self._active_spell_token is None:
            return
        start, end, _word = self._active_spell_token
        cursor = self.last_message_editor.textCursor()
        cursor.beginEditBlock()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(replacement)
        cursor.endEditBlock()
        self.last_message_editor.setTextCursor(cursor)
        self.last_message_editor.setFocus()
        self._hide_spell_popup()

    def _on_popup_ignore_selected(self, word: str) -> None:
        self._ignore_spellcheck_word(word)
        self.last_message_editor.setFocus()
        self._hide_spell_popup()

    @Slot()
    def _review_text_with_ai(self) -> None:
        text = self.last_message_editor.toPlainText()
        if not text.strip():
            self.status_label.setText("Não há texto para revisar.")
            return
        if not self.settings.has_api_key or self.transcriber is None:
            self.status_label.setText(
                "Configure a chave API para revisar o texto com IA."
            )
            return
        if self._is_reviewing:
            return

        self._hide_spell_popup()
        self._is_reviewing = True
        self.last_message_editor.setReadOnly(True)
        self.status_label.setText("Revisando texto com IA...")
        self._update_actions()

        self._proofreading_thread = QThread(self)
        self._proofreading_worker = ProofreadingWorker(self.transcriber, text)
        self._proofreading_worker.moveToThread(self._proofreading_thread)

        self._proofreading_thread.started.connect(self._proofreading_worker.run)
        self._proofreading_worker.finished.connect(self._on_proofreading_finished)
        self._proofreading_worker.failed.connect(self._on_proofreading_failed)
        self._proofreading_worker.finished.connect(self._proofreading_thread.quit)
        self._proofreading_worker.failed.connect(self._proofreading_thread.quit)
        self._proofreading_worker.finished.connect(self._proofreading_worker.deleteLater)
        self._proofreading_worker.failed.connect(self._proofreading_worker.deleteLater)
        self._proofreading_thread.finished.connect(self._on_proofreading_thread_finished)
        self._proofreading_thread.start()

    @Slot(str, object)
    def _on_proofreading_finished(
        self,
        revised_text: str,
        debug: TranscriptionDebug | None,
    ) -> None:
        if self._is_closing:
            return
        cursor = self.last_message_editor.textCursor()
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(revised_text)
        cursor.endEditBlock()

        QApplication.clipboard().setText(revised_text)

        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.last_message_editor.setTextCursor(cursor)

        self._render_transcription_debug(debug, text=revised_text)
        self._record_and_render_usage(debug, "success")
        self.status_label.setText("Texto revisado e copiado.")
        self._focus_if_workflow_active(self.last_message_editor)

    @Slot(str, object)
    def _on_proofreading_failed(
        self,
        message: str,
        debug: TranscriptionDebug | None,
    ) -> None:
        if self._is_closing:
            return
        self._render_transcription_debug(debug, error=message)
        self._record_and_render_usage(debug, "error")
        self.status_label.setText(message)

    def _clear_proofreading_worker(self) -> None:
        if self._proofreading_thread is not None:
            try:
                self._proofreading_thread.deleteLater()
            except Exception:
                pass
            self._proofreading_thread = None
        self._proofreading_worker = None

    @Slot()
    def _on_proofreading_thread_finished(self) -> None:
        self._clear_proofreading_worker()

        if self._is_closing:
            self._finish_deferred_close_if_ready()
            return
        self.last_message_editor.setReadOnly(False)
        self._is_reviewing = False
        self._update_actions()
    def _set_error(self, message: str) -> None:
        self.state = AppState.ERROR
        self.status_label.setText(message)
        self._update_actions()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._hide_spell_popup()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._hide_spell_popup()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ShortcutOverride:
            if isinstance(event, QKeyEvent) and event.key() == Qt.Key.Key_Space:
                if self._is_text_input_focused():
                    event.accept()
                    return True
        return super().event(event)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        try:
            event_type = event.type()
        except (RuntimeError, SystemError):
            return False

        if event_type == QEvent.Type.ShortcutOverride:
            if isinstance(event, QKeyEvent) and event.key() == Qt.Key.Key_Space:
                if self._is_text_input_widget(watched) or self._is_text_input_focused():
                    event.accept()
                    return True
        if not hasattr(self, "last_message_editor") or self.last_message_editor is None:
            return super().eventFilter(watched, event)
        try:
            is_viewport = watched is self.last_message_editor.viewport()
        except (RuntimeError, SystemError):
            is_viewport = False

        if is_viewport:
            if event_type == QEvent.Type.MouseMove:
                if (
                    hasattr(self, "_popup_dismiss_timer")
                    and self._popup_dismiss_timer.isActive()
                ):
                    self._popup_dismiss_timer.stop()
                pos = (
                    event.position().toPoint()
                    if hasattr(event, "position")
                    else event.pos()
                )
                self._last_hover_pos = pos
                self._hover_spell_timer.start(250)
            elif event_type == QEvent.Type.Leave:
                if (
                    hasattr(self, "_hover_spell_timer")
                    and self._hover_spell_timer.isActive()
                ):
                    self._hover_spell_timer.stop()
                if self._spell_popup is not None and self._spell_popup.isVisible():
                    self._popup_dismiss_timer.start(200)
            elif event_type in (QEvent.Type.Wheel, QEvent.Type.Resize):
                if (
                    hasattr(self, "_hover_spell_timer")
                    and self._hover_spell_timer.isActive()
                ):
                    self._hover_spell_timer.stop()
                self._hide_spell_popup()
        elif hasattr(self, "_spell_popup") and self._spell_popup is not None and watched is self._spell_popup:
            if event_type in (QEvent.Type.Enter, QEvent.Type.MouseMove):
                self._is_mouse_over_popup = True
                if (
                    hasattr(self, "_popup_dismiss_timer")
                    and self._popup_dismiss_timer.isActive()
                ):
                    self._popup_dismiss_timer.stop()
            elif event_type == QEvent.Type.Leave:
                self._is_mouse_over_popup = False
                cursor_pos = QCursor.pos()
                viewport = self.last_message_editor.viewport()
                vp_pos = viewport.mapFromGlobal(cursor_pos)
                is_over_token = False
                if (
                    viewport.rect().contains(vp_pos)
                    and self._active_spell_token is not None
                ):
                    text_cursor = self.last_message_editor.cursorForPosition(vp_pos)
                    start, end, _word = self._active_spell_token
                    pos = text_cursor.position()
                    if start <= pos <= end:
                        is_over_token = True
                if not is_over_token:
                    self._hide_spell_popup()
        elif hasattr(self, "last_message_editor") and self.last_message_editor is not None and watched is self.last_message_editor:
            event_type = event.type()
            if event_type == QEvent.Type.KeyPress:
                if event.key() == Qt.Key.Key_Escape:
                    if self._spell_popup is not None and self._spell_popup.isVisible():
                        self._hide_spell_popup()
                        return True
            elif event_type in (QEvent.Type.Resize, QEvent.Type.Move):
                self._hide_spell_popup()
        return super().eventFilter(watched, event)
    def _finish_deferred_close_if_ready(self) -> None:
        if not self._is_closing or not self._close_pending:
            return
        if (
            self._thread is not None
            or self._worker is not None
            or self._proofreading_thread is not None
            or self._proofreading_worker is not None
        ):
            return
        self._close_pending = False
        if not self._final_close_scheduled:
            self._final_close_scheduled = True
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event: QCloseEvent) -> None:
        if (
            self.homebrew_update_controller is not None
            and self.homebrew_update_controller.running
        ):
            self.status_label.setText(
                "A atualização pelo Homebrew está em andamento. Aguarde a conclusão."
            )
            event.ignore()
            return
        if self._is_closing:
            had_deferred_work = (
                self._close_pending
                or self._thread is not None
                or self._worker is not None
                or self._proofreading_thread is not None
                or self._proofreading_worker is not None
            )
            if self._thread is not None and not self._thread.isRunning():
                self._clear_transcription_worker()
            elif self._thread is None and self._worker is not None:
                self._clear_transcription_worker()

            if (
                self._proofreading_thread is not None
                and not self._proofreading_thread.isRunning()
            ):
                self._clear_proofreading_worker()
            elif (
                self._proofreading_thread is None
                and self._proofreading_worker is not None
            ):
                self._clear_proofreading_worker()

            if self._close_pending:
                self._finish_deferred_close_if_ready()

            if (
                had_deferred_work
                or self._close_pending
                or self._thread is not None
                or self._worker is not None
                or self._proofreading_thread is not None
                or self._proofreading_worker is not None
            ):
                event.ignore()
                return
            event.accept()
            return
        if not self._stop_playback():
            self.status_label.setText("Não foi possível parar a reprodução do áudio.")
            event.ignore()
            return
        self._is_closing = True
        if hasattr(self, "_hover_spell_timer") and self._hover_spell_timer.isActive():
            self._hover_spell_timer.stop()
        if (
            hasattr(self, "_popup_dismiss_timer")
            and self._popup_dismiss_timer.isActive()
        ):
            self._popup_dismiss_timer.stop()
        if hasattr(self, "_spell_popup") and self._spell_popup is not None:
            try:
                self._spell_popup.close()
            except Exception:
                pass
            self._spell_popup = None
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
            except Exception:
                pass
        if QApplication.instance() is not None:
            try:
                QApplication.instance().removeEventFilter(self)
            except Exception:
                pass
        self._pending_capture = None
        self._preserved_capture = None
        self._origin_terminal_target = None
        if self.local_store is not None:
            store = self.local_store
            self.local_store = None
            try:
                store.close()
            except Exception:
                pass

        transcription_running = (
            self._thread is not None and self._thread.isRunning()
        )
        proofreading_running = (
            self._proofreading_thread is not None
            and self._proofreading_thread.isRunning()
        )
        if transcription_running and self._thread is not None:
            try:
                self._thread.quit()
            except Exception:
                pass
        else:
            self._clear_transcription_worker()

        if proofreading_running and self._proofreading_thread is not None:
            try:
                self._proofreading_thread.quit()
            except Exception:
                pass
        else:
            self._clear_proofreading_worker()

        if transcription_running or proofreading_running:
            self._close_pending = True
            self.hide()
            event.ignore()
            return

        event.accept()
