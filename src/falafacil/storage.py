from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from typing import Any, Literal

from PySide6.QtCore import QStandardPaths

from .shortcuts import normalize_keyboard_shortcut, normalize_mouse_button_name

class LocalStoreError(RuntimeError):
    """Erro em operações do armazenamento local SQLite."""


@dataclass(frozen=True)
class TokenTotals:
    input_tokens: int | None = 0
    output_tokens: int | None = 0
    thought_tokens: int | None = 0
    cached_tokens: int | None = 0
    tool_use_tokens: int | None = 0
    total_tokens: int | None = 0


@dataclass(frozen=True)
class TokenUsageRecord:
    id: int
    recorded_at: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    thought_tokens: int | None = None
    cached_tokens: int | None = None
    tool_use_tokens: int | None = None
    total_tokens: int | None = None
    outcome: Literal["success", "error"] | str = "success"


def _extract_token_count(usage: Any, field: str) -> int | None:
    if usage is None:
        return None
    if isinstance(usage, dict):
        value = usage.get(field)
    else:
        value = getattr(usage, field, None)
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def resolve_storage_path() -> Path:
    app_data_dir = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation
    )
    if not app_data_dir:
        raise LocalStoreError("Não foi possível resolver o diretório AppDataLocation.")
    return Path(app_data_dir) / "falafacil.sqlite3"


default_storage_path = resolve_storage_path


class LocalStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._closed = False
        self._conn: sqlite3.Connection | None = None
        try:
            if str(self.path) != ":memory:":
                parent_dir = self.path.parent
                if not parent_dir.exists():
                    parent_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                else:
                    try:
                        os.chmod(parent_dir, 0o700)
                    except OSError:
                        pass

            file_existed = self.path.exists() if str(self.path) != ":memory:" else False

            self._conn = sqlite3.connect(
                str(self.path),
                timeout=2.0,
                check_same_thread=True,
            )
            self._conn.row_factory = sqlite3.Row

            if not file_existed and str(self.path) != ":memory:" and self.path.exists():
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass

            self._conn.execute("PRAGMA busy_timeout = 2000;")
            self._init_schema()
        except Exception as exc:
            self._closed = True
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
            raise LocalStoreError(f"Falha ao inicializar banco local: {exc}") from exc

    def _init_schema(self) -> None:
        if self._conn is None:
            raise LocalStoreError("Conexão com o banco não inicializada.")
        cursor = self._conn.cursor()
        cursor.execute("PRAGMA user_version;")
        row = cursor.fetchone()
        version = row[0] if row else 0

        if version == 0:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS token_usage (
                    id INTEGER PRIMARY KEY,
                    recorded_at TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    thought_tokens INTEGER,
                    cached_tokens INTEGER,
                    tool_use_tokens INTEGER,
                    total_tokens INTEGER,
                    outcome TEXT NOT NULL CHECK(outcome IN ('success', 'error'))
                );
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_token_usage_recorded_at
                ON token_usage(recorded_at);
                """
            )
            cursor.execute("PRAGMA user_version = 1;")
            self._conn.commit()
        elif version == 1:
            pass
        else:
            raise LocalStoreError(f"Versão de schema incompatível: {version}.")

    def _ensure_open(self) -> sqlite3.Connection:
        if self._closed or self._conn is None:
            raise LocalStoreError("O armazenamento local está fechado.")
        return self._conn

    def get_last_microphone_identity(self) -> str | None:
        conn = self._ensure_open()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT value FROM preferences WHERE key = 'last_microphone_identity';"
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return str(row[0])
        except sqlite3.Error as exc:
            raise LocalStoreError(
                f"Erro ao ler preferência de microfone: {exc}"
            ) from exc

    def save_last_microphone_identity(self, identity: str) -> None:
        conn = self._ensure_open()
        if not isinstance(identity, str) or not identity.strip():
            raise LocalStoreError("Identidade de microfone inválida.")
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO preferences (key, value)
                    VALUES ('last_microphone_identity', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value;
                    """,
                    (identity,),
                )
        except sqlite3.Error as exc:
            raise LocalStoreError(
                f"Erro ao salvar preferência de microfone: {exc}"
            ) from exc

    def get_recording_mouse_button(self) -> str | None:
        conn = self._ensure_open()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT value FROM preferences WHERE key = 'recording_mouse_button';"
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return str(row[0])
        except sqlite3.Error:
            raise LocalStoreError(
                "Erro ao ler preferência de atalho do mouse."
            ) from None

    def save_recording_mouse_button(self, button_name: str) -> None:
        conn = self._ensure_open()
        canonical = normalize_mouse_button_name(button_name)
        if canonical is None:
            raise LocalStoreError("Identificador de botão do mouse inválido.")
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO preferences (key, value)
                    VALUES ('recording_mouse_button', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value;
                    """,
                    (canonical,),
                )
        except sqlite3.Error:
            raise LocalStoreError(
                "Erro ao salvar preferência de atalho do mouse."
            ) from None

    def clear_recording_mouse_button(self) -> None:
        conn = self._ensure_open()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM preferences WHERE key = 'recording_mouse_button';"
                )
        except sqlite3.Error:
            raise LocalStoreError(
                "Erro ao remover preferência de atalho do mouse."
            ) from None

    def get_recording_keyboard_shortcut(self) -> str | None:
        conn = self._ensure_open()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT value FROM preferences WHERE key = 'recording_keyboard_shortcut';"
            )
            row = cursor.fetchone()
            return None if row is None else str(row[0])
        except sqlite3.Error:
            raise LocalStoreError(
                "Erro ao ler preferência de atalho do teclado."
            ) from None

    def save_recording_keyboard_shortcut(self, shortcut: str) -> None:
        conn = self._ensure_open()
        canonical = normalize_keyboard_shortcut(shortcut)
        if canonical is None:
            raise LocalStoreError("Atalho de teclado inválido.")
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO preferences (key, value)
                    VALUES ('recording_keyboard_shortcut', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value;
                    """,
                    (canonical,),
                )
        except sqlite3.Error:
            raise LocalStoreError(
                "Erro ao salvar preferência de atalho do teclado."
            ) from None

    def clear_recording_keyboard_shortcut(self) -> None:
        conn = self._ensure_open()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM preferences WHERE key = 'recording_keyboard_shortcut';"
                )
        except sqlite3.Error:
            raise LocalStoreError(
                "Erro ao remover preferência de atalho do teclado."
            ) from None
    def record_token_usage(
        self,
        model: str,
        usage: Any,
        outcome: Literal["success", "error"] | str,
    ) -> None:
        conn = self._ensure_open()
        if outcome not in ("success", "error"):
            raise LocalStoreError(f"Resultado de transcrição inválido: {outcome}")

        input_tokens = _extract_token_count(usage, "input_tokens")
        output_tokens = _extract_token_count(usage, "output_tokens")
        thought_tokens = _extract_token_count(usage, "thought_tokens")
        cached_tokens = _extract_token_count(usage, "cached_tokens")
        tool_use_tokens = _extract_token_count(usage, "tool_use_tokens")
        total_tokens = _extract_token_count(usage, "total_tokens")

        recorded_at = datetime.now(timezone.utc).isoformat()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO token_usage (
                        recorded_at,
                        model,
                        input_tokens,
                        output_tokens,
                        thought_tokens,
                        cached_tokens,
                        tool_use_tokens,
                        total_tokens,
                        outcome
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        recorded_at,
                        model,
                        input_tokens,
                        output_tokens,
                        thought_tokens,
                        cached_tokens,
                        tool_use_tokens,
                        total_tokens,
                        outcome,
                    ),
                )
        except sqlite3.Error as exc:
            raise LocalStoreError(f"Erro ao registrar uso de tokens: {exc}") from exc

    def get_token_totals(self) -> TokenTotals:
        conn = self._ensure_open()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    COUNT(*),
                    COUNT(input_tokens), SUM(input_tokens),
                    COUNT(output_tokens), SUM(output_tokens),
                    COUNT(thought_tokens), SUM(thought_tokens),
                    COUNT(cached_tokens), SUM(cached_tokens),
                    COUNT(tool_use_tokens), SUM(tool_use_tokens),
                    COUNT(total_tokens), SUM(total_tokens)
                FROM token_usage;
                """
            )
            row = cursor.fetchone()
            if row is None or row[0] == 0:
                return TokenTotals(
                    input_tokens=0,
                    output_tokens=0,
                    thought_tokens=0,
                    cached_tokens=0,
                    tool_use_tokens=0,
                    total_tokens=0,
                )
            total_rows = int(row[0])

            def _aggregate(count_val: Any, sum_val: Any) -> int | None:
                if int(count_val) < total_rows or sum_val is None:
                    return None
                return int(sum_val)

            return TokenTotals(
                input_tokens=_aggregate(row[1], row[2]),
                output_tokens=_aggregate(row[3], row[4]),
                thought_tokens=_aggregate(row[5], row[6]),
                cached_tokens=_aggregate(row[7], row[8]),
                tool_use_tokens=_aggregate(row[9], row[10]),
                total_tokens=_aggregate(row[11], row[12]),
            )
        except sqlite3.Error as exc:
            raise LocalStoreError(
                f"Erro ao calcular totais de tokens: {exc}"
            ) from exc

    def get_token_usage_history(
        self, limit: int = 30
    ) -> tuple[TokenUsageRecord, ...]:
        conn = self._ensure_open()
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or limit > 100:
            raise LocalStoreError(
                f"Limite inválido para histórico de tokens: {limit!r}"
            )
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    id,
                    recorded_at,
                    model,
                    input_tokens,
                    output_tokens,
                    thought_tokens,
                    cached_tokens,
                    tool_use_tokens,
                    total_tokens,
                    outcome
                FROM (
                    SELECT
                        id,
                        recorded_at,
                        model,
                        input_tokens,
                        output_tokens,
                        thought_tokens,
                        cached_tokens,
                        tool_use_tokens,
                        total_tokens,
                        outcome
                    FROM token_usage
                    ORDER BY recorded_at DESC, id DESC
                    LIMIT ?
                )
                ORDER BY recorded_at ASC, id ASC;
                """,
                (limit,),
            )
            rows = cursor.fetchall()
            records = [
                TokenUsageRecord(
                    id=int(row["id"]),
                    recorded_at=str(row["recorded_at"]),
                    model=str(row["model"]),
                    input_tokens=(
                        int(row["input_tokens"])
                        if row["input_tokens"] is not None
                        else None
                    ),
                    output_tokens=(
                        int(row["output_tokens"])
                        if row["output_tokens"] is not None
                        else None
                    ),
                    thought_tokens=(
                        int(row["thought_tokens"])
                        if row["thought_tokens"] is not None
                        else None
                    ),
                    cached_tokens=(
                        int(row["cached_tokens"])
                        if row["cached_tokens"] is not None
                        else None
                    ),
                    tool_use_tokens=(
                        int(row["tool_use_tokens"])
                        if row["tool_use_tokens"] is not None
                        else None
                    ),
                    total_tokens=(
                        int(row["total_tokens"])
                        if row["total_tokens"] is not None
                        else None
                    ),
                    outcome=str(row["outcome"]),
                )
                for row in rows
            ]
            return tuple(records)
        except sqlite3.Error as exc:
            raise LocalStoreError(
                f"Erro ao consultar histórico de tokens: {exc}"
            ) from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error as exc:
                raise LocalStoreError(f"Erro ao fechar o banco local: {exc}") from exc
            finally:
                self._conn = None

    def __enter__(self) -> LocalStore:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
