"""Explicit pytest fixtures backed by tests.testkit helpers."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

import pytest

from tests.testkit.records import InMemoryRecordStore


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Create an isolated SQLite database path and clear DB override env vars."""
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    db_path = tmp_path / f"test_db_{str(uuid.uuid4())[:8]}.db"
    yield db_path

    if db_path.exists():
        with suppress(Exception):
            db_path.unlink()


@pytest.fixture
def record_store() -> Iterator[InMemoryRecordStore]:
    """Provide an in-memory SQL-backed RecordStoreABC."""
    store = InMemoryRecordStore()
    try:
        yield store
    finally:
        store.close()


__all__ = ["isolated_db", "record_store"]
