"""Integration tests for composed workflow patterns.

These tests verify the correct behavior when combining multiple NexusFS primitives
in real-world usage patterns:

1. Wait + Read patterns (event propagation via Redis)
2. Lock + Write patterns (single-instance Raft locks)
3. Concurrent access patterns
4. Error handling patterns

Requirements:
- Redis or Dragonfly running at localhost:6379 (or NEXUS_REDIS_URL) for events
- Tests use PassthroughBackend for file operations

Architecture Notes (Raft Zone Migration):
- Metadata is stored in sled (RaftMetadataStore) - each instance has its own database
- Locks are Raft-based (per-instance RaftLockManager)
- Events propagate via Redis (shared event bus)
- For multi-instance lock testing with shared locks, use:
  - test_raft_locks.py (single-instance Raft locks)
  - test_raft_distributed.py (Docker-based distributed Raft cluster)

Related: Issue #1106 Block 2, Issue #1159 (Raft Consensus Zones)
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

import pytest

from nexus.factory import create_nexus_fs
from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore

# Skip entire module if Redis is not available (needed for event bus)
pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("NEXUS_REDIS_URL") and not os.environ.get("REDIS_ENABLED"),
        reason="Redis not available (set NEXUS_REDIS_URL or REDIS_ENABLED=1)",
    ),
    pytest.mark.skipif(
        sys.platform not in ("linux", "win32"),
        reason="File watching only supported on Linux and Windows",
    ),
]


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def temp_nexus_dir(tmp_path):
    """Create a temporary directory for NexusFS."""
    nexus_dir = tmp_path / "nexus_data"
    nexus_dir.mkdir(parents=True)
    return nexus_dir


@pytest.fixture
def db_path_agent1(temp_nexus_dir):
    """Create a database path for agent 1 (sled uses exclusive file locks)."""
    return temp_nexus_dir / "agent1.db"


@pytest.fixture
def db_path_agent2(temp_nexus_dir):
    """Create a database path for agent 2 (sled uses exclusive file locks)."""
    return temp_nexus_dir / "agent2.db"


@pytest.fixture
async def shared_event_bus(redis_client):
    """Create a shared event bus for all NexusFS instances in a test."""
    from nexus.core.event_bus import RedisEventBus

    bus = RedisEventBus(redis_client)
    await bus.start()
    yield bus
    await bus.stop()


@pytest.fixture
async def redis_client():
    """Create a DragonflyClient for testing."""
    from nexus.cache.dragonfly import DragonflyClient

    redis_url = os.environ.get("NEXUS_REDIS_URL", "redis://localhost:6379")
    client = DragonflyClient(url=redis_url)

    try:
        await client.connect()
        is_healthy = await client.health_check()
        if not is_healthy:
            pytest.skip("Redis not healthy")
    except Exception as e:
        pytest.skip(f"Redis connection failed: {e}")

    yield client

    await client.disconnect()


@pytest.fixture
async def nexus_fs(temp_nexus_dir, db_path_agent1, shared_event_bus):
    """Create a NexusFS instance with event bus and Raft-based locks."""
    from nexus.backends.passthrough import PassthroughBackend

    backend = PassthroughBackend(base_path=temp_nexus_dir)
    # Each agent gets its own sled database (sled uses exclusive file locks)
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        nexus = create_nexus_fs(
            backend=backend,
            metadata_store=RaftMetadataStore.embedded(
                str(db_path_agent1).replace(
                    chr(34) + chr(46) + chr(100) + chr(98) + chr(34),
                    chr(34) + chr(45) + chr(114) + chr(97) + chr(102) + chr(116) + chr(34),
                )
            ),
            record_store=SQLAlchemyRecordStore(db_path=db_path_agent1),
            is_admin=True,  # Bypass router access checks
            enforce_permissions=False,  # Disable permissions for testing
            enforce_zone_isolation=False,  # Disable zone isolation for testing
            zone_id="test",  # Explicit zone for consistent event routing
            enable_content_cache=True,  # Enable cache with invalidation
            enable_metadata_cache=True,  # Enable cache with invalidation
            enable_distributed_locks=True,  # Uses Raft-based locks via RaftMetadataStore
        )

    # Use shared event bus (same Redis connection = events propagate)
    nexus._event_bus = shared_event_bus

    # Start cache invalidation (events from other instances will invalidate local cache)
    nexus._start_cache_invalidation()

    yield nexus

    # Stop cache invalidation
    nexus._stop_cache_invalidation()


@pytest.fixture
async def second_nexus_fs(temp_nexus_dir, db_path_agent2, shared_event_bus):
    """Create a second NexusFS instance (simulating another agent on different machine).

    Note: Each instance has its own Raft-based lock manager, so locks are NOT
    shared between instances without a Raft cluster. For shared lock tests,
    see test_raft_distributed.py (Docker-based).
    """
    from nexus.backends.passthrough import PassthroughBackend

    backend = PassthroughBackend(base_path=temp_nexus_dir)
    # Each agent gets its own sled database (sled uses exclusive file locks)
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        nexus = create_nexus_fs(
            backend=backend,
            metadata_store=RaftMetadataStore.embedded(str(db_path_agent2).replace(".db", "-raft")),
            record_store=SQLAlchemyRecordStore(db_path=db_path_agent2),
            is_admin=True,  # Bypass router access checks
            enforce_permissions=False,  # Disable permissions for testing
            enforce_zone_isolation=False,  # Disable zone isolation for testing
            zone_id="test",  # Explicit zone for consistent event routing
            enable_content_cache=True,  # Enable cache with invalidation
            enable_metadata_cache=True,  # Enable cache with invalidation
            enable_distributed_locks=True,  # Uses Raft-based locks via RaftMetadataStore
        )

    # Use shared event bus (same Redis connection = events propagate)
    nexus._event_bus = shared_event_bus

    # Start cache invalidation (events from other instances will invalidate local cache)
    nexus._start_cache_invalidation()

    yield nexus

    # Stop cache invalidation
    nexus._stop_cache_invalidation()


# =============================================================================
# Wait + Read Patterns (Event Propagation)
# =============================================================================


class TestWaitThenRead:
    """Tests for wait_for_changes() + read() composed pattern.

    ARCHITECTURE NOTE:
    These tests use two NexusFS instances with SEPARATE sled databases but
    SHARED Redis event bus. Events propagate via Redis, but metadata does NOT
    propagate without Raft cluster replication.

    Tests that need to read file content after receiving an event from another
    instance require shared metadata via Raft. These tests are in:
    - test_raft_distributed.py::TestDistributedEventNotifications (Docker-based)

    Tests in this class only verify event propagation, not content reads.
    """

    @pytest.mark.asyncio
    async def test_wait_then_read_with_glob_pattern(self, nexus_fs, second_nexus_fs):
        """Agent A waits for *.json, Agent B writes .json and .txt -> only .json triggers."""
        test_id = uuid.uuid4().hex[:8]
        json_path = f"/data/config_{test_id}.json"
        txt_path = f"/data/readme_{test_id}.txt"

        nexus_fs.mkdir("/data", parents=True)

        received_path = {"path": None}

        async def agent_a_wait_json():
            """Agent A: Wait for JSON files only."""
            change = await nexus_fs.wait_for_changes("/data/*.json", timeout=5.0)
            if change:
                received_path["path"] = change["path"]

        async def agent_b_write_files():
            """Agent B: Write .txt first, then .json."""
            await asyncio.sleep(0.2)
            second_nexus_fs.write(txt_path, b"readme content")
            await asyncio.sleep(0.1)
            second_nexus_fs.write(json_path, b'{"key": "value"}')

        await asyncio.gather(agent_a_wait_json(), agent_b_write_files())

        # Should have received the .json file, not the .txt
        assert received_path["path"] is not None
        assert received_path["path"].endswith(".json")

    @pytest.mark.asyncio
    async def test_wait_timeout_no_write(self, nexus_fs):
        """Agent A waits but no file written -> timeout returns None."""
        nexus_fs.mkdir("/empty", parents=True)

        change = await nexus_fs.wait_for_changes("/empty/", timeout=0.5)

        assert change is None


# =============================================================================
# Lock + Write Patterns (Single-Instance Raft Locks)
# =============================================================================


class TestLockThenWrite:
    """Tests for lock() + write() + unlock() composed pattern.

    NOTE: These tests use single-instance Raft locks. For multi-instance
    distributed lock tests, see test_raft_distributed.py (Docker-based).
    """

    @pytest.mark.asyncio
    async def test_lock_write_unlock_basic(self, nexus_fs):
        """Basic lock -> write -> unlock workflow."""
        test_path = "/shared/config.json"
        nexus_fs.mkdir("/shared", parents=True)

        lock_id = await nexus_fs.lock(test_path, timeout=5.0)
        assert lock_id is not None

        try:
            nexus_fs.write(test_path, b'{"version": 1}')
            content = nexus_fs.read(test_path)
            assert content == b'{"version": 1}'
        finally:
            released = await nexus_fs.unlock(lock_id, test_path)
            assert released is True

    @pytest.mark.asyncio
    async def test_lock_timeout_in_try_finally(self, nexus_fs):
        """Verify unlock in finally works even if operation fails."""
        test_path = "/safe/important.txt"
        nexus_fs.mkdir("/safe", parents=True)

        lock_acquired = False
        lock_released = False
        operation_failed = False

        lock_id = await nexus_fs.lock(test_path, timeout=5.0)
        if lock_id:
            lock_acquired = True
            try:
                # Simulate operation that raises
                raise ValueError("Simulated failure")
            except ValueError:
                operation_failed = True
            finally:
                released = await nexus_fs.unlock(lock_id, test_path)
                lock_released = released

        assert lock_acquired is True
        assert operation_failed is True
        assert lock_released is True


# =============================================================================
# Concurrent Access Patterns
# =============================================================================


class TestConcurrentAccess:
    """Tests for concurrent read/write scenarios."""

    @pytest.mark.asyncio
    async def test_concurrent_reads_no_lock(self, nexus_fs, second_nexus_fs):
        """Multiple agents can read same file concurrently without lock."""
        test_path = "/shared/data.txt"
        nexus_fs.mkdir("/shared", parents=True)
        nexus_fs.write(test_path, b"shared content")

        results = []

        async def read_file(agent_name):
            content = nexus_fs.read(test_path)
            results.append((agent_name, content))

        await asyncio.gather(
            read_file("A"),
            read_file("B"),
            read_file("C"),
        )

        assert len(results) == 3
        for _name, content in results:
            assert content == b"shared content"

    @pytest.mark.asyncio
    async def test_concurrent_writes_without_lock_race(self, nexus_fs, second_nexus_fs):
        """Concurrent writes without lock -> last write wins (race condition)."""
        test_id = uuid.uuid4().hex[:8]
        test_path = f"/race/file_{test_id}.txt"
        nexus_fs.mkdir("/race", parents=True)

        async def write_content(nexus, content, delay):
            await asyncio.sleep(delay)
            nexus.write(test_path, content)

        # Both try to write around the same time
        await asyncio.gather(
            write_content(nexus_fs, b"Content A", 0.0),
            write_content(second_nexus_fs, b"Content B", 0.05),
        )

        # One of them won - we can't predict which
        content = nexus_fs.read(test_path)
        assert content in (b"Content A", b"Content B")

    @pytest.mark.asyncio
    async def test_read_while_write_in_progress(self, nexus_fs, second_nexus_fs):
        """Read during write -> gets consistent content (no partial reads)."""
        test_id = uuid.uuid4().hex[:8]
        test_path = f"/atomic/large_{test_id}.txt"
        nexus_fs.mkdir("/atomic", parents=True)

        # Create initial content
        initial_content = b"A" * 10000
        nexus_fs.write(test_path, initial_content)

        new_content = b"B" * 10000
        read_results = []

        async def reader():
            for _ in range(5):
                await asyncio.sleep(0.01)
                try:
                    content = nexus_fs.read(test_path)
                    read_results.append(content)
                except Exception:
                    pass  # File might not exist yet or be in transition

        async def writer():
            await asyncio.sleep(0.02)
            second_nexus_fs.write(test_path, new_content)

        await asyncio.gather(reader(), writer())

        # All reads should be consistent (all A's or all B's, no mix)
        for content in read_results:
            assert content in (initial_content, new_content)
            # No partial reads
            assert not (b"A" in content and b"B" in content)


# =============================================================================
# Event Notification Patterns
# =============================================================================


class TestEventNotification:
    """Tests for event emission and notification patterns."""

    @pytest.mark.asyncio
    async def test_write_emits_event_to_waiter(self, nexus_fs, second_nexus_fs):
        """Write operation emits event that waiter receives."""
        test_id = uuid.uuid4().hex[:8]
        test_path = f"/notify/file_{test_id}.txt"
        nexus_fs.mkdir("/notify", parents=True)

        received_event = {"event": None}

        async def waiter():
            received_event["event"] = await nexus_fs.wait_for_changes("/notify/", timeout=5.0)

        async def writer():
            await asyncio.sleep(0.2)
            second_nexus_fs.write(test_path, b"notification content")

        await asyncio.gather(waiter(), writer())

        assert received_event["event"] is not None
        assert received_event["event"]["type"] == "file_write"
        assert test_path in received_event["event"]["path"]

    @pytest.mark.asyncio
    async def test_delete_emits_event_to_waiter(self, nexus_fs, second_nexus_fs):
        """Delete operation emits event that waiter receives."""
        test_id = uuid.uuid4().hex[:8]
        test_path = f"/notify_del/file_{test_id}.txt"
        nexus_fs.mkdir("/notify_del", parents=True)
        nexus_fs.write(test_path, b"to be deleted")

        # Wait for setup write event to propagate and drain
        await asyncio.sleep(0.1)

        received_event = {"event": None}

        async def waiter():
            # Wait specifically for delete event (ignore any lingering write events)
            event = await nexus_fs.wait_for_changes("/notify_del/", timeout=5.0)
            while event and event.get("type") != "file_delete":
                event = await nexus_fs.wait_for_changes("/notify_del/", timeout=3.0)
            received_event["event"] = event

        async def deleter():
            await asyncio.sleep(0.2)
            second_nexus_fs.delete(test_path)

        await asyncio.gather(waiter(), deleter())

        assert received_event["event"] is not None
        assert received_event["event"]["type"] == "file_delete"

    @pytest.mark.asyncio
    async def test_rename_emits_event_with_old_path(self, nexus_fs, second_nexus_fs):
        """Rename operation emits event with old_path field."""
        test_id = uuid.uuid4().hex[:8]
        old_path = f"/notify_ren/old_{test_id}.txt"
        new_path = f"/notify_ren/new_{test_id}.txt"
        nexus_fs.mkdir("/notify_ren", parents=True)
        nexus_fs.write(old_path, b"to be renamed")

        # Wait for setup write event to propagate and drain
        await asyncio.sleep(0.1)

        received_event = {"event": None}

        async def waiter():
            # Wait specifically for rename event (ignore any lingering write events)
            event = await nexus_fs.wait_for_changes("/notify_ren/", timeout=5.0)
            while event and event.get("type") != "file_rename":
                event = await nexus_fs.wait_for_changes("/notify_ren/", timeout=3.0)
            received_event["event"] = event

        async def renamer():
            await asyncio.sleep(0.2)
            second_nexus_fs.rename(old_path, new_path)

        await asyncio.gather(waiter(), renamer())

        assert received_event["event"] is not None
        assert received_event["event"]["type"] == "file_rename"


# =============================================================================
# Error Handling Patterns
# =============================================================================


class TestErrorHandling:
    """Tests for error handling in composed patterns."""

    @pytest.mark.asyncio
    async def test_read_nonexistent_file_after_wait(self, nexus_fs):
        """Wait returns event but file doesn't exist -> graceful error."""
        from nexus.core.exceptions import NexusFileNotFoundError

        with pytest.raises(NexusFileNotFoundError):
            nexus_fs.read("/nonexistent/file.txt")

    @pytest.mark.asyncio
    async def test_unlock_after_ttl_expired(self, nexus_fs):
        """Unlock after TTL expired -> returns False (lock gone)."""
        test_path = "/expired/lock.txt"
        nexus_fs.mkdir("/expired", parents=True)

        lock_id = await nexus_fs.lock(test_path, timeout=5.0, ttl=0.3)
        assert lock_id is not None

        # Wait for TTL to expire
        await asyncio.sleep(0.5)

        # Unlock should fail (already expired)
        released = await nexus_fs.unlock(lock_id, test_path)
        assert released is False

    @pytest.mark.asyncio
    async def test_extend_wrong_lock_id(self, nexus_fs):
        """Extend with wrong lock_id -> returns False."""
        test_path = "/wrong/lock.txt"
        nexus_fs.mkdir("/wrong", parents=True)

        lock_id = await nexus_fs.lock(test_path, timeout=5.0)
        assert lock_id is not None

        try:
            extended = await nexus_fs.extend_lock("wrong-id", test_path, ttl=30.0)
            assert extended is False
        finally:
            await nexus_fs.unlock(lock_id, test_path)
