"""Unit tests for PostgreSQLStorageDriver.

Tests use an in-memory SQLite database with a real SQLAlchemy session
factory, verifying ORM queries and error handling without requiring
a real PostgreSQL instance.

For true integration testing against PostgreSQL, see
tests/integration/ipc/storage/ (marked with @pytest.mark.integration).

Rewritten for Issue #1469: driver now uses RecordStoreABC session_factory
instead of raw asyncpg.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.ipc.storage.postgresql_driver import (
    PostgreSQLStorageDriver,
    _basename,
    _parent_dir,
)
from nexus.storage.models._base import Base
from nexus.storage.models.ipc_message import IPCMessageModel  # noqa: F401 — register model

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory():
    """Create an in-memory SQLite engine + session factory with tables.

    Uses StaticPool so that ``asyncio.to_thread()`` in the driver shares
    the same underlying connection (in-memory SQLite is per-connection).
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    return factory


@pytest.fixture
def driver(session_factory):
    """Create a PostgreSQLStorageDriver with test session factory."""
    return PostgreSQLStorageDriver(session_factory=session_factory)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_file(session_factory, path: str, data: bytes, zone_id: str = "zone1") -> None:
    """Insert a file row directly for test setup."""

    with session_factory() as session:
        session.add(
            IPCMessageModel(
                zone_id=zone_id,
                path=path,
                dir_path=_parent_dir(path),
                filename=_basename(path),
                data=data,
                is_dir=False,
            )
        )
        session.commit()


def _seed_dir(session_factory, path: str, zone_id: str = "zone1") -> None:
    """Insert a directory marker directly for test setup."""

    normalized = path.rstrip("/")
    with session_factory() as session:
        session.add(
            IPCMessageModel(
                zone_id=zone_id,
                path=normalized,
                dir_path=_parent_dir(normalized),
                filename=_basename(normalized),
                data=b"",
                is_dir=True,
            )
        )
        session.commit()


# ---------------------------------------------------------------------------
# Helper path function tests
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    """Tests for _parent_dir and _basename utilities."""

    def test_parent_dir_nested(self) -> None:
        assert _parent_dir("/agents/bob/inbox/msg.json") == "/agents/bob/inbox"

    def test_parent_dir_root_child(self) -> None:
        assert _parent_dir("/agents") == "/"

    def test_parent_dir_trailing_slash(self) -> None:
        assert _parent_dir("/agents/bob/") == "/agents"

    def test_basename_file(self) -> None:
        assert _basename("/agents/bob/inbox/msg.json") == "msg.json"

    def test_basename_dir_trailing_slash(self) -> None:
        assert _basename("/agents/bob/") == "bob"

    def test_basename_single(self) -> None:
        assert _basename("file.txt") == "file.txt"


# ---------------------------------------------------------------------------
# Read tests
# ---------------------------------------------------------------------------


class TestPostgreSQLStorageDriverRead:
    """Tests for read operations."""

    @pytest.mark.asyncio
    async def test_read_returns_data(self, driver, session_factory) -> None:
        _seed_file(session_factory, "/agents/bob/inbox/msg.json", b'{"msg": "hello"}')

        result = await driver.read("/agents/bob/inbox/msg.json", "zone1")

        assert result == b'{"msg": "hello"}'

    @pytest.mark.asyncio
    async def test_read_nonexistent_raises_file_not_found(self, driver) -> None:
        with pytest.raises(FileNotFoundError, match="No such file"):
            await driver.read("/nonexistent", "zone1")

    @pytest.mark.asyncio
    async def test_read_does_not_return_directories(self, driver, session_factory) -> None:
        _seed_dir(session_factory, "/agents/bob/inbox")

        with pytest.raises(FileNotFoundError, match="No such file"):
            await driver.read("/agents/bob/inbox", "zone1")


# ---------------------------------------------------------------------------
# Write tests
# ---------------------------------------------------------------------------


class TestPostgreSQLStorageDriverWrite:
    """Tests for write operations."""

    @pytest.mark.asyncio
    async def test_write_creates_file(self, driver) -> None:
        await driver.write("/agents/bob/inbox/msg.json", b"data", "zone1")

        result = await driver.read("/agents/bob/inbox/msg.json", "zone1")
        assert result == b"data"

    @pytest.mark.asyncio
    async def test_write_upsert_overwrites(self, driver) -> None:
        await driver.write("/agents/bob/inbox/msg.json", b"old", "zone1")
        await driver.write("/agents/bob/inbox/msg.json", b"new", "zone1")

        result = await driver.read("/agents/bob/inbox/msg.json", "zone1")
        assert result == b"new"


# ---------------------------------------------------------------------------
# List dir tests
# ---------------------------------------------------------------------------


class TestPostgreSQLStorageDriverListDir:
    """Tests for list_dir operations."""

    @pytest.mark.asyncio
    async def test_list_dir_returns_filenames(self, driver, session_factory) -> None:
        _seed_dir(session_factory, "/agents/bob/inbox")
        _seed_file(session_factory, "/agents/bob/inbox/msg_001.json", b"a")
        _seed_file(session_factory, "/agents/bob/inbox/msg_002.json", b"b")

        result = await driver.list_dir("/agents/bob/inbox", "zone1")

        assert result == ["msg_001.json", "msg_002.json"]

    @pytest.mark.asyncio
    async def test_list_dir_nonexistent_raises(self, driver) -> None:
        with pytest.raises(FileNotFoundError, match="No such directory"):
            await driver.list_dir("/nonexistent", "zone1")

    @pytest.mark.asyncio
    async def test_list_dir_strips_trailing_slash(self, driver, session_factory) -> None:
        _seed_dir(session_factory, "/agents/bob/inbox")

        result = await driver.list_dir("/agents/bob/inbox/", "zone1")

        assert result == []  # Empty dir


