from __future__ import annotations

from PySide6.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat, QTextDocument
from PySide6.QtWidgets import QPlainTextEdit, QTextEdit

from .spellcheck import LocalSpellChecker, utf16_code_unit_offsets

ERROR_UNDERLINE_COLOR = "#EF4444"


class SpellHighlighter(QSyntaxHighlighter):
    """Sublinhador ortográfico visual para editores Qt."""

    def __init__(
        self,
        parent: QTextDocument | QTextEdit | QPlainTextEdit | None = None,
        spell_checker: LocalSpellChecker | None = None,
        enabled: bool = True,
    ) -> None:
        if isinstance(parent, (QTextEdit, QPlainTextEdit)):
            super().__init__(parent.document())
        else:
            super().__init__(parent)

        self._spell_checker: LocalSpellChecker | None = spell_checker
        self._enabled: bool = bool(enabled)

        self._error_format = QTextCharFormat()
        self._error_format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SpellCheckUnderline)
        self._error_format.setUnderlineColor(QColor(ERROR_UNDERLINE_COLOR))

    @property
    def enabled(self) -> bool:
        """Indica se o sublinhado ortográfico está ativo."""
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        new_value = bool(value)
        if self._enabled != new_value:
            self._enabled = new_value
            self.rehighlight()

    @property
    def spell_checker(self) -> LocalSpellChecker | None:
        """Instância do corretor ortográfico associada ao highlighter."""
        return self._spell_checker

    @spell_checker.setter
    def spell_checker(self, checker: LocalSpellChecker | None) -> None:
        if self._spell_checker is not checker:
            self._spell_checker = checker
            self.rehighlight()

    def highlightBlock(self, text: str) -> None:
        """Destaca com sublinhado vermelho ondulado palavras desconhecidas pelo corretor."""
        if not self._enabled:
            return
        if self._spell_checker is None or not self._spell_checker.is_available():
            return

        tokens = self._spell_checker.tokenize(text)
        if not tokens:
            return

        utf16_map = utf16_code_unit_offsets(text)
        for start, end, word in tokens:
            if not self._spell_checker.check(word):
                qt_start = utf16_map[start]
                qt_length = utf16_map[end] - qt_start
                self.setFormat(qt_start, qt_length, self._error_format)
