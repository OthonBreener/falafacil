from __future__ import annotations

import ctypes
import ctypes.util
import re
from typing import Iterable

# Padrões para ignorar URLs, caminhos de arquivo Unix, e-mails e termos alfanuméricos com dígitos
_URL_PATTERN = re.compile(r"https?://\S+")
_WWW_URL_PATTERN = re.compile(r"\bwww\.[^\s/$.?#].[^\s]*")
_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PATH_PATTERN = re.compile(r"(?:/[\w.-]+)+")
_RELATIVE_PATH_PATTERN = re.compile(r"\b[\w.-]+/(?:[\w.-]+/)*[\w.-]+")
_FILENAME_PATTERN = re.compile(
    r"\b[\w-]+\.(?:py|js|ts|json|txt|wav|md|toml|yml|yaml|spec|rb|sh|c|h|cpp|so|dll|exe|sqlite3|csv|html|css)\b"
)
_ALPHANUMERIC_WITH_DIGIT_PATTERN = re.compile(r"\b\w*\d\w*\b")

# Padrão de palavras pt-BR (com suporte a acentuação e hífen entre letras)
_WORD_PATTERN = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]+(?:-[A-Za-zÀ-ÖØ-öø-ÿ]+)*")

def _load_enchant_library(lib_path: str | None = None) -> ctypes.CDLL | None:
    """Tenta carregar libenchant-2 de forma fail-soft."""
    candidates: list[str] = []
    if lib_path:
        candidates.append(lib_path)
    else:
        found = ctypes.util.find_library("enchant-2")
        if found:
            candidates.append(found)
        candidates.extend(
            [
                "libenchant-2.so.2",
                "libenchant-2.so",
                "libenchant.so.1",
            ]
        )

    for candidate in candidates:
        try:
            lib = ctypes.CDLL(candidate)
            if hasattr(lib, "enchant_broker_init") and hasattr(lib, "enchant_dict_check"):
                return lib
        except (OSError, AttributeError):
            continue
    return None

def utf16_code_unit_offsets(text: str) -> list[int]:
    """Mapeia cada índice de código Python (code point) para seu offset em unidades UTF-16."""
    offsets = [0] * (len(text) + 1)
    utf16_pos = 0
    for i, ch in enumerate(text):
        offsets[i] = utf16_pos
        utf16_pos += 2 if ord(ch) > 0xFFFF else 1
    offsets[len(text)] = utf16_pos
    return offsets


