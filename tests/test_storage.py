from __future__ import annotations

import os
from pathlib import Path
import sqlite3

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