# ---------------------------------------------------------------------------
# Count dir tests
# ---------------------------------------------------------------------------


class TestPostgreSQLStorageDriverCountDir:
    """Tests for count_dir operations."""

    @pytest.mark.asyncio
    async def test_count_dir_returns_count(self, driver, session_factory) -> None:
        _seed_dir(session_factory, "/agents/bob/inbox")
        _seed_file(session_factory, "/agents/bob/inbox/msg_001.json", b"a")
        _seed_file(session_factory, "/agents/bob/inbox/msg_002.json", b"b")

        result = await driver.count_dir("/agents/bob/inbox", "zone1")

        assert result == 2

    @pytest.mark.asyncio
    async def test_count_dir_excludes_subdirectories(self, driver, session_factory) -> None:
        _seed_dir(session_factory, "/agents/bob/inbox")
        _seed_file(session_factory, "/agents/bob/inbox/msg.json", b"a")
        _seed_dir(session_factory, "/agents/bob/inbox/subdir")

        result = await driver.count_dir("/agents/bob/inbox", "zone1")

        assert result == 1  # Only files, not subdirs

    @pytest.mark.asyncio
    async def test_count_dir_nonexistent_raises(self, driver) -> None:
        with pytest.raises(FileNotFoundError, match="No such directory"):
            await driver.count_dir("/nonexistent", "zone1")


# ---------------------------------------------------------------------------
# Rename tests
# ---------------------------------------------------------------------------


class TestPostgreSQLStorageDriverRename:
    """Tests for rename operations."""

    @pytest.mark.asyncio
    async def test_rename_moves_file(self, driver, session_factory) -> None:
        _seed_file(session_factory, "/agents/bob/inbox/msg.json", b"data")

        await driver.rename(
            "/agents/bob/inbox/msg.json",
            "/agents/bob/processed/msg.json",
            "zone1",
        )

        # Old path gone
        assert await driver.exists("/agents/bob/inbox/msg.json", "zone1") is False
        # New path exists with same data
        result = await driver.read("/agents/bob/processed/msg.json", "zone1")
        assert result == b"data"

    @pytest.mark.asyncio
    async def test_rename_nonexistent_raises(self, driver) -> None:
        with pytest.raises(FileNotFoundError, match="No such file"):
            await driver.rename("/nonexistent", "/dst", "zone1")


# ---------------------------------------------------------------------------
# Mkdir tests
# ---------------------------------------------------------------------------


class TestPostgreSQLStorageDriverMkdir:
    """Tests for mkdir operations."""

    @pytest.mark.asyncio
    async def test_mkdir_creates_directory(self, driver) -> None:
        await driver.mkdir("/agents/bob/inbox", "zone1")

        assert await driver.exists("/agents/bob/inbox", "zone1") is True

    @pytest.mark.asyncio
    async def test_mkdir_idempotent(self, driver) -> None:
        await driver.mkdir("/agents/bob", "zone1")
        await driver.mkdir("/agents/bob", "zone1")  # Should not raise

        assert await driver.exists("/agents/bob", "zone1") is True


# ---------------------------------------------------------------------------
# Exists tests
# ---------------------------------------------------------------------------


class TestPostgreSQLStorageDriverExists:
    """Tests for exists operations."""

    @pytest.mark.asyncio
    async def test_exists_returns_true_for_file(self, driver, session_factory) -> None:
        _seed_file(session_factory, "/agents/bob/inbox/msg.json", b"data")

        result = await driver.exists("/agents/bob/inbox/msg.json", "zone1")

        assert result is True

    @pytest.mark.asyncio
    async def test_exists_returns_true_for_directory(self, driver, session_factory) -> None:
        _seed_dir(session_factory, "/agents/bob/inbox")

        result = await driver.exists("/agents/bob/inbox", "zone1")

        assert result is True

    @pytest.mark.asyncio
    async def test_exists_returns_false(self, driver) -> None:
        result = await driver.exists("/nonexistent", "zone1")

        assert result is False

    @pytest.mark.asyncio
    async def test_exists_strips_trailing_slash(self, driver, session_factory) -> None:
        _seed_dir(session_factory, "/agents/bob")

        result = await driver.exists("/agents/bob/", "zone1")

        assert result is True


# ---------------------------------------------------------------------------
# Zone isolation tests
# ---------------------------------------------------------------------------


class TestPostgreSQLStorageDriverZoneIsolation:
    """Tests that zone_id properly isolates data."""

    @pytest.mark.asyncio
    async def test_read_isolated_by_zone(self, driver, session_factory) -> None:
        _seed_file(session_factory, "/shared/file.json", b"zone1data", zone_id="zone1")
        _seed_file(session_factory, "/shared/file.json", b"zone2data", zone_id="zone2")

        assert await driver.read("/shared/file.json", "zone1") == b"zone1data"
        assert await driver.read("/shared/file.json", "zone2") == b"zone2data"

    @pytest.mark.asyncio
    async def test_exists_isolated_by_zone(self, driver, session_factory) -> None:
        _seed_file(session_factory, "/only-in-zone1/file.json", b"data", zone_id="zone1")

        assert await driver.exists("/only-in-zone1/file.json", "zone1") is True
        assert await driver.exists("/only-in-zone1/file.json", "zone2") is False