class LocalSpellChecker:
    """Verificador ortográfico local baseado em libenchant-2 via ctypes com comportamento fail-soft."""

    def __init__(
        self,
        dictionary_tag: str = "pt_BR",
        ignored_words: Iterable[str] | None = None,
        lib_path: str | None = None,
    ) -> None:
        self._tag = dictionary_tag
        self._ignored: set[str] = set()
        if ignored_words:
            for w in ignored_words:
                if isinstance(w, str) and w.strip():
                    self._ignored.add(w.strip().lower())

        self._available = False
        self._closed = False
        self._lib: ctypes.CDLL | None = None
        self._broker: int | None = None
        self._dict: int | None = None

        self._init_enchant(lib_path)

    def _init_enchant(self, lib_path: str | None) -> None:
        try:
            lib = _load_enchant_library(lib_path)
            if lib is None:
                self._available = False
                return

            lib.enchant_broker_init.restype = ctypes.c_void_p
            lib.enchant_broker_init.argtypes = []

            lib.enchant_broker_free.restype = None
            lib.enchant_broker_free.argtypes = [ctypes.c_void_p]

            lib.enchant_broker_dict_exists.restype = ctypes.c_int
            lib.enchant_broker_dict_exists.argtypes = [ctypes.c_void_p, ctypes.c_char_p]

            lib.enchant_broker_request_dict.restype = ctypes.c_void_p
            lib.enchant_broker_request_dict.argtypes = [ctypes.c_void_p, ctypes.c_char_p]

            lib.enchant_broker_free_dict.restype = None
            lib.enchant_broker_free_dict.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

            lib.enchant_dict_check.restype = ctypes.c_int
            lib.enchant_dict_check.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_ssize_t]

            lib.enchant_dict_suggest.restype = ctypes.POINTER(ctypes.c_char_p)
            lib.enchant_dict_suggest.argtypes = [
                ctypes.c_void_p,
                ctypes.c_char_p,
                ctypes.c_ssize_t,
                ctypes.POINTER(ctypes.c_size_t),
            ]

            lib.enchant_dict_free_string_list.restype = None
            lib.enchant_dict_free_string_list.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_char_p),
            ]

            broker = lib.enchant_broker_init()
            if not broker:
                self._available = False
                return

            self._lib = lib
            self._broker = broker

            tags_to_try = [self._tag]
            if "_" in self._tag:
                tags_to_try.append(self._tag.replace("_", "-"))
            elif "-" in self._tag:
                tags_to_try.append(self._tag.replace("-", "_"))

            dict_handle = None
            for tag in tags_to_try:
                tag_bytes = tag.encode("utf-8")
                if lib.enchant_broker_dict_exists(broker, tag_bytes):
                    dict_handle = lib.enchant_broker_request_dict(broker, tag_bytes)
                    if dict_handle:
                        break

            if dict_handle:
                self._dict = dict_handle
                self._available = True
            else:
                self._available = False
        except Exception:
            self._cleanup()
            self._available = False

    def is_available(self) -> bool:
        """Indica se a biblioteca e o dicionário estão prontos para uso."""
        return bool(self._available and not self._closed and self._dict is not None)

    def tokenize(self, text: str) -> list[tuple[int, int, str]]:
        """Extrai palavras em pt-BR preservando posições (start, end, word).

        Ignora URLs, caminhos de arquivo Unix e relativos, nomes de arquivos técnicos,
        e-mails e termos alfanuméricos com dígitos.
        """
        if not text:
            return []

        ignored_spans: list[tuple[int, int]] = []
        for pattern in (
            _URL_PATTERN,
            _WWW_URL_PATTERN,
            _EMAIL_PATTERN,
            _PATH_PATTERN,
            _RELATIVE_PATH_PATTERN,
            _FILENAME_PATTERN,
            _ALPHANUMERIC_WITH_DIGIT_PATTERN,
        ):
            for match in pattern.finditer(text):
                ignored_spans.append((match.start(), match.end()))

        tokens: list[tuple[int, int, str]] = []
        for match in _WORD_PATTERN.finditer(text):
            start, end = match.start(), match.end()
            if any(s <= start and end <= e for s, e in ignored_spans):
                continue
            tokens.append((start, end, match.group()))

        return tokens

    def check(self, word: str) -> bool:
        """Verifica se a palavra é válida pelo dicionário pt-BR.

        Retorna True se:
        - O corretor estiver indisponível (fail-soft).
        - A palavra tiver comprimento <= 1.
        - Conter dígitos numéricos.
        - For um acrônimo/sigla (toda em maiúsculas com len >= 2).
        - Estiver na lista de palavras ignoradas.
        - O dicionário nativo reconhecer a palavra (incluindo capitalização).
        """
        if not self.is_available():
            return True
        if not isinstance(word, str):
            return True

        w = word.strip()
        if len(w) <= 1:
            return True

        if any(c.isdigit() for c in w):
            return True

        if w.isupper() and len(w) >= 2:
            return True

        if self.is_ignored(w):
            return True

        assert self._lib is not None
        assert self._dict is not None

        try:
            res = self._lib.enchant_dict_check(self._dict, w.encode("utf-8"), -1)
            if res == 0 or res < 0:
                return True
            if w[0].isupper() and not w.isupper():
                res_lower = self._lib.enchant_dict_check(
                    self._dict, w.lower().encode("utf-8"), -1
                )
                if res_lower == 0 or res_lower < 0:
                    return True
            return False
        except Exception:
            return True

    def suggest(self, word: str, limit: int = 5) -> list[str]:
        """Retorna até `limit` sugestões de correção para a palavra."""
        if not self.is_available() or limit <= 0:
            return []
        if not isinstance(word, str):
            return []

        w = word.strip()
        if not w:
            return []

        assert self._lib is not None
        assert self._dict is not None

        try:
            n_suggs = ctypes.c_size_t(0)
            suggs_ptr = self._lib.enchant_dict_suggest(
                self._dict, w.encode("utf-8"), -1, ctypes.byref(n_suggs)
            )
            if not suggs_ptr:
                return []

            results: list[str] = []
            try:
                count = min(limit, n_suggs.value)
                for i in range(count):
                    raw = suggs_ptr[i]
                    if raw is not None:
                        results.append(raw.decode("utf-8", errors="replace"))
            finally:
                self._lib.enchant_dict_free_string_list(self._dict, suggs_ptr)
            return results
        except Exception:
            return []

    def ignore_word(self, word: str) -> None:
        """Adiciona uma palavra ao conjunto de palavras ignoradas."""
        if isinstance(word, str) and word.strip():
            self._ignored.add(word.strip().lower())

    def is_ignored(self, word: str) -> bool:
        """Verifica se a palavra está na lista de palavras ignoradas."""
        if not isinstance(word, str) or not word.strip():
            return False
        return word.strip().lower() in self._ignored

    def ignored_words(self) -> set[str]:
        """Retorna uma cópia do conjunto de palavras ignoradas."""
        return set(self._ignored)

    def _cleanup(self) -> None:
        if self._lib is not None:
            if self._broker is not None and self._dict is not None:
                try:
                    self._lib.enchant_broker_free_dict(self._broker, self._dict)
                except Exception:
                    pass
                self._dict = None
            if self._broker is not None:
                try:
                    self._lib.enchant_broker_free(self._broker)
                except Exception:
                    pass
                self._broker = None

    def close(self) -> None:
        """Libera os recursos alocados pelo corretor ortográfico."""
        if self._closed:
            return
        self._closed = True
        self._available = False
        self._cleanup()

    def __enter__(self) -> LocalSpellChecker:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()
