from __future__ import annotations

import ctypes
import sys
from typing import Iterable
from PySide6.QtGui import QTextCharFormat, QTextDocument
from PySide6.QtWidgets import QApplication, QPlainTextEdit
import pytest

from falafacil.spell_highlighter import SpellHighlighter
from falafacil.spellcheck import LocalSpellChecker, utf16_code_unit_offsets


class MockCFunction:
    """Invólucro para emular funções C chamadas via ctypes com suporte a restype/argtypes."""

    def __init__(self, func) -> None:
        self.func = func
        self.restype = None
        self.argtypes = None

    def __call__(self, *args, **kwargs):
        return self.func(*args, **kwargs)


class FakeEnchantCDLL:
    """Mock da biblioteca nativa libenchant-2 para testes determinísticos de ctypes."""

    def __init__(
        self,
        valid_words: Iterable[str] | None = None,
        suggestions: dict[str, list[str]] | None = None,
        dict_exists: bool = True,
        check_map: dict[str, int] | None = None,
    ) -> None:
        self.valid_words = {
            w.lower() for w in (valid_words or ["computador", "correto", "teste"])
        }
        self.suggestions = suggestions or {"computadro": ["computador", "computadora"]}
        self._dict_exists_flag = dict_exists
        self.check_map = check_map
        self.checked_terms: list[str] = []
        self._keepalive: list[object] = []

        self.enchant_broker_init = MockCFunction(self._broker_init)
        self.enchant_broker_free = MockCFunction(self._broker_free)
        self.enchant_broker_dict_exists = MockCFunction(self._dict_exists)
        self.enchant_broker_request_dict = MockCFunction(self._request_dict)
        self.enchant_broker_free_dict = MockCFunction(self._free_dict)
        self.enchant_dict_check = MockCFunction(self._dict_check)
        self.enchant_dict_suggest = MockCFunction(self._dict_suggest)
        self.enchant_dict_free_string_list = MockCFunction(self._free_string_list)

    def _broker_init(self) -> int:
        return 0x1000

    def _broker_free(self, broker: int) -> None:
        pass

    def _dict_exists(self, broker: int, tag_bytes: bytes) -> int:
        return 1 if self._dict_exists_flag else 0

    def _request_dict(self, broker: int, tag_bytes: bytes) -> int:
        return 0x2000 if self._dict_exists_flag else 0

    def _free_dict(self, broker: int, dict_handle: int) -> None:
        pass

    def _dict_check(self, dict_handle: int, word_bytes: bytes, length: int) -> int:
        w = (
            word_bytes.decode("utf-8")
            if isinstance(word_bytes, bytes)
            else str(word_bytes)
        )
        self.checked_terms.append(w)
        if self.check_map is not None and w in self.check_map:
            return self.check_map[w]
        return 0 if w.lower() in self.valid_words else 1
    def _dict_suggest(
        self, dict_handle: int, word_bytes: bytes, length: int, n_suggs_ptr: object
    ) -> object:
        w = (
            word_bytes.decode("utf-8")
            if isinstance(word_bytes, bytes)
            else str(word_bytes)
        )
        suggs = [
            s.encode("utf-8")
            for s in self.suggestions.get(w.lower(), ["sugestao"])
        ]
        arr_type = ctypes.c_char_p * len(suggs)
        arr = arr_type(*suggs)
        self._keepalive.append(arr)

        if hasattr(n_suggs_ptr, "_obj"):
            n_suggs_ptr._obj.value = len(suggs)
        elif hasattr(n_suggs_ptr, "contents"):
            n_suggs_ptr.contents.value = len(suggs)

        return arr

    def _free_string_list(self, dict_handle: int, string_list_ptr: object) -> None:
        pass

@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def test_spellcheck_deterministic_with_fake_enchant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_lib = FakeEnchantCDLL(
        valid_words=["computador", "paralelepípedo", "correto", "coração"],
        suggestions={"computadro": ["computador", "computadora"]},
    )
    monkeypatch.setattr(
        "falafacil.spellcheck._load_enchant_library", lambda lib_path=None: fake_lib
    )

    checker = LocalSpellChecker()
    assert checker.is_available() is True

    # Validação de palavras corretas
    assert checker.check("computador") is True
    assert checker.check("paralelepípedo") is True
    assert checker.check("correto") is True
    assert checker.check("coração") is True
    assert checker.check("Computador") is True

    # Validação de palavras incorretas
    assert checker.check("computadro") is False
    assert checker.check("errrrooo") is False

    # Sugestões
    suggs = checker.suggest("computadro", limit=5)
    assert "computador" in suggs
    assert len(suggs) <= 5

    # Palavras ignoradas
    assert checker.is_ignored("computadro") is False
    checker.ignore_word("computadro")
    assert checker.is_ignored("computadro") is True
    assert checker.check("computadro") is True

    checker.close()
    assert checker.is_available() is False



