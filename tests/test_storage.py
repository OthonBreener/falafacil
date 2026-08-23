from __future__ import annotations

import os
from pathlib import Path
import sqlite3

import pytest

from falafacil.storage import LocalStore, LocalStoreError, TokenTotals
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
