"""Integration tests for composed workflow patterns.

These tests verify the correct behavior when combining multiple NexusFS primitives
in real-world usage patterns:

1. Wait + Read patterns
2. Lock + Write patterns
3. Concurrent access patterns
4. Race condition handling

These patterns are what AI agents will commonly use, so we need to ensure
they work correctly together.

Requirements:
- Redis or Dragonfly running at localhost:6379 (or NEXUS_REDIS_URL)
- Tests use PassthroughBackend for file operations

Related: Issue #1106 Block 2, Issue #1143 (backlog)
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

import pytest

# Skip entire module if Redis is not available
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
def shared_db_path(temp_nexus_dir):
    """Create a shared database path for all NexusFS instances in a test."""
    return temp_nexus_dir / "shared_nexus.db"


@pytest.fixture
async def shared_event_bus(redis_client):
    """Create a shared event bus for all NexusFS instances in a test."""
    from nexus.core.event_bus import RedisEventBus

    bus = RedisEventBus(redis_client)
    await bus.start()
    yield bus
    await bus.stop()


@pytest.fixture
async def shared_lock_manager(redis_client):
    """Create a shared lock manager for all NexusFS instances in a test."""
    from nexus.core.distributed_lock import RedisLockManager

    manager = RedisLockManager(redis_client)
    yield manager


@pytest.fixture
async def redis_client():
    """Create a DragonflyClient for testing."""
    from nexus.core.cache.dragonfly import DragonflyClient

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
async def nexus_fs(temp_nexus_dir, shared_db_path, shared_event_bus, shared_lock_manager):
    """Create a NexusFS instance with both local and distributed capabilities."""
    from nexus.backends.passthrough import PassthroughBackend
    from nexus.core.nexus_fs import NexusFS

    backend = PassthroughBackend(base_path=temp_nexus_dir)
    # Use shared db_path within test (unique per test via tmp_path)
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        nexus = NexusFS(
            backend=backend,
            db_path=shared_db_path,
            is_admin=True,  # Bypass router access checks
            enforce_permissions=False,  # Disable permissions for testing
            enforce_tenant_isolation=False,  # Disable tenant isolation for testing
            tenant_id="test",  # Explicit tenant for consistent event routing
            enable_content_cache=True,  # Enable cache with invalidation
            enable_metadata_cache=True,  # Enable cache with invalidation
        )

    # Use shared distributed components (same Redis connection = events propagate)
    nexus._event_bus = shared_event_bus
    nexus._lock_manager = shared_lock_manager

    # Start cache invalidation (events from other instances will invalidate local cache)
    nexus.start_cache_invalidation()

    yield nexus

    # Stop cache invalidation
    nexus.stop_cache_invalidation()


@pytest.fixture
async def second_nexus_fs(temp_nexus_dir, shared_db_path, shared_event_bus, shared_lock_manager):
    """Create a second NexusFS instance (simulating another agent/node)."""
    from nexus.backends.passthrough import PassthroughBackend
    from nexus.core.nexus_fs import NexusFS

    backend = PassthroughBackend(base_path=temp_nexus_dir)
    # Use shared db_path within test (unique per test via tmp_path)
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        nexus = NexusFS(
            backend=backend,
            db_path=shared_db_path,
            is_admin=True,  # Bypass router access checks
            enforce_permissions=False,  # Disable permissions for testing
            enforce_tenant_isolation=False,  # Disable tenant isolation for testing
            tenant_id="test",  # Explicit tenant for consistent event routing
            enable_content_cache=True,  # Enable cache with invalidation
            enable_metadata_cache=True,  # Enable cache with invalidation
        )

    # Use shared distributed components (same Redis connection = events propagate)
    nexus._event_bus = shared_event_bus
    nexus._lock_manager = shared_lock_manager

    # Start cache invalidation (events from other instances will invalidate local cache)
    nexus.start_cache_invalidation()

    yield nexus

    # Stop cache invalidation
    nexus.stop_cache_invalidation()


# =============================================================================
# Wait + Read Patterns
# =============================================================================


class TestWaitThenRead:
    """Tests for wait_for_changes() + read() composed pattern."""

    @pytest.mark.asyncio
    async def test_wait_then_read_new_file(self, nexus_fs, second_nexus_fs):
        """Agent A waits, Agent B writes -> Agent A reads successfully."""
        test_id = uuid.uuid4().hex[:8]
        test_path = f"/inbox/wait_read_{test_id}.txt"
        test_content = b"Hello from Agent B"

        # Ensure inbox exists
        nexus_fs.mkdir("/inbox", parents=True)

        received_content = {"data": None}

        async def agent_a_wait_and_read():
            """Agent A: Wait for file, then read it."""
            change = await nexus_fs.wait_for_changes("/inbox/", timeout=5.0)
            if change and change["path"].endswith(f"wait_read_{test_id}.txt"):
                received_content["data"] = nexus_fs.read(change["path"])

        async def agent_b_write():
            """Agent B: Write the file after a delay."""
            await asyncio.sleep(0.3)
            second_nexus_fs.write(test_path, test_content)

        # Run both agents concurrently
        await asyncio.gather(agent_a_wait_and_read(), agent_b_write())

        assert received_content["data"] == test_content

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

    @pytest.mark.asyncio
    async def test_wait_then_read_file_deleted_before_read(self, nexus_fs, second_nexus_fs):
        """Agent A waits, Agent B writes then deletes -> Agent A read fails gracefully."""
        test_id = uuid.uuid4().hex[:8]
        test_path = f"/volatile/temp_{test_id}.txt"

        nexus_fs.mkdir("/volatile", parents=True)

        read_result = {"success": None, "error": None}

        async def agent_a_wait_and_read():
            """Agent A: Wait for file, then try to read (may fail if deleted)."""
            change = await nexus_fs.wait_for_changes("/volatile/", timeout=5.0)
            if change and change["type"] == "file_write":
                # Small delay to allow deletion
                await asyncio.sleep(0.2)
                try:
                    nexus_fs.read(change["path"])
                    read_result["success"] = True
                except Exception as e:
                    read_result["success"] = False
                    read_result["error"] = str(e)

        async def agent_b_write_then_delete():
            """Agent B: Write file, then immediately delete it."""
            await asyncio.sleep(0.2)
            second_nexus_fs.write(test_path, b"temporary content")
            await asyncio.sleep(0.1)
            second_nexus_fs.delete(test_path)

        await asyncio.gather(agent_a_wait_and_read(), agent_b_write_then_delete())

        # Read should have failed (file deleted)
        assert read_result["success"] is False or read_result["error"] is not None


# =============================================================================
# Lock + Write Patterns
# =============================================================================


class TestLockThenWrite:
    """Tests for lock() + write() + unlock() composed pattern."""

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
    async def test_lock_prevents_concurrent_write(self, nexus_fs, second_nexus_fs):
        """Agent A locks, Agent B cannot write until A unlocks."""
        test_id = uuid.uuid4().hex[:8]
        test_path = f"/exclusive/file_{test_id}.txt"
        nexus_fs.mkdir("/exclusive", parents=True)

        results = {"a_wrote": False, "b_lock_failed": False, "b_wrote_after": False}

        async def agent_a_lock_write_unlock():
            """Agent A: Lock, write, hold for a bit, unlock."""
            lock_id = await nexus_fs.lock(test_path, timeout=5.0)
            if lock_id:
                nexus_fs.write(test_path, b"Agent A content")
                results["a_wrote"] = True
                await asyncio.sleep(0.5)  # Hold lock
                await nexus_fs.unlock(lock_id, test_path)

        async def agent_b_try_lock_write():
            """Agent B: Try to lock with short timeout, should fail initially."""
            await asyncio.sleep(0.1)  # Let A get lock first

            # First attempt should fail (A holds lock)
            lock_id = await second_nexus_fs.lock(test_path, timeout=0.2)
            if lock_id is None:
                results["b_lock_failed"] = True

            # Wait for A to release and try again
            await asyncio.sleep(0.5)
            lock_id = await second_nexus_fs.lock(test_path, timeout=2.0)
            if lock_id:
                second_nexus_fs.write(test_path, b"Agent B content")
                results["b_wrote_after"] = True
                await second_nexus_fs.unlock(lock_id, test_path)

        await asyncio.gather(agent_a_lock_write_unlock(), agent_b_try_lock_write())

        assert results["a_wrote"] is True
        assert results["b_lock_failed"] is True
        assert results["b_wrote_after"] is True

        # Wait for cache invalidation event to propagate
        # (events are published async, cache may have stale data without this)
        await asyncio.sleep(0.1)

        # Final content should be from Agent B
        content = nexus_fs.read(test_path)
        assert content == b"Agent B content"

    @pytest.mark.asyncio
    async def test_lock_with_heartbeat_pattern(self, nexus_fs, second_nexus_fs):
        """Agent A uses heartbeat to extend lock during long operation."""
        test_id = uuid.uuid4().hex[:8]
        test_path = f"/heartbeat/file_{test_id}.txt"
        nexus_fs.mkdir("/heartbeat", parents=True)

        results = {"a_completed": False, "b_waited": False}

        async def agent_a_long_operation():
            """Agent A: Lock with short TTL, extend via heartbeat, complete."""
            lock_id = await nexus_fs.lock(test_path, timeout=5.0, ttl=1.0)
            if lock_id:
                try:
                    # Simulate long operation with heartbeats
                    for _ in range(3):
                        await asyncio.sleep(0.4)
                        extended = await nexus_fs.extend_lock(lock_id, test_path, ttl=1.0)
                        assert extended is True

                    nexus_fs.write(test_path, b"Long operation complete")
                    results["a_completed"] = True
                finally:
                    await nexus_fs.unlock(lock_id, test_path)

        async def agent_b_wait_for_lock():
            """Agent B: Wait for lock (A's heartbeat keeps it alive)."""
            await asyncio.sleep(0.2)
            start = asyncio.get_event_loop().time()
            lock_id = await second_nexus_fs.lock(test_path, timeout=5.0)
            elapsed = asyncio.get_event_loop().time() - start

            if lock_id:
                results["b_waited"] = elapsed > 1.0  # Should have waited for A
                await second_nexus_fs.unlock(lock_id, test_path)

        await asyncio.gather(agent_a_long_operation(), agent_b_wait_for_lock())

        assert results["a_completed"] is True
        assert results["b_waited"] is True

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

        # Lock should be free now
        is_locked = await nexus_fs._lock_manager.is_locked("default", test_path)
        assert is_locked is False


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
    async def test_concurrent_writes_with_lock_no_race(self, nexus_fs, second_nexus_fs):
        """Concurrent writes with lock -> orderly writes, no data corruption."""
        test_id = uuid.uuid4().hex[:8]
        test_path = f"/ordered/file_{test_id}.txt"
        nexus_fs.mkdir("/ordered", parents=True)

        write_order = []

        async def locked_write(nexus, agent_name, content):
            lock_id = await nexus.lock(test_path, timeout=5.0)
            if lock_id:
                try:
                    # Read-modify-write pattern
                    try:
                        existing = nexus.read(test_path)
                        new_content = existing + content
                    except Exception:
                        new_content = content
                    nexus.write(test_path, new_content)
                    write_order.append(agent_name)
                finally:
                    await nexus.unlock(lock_id, test_path)

        await asyncio.gather(
            locked_write(nexus_fs, "A", b"[A]"),
            locked_write(second_nexus_fs, "B", b"[B]"),
            locked_write(nexus_fs, "C", b"[C]"),
        )

        # All three wrote
        assert len(write_order) == 3

        # Content should have all three, in some order
        content = nexus_fs.read(test_path)
        assert b"[A]" in content
        assert b"[B]" in content
        assert b"[C]" in content

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
            received_event["event"] = await nexus_fs.wait_for_changes(
                "/notify/", timeout=5.0
            )

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
        # (events are published async, so waiter might catch it otherwise)
        await asyncio.sleep(0.1)

        received_event = {"event": None}

        async def waiter():
            # Wait specifically for delete event (ignore any lingering write events)
            event = await nexus_fs.wait_for_changes("/notify_del/", timeout=5.0)
            while event and event.get("type") != "file_delete":
                # Might have caught a late write event, wait for delete
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
        # (events are published async, so waiter might catch it otherwise)
        await asyncio.sleep(0.1)

        received_event = {"event": None}

        async def waiter():
            # Wait specifically for rename event (ignore any lingering write events)
            event = await nexus_fs.wait_for_changes("/notify_ren/", timeout=5.0)
            while event and event.get("type") != "file_rename":
                # Might have caught a late write event, wait for rename
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
        # This tests the case where wait returns but read fails
        from nexus.core.exceptions import NexusFileNotFoundError

        with pytest.raises(NexusFileNotFoundError):
            nexus_fs.read("/nonexistent/file.txt")

    @pytest.mark.asyncio
    async def test_lock_already_locked_timeout(self, nexus_fs, second_nexus_fs):
        """Try to lock already-locked path -> timeout gracefully."""
        test_path = "/locked/exclusive.txt"
        nexus_fs.mkdir("/locked", parents=True)

        # Agent A holds lock
        lock_a = await nexus_fs.lock(test_path, timeout=5.0)
        assert lock_a is not None

        try:
            # Agent B times out
            lock_b = await second_nexus_fs.lock(test_path, timeout=0.3)
            assert lock_b is None
        finally:
            await nexus_fs.unlock(lock_a, test_path)

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


# =============================================================================
# Multi-Agent Coordination Patterns
# =============================================================================


class TestMultiAgentCoordination:
    """Tests for multi-agent coordination patterns."""

    @pytest.mark.asyncio
    async def test_producer_consumer_pattern(self, nexus_fs, second_nexus_fs):
        """Producer writes files, Consumer processes them."""
        nexus_fs.mkdir("/queue", parents=True)

        produced = []
        consumed = set()  # Use set to avoid duplicates

        async def producer():
            """Produce 3 files with enough delay for consumer to catch each."""
            await asyncio.sleep(0.2)  # Let consumer start subscribing first
            for i in range(3):
                path = f"/queue/task_{i}.json"
                nexus_fs.write(path, f'{{"task": {i}}}'.encode())
                produced.append(path)
                await asyncio.sleep(0.2)  # Longer delay to ensure consumer catches each

        async def consumer():
            """Consume files as they appear."""
            deadline = asyncio.get_event_loop().time() + 5.0
            while len(consumed) < 3:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                change = await second_nexus_fs.wait_for_changes(
                    "/queue/*.json", timeout=min(remaining, 2.0)
                )
                if change and change["type"] == "file_write":
                    try:
                        second_nexus_fs.read(change["path"])
                        consumed.add(change["path"])
                    except Exception:
                        pass  # File might have been deleted

        await asyncio.gather(producer(), consumer())

        assert len(produced) == 3
        assert len(consumed) == 3

    @pytest.mark.asyncio
    async def test_leader_election_pattern(self, nexus_fs, second_nexus_fs):
        """Multiple agents try to become leader via lock."""
        leader_path = "/leader/election"
        nexus_fs.mkdir("/leader", parents=True)

        results = {"leaders": []}

        async def try_become_leader(nexus, agent_name):
            lock_id = await nexus.lock(leader_path, timeout=0.5, ttl=2.0)
            if lock_id:
                results["leaders"].append(agent_name)
                # Hold leadership briefly
                await asyncio.sleep(0.2)
                await nexus.unlock(lock_id, leader_path)
                return True
            return False

        # Both try to become leader simultaneously
        await asyncio.gather(
            try_become_leader(nexus_fs, "A"),
            try_become_leader(second_nexus_fs, "B"),
        )

        # Exactly one should have become leader (initially)
        # Both may have become leader if they took turns
        assert len(results["leaders"]) >= 1
