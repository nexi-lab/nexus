"""Unit tests for PostgreSQLStorageDriver.

Tests use a lightweight mock asyncpg pool to verify SQL logic and
error handling without requiring a real PostgreSQL instance.

For true integration testing against PostgreSQL, see
tests/integration/ipc/storage/ (marked with @pytest.mark.integration).
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus.ipc.storage.postgresql_driver import (
    PostgreSQLStorageDriver,
    _basename,
    _parent_dir,
)

# ---------------------------------------------------------------------------
# Mock asyncpg pool + connection
# ---------------------------------------------------------------------------


class MockConnection:
    """Fake asyncpg connection that records SQL executions."""

    def __init__(self, rows: dict[str, Any] | None = None) -> None:
        self._rows = rows or {}
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        # Simulate UPDATE result string
        if sql.strip().upper().startswith("UPDATE"):
            return self._rows.get("__update_result__", "UPDATE 1")
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.executed.append((sql, args))
        return self._rows.get("__fetch__", [])

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.executed.append((sql, args))
        return self._rows.get("__fetchrow__", None)


class MockPool:
    """Fake asyncpg pool that yields a MockConnection."""

    def __init__(self, conn: MockConnection | None = None) -> None:
        self._conn = conn or MockConnection()

    def acquire(self) -> MockPoolContext:
        return MockPoolContext(self._conn)


class MockPoolContext:
    """Async context manager for pool.acquire()."""

    def __init__(self, conn: MockConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> MockConnection:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        pass


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
# PostgreSQLStorageDriver tests
# ---------------------------------------------------------------------------


class TestPostgreSQLStorageDriverInitialize:
    """Tests for table initialization."""

    @pytest.mark.asyncio
    async def test_initialize_creates_table_and_indexes(self) -> None:
        conn = MockConnection()
        pool = MockPool(conn)
        driver = PostgreSQLStorageDriver(pool=pool)

        await driver.initialize()

        # Should execute CREATE TABLE + 2 indexes = 3 statements
        assert len(conn.executed) == 3
        assert "CREATE TABLE" in conn.executed[0][0]
        assert "CREATE UNIQUE INDEX" in conn.executed[1][0]
        assert "CREATE INDEX" in conn.executed[2][0]

    @pytest.mark.asyncio
    async def test_initialize_is_idempotent(self) -> None:
        conn = MockConnection()
        pool = MockPool(conn)
        driver = PostgreSQLStorageDriver(pool=pool)

        await driver.initialize()
        count_after_first = len(conn.executed)

        await driver.initialize()  # Second call should be a no-op
        assert len(conn.executed) == count_after_first


class TestPostgreSQLStorageDriverRead:
    """Tests for read operations."""

    @pytest.mark.asyncio
    async def test_read_returns_data(self) -> None:
        conn = MockConnection(rows={"__fetchrow__": {"data": b'{"msg": "hello"}'}})
        driver = PostgreSQLStorageDriver(pool=MockPool(conn))

        result = await driver.read("/agents/bob/inbox/msg.json", "zone1")

        assert result == b'{"msg": "hello"}'
        assert len(conn.executed) == 1
        sql, args = conn.executed[0]
        assert "SELECT data" in sql
        assert "is_dir = FALSE" in sql
        assert args == ("zone1", "/agents/bob/inbox/msg.json")

    @pytest.mark.asyncio
    async def test_read_nonexistent_raises_file_not_found(self) -> None:
        conn = MockConnection(rows={"__fetchrow__": None})
        driver = PostgreSQLStorageDriver(pool=MockPool(conn))

        with pytest.raises(FileNotFoundError, match="No such file"):
            await driver.read("/nonexistent", "zone1")


class TestPostgreSQLStorageDriverWrite:
    """Tests for write operations."""

    @pytest.mark.asyncio
    async def test_write_inserts_with_upsert(self) -> None:
        conn = MockConnection()
        driver = PostgreSQLStorageDriver(pool=MockPool(conn))

        await driver.write("/agents/bob/inbox/msg.json", b"data", "zone1")

        assert len(conn.executed) == 1
        sql, args = conn.executed[0]
        assert "INSERT INTO ipc_messages" in sql
        assert "ON CONFLICT" in sql
        assert args == (
            "zone1",
            "/agents/bob/inbox/msg.json",
            "/agents/bob/inbox",
            "msg.json",
            b"data",
        )


class TestPostgreSQLStorageDriverListDir:
    """Tests for list_dir operations."""

    @pytest.mark.asyncio
    async def test_list_dir_returns_filenames(self) -> None:
        conn = MockConnection(
            rows={
                # _dir_exists check returns True
                "__fetchrow__": {"1": 1},
                # list_dir fetch returns filenames
                "__fetch__": [
                    {"filename": "msg_001.json"},
                    {"filename": "msg_002.json"},
                ],
            }
        )
        driver = PostgreSQLStorageDriver(pool=MockPool(conn))

        result = await driver.list_dir("/agents/bob/inbox", "zone1")

        assert result == ["msg_001.json", "msg_002.json"]

    @pytest.mark.asyncio
    async def test_list_dir_nonexistent_raises(self) -> None:
        conn = MockConnection(rows={"__fetchrow__": None})
        driver = PostgreSQLStorageDriver(pool=MockPool(conn))

        with pytest.raises(FileNotFoundError, match="No such directory"):
            await driver.list_dir("/nonexistent", "zone1")

    @pytest.mark.asyncio
    async def test_list_dir_strips_trailing_slash(self) -> None:
        conn = MockConnection(
            rows={
                "__fetchrow__": {"1": 1},
                "__fetch__": [],
            }
        )
        driver = PostgreSQLStorageDriver(pool=MockPool(conn))

        await driver.list_dir("/agents/bob/inbox/", "zone1")

        # _dir_exists should receive normalized path without trailing slash
        sql, args = conn.executed[0]
        assert args[1] == "/agents/bob/inbox"


class TestPostgreSQLStorageDriverCountDir:
    """Tests for count_dir operations."""

    @pytest.mark.asyncio
    async def test_count_dir_returns_count(self) -> None:
        # The driver calls _dir_exists first, then COUNT(*)
        # Both use fetchrow — mock needs to return sequentially
        # Since our mock returns the same value for all fetchrow calls,
        # we'll test the SQL structure
        call_count = 0
        results = [{"1": 1}, {"cnt": 5}]  # _dir_exists, then COUNT

        class SeqConnection(MockConnection):
            async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
                nonlocal call_count
                self.executed.append((sql, args))
                result = results[call_count] if call_count < len(results) else None
                call_count += 1
                return result

        conn = SeqConnection()
        driver = PostgreSQLStorageDriver(pool=MockPool(conn))

        result = await driver.count_dir("/agents/bob/inbox", "zone1")

        assert result == 5

    @pytest.mark.asyncio
    async def test_count_dir_nonexistent_raises(self) -> None:
        conn = MockConnection(rows={"__fetchrow__": None})
        driver = PostgreSQLStorageDriver(pool=MockPool(conn))

        with pytest.raises(FileNotFoundError, match="No such directory"):
            await driver.count_dir("/nonexistent", "zone1")


class TestPostgreSQLStorageDriverRename:
    """Tests for rename operations."""

    @pytest.mark.asyncio
    async def test_rename_updates_path(self) -> None:
        conn = MockConnection(rows={"__update_result__": "UPDATE 1"})
        driver = PostgreSQLStorageDriver(pool=MockPool(conn))

        await driver.rename(
            "/agents/bob/inbox/msg.json",
            "/agents/bob/processed/msg.json",
            "zone1",
        )

        assert len(conn.executed) == 1
        sql, args = conn.executed[0]
        assert "UPDATE ipc_messages" in sql
        assert args == (
            "zone1",
            "/agents/bob/inbox/msg.json",
            "/agents/bob/processed/msg.json",
            "/agents/bob/processed",
            "msg.json",
        )

    @pytest.mark.asyncio
    async def test_rename_nonexistent_raises(self) -> None:
        conn = MockConnection(rows={"__update_result__": "UPDATE 0"})
        driver = PostgreSQLStorageDriver(pool=MockPool(conn))

        with pytest.raises(FileNotFoundError, match="No such file"):
            await driver.rename("/nonexistent", "/dst", "zone1")


class TestPostgreSQLStorageDriverMkdir:
    """Tests for mkdir operations."""

    @pytest.mark.asyncio
    async def test_mkdir_inserts_dir_marker(self) -> None:
        conn = MockConnection()
        driver = PostgreSQLStorageDriver(pool=MockPool(conn))

        await driver.mkdir("/agents/bob/inbox", "zone1")

        assert len(conn.executed) == 1
        sql, args = conn.executed[0]
        assert "INSERT INTO ipc_messages" in sql
        assert "is_dir" in sql
        assert "ON CONFLICT" in sql
        assert "DO NOTHING" in sql
        assert args[2] == "/agents/bob"  # dir_path (parent)
        assert args[3] == "inbox"  # filename (basename)

    @pytest.mark.asyncio
    async def test_mkdir_idempotent(self) -> None:
        conn = MockConnection()
        driver = PostgreSQLStorageDriver(pool=MockPool(conn))

        # Should not raise even if directory already exists (ON CONFLICT DO NOTHING)
        await driver.mkdir("/agents/bob", "zone1")
        await driver.mkdir("/agents/bob", "zone1")


class TestPostgreSQLStorageDriverExists:
    """Tests for exists operations."""

    @pytest.mark.asyncio
    async def test_exists_returns_true(self) -> None:
        conn = MockConnection(rows={"__fetchrow__": {"1": 1}})
        driver = PostgreSQLStorageDriver(pool=MockPool(conn))

        result = await driver.exists("/agents/bob/inbox", "zone1")

        assert result is True

    @pytest.mark.asyncio
    async def test_exists_returns_false(self) -> None:
        conn = MockConnection(rows={"__fetchrow__": None})
        driver = PostgreSQLStorageDriver(pool=MockPool(conn))

        result = await driver.exists("/nonexistent", "zone1")

        assert result is False

    @pytest.mark.asyncio
    async def test_exists_strips_trailing_slash(self) -> None:
        conn = MockConnection(rows={"__fetchrow__": {"1": 1}})
        driver = PostgreSQLStorageDriver(pool=MockPool(conn))

        await driver.exists("/agents/bob/", "zone1")

        _, args = conn.executed[0]
        assert args[1] == "/agents/bob"