def test_spellcheck_three_way_return_contract_and_fail_soft(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 0 = correct, >0 = misspelled, <0 = native error (fail-soft -> returns True)
    fake_lib = FakeEnchantCDLL(
        valid_words=["correto", "valido"],
        check_map={
            "correto": 0,
            "errado": 1,
            "outro_erro": 2,
            "falha_nativa": -1,
            "falha_grave": -2,
            "falhanativa": -1,
            # Capitalized original negative (fail-soft with 1 native call, no lowercase retry)
            "Falhacapital": -1,
            # Capitalized original positive, lowercase retry zero
            "Brasil": 1,
            "brasil": 0,
            # Capitalized original positive, lowercase retry negative (fail-soft)
            "Falhanativa": 1,
            # Capitalized original positive, lowercase retry positive (misspelled)
            "Incorreto": 1,
            "incorreto": 1,
        },
    )
    monkeypatch.setattr(
        "falafacil.spellcheck._load_enchant_library", lambda lib_path=None: fake_lib
    )

    checker = LocalSpellChecker()
    try:
        assert checker.is_available() is True
        fake_lib.checked_terms.clear()

        # 0 -> True
        assert checker.check("correto") is True
        assert fake_lib.checked_terms == ["correto"]

        # positive -> False
        fake_lib.checked_terms.clear()
        assert checker.check("errado") is False
        assert fake_lib.checked_terms == ["errado"]

        fake_lib.checked_terms.clear()
        assert checker.check("outro_erro") is False
        assert fake_lib.checked_terms == ["outro_erro"]

        # negative (native error) -> True (fail-soft)
        fake_lib.checked_terms.clear()
        assert checker.check("falha_nativa") is True
        assert fake_lib.checked_terms == ["falha_nativa"]

        fake_lib.checked_terms.clear()
        assert checker.check("falha_grave") is True
        assert fake_lib.checked_terms == ["falha_grave"]

        # Capitalized original negative -> True (fail-soft with exactly one native call, NO lowercase retry)
        fake_lib.checked_terms.clear()
        assert checker.check("Falhacapital") is True
        assert fake_lib.checked_terms == ["Falhacapital"]

        # Capitalized: original positive, lowercase retry zero -> True (two native calls)
        fake_lib.checked_terms.clear()
        assert checker.check("Brasil") is True
        assert fake_lib.checked_terms == ["Brasil", "brasil"]

        # Capitalized: original positive, lowercase retry negative -> True (fail-soft, two native calls)
        fake_lib.checked_terms.clear()
        assert checker.check("Falhanativa") is True
        assert fake_lib.checked_terms == ["Falhanativa", "falhanativa"]

        # Capitalized: original positive, lowercase retry positive -> False (two native calls)
        fake_lib.checked_terms.clear()
        assert checker.check("Incorreto") is False
        assert fake_lib.checked_terms == ["Incorreto", "incorreto"]

        # Exercise SpellHighlighter for native-negative words and assert no underline format is created
        doc = QTextDocument()
        highlighter = SpellHighlighter(doc, spell_checker=checker, enabled=True)

        # Document with native-negative words ("Falhacapital", "falhanativa"), valid words ("correto"), and misspelled ("errado")
        doc.setPlainText("Falhacapital falhanativa correto errado")
        highlighter.rehighlight()

        block = doc.firstBlock()
        formats = block.layout().formats()
        assert len(formats) == 1
        errado_idx = doc.toPlainText().index("errado")
        assert formats[0].start == errado_idx
        assert formats[0].length == len("errado")
        assert (
            formats[0].format.underlineStyle()
            == QTextCharFormat.UnderlineStyle.SpellCheckUnderline
        )

        # Document containing ONLY native-negative words creates zero underline formats
        doc.setPlainText("Falhacapital falhanativa")
        highlighter.rehighlight()
        assert len(doc.firstBlock().layout().formats()) == 0
    finally:
        checker.close()

def test_spell_highlighter_deterministic_with_fake_enchant(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_lib = FakeEnchantCDLL(
        valid_words=["o", "computador", "está", "correto", "mas", "com", "erro"],
        suggestions={"computadro": ["computador"]},
    )
    monkeypatch.setattr(
        "falafacil.spellcheck._load_enchant_library", lambda lib_path=None: fake_lib
    )

    checker = LocalSpellChecker()
    assert checker.is_available() is True

    doc = QTextDocument()
    highlighter = SpellHighlighter(doc, spell_checker=checker, enabled=True)

    text = "O computador está correto mas o computadro está com erro."
    doc.setPlainText(text)
    highlighter.rehighlight()

    computadro_idx = text.index("computadro")
    block = doc.firstBlock()
    formats = block.layout().formats()
    assert len(formats) == 1
    assert formats[0].start == computadro_idx
    assert formats[0].length == len("computadro")
    assert (
        formats[0].format.underlineStyle()
        == QTextCharFormat.UnderlineStyle.SpellCheckUnderline
    )
    assert formats[0].format.underlineColor().name().upper() == "#EF4444"

    # Ignorar palavra remove sublinhado
    checker.ignore_word("computadro")
    highlighter.rehighlight()
    assert len(doc.firstBlock().layout().formats()) == 0

    # Palavra corrigida também não gera sublinhado
    checker.close()
    checker2 = LocalSpellChecker()
    highlighter.spell_checker = checker2
    doc.setPlainText("O computador está correto mas o computador está com erro.")
    highlighter.rehighlight()
    assert len(doc.firstBlock().layout().formats()) == 0

    # Desabilitar highlighter remove qualquer sublinhado
    doc.setPlainText("O computador está correto mas o computadro está com erro.")
    highlighter.rehighlight()
    assert len(doc.firstBlock().layout().formats()) == 1
    highlighter.enabled = False
    assert len(doc.firstBlock().layout().formats()) == 0

    checker2.close()

def test_spellcheck_availability_and_basic_words() -> None:
    checker = LocalSpellChecker()
    # No ambiente com libenchant e hunspell-pt-br instalados
    if checker.is_available():
        # Palavras corretas
        assert checker.check("computador") is True
        assert checker.check("paralelepípedo") is True
        assert checker.check("coração") is True
        assert checker.check("guarda-chuva") is True
        assert checker.check("anti-inflamatório") is True

        # Palavras com erros ortográficos evidentes
        assert checker.check("computadro") is False
        assert checker.check("errrrooo") is False
        assert checker.check("paralelepipedu") is False
    checker.close()


def test_spellcheck_capitalized_and_acronyms() -> None:
    checker = LocalSpellChecker()
    if checker.is_available():
        # Palavras com inicial maiúscula
        assert checker.check("Computador") is True
        assert checker.check("Paralelepípedo") is True
        assert checker.check("Computadro") is False

        # Siglas e acrônimos (all-caps >= 2 chars)
        assert checker.check("CPF") is True
        assert checker.check("CNPJ") is True
        assert checker.check("API") is True
        assert checker.check("RAM") is True
        assert checker.check("CPU") is True
    checker.close()


def test_spellcheck_numbers_and_short_words() -> None:
    checker = LocalSpellChecker()
    # Palavras curtas (<=1 caractere) são aceitas
    assert checker.check("a") is True
    assert checker.check("e") is True
    assert checker.check("o") is True
    assert checker.check("") is True
    assert checker.check(" ") is True

    # Palavras com dígitos
    assert checker.check("win32") is True
    assert checker.check("mp3") is True
    assert checker.check("h2o") is True
    assert checker.check("12345") is True
    checker.close()


def test_spellcheck_suggestions() -> None:
    checker = LocalSpellChecker()
    if checker.is_available():
        suggestions = checker.suggest("computadro", limit=5)
        assert len(suggestions) > 0
        assert len(suggestions) <= 5
        assert "computador" in suggestions

        # Limite respeitado
        suggs_2 = checker.suggest("computadro", limit=2)
        assert len(suggs_2) == 2

        # Palavra vazia
        assert checker.suggest("") == []
        assert checker.suggest("   ") == []
    checker.close()


def test_spellcheck_ignored_words() -> None:
    checker = LocalSpellChecker()
    if checker.is_available():
        # Inicialmente palavra com erro não é aceita
        assert checker.check("palavraincorreta123xyz") is True  # Contém dígitos -> True
        assert checker.check("palavraincorretaxyz") is False

        # Adiciona a ignoradas
        checker.ignore_word("palavraincorretaxyz")
        assert checker.is_ignored("palavraincorretaxyz") is True
        assert checker.is_ignored("PALAVRAINCORRETAXYZ") is True
        assert "palavraincorretaxyz" in checker.ignored_words()

        # Agora é aceita
        assert checker.check("palavraincorretaxyz") is True
        assert checker.check("PALAVRAINCORRETAXYZ") is True
    checker.close()


def test_spellcheck_initial_ignored_words() -> None:
    checker = LocalSpellChecker(ignored_words=["docker", "Kubernetes", "falafacil"])
    assert checker.is_ignored("docker") is True
    assert checker.is_ignored("DOCKER") is True
    assert checker.is_ignored("kubernetes") is True
    assert checker.is_ignored("falafacil") is True

    assert checker.check("docker") is True
    assert checker.check("Kubernetes") is True
    assert checker.check("falafacil") is True
    checker.close()


def test_spellcheck_tokenization_with_urls_paths_and_emails() -> None:
    checker = LocalSpellChecker()
    text = (
        "Consulte https://exemplo.com/docs/api.html e veja /etc/default/grub "
        "ou envie para suporte@exemplo.com.br o relatório do computador!"
    )
    tokens = checker.tokenize(text)

    words = [t[2] for t in tokens]
    # Palavras esperadas
    assert "Consulte" in words
    assert "e" in words
    assert "veja" in words
    assert "ou" in words
    assert "envie" in words
    assert "para" in words
    assert "o" in words
    assert "relatório" in words
    assert "do" in words
    assert "computador" in words

    # Termos de URL, caminho e e-mail não devem aparecer como tokens
    assert "https" not in words
    assert "exemplo" not in words
    assert "docs" not in words
    assert "etc" not in words
    assert "default" not in words
    assert "grub" not in words
    assert "suporte" not in words

    # Verifica integridade dos offsets
    for start, end, word in tokens:
        assert text[start:end] == word

    checker.close()


def test_spellcheck_tokenization_with_alphanumeric_digits() -> None:
    checker = LocalSpellChecker()
    text = (
        "O arquivo mp3 no win32 precisa de h2o e hash sha256 para funcionar corretamente."
    )
    tokens = checker.tokenize(text)
    words = [t[2] for t in tokens]

    assert "O" in words
    assert "arquivo" in words
    assert "no" in words
    assert "precisa" in words
    assert "de" in words
    assert "e" in words
    assert "hash" in words
    assert "para" in words
    assert "funcionar" in words
    assert "corretamente" in words

    assert "mp3" not in words
    assert "mp" not in words
    assert "win32" not in words
    assert "win" not in words
    assert "h2o" not in words
    assert "h" not in words
    assert "sha256" not in words
    assert "sha" not in words

    for start, end, word in tokens:
        assert text[start:end] == word

    checker.close()

def test_spellcheck_tokenization_ignores_relative_paths_and_filenames() -> None:
    checker = LocalSpellChecker()
    text = (
        "Acesse www.exemplo.com para ler a documentação e verifique o script main.py "
        "além do arquivo src/falafacil/ui.py para confirmar os testes."
    )
    tokens = checker.tokenize(text)
    words = [t[2] for t in tokens]

    # Palavras legítimas preservadas
    assert "Acesse" in words
    assert "para" in words
    assert "ler" in words
    assert "a" in words
    assert "documentação" in words
    assert "e" in words
    assert "verifique" in words
    assert "o" in words
    assert "script" in words
    assert "além" in words
    assert "do" in words
    assert "arquivo" in words
    assert "confirmar" in words
    assert "os" in words
    assert "testes" in words

    # www URL sem esquema não deve gerar tokens
    assert "www" not in words
    assert "exemplo" not in words
    assert "com" not in words

    # Nome de arquivo comum não deve gerar tokens
    assert "main" not in words
    assert "py" not in words

    # Caminho relativo com barras não deve gerar tokens
    assert "src" not in words
    assert "falafacil" not in words
    assert "ui" not in words

    for start, end, word in tokens:
        assert text[start:end] == word

    checker.close()

def test_spellcheck_fail_soft_missing_library() -> None:
    checker = LocalSpellChecker(lib_path="/caminho/inexistente/libenchant-inexistente.so")
    assert checker.is_available() is False

    # Todos os métodos devem operar sem exceção (fail-soft)
    assert checker.check("computadro") is True
    assert checker.check("palavra_qualquer") is True
    assert checker.suggest("computadro") == []
    assert checker.is_ignored("termo") is False

    checker.ignore_word("termo")
    assert checker.is_ignored("termo") is True
    assert "termo" in checker.ignored_words()

    tokens = checker.tokenize("Olá mundo!")
    assert [t[2] for t in tokens] == ["Olá", "mundo"]

    checker.close()


def test_spellcheck_fail_soft_invalid_dictionary_tag() -> None:
    checker = LocalSpellChecker(dictionary_tag="tag_linguagem_totalmente_invalida_123")
    assert checker.is_available() is False
    assert checker.check("computadro") is True
    checker.close()


def test_spellcheck_close_and_context_manager() -> None:
    with LocalSpellChecker() as checker:
        available_initially = checker.is_available()
    # Ao sair do bloco 'with', close() foi chamado
    assert checker.is_available() is False

    # Múltiplas chamadas a close() são seguras
    checker.close()
    checker.close()


def test_spell_highlighter_highlight_block(qapp: QApplication) -> None:
    checker = LocalSpellChecker()
    doc = QTextDocument()
    highlighter = SpellHighlighter(doc, spell_checker=checker, enabled=True)

    text = "O computador está correto mas o computadro está com erro."
    doc.setPlainText(text)
    highlighter.rehighlight()

    # Se checker disponível, computadro deve ter formato com SpellCheckUnderline
    if checker.is_available():
        computadro_idx = text.index("computadro")

        block = doc.firstBlock()
        formats = block.layout().formats()
        assert len(formats) == 1
        assert formats[0].start == computadro_idx
        assert formats[0].length == len("computadro")
        assert (
            formats[0].format.underlineStyle()
            == QTextCharFormat.UnderlineStyle.SpellCheckUnderline
        )
        assert formats[0].format.underlineColor().name().upper() == "#EF4444"

        # Desabilita e rehighlight
        highlighter.enabled = False
        assert highlighter.enabled is False
        assert len(doc.firstBlock().layout().formats()) == 0

        # Re-habilita
        highlighter.enabled = True
        assert highlighter.enabled is True
        assert len(doc.firstBlock().layout().formats()) == 1

        # Troca checker por None
        highlighter.spell_checker = None
        assert highlighter.spell_checker is None
        assert len(doc.firstBlock().layout().formats()) == 0
    checker.close()


def test_spell_highlighter_with_qplaintextedit(qapp: QApplication) -> None:
    editor = QPlainTextEdit()
    checker = LocalSpellChecker()
    highlighter = SpellHighlighter(editor, spell_checker=checker, enabled=True)
    assert highlighter.enabled is True
    assert highlighter.spell_checker is checker

    editor.setPlainText("Texto com computadro errado.")
    highlighter.rehighlight()
    checker.close()


def test_utf16_code_unit_offsets() -> None:
    """Verifica mapeamento de code points Python para offsets UTF-16 do Qt."""
    # String vazia
    assert utf16_code_unit_offsets("") == [0]

    # Apenas BMP (1 unidade por code point)
    assert utf16_code_unit_offsets("abc") == [0, 1, 2, 3]
    assert utf16_code_unit_offsets("olá mundo") == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]

    # Emoji único (caractere não-BMP ord > 0xFFFF, 2 unidades UTF-16)
    assert utf16_code_unit_offsets("😀") == [0, 2]

    # Emoji seguido de espaço e palavra inválida
    text = "😀 errrrooo"
    offsets = utf16_code_unit_offsets(text)
    assert offsets == [0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]

    # Invariante com o tamanho UTF-16 em bytes
    test_cases = ["", "abc", "olá mundo", "😀", "😀 errrrooo", "🚀🎉 café 🇧🇷"]
    for s in test_cases:
        offs = utf16_code_unit_offsets(s)
        assert len(offs) == len(s) + 1
        assert offs[0] == 0
        expected_total_utf16 = len(s.encode("utf-16-le")) // 2
        assert offs[-1] == expected_total_utf16


def test_spell_highlighter_non_bmp_emoji_offset(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifica que SpellHighlighter alinha o formato na posição UTF-16 na presença de emoji."""
    fake_lib = FakeEnchantCDLL(
        valid_words=["o", "computador"],
        suggestions={"errrrooo": ["erro"]},
    )
    monkeypatch.setattr(
        "falafacil.spellcheck._load_enchant_library", lambda lib_path=None: fake_lib
    )

    checker = LocalSpellChecker()
    assert checker.is_available() is True

    doc = QTextDocument()
    highlighter = SpellHighlighter(doc, spell_checker=checker, enabled=True)

    # "😀 errrrooo": em Python, 'errrrooo' começa no índice 2.
    # Em UTF-16, '😀' ocupa 2 unidades e ' ' ocupa 1, então 'errrrooo' começa em 3.
    text = "😀 errrrooo"
    doc.setPlainText(text)
    highlighter.rehighlight()

    block = doc.firstBlock()
    formats = block.layout().formats()
    assert len(formats) == 1
    assert formats[0].start == 3
    assert formats[0].length == len("errrrooo")
    assert (
        formats[0].format.underlineStyle()
        == QTextCharFormat.UnderlineStyle.SpellCheckUnderline
    )
    assert formats[0].format.underlineColor().name().upper() == "#EF4444"

    checker.close()
