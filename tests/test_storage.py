from __future__ import annotations

import os
from pathlib import Path
import sqlite3
from typing import Any
import pytest

from falafacil.storage import LocalStore, LocalStoreError, TokenTotals, TokenUsageRecord
from falafacil.transcription import TokenUsage


def test_schema_initialization_and_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        assert db_path.exists()
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert store.get_last_microphone_identity() is None
        totals = store.get_token_totals()
        assert totals == TokenTotals(
            input_tokens=0,
            output_tokens=0,
            thought_tokens=0,
            cached_tokens=0,
            tool_use_tokens=0,
            total_tokens=0,
        )

    # Reopening should preserve schema and data without errors
    with LocalStore(db_path) as store2:
        assert store2._conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert store2.get_last_microphone_identity() is None
        assert store2.get_token_totals() == TokenTotals(0, 0, 0, 0, 0, 0)


def test_parent_and_file_permissions(tmp_path: Path) -> None:
    nested_dir = tmp_path / "private_dir"
    db_path = nested_dir / "falafacil.sqlite3"

    with LocalStore(db_path) as store:
        assert store.get_last_microphone_identity() is None

    if os.name == "posix":
        dir_stat = nested_dir.stat().st_mode & 0o777
        file_stat = db_path.stat().st_mode & 0o777
        assert dir_stat == 0o700
        assert file_stat == 0o600


def test_microphone_identity_persistence_and_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        assert store.get_last_microphone_identity() is None
        store.save_last_microphone_identity("mic-usb-123")
        assert store.get_last_microphone_identity() == "mic-usb-123"

        # Overwrite identity
        store.save_last_microphone_identity("mic-headset-456")
        assert store.get_last_microphone_identity() == "mic-headset-456"

    # Reopen and check persisted identity
    with LocalStore(db_path) as store2:
        assert store2.get_last_microphone_identity() == "mic-headset-456"


