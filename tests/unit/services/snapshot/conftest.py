"""Shared fixtures for snapshot service tests (Issue #1752)."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.services.snapshot.registry import TransactionRegistry
from nexus.services.snapshot.service import TransactionalSnapshotService


class FakeSession:
    """Minimal session mock that supports context manager and basic operations."""

    def __init__(self, store: dict[str, Any] | None = None) -> None:
        self._store: dict[str, Any] = store if store is not None else {}
        self._pending: list[Any] = []

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def add(self, obj: Any) -> None:
        self._pending.append(obj)

    def get(self, model_class: type, pk: str) -> Any:
        return self._store.get(pk)

    def commit(self) -> None:
        for obj in self._pending:
            pk_attr = (
                "transaction_id"
                if hasattr(obj, "transaction_id")
                and hasattr(obj, "status")
                and not hasattr(obj, "entry_id")
                else "entry_id"
            )
            pk = getattr(obj, pk_attr, None)
            if pk:
                self._store[pk] = obj
        self._pending.clear()

    def refresh(self, obj: Any) -> None:
        pass

    def execute(self, stmt: Any) -> Any:
        return FakeResult([])

class FakeResult:
    """Minimal result mock for SQLAlchemy queries."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return self._items

@pytest.fixture
def mock_cas_store() -> MagicMock:
    """Mock CASBlobStore with hold_reference and release."""
    store = MagicMock()
    store.hold_reference.return_value = True
    store.release.return_value = False
    return store

@pytest.fixture
def mock_metadata_store() -> MagicMock:
    """Mock metadata store with get/put/delete."""
    store = MagicMock()
    store.get.return_value = None
    return store

@pytest.fixture
def mock_session_factory() -> MagicMock:
    """Mock session factory that returns FakeSession."""
    session = FakeSession()
    factory = MagicMock(return_value=session)
    return factory

@pytest.fixture
def registry() -> TransactionRegistry:
    """Fresh TransactionRegistry instance."""
    return TransactionRegistry()

@pytest.fixture
def snapshot_service(
    mock_session_factory: MagicMock,
    mock_cas_store: MagicMock,
    mock_metadata_store: MagicMock,
) -> TransactionalSnapshotService:
    """TransactionalSnapshotService with mocked dependencies."""
    return TransactionalSnapshotService(
        session_factory=mock_session_factory,
        cas_store=mock_cas_store,
        metadata_store=mock_metadata_store,
    )
