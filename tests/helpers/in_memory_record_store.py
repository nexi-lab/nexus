"""In-memory RecordStoreABC implementation for unit tests (Issue #2200).

Wraps an in-memory SQLite database with all tables created, providing a
lightweight test double that supports real SQL operations without external
dependencies. Follows the same pattern as DictMetastore.
"""

from typing import Any

from nexus.storage.record_store import RecordStoreABC


class InMemoryRecordStore(RecordStoreABC):
    """Lightweight RecordStoreABC for unit tests. Wraps in-memory SQLite."""

    def __init__(self) -> None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from nexus.storage.models import Base

        self._engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False)

    @property
    def engine(self) -> Any:
        return self._engine

    @property
    def session_factory(self) -> Any:
        return self._session_factory

    def close(self) -> None:
        self._engine.dispose()
