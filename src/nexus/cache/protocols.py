"""Protocol interfaces for Cache brick external dependencies.

Defines the minimal contract the Cache brick requires from the
relational storage pillar. Only ``engine`` and ``async_engine``
properties are needed for PostgreSQL cache operations.

Issue #2189: Replace concrete nexus.storage imports with Protocol abstractions.
"""

from typing import Any, Protocol


class RecordStoreProtocol(Protocol):
    """Slim protocol for relational storage engine access.

    The Cache brick only needs synchronous and asynchronous SQLAlchemy
    engine handles for running raw SQL queries. This avoids importing
    the full ``RecordStoreABC`` from ``nexus.storage``.
    """

    @property
    def engine(self) -> Any:
        """Synchronous SQLAlchemy engine."""
        ...

    @property
    def async_engine(self) -> Any:
        """Async SQLAlchemy engine, or None if async is not supported."""
        ...