def test_invalid_microphone_identity_raises_error(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        with pytest.raises(LocalStoreError, match="inválida"):
            store.save_last_microphone_identity("")
        with pytest.raises(LocalStoreError, match="inválida"):
            store.save_last_microphone_identity("   ")

def test_recording_shortcuts_persist_reopen_and_clear_independently(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        assert store.get_recording_mouse_button() is None
        assert store.get_recording_keyboard_shortcut() is None
        store.save_recording_mouse_button("Button.button8")
        store.save_recording_keyboard_shortcut("ALT+CTRL+R")
        assert store.get_recording_mouse_button() == "x1"
        assert store.get_recording_keyboard_shortcut() == "ctrl+alt+r"

    with LocalStore(db_path) as reopened:
        assert reopened.get_recording_mouse_button() == "x1"
        assert reopened.get_recording_keyboard_shortcut() == "ctrl+alt+r"
        reopened.clear_recording_keyboard_shortcut()
        assert reopened.get_recording_mouse_button() == "x1"
        assert reopened.get_recording_keyboard_shortcut() is None
        reopened.clear_recording_mouse_button()

    with LocalStore(db_path) as final:
        assert final.get_recording_mouse_button() is None
        assert final.get_recording_keyboard_shortcut() is None


def test_invalid_recording_shortcuts_raise_error(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        for invalid in ("", "left", "right", "unknown", "button.unknown", "@invalid"):
            with pytest.raises(LocalStoreError, match="inválido"):
                store.save_recording_mouse_button(invalid)
        for invalid in ("", "r", "shift+r", "ctrl", "f25", "ctrl+r+s"):
            with pytest.raises(LocalStoreError, match="inválido"):
                store.save_recording_keyboard_shortcut(invalid)



def test_gemini_model_persistence_reopen_and_overwrite(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        assert store.get_gemini_model() is None
        store.save_gemini_model("gemini-3.5-flash-lite")
        assert store.get_gemini_model() == "gemini-3.5-flash-lite"

        # Overwrite with gemini-3.7-flash
        store.save_gemini_model("gemini-3.7-flash")
        assert store.get_gemini_model() == "gemini-3.7-flash"

        # Overwrite with gemini-3.8-flash
        store.save_gemini_model("gemini-3.8-flash")
        assert store.get_gemini_model() == "gemini-3.8-flash"

        # Verify schema version remains 1 (no schema bump)
        cursor = store._conn.cursor()
        cursor.execute("PRAGMA user_version;")
        assert cursor.fetchone()[0] == 1

    with LocalStore(db_path) as reopened:
        assert reopened.get_gemini_model() == "gemini-3.8-flash"
        cursor = reopened._conn.cursor()
        cursor.execute("PRAGMA user_version;")
        assert cursor.fetchone()[0] == 1

def test_invalid_gemini_model_raises_error(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        for invalid in ("", "   ", "unknown", "gemini-1.5-flash", "gemini-test", "gemini-2.5-flash-lite"):
            with pytest.raises(LocalStoreError, match="inválido"):
                store.save_gemini_model(invalid)

def test_legacy_gemini_2_5_model_is_migrated_on_read(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        with store._conn:
            store._conn.execute(
                "INSERT INTO preferences (key, value) VALUES ('gemini_model', 'gemini-2.5-flash-lite');"
            )
        assert store.get_gemini_model() == "gemini-3.5-flash-lite"
        cursor = store._conn.cursor()
        cursor.execute("SELECT value FROM preferences WHERE key = 'gemini_model';")
        assert cursor.fetchone()[0] == "gemini-3.5-flash-lite"


def test_legacy_gemini_2_5_model_is_migrated_on_schema_init(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    conn.execute(
        "CREATE TABLE token_usage ("
        "id INTEGER PRIMARY KEY, "
        "recorded_at TEXT NOT NULL, "
        "model TEXT NOT NULL, "
        "input_tokens INTEGER, "
        "output_tokens INTEGER, "
        "thought_tokens INTEGER, "
        "cached_tokens INTEGER, "
        "tool_use_tokens INTEGER, "
        "total_tokens INTEGER, "
        "outcome TEXT NOT NULL CHECK(outcome IN ('success', 'error'))"
        ");"
    )
    conn.execute("INSERT INTO preferences (key, value) VALUES ('gemini_model', 'gemini-2.5-flash-lite');")
    conn.execute("PRAGMA user_version = 1;")
    conn.commit()
    conn.close()

    with LocalStore(db_path) as store:
        assert store.get_gemini_model() == "gemini-3.5-flash-lite"
        cursor = store._conn.cursor()
        cursor.execute("SELECT value FROM preferences WHERE key = 'gemini_model';")
        assert cursor.fetchone()[0] == "gemini-3.5-flash-lite"


def test_legacy_gemini_2_5_migration_is_fail_soft_on_db_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        with store._conn:
            store._conn.execute(
                "INSERT INTO preferences (key, value) VALUES ('gemini_model', 'gemini-2.5-flash-lite');"
            )

        real_conn = store._conn
        assert real_conn is not None

        class FailingCommitConnection:
            def __getattr__(self, name: str) -> Any:
                return getattr(real_conn, name)

            def commit(self) -> None:
                raise sqlite3.OperationalError("simulated commit failure during migration")

            def __enter__(self) -> FailingCommitConnection:
                return self

            def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
                if exc_type is None:
                    self.commit()
                else:
                    real_conn.rollback()

        failing_conn = FailingCommitConnection()
        monkeypatch.setattr(store, "_conn", failing_conn)

        assert store.get_gemini_model() == "gemini-3.5-flash-lite"
        assert store._conn.in_transaction is False

        monkeypatch.undo()

        store.save_gemini_model("gemini-3.7-flash")
        assert store.get_gemini_model() == "gemini-3.7-flash"

def test_unrecognized_stored_gemini_model_returns_none(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        with store._conn:
            store._conn.execute(
                "INSERT INTO preferences (key, value) VALUES ('gemini_model', 'obsolete-model');"
            )
        assert store.get_gemini_model() is None

def test_gemini_model_storage_errors_are_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    leak_marker = "SYNTHETIC_MODEL_SQLITE_SECRET_98765"
    with LocalStore(db_path) as store:
        failing_conn = _FailingConnection(sqlite3.OperationalError(leak_marker))
        for operation, expected in (
            (
                lambda: store.get_gemini_model(),
                "Erro ao ler preferência de modelo Gemini.",
            ),
            (
                lambda: store.save_gemini_model("gemini-3.5-flash-lite"),
                "Erro ao salvar preferência de modelo Gemini.",
            ),
        ):
            monkeypatch.setattr(store, "_conn", failing_conn)
            with pytest.raises(LocalStoreError) as exc_info:
                operation()
            assert str(exc_info.value) == expected
            assert leak_marker not in str(exc_info.value)
            assert exc_info.value.__cause__ is None
            monkeypatch.undo()


class _FailingConnection:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def cursor(self) -> Any:
        error = self._error

        class FailingCursor:
            def execute(self, *args: Any, **kwargs: Any) -> Any:
                raise error

        return FailingCursor()

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise self._error

    def __enter__(self) -> _FailingConnection:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        return None

    def close(self) -> None:
        pass


def test_recording_mouse_button_storage_errors_sanitize_raw_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    leak_marker = "SYNTHETIC_SQLITE_ERROR_LEAK_SECRET_12345"

    with LocalStore(db_path) as store:
        failing_conn = _FailingConnection(sqlite3.OperationalError(leak_marker))

        # 1. Erro ao ler preferência de atalho do mouse
        monkeypatch.setattr(store, "_conn", failing_conn)
        with pytest.raises(LocalStoreError) as exc_info:
            store.get_recording_mouse_button()
        assert str(exc_info.value) == "Erro ao ler preferência de atalho do mouse."
        assert leak_marker not in str(exc_info.value)
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__suppress_context__ is True
        monkeypatch.undo()

        # 2. Erro ao salvar preferência de atalho do mouse
        monkeypatch.setattr(store, "_conn", failing_conn)
        with pytest.raises(LocalStoreError) as exc_info:
            store.save_recording_mouse_button("x1")
        assert str(exc_info.value) == "Erro ao salvar preferência de atalho do mouse."
        assert leak_marker not in str(exc_info.value)
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__suppress_context__ is True
        monkeypatch.undo()

        # 3. Erro ao remover preferência de atalho do mouse
        monkeypatch.setattr(store, "_conn", failing_conn)
        with pytest.raises(LocalStoreError) as exc_info:
            store.clear_recording_mouse_button()
        assert str(exc_info.value) == "Erro ao remover preferência de atalho do mouse."
        assert leak_marker not in str(exc_info.value)
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__suppress_context__ is True
        monkeypatch.undo()

def test_recording_keyboard_shortcut_storage_errors_are_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    leak_marker = "SYNTHETIC_KEYBOARD_SQLITE_SECRET"
    with LocalStore(db_path) as store:
        failing_conn = _FailingConnection(sqlite3.OperationalError(leak_marker))
        for operation, expected in (
            (
                lambda: store.get_recording_keyboard_shortcut(),
                "Erro ao ler preferência de atalho do teclado.",
            ),
            (
                lambda: store.save_recording_keyboard_shortcut("f12"),
                "Erro ao salvar preferência de atalho do teclado.",
            ),
            (
                lambda: store.clear_recording_keyboard_shortcut(),
                "Erro ao remover preferência de atalho do teclado.",
            ),
        ):
            monkeypatch.setattr(store, "_conn", failing_conn)
            with pytest.raises(LocalStoreError) as exc_info:
                operation()
            assert str(exc_info.value) == expected
            assert leak_marker not in str(exc_info.value)
            assert exc_info.value.__cause__ is None
            monkeypatch.undo()


def test_mouse_button_coexists_with_microphone_and_token_history(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        store.save_last_microphone_identity("mic-primary")
        store.save_recording_mouse_button("x2")
        store.save_recording_keyboard_shortcut("f12")
        store.record_token_usage(
            "model",
            TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            "success",
        )

        # Clear only mouse button
        store.clear_recording_mouse_button()
        assert store.get_recording_mouse_button() is None
        assert store.get_recording_keyboard_shortcut() == "f12"
        assert store.get_last_microphone_identity() == "mic-primary"
        assert len(store.get_token_usage_history()) == 1


def test_token_totals_empty_history_returns_zeros(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        totals = store.get_token_totals()
        assert totals.input_tokens == 0
        assert totals.output_tokens == 0
        assert totals.thought_tokens == 0
        assert totals.cached_tokens == 0
        assert totals.tool_use_tokens == 0
        assert totals.total_tokens == 0


def test_token_totals_full_records_summed(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        usage1 = TokenUsage(
            input_tokens=10,
            output_tokens=4,
            thought_tokens=2,
            cached_tokens=1,
            tool_use_tokens=0,
            total_tokens=17,
        )
        store.record_token_usage("gemini-2.5", usage1, "success")

        totals1 = store.get_token_totals()
        assert totals1 == TokenTotals(
            input_tokens=10,
            output_tokens=4,
            thought_tokens=2,
            cached_tokens=1,
            tool_use_tokens=0,
            total_tokens=17,
        )

        usage2 = TokenUsage(
            input_tokens=5,
            output_tokens=3,
            thought_tokens=1,
            cached_tokens=0,
            tool_use_tokens=0,
            total_tokens=9,
        )
        store.record_token_usage("gemini-2.5", usage2, "error")

        totals2 = store.get_token_totals()
        assert totals2 == TokenTotals(
            input_tokens=15,
            output_tokens=7,
            thought_tokens=3,
            cached_tokens=1,
            tool_use_tokens=0,
            total_tokens=26,
        )


def test_token_totals_null_aggregate_semantics(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        # First record has some None fields
        usage1 = TokenUsage(
            input_tokens=10,
            output_tokens=4,
            thought_tokens=None,
            cached_tokens=None,
            tool_use_tokens=None,
            total_tokens=14,
        )
        store.record_token_usage("gemini-2.5", usage1, "success")

        totals1 = store.get_token_totals()
        assert totals1.input_tokens == 10
        assert totals1.output_tokens == 4
        assert totals1.thought_tokens is None
        assert totals1.cached_tokens is None
        assert totals1.tool_use_tokens is None
        assert totals1.total_tokens == 14

        # Second record provides all fields, but because record 1 has NULL for thought/cached/tool_use,
        # the aggregate across all rows for those fields must remain None.
        usage2 = TokenUsage(
            input_tokens=5,
            output_tokens=2,
            thought_tokens=1,
            cached_tokens=0,
            tool_use_tokens=0,
            total_tokens=8,
        )
        store.record_token_usage("gemini-2.5", usage2, "success")

        totals2 = store.get_token_totals()
        assert totals2.input_tokens == 15
        assert totals2.output_tokens == 6
        assert totals2.thought_tokens is None
        assert totals2.cached_tokens is None
        assert totals2.tool_use_tokens is None
        assert totals2.total_tokens == 22


def test_invalid_outcome_raises_error(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        usage = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
        with pytest.raises(LocalStoreError, match="inválido"):
            store.record_token_usage("model", usage, "other")  # type: ignore[arg-type]


def test_closed_store_raises_error(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    store = LocalStore(db_path)
    store.close()
    assert store._closed is True
    # Multiple close calls must be safe
    store.close()

    with pytest.raises(LocalStoreError, match="fechado"):
        store.get_last_microphone_identity()

    with pytest.raises(LocalStoreError, match="fechado"):
        store.save_last_microphone_identity("mic-1")

    with pytest.raises(LocalStoreError, match="fechado"):
        store.record_token_usage("model", TokenUsage(), "success")

    with pytest.raises(LocalStoreError, match="fechado"):
        store.get_token_totals()

    with pytest.raises(LocalStoreError, match="fechado"):
        store.get_token_usage_history()

    with pytest.raises(LocalStoreError, match="fechado"):
        store.get_recording_mouse_button()

    with pytest.raises(LocalStoreError, match="fechado"):
        store.save_recording_mouse_button("x1")

    with pytest.raises(LocalStoreError, match="fechado"):
        store.clear_recording_mouse_button()

    with pytest.raises(LocalStoreError, match="fechado"):
        store.get_recording_keyboard_shortcut()

    with pytest.raises(LocalStoreError, match="fechado"):
        store.save_recording_keyboard_shortcut("f12")

    with pytest.raises(LocalStoreError, match="fechado"):
        store.clear_recording_keyboard_shortcut()

    with pytest.raises(LocalStoreError, match="fechado"):
        store.get_spellcheck_enabled()

    with pytest.raises(LocalStoreError, match="fechado"):
        store.save_spellcheck_enabled(True)

    with pytest.raises(LocalStoreError, match="fechado"):
        store.get_spellcheck_ignored_words()

    with pytest.raises(LocalStoreError, match="fechado"):
        store.add_spellcheck_ignored_word("docker")


def test_token_usage_history_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        history = store.get_token_usage_history()
        assert history == ()


def test_token_usage_history_chronological_order_and_limit(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        for i in range(1, 6):
            usage = TokenUsage(
                input_tokens=10 * i,
                output_tokens=2 * i,
                total_tokens=12 * i,
            )
            store.record_token_usage(f"gemini-{i}", usage, "success")

        # Default limit is 30, all 5 returned in chronological order (ASC by recorded_at, id)
        all_history = store.get_token_usage_history()
        assert len(all_history) == 5
        assert [r.model for r in all_history] == [
            "gemini-1",
            "gemini-2",
            "gemini-3",
            "gemini-4",
            "gemini-5",
        ]
        assert [r.total_tokens for r in all_history] == [12, 24, 36, 48, 60]

        # Limit 3: returns the latest 3 inserted records, sorted in ASC chronological order
        limited_history = store.get_token_usage_history(limit=3)
        assert len(limited_history) == 3
        assert [r.model for r in limited_history] == [
            "gemini-3",
            "gemini-4",
            "gemini-5",
        ]
        assert [r.total_tokens for r in limited_history] == [36, 48, 60]


def test_token_usage_history_success_and_error_outcomes(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        usage_success = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
        usage_error = TokenUsage(input_tokens=20, output_tokens=0, total_tokens=20)
        store.record_token_usage("gemini-3.7-flash", usage_success, "success")
        store.record_token_usage("gemini-3.7-flash", usage_error, "error")

        history = store.get_token_usage_history()
        assert len(history) == 2
        assert history[0].outcome == "success"
        assert history[0].total_tokens == 15
        assert history[1].outcome == "error"
        assert history[1].total_tokens == 20


def test_token_usage_history_null_totals_and_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        usage = TokenUsage(
            input_tokens=10,
            output_tokens=None,
            thought_tokens=None,
            cached_tokens=None,
            tool_use_tokens=None,
            total_tokens=None,
        )
        store.record_token_usage("gemini-3.7-flash", usage, "success")

        history = store.get_token_usage_history()
        assert len(history) == 1
        rec = history[0]
        assert isinstance(rec, TokenUsageRecord)
        assert rec.model == "gemini-3.7-flash"
        assert rec.input_tokens == 10
        assert rec.output_tokens is None
        assert rec.thought_tokens is None
        assert rec.cached_tokens is None
        assert rec.tool_use_tokens is None
        assert rec.total_tokens is None
        assert rec.outcome == "success"
        assert rec.recorded_at != ""


def test_token_usage_history_persisted_across_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        usage = TokenUsage(input_tokens=15, output_tokens=5, total_tokens=20)
        store.record_token_usage("gemini-3.7-flash", usage, "success")
        assert len(store.get_token_usage_history()) == 1

    with LocalStore(db_path) as store2:
        history = store2.get_token_usage_history()
        assert len(history) == 1
        assert history[0].model == "gemini-3.7-flash"
        assert history[0].total_tokens == 20
        assert history[0].outcome == "success"


def test_token_usage_history_invalid_limit_raises_error(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        with pytest.raises(LocalStoreError, match="Limite inválido"):
            store.get_token_usage_history(limit=0)
        with pytest.raises(LocalStoreError, match="Limite inválido"):
            store.get_token_usage_history(limit=-1)
        with pytest.raises(LocalStoreError, match="Limite inválido"):
            store.get_token_usage_history(limit=101)
        with pytest.raises(LocalStoreError, match="Limite inválido"):
            store.get_token_usage_history(limit="10")  # type: ignore[arg-type]
        with pytest.raises(LocalStoreError, match="Limite inválido"):
            store.get_token_usage_history(limit=True)  # type: ignore[arg-type]
        with pytest.raises(LocalStoreError, match="Limite inválido"):
            store.get_token_usage_history(limit=None)  # type: ignore[arg-type]


def test_token_usage_history_boundary_limit_100_and_101(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        for i in range(1, 101):
            usage = TokenUsage(
                input_tokens=10 * i,
                output_tokens=2 * i,
                total_tokens=12 * i,
            )
            store.record_token_usage(f"gemini-{i:03d}", usage, "success")

        history_100 = store.get_token_usage_history(limit=100)
        assert len(history_100) == 100
        assert [r.model for r in history_100] == [f"gemini-{i:03d}" for i in range(1, 101)]
        assert history_100[0].model == "gemini-001"
        assert history_100[0].total_tokens == 12
        assert history_100[-1].model == "gemini-100"
        assert history_100[-1].total_tokens == 1200

        for i in range(101, 106):
            usage = TokenUsage(
                input_tokens=10 * i,
                output_tokens=2 * i,
                total_tokens=12 * i,
            )
            store.record_token_usage(f"gemini-{i:03d}", usage, "success")

        history_105 = store.get_token_usage_history(limit=100)
        assert len(history_105) == 100
        assert [r.model for r in history_105] == [f"gemini-{i:03d}" for i in range(6, 106)]
        assert history_105[0].model == "gemini-006"
        assert history_105[0].total_tokens == 12 * 6
        assert history_105[-1].model == "gemini-105"
        assert history_105[-1].total_tokens == 12 * 105

        with pytest.raises(LocalStoreError, match="Limite inválido"):
            store.get_token_usage_history(limit=101)

def test_incompatible_schema_version_raises_error_without_file_mutation(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    # Criar banco pré-existente com versão futura (ex.: 2) e tabela customizada
    raw_conn = sqlite3.connect(str(db_path))
    raw_conn.execute("PRAGMA user_version = 2;")
    raw_conn.execute("CREATE TABLE future_schema (data TEXT);")
    raw_conn.execute("INSERT INTO future_schema VALUES ('dados_preservados');")
    raw_conn.commit()
    raw_conn.close()

    # LocalStore deve recusar versão futura com LocalStoreError sanitizado
    with pytest.raises(LocalStoreError, match="Versão de schema incompatível: 2"):
        LocalStore(db_path)

    # Confirmar ausência de mutação no arquivo SQLite
    verify_conn = sqlite3.connect(str(db_path))
    version = verify_conn.execute("PRAGMA user_version;").fetchone()[0]
    assert version == 2

    tables = [
        row[0]
        for row in verify_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
        ).fetchall()
    ]
    assert tables == ["future_schema"]
    data = verify_conn.execute("SELECT data FROM future_schema;").fetchall()
    assert data == [("dados_preservados",)]
    verify_conn.close()


def test_spellcheck_preferences_persistence_and_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        # Valor padrão inicial é True
        assert store.get_spellcheck_enabled() is True

        # Salva desabilitado
        store.save_spellcheck_enabled(False)
        assert store.get_spellcheck_enabled() is False

    # Reabre e verifica que persistiu
    with LocalStore(db_path) as store2:
        assert store2.get_spellcheck_enabled() is False
        # Salva habilitado novamente
        store2.save_spellcheck_enabled(True)
        assert store2.get_spellcheck_enabled() is True

    with LocalStore(db_path) as store3:
        assert store3.get_spellcheck_enabled() is True


def test_spellcheck_ignored_words_persistence_and_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        # Lista padrão inicial é vazia
        assert store.get_spellcheck_ignored_words() == []

        # Adiciona palavras
        store.add_spellcheck_ignored_word("docker")
        store.add_spellcheck_ignored_word("Docker")  # Case insensitive, não duplica
        store.add_spellcheck_ignored_word("kubernetes")

        assert store.get_spellcheck_ignored_words() == ["docker", "kubernetes"]

    # Reabre e verifica persistência
    with LocalStore(db_path) as store2:
        assert store2.get_spellcheck_ignored_words() == ["docker", "kubernetes"]
        store2.add_spellcheck_ignored_word("falafacil")
        assert store2.get_spellcheck_ignored_words() == ["docker", "kubernetes", "falafacil"]


def test_spellcheck_invalid_inputs_raise_errors(tmp_path: Path) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    with LocalStore(db_path) as store:
        with pytest.raises(LocalStoreError, match="Valor inválido"):
            store.save_spellcheck_enabled("true")  # type: ignore[arg-type]

        with pytest.raises(LocalStoreError, match="Valor inválido"):
            store.save_spellcheck_enabled(1)  # type: ignore[arg-type]

        with pytest.raises(LocalStoreError, match="Palavra ignorada inválida"):
            store.add_spellcheck_ignored_word("")

        with pytest.raises(LocalStoreError, match="Palavra ignorada inválida"):
            store.add_spellcheck_ignored_word("   ")

        with pytest.raises(LocalStoreError, match="Palavra ignorada inválida"):
            store.add_spellcheck_ignored_word(123)  # type: ignore[arg-type]


def test_spellcheck_storage_errors_are_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "falafacil.sqlite3"
    leak_marker = "SYNTHETIC_SPELLCHECK_SQLITE_SECRET"

    with LocalStore(db_path) as store:
        failing_conn = _FailingConnection(sqlite3.OperationalError(leak_marker))
        for operation, expected in (
            (
                lambda: store.get_spellcheck_enabled(),
                "Erro ao ler preferência de corretor ortográfico.",
            ),
            (
                lambda: store.save_spellcheck_enabled(True),
                "Erro ao salvar preferência de corretor ortográfico.",
            ),
            (
                lambda: store.get_spellcheck_ignored_words(),
                "Erro ao ler lista de palavras ignoradas.",
            ),
            (
                lambda: store.add_spellcheck_ignored_word("docker"),
                "Erro ao salvar lista de palavras ignoradas.",
            ),
        ):
            monkeypatch.setattr(store, "_conn", failing_conn)
            with pytest.raises(LocalStoreError) as exc_info:
                operation()
            assert str(exc_info.value) == expected
            assert leak_marker not in str(exc_info.value)
            assert exc_info.value.__cause__ is None
            assert exc_info.value.__suppress_context__ is True
            monkeypatch.undo()
