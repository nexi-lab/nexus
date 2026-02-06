"""Integration tests for multi-slot lock (semaphore) functionality.

Tests semaphore behavior with real Redis/Dragonfly, including:
- Multiple concurrent holders
- max_holders enforcement
- SSOT config validation
- TTL expiry and auto-cleanup
- Cross-platform semaphore sharing
- High contention scenarios (boardroom simulation)

Prerequisites:
- Dragonfly running: docker compose -f dockerfiles/docker-compose.cross-platform-test.yml up -d dragonfly-coordination
- NEXUS_DRAGONFLY_COORDINATION_URL=redis://localhost:6379

Usage:
    pytest tests/integration/test_semaphore.py -v --tb=short
"""

import asyncio
import os
import subprocess
import threading
import time

import pytest


# Skip all tests if Redis is not available
def is_redis_available():
    """Check if Redis/Dragonfly is available."""
    redis_url = os.environ.get(
        "NEXUS_DRAGONFLY_COORDINATION_URL",
        os.environ.get("NEXUS_REDIS_URL"),
    )
    if not redis_url:
        return False
    try:
        import redis

        r = redis.from_url(redis_url)
        r.ping()
        return True
    except Exception:
        return False


def is_linux_container_available():
    """Check if the Linux test container is running."""
    try:
        result = subprocess.run(
            ["docker", "exec", "nexus-linux-test", "echo", "ok"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


# Conditional skip markers
requires_redis = pytest.mark.skipif(
    not is_redis_available(),
    reason="Redis not available (set NEXUS_DRAGONFLY_COORDINATION_URL)",
)

requires_linux_container = pytest.mark.skipif(
    not is_linux_container_available(),
    reason="Linux container not running",
)


def get_redis_url():
    """Get Redis URL from environment."""
    return os.environ.get(
        "NEXUS_DRAGONFLY_COORDINATION_URL",
        os.environ.get("NEXUS_REDIS_URL", "redis://localhost:6379"),
    )


# =============================================================================
# Basic Semaphore Tests (single platform)
# =============================================================================


@requires_redis
class TestSemaphoreBasic:
    """Basic semaphore functionality tests with real Redis."""

    @pytest.mark.asyncio
    async def test_semaphore_allows_multiple_holders(self):
        """Test that semaphore allows up to max_holders concurrent acquisitions."""
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        client = DragonflyClient(url=get_redis_url())
        await client.connect()
        lock_mgr = RedisLockManager(client)

        zone_id = "test-zone"
        path = "/test/semaphore-basic"
        max_holders = 5

        try:
            # Acquire max_holders locks (all should succeed)
            lock_ids = []
            for i in range(max_holders):
                lock_id = await lock_mgr.acquire(
                    zone_id=zone_id,
                    path=path,
                    timeout=5.0,
                    ttl=30.0,
                    max_holders=max_holders,
                )
                assert lock_id is not None, f"Should acquire lock {i + 1}/{max_holders}"
                lock_ids.append(lock_id)

            # (max_holders + 1)th should timeout
            extra_lock = await lock_mgr.acquire(
                zone_id=zone_id,
                path=path,
                timeout=0.5,
                ttl=30.0,
                max_holders=max_holders,
            )
            assert extra_lock is None, "Should not exceed max_holders"

            # Release one slot
            await lock_mgr.release(lock_ids[0], zone_id, path)
            lock_ids.pop(0)

            # Now should be able to acquire
            new_lock = await lock_mgr.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=30.0,
                max_holders=max_holders,
            )
            assert new_lock is not None, "Should acquire after release"
            lock_ids.append(new_lock)

        finally:
            # Cleanup
            for lock_id in lock_ids:
                await lock_mgr.release(lock_id, zone_id, path)
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_semaphore_ssot_mismatch(self):
        """Test that max_holders mismatch raises ValueError (SSOT enforcement)."""
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        client = DragonflyClient(url=get_redis_url())
        await client.connect()
        lock_mgr = RedisLockManager(client)

        zone_id = "test-zone"
        path = "/test/semaphore-ssot"

        try:
            # First acquire with max_holders=5
            lock_id = await lock_mgr.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=30.0,
                max_holders=5,
            )
            assert lock_id is not None

            # Try to acquire with different max_holders
            with pytest.raises(ValueError, match="max_holders mismatch"):
                await lock_mgr.acquire(
                    zone_id=zone_id,
                    path=path,
                    timeout=1.0,
                    ttl=30.0,
                    max_holders=3,  # Different from 5
                )

        finally:
            # Cleanup
            if lock_id:
                await lock_mgr.release(lock_id, zone_id, path)
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_semaphore_config_cleanup_after_all_release(self):
        """Test that config is cleaned up when all holders release."""
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        client = DragonflyClient(url=get_redis_url())
        await client.connect()
        lock_mgr = RedisLockManager(client)

        zone_id = "test-zone"
        path = "/test/semaphore-cleanup"

        try:
            # Acquire with max_holders=5
            lock_id = await lock_mgr.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=30.0,
                max_holders=5,
            )
            assert lock_id is not None

            # Config key should exist
            config_key = f"nexus:semaphore_config:{zone_id}:{path}"
            config_val = await client.client.get(config_key)
            assert config_val is not None, "Config should exist while holding"

            # Release
            await lock_mgr.release(lock_id, zone_id, path)

            # Config should be cleaned up
            config_val = await client.client.get(config_key)
            assert config_val is None, "Config should be cleaned up after release"

            # Now can acquire with different max_holders
            new_lock = await lock_mgr.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=30.0,
                max_holders=3,  # Different value now allowed
            )
            assert new_lock is not None, "Should allow new max_holders after cleanup"
            await lock_mgr.release(new_lock, zone_id, path)

        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_semaphore_ttl_expiry(self):
        """Test that expired semaphore slots are automatically cleaned."""
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        client = DragonflyClient(url=get_redis_url())
        await client.connect()
        lock_mgr = RedisLockManager(client)

        zone_id = "test-zone"
        path = "/test/semaphore-ttl"
        max_holders = 2
        short_ttl = 1.5  # Short TTL

        try:
            # Acquire both slots with short TTL
            lock1 = await lock_mgr.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=short_ttl,
                max_holders=max_holders,
            )
            lock2 = await lock_mgr.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=short_ttl,
                max_holders=max_holders,
            )
            assert lock1 is not None and lock2 is not None

            # Third should fail (slots full)
            lock3 = await lock_mgr.acquire(
                zone_id=zone_id,
                path=path,
                timeout=0.3,
                ttl=30.0,
                max_holders=max_holders,
            )
            assert lock3 is None, "Should be blocked (slots full)"

            # Wait for TTL to expire
            await asyncio.sleep(short_ttl + 0.5)

            # Now should be able to acquire (expired slots cleaned)
            lock4 = await lock_mgr.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=30.0,
                max_holders=max_holders,
            )
            assert lock4 is not None, "Should acquire after TTL expiry"

            await lock_mgr.release(lock4, zone_id, path)

        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_semaphore_extend(self):
        """Test extending semaphore slot TTL."""
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        client = DragonflyClient(url=get_redis_url())
        await client.connect()
        lock_mgr = RedisLockManager(client)

        zone_id = "test-zone"
        path = "/test/semaphore-extend"
        max_holders = 3
        short_ttl = 2.0

        try:
            # Acquire with short TTL
            lock_id = await lock_mgr.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=short_ttl,
                max_holders=max_holders,
            )
            assert lock_id is not None

            # Extend TTL
            extended = await lock_mgr.extend(
                lock_id=lock_id,
                zone_id=zone_id,
                path=path,
                ttl=30.0,  # Much longer TTL
            )
            assert extended is True, "Should extend successfully"

            # Wait past original TTL
            await asyncio.sleep(short_ttl + 0.5)

            # Should still be able to extend (not expired)
            extended_again = await lock_mgr.extend(
                lock_id=lock_id,
                zone_id=zone_id,
                path=path,
                ttl=30.0,
            )
            assert extended_again is True, "Should still hold lock after original TTL"

            await lock_mgr.release(lock_id, zone_id, path)

        finally:
            await client.disconnect()


# =============================================================================
# Concurrent Semaphore Tests (multi-threaded)
# =============================================================================


@requires_redis
class TestSemaphoreConcurrent:
    """Concurrent semaphore tests simulating boardroom scenarios."""

    @pytest.mark.asyncio
    async def test_boardroom_simulation(self):
        """Simulate a boardroom with 5 seats and 10 participants trying to join.

        Each participant tries to join, stays for a short time, then leaves.
        At any time, at most 5 participants should be in the room.
        """
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        zone_id = "test-zone"
        path = "/boardroom/room-01"
        max_holders = 5
        num_participants = 10
        hold_time = 0.5  # Each participant stays for 0.5s

        results = {"joined": 0, "left": 0, "errors": []}
        results_lock = threading.Lock()

        def participant(participant_id):
            """Simulate a participant trying to join the boardroom."""

            async def _participate():
                client = DragonflyClient(url=get_redis_url())
                await client.connect()
                lock_mgr = RedisLockManager(client)

                try:
                    lock_id = await lock_mgr.acquire(
                        zone_id=zone_id,
                        path=path,
                        timeout=30.0,  # Wait up to 30s
                        ttl=30.0,
                        max_holders=max_holders,
                    )
                    if lock_id:
                        with results_lock:
                            results["joined"] += 1

                        await asyncio.sleep(hold_time)  # Stay in room

                        await lock_mgr.release(lock_id, zone_id, path)

                        with results_lock:
                            results["left"] += 1
                    else:
                        with results_lock:
                            results["errors"].append(f"P{participant_id}: timeout")

                finally:
                    await client.disconnect()

            try:
                asyncio.run(_participate())
            except Exception as e:
                with results_lock:
                    results["errors"].append(f"P{participant_id}: {e}")

        # Start all participants
        threads = [threading.Thread(target=participant, args=(i,)) for i in range(num_participants)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        # Verify
        assert len(results["errors"]) == 0, f"Errors: {results['errors']}"
        assert results["joined"] == num_participants, (
            f"All participants should join. Joined: {results['joined']}"
        )
        assert results["left"] == num_participants, (
            f"All participants should leave. Left: {results['left']}"
        )

    @pytest.mark.asyncio
    async def test_concurrent_max_holders_verification(self):
        """Verify that at no point more than max_holders hold the lock.

        Uses a shared counter to track concurrent holders.
        """
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        zone_id = "test-zone"
        path = "/boardroom/verify-max"
        max_holders = 3
        num_participants = 15
        counter_key = "test:concurrent-holders"

        max_observed = {"value": 0}
        max_lock = threading.Lock()

        # Initialize counter
        init_client = DragonflyClient(url=get_redis_url())
        await init_client.connect()
        await init_client.client.set(counter_key, "0")
        await init_client.disconnect()

        def participant(participant_id):
            async def _work():
                client = DragonflyClient(url=get_redis_url())
                await client.connect()
                lock_mgr = RedisLockManager(client)

                try:
                    lock_id = await lock_mgr.acquire(
                        zone_id=zone_id,
                        path=path,
                        timeout=60.0,
                        ttl=30.0,
                        max_holders=max_holders,
                    )
                    if lock_id:
                        try:
                            # Increment concurrent counter
                            current = await client.client.incr(counter_key)

                            with max_lock:
                                if current > max_observed["value"]:
                                    max_observed["value"] = current

                            # Stay a bit
                            await asyncio.sleep(0.1)

                            # Decrement
                            await client.client.decr(counter_key)
                        finally:
                            await lock_mgr.release(lock_id, zone_id, path)

                finally:
                    await client.disconnect()

            asyncio.run(_work())

        threads = [threading.Thread(target=participant, args=(i,)) for i in range(num_participants)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        # Cleanup
        cleanup = DragonflyClient(url=get_redis_url())
        await cleanup.connect()
        await cleanup.client.delete(counter_key)
        await cleanup.disconnect()

        # Verify max_holders was never exceeded
        assert max_observed["value"] <= max_holders, (
            f"Max holders exceeded! Observed: {max_observed['value']}, Max: {max_holders}"
        )


# =============================================================================
# Cross-Platform Semaphore Tests
# =============================================================================


@requires_redis
@requires_linux_container
class TestSemaphoreCrossPlatform:
    """Cross-platform semaphore tests between Windows host and Linux container."""

    @pytest.mark.asyncio
    async def test_cross_platform_shared_boardroom(self):
        """Test Windows and Linux sharing a boardroom with limited seats.

        Boardroom has 3 seats.
        Windows acquires 2 seats, Linux should only get 1.
        """
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        zone_id = "test-zone"
        path = "/boardroom/cross-platform"
        max_holders = 3

        # Windows: acquire 2 seats
        win_client = DragonflyClient(url=get_redis_url())
        await win_client.connect()
        win_lock_mgr = RedisLockManager(win_client)

        try:
            win_lock1 = await win_lock_mgr.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=30.0,
                max_holders=max_holders,
            )
            win_lock2 = await win_lock_mgr.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=30.0,
                max_holders=max_holders,
            )
            assert win_lock1 is not None and win_lock2 is not None

            # Linux: try to acquire 2 seats (only 1 should succeed)
            linux_script = f'''
import asyncio
import sys
sys.path.insert(0, "/app/src")
from nexus.core.cache.dragonfly import DragonflyClient
from nexus.core.distributed_lock import RedisLockManager

async def try_acquire():
    client = DragonflyClient(url="redis://dragonfly-coordination:6379")
    await client.connect()
    lock_mgr = RedisLockManager(client)

    acquired_count = 0
    lock_ids = []

    # Try to acquire 2 seats
    for i in range(2):
        lock_id = await lock_mgr.acquire(
            zone_id="{zone_id}",
            path="{path}",
            timeout=1.0,  # Short timeout
            ttl=30.0,
            max_holders={max_holders},
        )
        if lock_id:
            acquired_count += 1
            lock_ids.append(lock_id)

    print(f"LINUX_ACQUIRED:{{acquired_count}}", flush=True)

    # Release what we got
    for lock_id in lock_ids:
        await lock_mgr.release(lock_id, "{zone_id}", "{path}")

    await client.disconnect()

asyncio.run(try_acquire())
'''
            result = subprocess.run(
                ["docker", "exec", "nexus-linux-test", "python", "-c", linux_script],
                capture_output=True,
                text=True,
                timeout=30,
            )

            # Linux should only get 1 seat (Windows has 2 of 3)
            assert "LINUX_ACQUIRED:1" in result.stdout, (
                f"Linux should only acquire 1 seat. stdout: {result.stdout}, stderr: {result.stderr}"
            )

        finally:
            await win_lock_mgr.release(win_lock1, zone_id, path)
            await win_lock_mgr.release(win_lock2, zone_id, path)
            await win_client.disconnect()

    @pytest.mark.asyncio
    async def test_cross_platform_ssot_mismatch(self):
        """Test SSOT enforcement across platforms.

        Windows creates semaphore with max_holders=5,
        Linux tries to acquire with max_holders=3 (should fail).
        """
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        zone_id = "test-zone"
        path = "/boardroom/ssot-cross"

        win_client = DragonflyClient(url=get_redis_url())
        await win_client.connect()
        win_lock_mgr = RedisLockManager(win_client)

        try:
            # Windows: create with max_holders=5
            win_lock = await win_lock_mgr.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=30.0,
                max_holders=5,
            )
            assert win_lock is not None

            # Linux: try with max_holders=3 (should get mismatch error)
            linux_script = f'''
import asyncio
import sys
sys.path.insert(0, "/app/src")
from nexus.core.cache.dragonfly import DragonflyClient
from nexus.core.distributed_lock import RedisLockManager

async def try_mismatch():
    client = DragonflyClient(url="redis://dragonfly-coordination:6379")
    await client.connect()
    lock_mgr = RedisLockManager(client)

    try:
        lock_id = await lock_mgr.acquire(
            zone_id="{zone_id}",
            path="{path}",
            timeout=1.0,
            ttl=30.0,
            max_holders=3,  # Different from Windows's 5
        )
        if lock_id:
            print("LINUX_ACQUIRED", flush=True)
            await lock_mgr.release(lock_id, "{zone_id}", "{path}")
        else:
            print("LINUX_TIMEOUT", flush=True)
    except ValueError as e:
        if "mismatch" in str(e):
            print("LINUX_MISMATCH_ERROR", flush=True)
        else:
            print(f"LINUX_OTHER_ERROR:{{e}}", flush=True)

    await client.disconnect()

asyncio.run(try_mismatch())
'''
            result = subprocess.run(
                ["docker", "exec", "nexus-linux-test", "python", "-c", linux_script],
                capture_output=True,
                text=True,
                timeout=30,
            )

            assert "LINUX_MISMATCH_ERROR" in result.stdout, (
                f"Should get mismatch error. stdout: {result.stdout}, stderr: {result.stderr}"
            )

        finally:
            await win_lock_mgr.release(win_lock, zone_id, path)
            await win_client.disconnect()

    @pytest.mark.asyncio
    async def test_cross_platform_semaphore_relay(self):
        """Test semaphore slot handoff between platforms.

        1. Windows acquires all 3 slots
        2. Linux waits for a slot
        3. Windows releases 1 slot
        4. Linux should immediately acquire it
        """
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        zone_id = "test-zone"
        path = "/boardroom/relay"
        max_holders = 3

        win_client = DragonflyClient(url=get_redis_url())
        await win_client.connect()
        win_lock_mgr = RedisLockManager(win_client)

        try:
            # Windows: acquire all slots
            win_locks = []
            for _ in range(max_holders):
                lock_id = await win_lock_mgr.acquire(
                    zone_id=zone_id,
                    path=path,
                    timeout=5.0,
                    ttl=30.0,
                    max_holders=max_holders,
                )
                assert lock_id is not None
                win_locks.append(lock_id)

            # Start Linux waiting in background
            linux_script = f'''
import asyncio
import sys
import time
sys.path.insert(0, "/app/src")
from nexus.core.cache.dragonfly import DragonflyClient
from nexus.core.distributed_lock import RedisLockManager

async def wait_for_slot():
    client = DragonflyClient(url="redis://dragonfly-coordination:6379")
    await client.connect()
    lock_mgr = RedisLockManager(client)

    start = time.time()
    lock_id = await lock_mgr.acquire(
        zone_id="{zone_id}",
        path="{path}",
        timeout=30.0,  # Wait up to 30s
        ttl=30.0,
        max_holders={max_holders},
    )
    wait_time = time.time() - start

    if lock_id:
        print(f"LINUX_ACQUIRED_AFTER:{{wait_time:.2f}}", flush=True)
        await lock_mgr.release(lock_id, "{zone_id}", "{path}")
    else:
        print("LINUX_TIMEOUT", flush=True)

    await client.disconnect()

asyncio.run(wait_for_slot())
'''
            linux_proc = subprocess.Popen(
                ["docker", "exec", "nexus-linux-test", "python", "-c", linux_script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            # Wait a bit, then release one slot
            await asyncio.sleep(2)
            await win_lock_mgr.release(win_locks[0], zone_id, path)
            win_locks.pop(0)

            # Wait for Linux to finish
            linux_proc.wait(timeout=35)
            stdout, stderr = linux_proc.communicate()

            assert "LINUX_ACQUIRED_AFTER:" in stdout, (
                f"Linux should acquire. stdout: {stdout}, stderr: {stderr}"
            )

            # Verify Linux waited approximately 2 seconds
            wait_time_str = stdout.split("LINUX_ACQUIRED_AFTER:")[1].split()[0]
            wait_time = float(wait_time_str)
            assert 1.5 <= wait_time <= 5.0, f"Wait time should be ~2s, got {wait_time}s"

        finally:
            for lock_id in win_locks:
                await win_lock_mgr.release(lock_id, zone_id, path)
            await win_client.disconnect()


# =============================================================================
# Edge Cases and Error Handling
# =============================================================================


@requires_redis
class TestSemaphoreEdgeCases:
    """Edge cases and error handling tests."""

    @pytest.mark.asyncio
    async def test_release_nonexistent_slot(self):
        """Test releasing a slot that doesn't exist."""
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        client = DragonflyClient(url=get_redis_url())
        await client.connect()
        lock_mgr = RedisLockManager(client)

        try:
            released = await lock_mgr.release(
                lock_id="nonexistent-lock-id",
                zone_id="test-zone",
                path="/nonexistent/path",
            )
            assert released is False, "Should return False for nonexistent lock"

        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_extend_nonexistent_slot(self):
        """Test extending a slot that doesn't exist."""
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        client = DragonflyClient(url=get_redis_url())
        await client.connect()
        lock_mgr = RedisLockManager(client)

        try:
            extended = await lock_mgr.extend(
                lock_id="nonexistent-lock-id",
                zone_id="test-zone",
                path="/nonexistent/path",
                ttl=30.0,
            )
            assert extended is False, "Should return False for nonexistent lock"

        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_semaphore_with_max_holders_one_behaves_like_mutex(self):
        """Test that max_holders=1 semaphore behaves like mutex."""
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        client = DragonflyClient(url=get_redis_url())
        await client.connect()
        lock_mgr = RedisLockManager(client)

        zone_id = "test-zone"
        path = "/test/semaphore-as-mutex"

        try:
            # Acquire with max_holders=1 (explicit)
            lock1 = await lock_mgr.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=30.0,
                max_holders=1,
            )
            assert lock1 is not None

            # Second should timeout (like mutex)
            lock2 = await lock_mgr.acquire(
                zone_id=zone_id,
                path=path,
                timeout=0.5,
                ttl=30.0,
                max_holders=1,
            )
            assert lock2 is None, "Should behave like mutex"

            await lock_mgr.release(lock1, zone_id, path)

        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_zero_timeout_immediate_fail(self):
        """Test that timeout=0 fails immediately if no slot available."""
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        client = DragonflyClient(url=get_redis_url())
        await client.connect()
        lock_mgr = RedisLockManager(client)

        zone_id = "test-zone"
        path = "/test/zero-timeout"
        max_holders = 1

        try:
            # Acquire the only slot
            lock1 = await lock_mgr.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=30.0,
                max_holders=max_holders,
            )
            assert lock1 is not None

            # Try with zero timeout (should fail immediately)
            start = time.time()
            lock2 = await lock_mgr.acquire(
                zone_id=zone_id,
                path=path,
                timeout=0.0,  # Zero timeout
                ttl=30.0,
                max_holders=max_holders,
            )
            elapsed = time.time() - start

            assert lock2 is None
            assert elapsed < 0.5, f"Should fail quickly with zero timeout, took {elapsed}s"

            await lock_mgr.release(lock1, zone_id, path)

        finally:
            await client.disconnect()


# =============================================================================
# Network Partition and Recovery Tests
# =============================================================================


@requires_redis
class TestNetworkPartitionRecovery:
    """Tests for behavior after network partition recovery.

    Simulates network partition by directly manipulating Redis state
    to mimic what happens when a client loses connection.
    """

    @pytest.mark.asyncio
    async def test_partition_recovery_release_returns_false(self):
        """Test that release() returns False after partition recovery.

        Scenario:
        1. Client A acquires slot
        2. Network partition (simulated by deleting A's slot from Redis)
        3. Client B acquires the slot
        4. Network recovers - A tries to release → should return False
        """
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        client_a = DragonflyClient(url=get_redis_url())
        client_b = DragonflyClient(url=get_redis_url())
        await client_a.connect()
        await client_b.connect()

        lock_mgr_a = RedisLockManager(client_a)
        lock_mgr_b = RedisLockManager(client_b)

        zone_id = "test-zone"
        path = "/test/partition-release"
        max_holders = 3

        try:
            # Step 1: A acquires slot
            lock_a = await lock_mgr_a.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=30.0,
                max_holders=max_holders,
            )
            assert lock_a is not None

            # Step 2: Simulate partition - delete A's slot from Redis
            sem_key = f"nexus:semaphore:{zone_id}:{path}"
            await client_a.client.zrem(sem_key, lock_a)

            # Step 3: B acquires the slot (now available)
            lock_b = await lock_mgr_b.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=30.0,
                max_holders=max_holders,
            )
            assert lock_b is not None

            # Step 4: A tries to release (after "partition recovery")
            released = await lock_mgr_a.release(lock_a, zone_id, path)
            assert released is False, "A should not be able to release (slot was lost)"

            # B should still hold the slot
            await lock_mgr_b.release(lock_b, zone_id, path)

        finally:
            await client_a.disconnect()
            await client_b.disconnect()

    @pytest.mark.asyncio
    async def test_partition_recovery_extend_returns_false(self):
        """Test that extend() returns False after partition causes slot loss.

        This is critical for detecting "I lost my slot" condition.
        """
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        client_a = DragonflyClient(url=get_redis_url())
        await client_a.connect()
        lock_mgr_a = RedisLockManager(client_a)

        zone_id = "test-zone"
        path = "/test/partition-extend"
        max_holders = 2

        try:
            # A acquires slot
            lock_a = await lock_mgr_a.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=30.0,
                max_holders=max_holders,
            )
            assert lock_a is not None

            # Simulate partition + TTL expiry by deleting A's slot
            sem_key = f"nexus:semaphore:{zone_id}:{path}"
            config_key = f"nexus:semaphore_config:{zone_id}:{path}"
            await client_a.client.zrem(sem_key, lock_a)

            # Also clean config if it's empty now
            count = await client_a.client.zcard(sem_key)
            if count == 0:
                await client_a.client.delete(config_key)

            # A tries to extend (should detect slot loss)
            extended = await lock_mgr_a.extend(lock_a, zone_id, path, ttl=30.0)
            assert extended is False, "A should detect slot was lost"

        finally:
            await client_a.disconnect()

    @pytest.mark.asyncio
    async def test_ttl_expiry_allows_new_holder(self):
        """Test actual TTL expiry (not simulated) allows new holder.

        Uses short TTL and waits for natural expiry.
        """
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        client_a = DragonflyClient(url=get_redis_url())
        client_b = DragonflyClient(url=get_redis_url())
        await client_a.connect()
        await client_b.connect()

        lock_mgr_a = RedisLockManager(client_a)
        lock_mgr_b = RedisLockManager(client_b)

        zone_id = "test-zone"
        path = "/test/natural-ttl-expiry"
        max_holders = 1
        short_ttl = 2.0

        try:
            # A acquires with short TTL and "crashes" (doesn't release or extend)
            lock_a = await lock_mgr_a.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=short_ttl,
                max_holders=max_holders,
            )
            assert lock_a is not None

            # B should not be able to acquire immediately
            lock_b_fail = await lock_mgr_b.acquire(
                zone_id=zone_id,
                path=path,
                timeout=0.5,
                ttl=30.0,
                max_holders=max_holders,
            )
            assert lock_b_fail is None, "B should be blocked while A holds slot"

            # Wait for A's slot to expire
            await asyncio.sleep(short_ttl + 0.5)

            # Now B should be able to acquire
            lock_b = await lock_mgr_b.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=30.0,
                max_holders=max_holders,
            )
            assert lock_b is not None, "B should acquire after A's TTL expires"

            # A tries to extend - should fail (TTL expired)
            extended = await lock_mgr_a.extend(lock_a, zone_id, path, ttl=30.0)
            assert extended is False, "A's extend should fail (TTL expired)"

            await lock_mgr_b.release(lock_b, zone_id, path)

        finally:
            await client_a.disconnect()
            await client_b.disconnect()


# =============================================================================
# Redis Restart Tests
# =============================================================================


@requires_redis
class TestRedisRestart:
    """Tests for behavior after Redis restart.

    Note: These tests simulate Redis restart by clearing keys,
    not actual Redis restart (which would require docker control).
    """

    @pytest.mark.asyncio
    async def test_redis_clear_loses_all_slots(self):
        """Test that clearing Redis (simulating restart) loses all slots."""
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        client = DragonflyClient(url=get_redis_url())
        await client.connect()
        lock_mgr = RedisLockManager(client)

        zone_id = "test-zone"
        path = "/test/redis-restart"
        max_holders = 5

        try:
            # Acquire multiple slots
            lock_ids = []
            for _ in range(3):
                lock_id = await lock_mgr.acquire(
                    zone_id=zone_id,
                    path=path,
                    timeout=5.0,
                    ttl=30.0,
                    max_holders=max_holders,
                )
                assert lock_id is not None
                lock_ids.append(lock_id)

            # Simulate Redis restart by deleting keys
            sem_key = f"nexus:semaphore:{zone_id}:{path}"
            config_key = f"nexus:semaphore_config:{zone_id}:{path}"
            await client.client.delete(sem_key, config_key)

            # All holders should fail to extend
            for lock_id in lock_ids:
                extended = await lock_mgr.extend(lock_id, zone_id, path, ttl=30.0)
                assert extended is False, "Should fail after Redis restart"

            # New client should be able to acquire fresh
            new_lock = await lock_mgr.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=30.0,
                max_holders=max_holders,
            )
            assert new_lock is not None, "Should acquire fresh slot after restart"

            await lock_mgr.release(new_lock, zone_id, path)

        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_fresh_config_after_full_clear(self):
        """Test that config can be set fresh after Redis clear."""
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        client = DragonflyClient(url=get_redis_url())
        await client.connect()
        lock_mgr = RedisLockManager(client)

        zone_id = "test-zone"
        path = "/test/fresh-config"

        try:
            # Create semaphore with max_holders=5
            lock1 = await lock_mgr.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=30.0,
                max_holders=5,
            )
            assert lock1 is not None

            # Simulate Redis restart
            sem_key = f"nexus:semaphore:{zone_id}:{path}"
            config_key = f"nexus:semaphore_config:{zone_id}:{path}"
            await client.client.delete(sem_key, config_key)

            # Now can create with different max_holders (config was cleared)
            lock2 = await lock_mgr.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=30.0,
                max_holders=3,  # Different value now allowed
            )
            assert lock2 is not None, "Should allow new max_holders after restart"

            await lock_mgr.release(lock2, zone_id, path)

        finally:
            await client.disconnect()


# =============================================================================
# Performance and Stress Tests
# =============================================================================


@requires_redis
class TestSemaphorePerformance:
    """Performance tests for high-volume scenarios."""

    @pytest.mark.asyncio
    async def test_100_holders_concurrent(self):
        """Test semaphore with 100 concurrent holders.

        Boardroom scenario: 100 seats, 200 participants trying to join.
        """
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        zone_id = "test-zone"
        path = "/boardroom/large-100"
        max_holders = 100
        num_participants = 200
        hold_time = 0.1

        results = {"acquired": 0, "released": 0, "errors": []}
        results_lock = threading.Lock()

        def participant(participant_id):
            async def _work():
                client = DragonflyClient(url=get_redis_url())
                await client.connect()
                lock_mgr = RedisLockManager(client)

                try:
                    lock_id = await lock_mgr.acquire(
                        zone_id=zone_id,
                        path=path,
                        timeout=60.0,
                        ttl=30.0,
                        max_holders=max_holders,
                    )
                    if lock_id:
                        with results_lock:
                            results["acquired"] += 1

                        await asyncio.sleep(hold_time)

                        await lock_mgr.release(lock_id, zone_id, path)

                        with results_lock:
                            results["released"] += 1

                finally:
                    await client.disconnect()

            try:
                asyncio.run(_work())
            except Exception as e:
                with results_lock:
                    results["errors"].append(f"P{participant_id}: {e}")

        # Start all participants
        threads = [threading.Thread(target=participant, args=(i,)) for i in range(num_participants)]

        start_time = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        elapsed = time.time() - start_time

        # Verify
        assert len(results["errors"]) == 0, f"Errors: {results['errors']}"
        assert results["acquired"] == num_participants, (
            f"All should acquire. Got: {results['acquired']}"
        )
        assert results["released"] == num_participants, (
            f"All should release. Got: {results['released']}"
        )

        print(f"\nPerformance: {num_participants} participants, {max_holders} seats")
        print(f"Total time: {elapsed:.2f}s")
        print(f"Throughput: {num_participants / elapsed:.1f} acquisitions/sec")

    @pytest.mark.asyncio
    async def test_rapid_acquire_release_cycles(self):
        """Test rapid acquire/release cycles for single slot."""
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        client = DragonflyClient(url=get_redis_url())
        await client.connect()
        lock_mgr = RedisLockManager(client)

        zone_id = "test-zone"
        path = "/test/rapid-cycles"
        num_cycles = 100

        try:
            start_time = time.time()

            for i in range(num_cycles):
                lock_id = await lock_mgr.acquire(
                    zone_id=zone_id,
                    path=path,
                    timeout=5.0,
                    ttl=30.0,
                    max_holders=1,
                )
                assert lock_id is not None, f"Failed at cycle {i}"
                await lock_mgr.release(lock_id, zone_id, path)

            elapsed = time.time() - start_time
            print(f"\nRapid cycles: {num_cycles} acquire/release in {elapsed:.2f}s")
            print(f"Rate: {num_cycles / elapsed:.1f} cycles/sec")

        finally:
            await client.disconnect()


# =============================================================================
# Heartbeat and Extend Failure Tests
# =============================================================================


@requires_redis
class TestHeartbeatAndExtendFailure:
    """Tests for heartbeat (extend) patterns and failure detection."""

    @pytest.mark.asyncio
    async def test_heartbeat_keeps_slot_alive(self):
        """Test that regular heartbeats keep slot alive past original TTL."""
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        client = DragonflyClient(url=get_redis_url())
        await client.connect()
        lock_mgr = RedisLockManager(client)

        zone_id = "test-zone"
        path = "/test/heartbeat-alive"
        max_holders = 2
        short_ttl = 2.0
        total_hold_time = 6.0  # Hold for 6s with 2s TTL

        try:
            # Acquire with short TTL
            lock_id = await lock_mgr.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=short_ttl,
                max_holders=max_holders,
            )
            assert lock_id is not None

            # Heartbeat every 1 second
            heartbeat_count = 0
            start = time.time()

            while time.time() - start < total_hold_time:
                await asyncio.sleep(1.0)
                extended = await lock_mgr.extend(lock_id, zone_id, path, ttl=short_ttl)
                assert extended is True, f"Heartbeat failed at count {heartbeat_count}"
                heartbeat_count += 1

            # Should have done multiple heartbeats
            assert heartbeat_count >= 4, f"Should heartbeat multiple times: {heartbeat_count}"

            # Clean release
            released = await lock_mgr.release(lock_id, zone_id, path)
            assert released is True

        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_detect_slot_loss_via_extend_failure(self):
        """Test pattern: detect slot loss by checking extend() result.

        This is the recommended pattern for long-running operations:
        1. Acquire slot
        2. Periodically extend
        3. If extend returns False → STOP WORK, slot was lost
        """
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        client_a = DragonflyClient(url=get_redis_url())
        client_admin = DragonflyClient(url=get_redis_url())
        await client_a.connect()
        await client_admin.connect()

        lock_mgr_a = RedisLockManager(client_a)

        zone_id = "test-zone"
        path = "/test/detect-loss"
        max_holders = 2  # Use >1 to ensure semaphore path (ZSET)

        work_should_stop = {"flag": False}

        try:
            # A acquires slot
            lock_a = await lock_mgr_a.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=5.0,
                max_holders=max_holders,
            )
            assert lock_a is not None

            # Simulate work with periodic heartbeat check
            async def do_work_with_heartbeat():
                for i in range(10):
                    # Check heartbeat
                    extended = await lock_mgr_a.extend(lock_a, zone_id, path, ttl=5.0)
                    if not extended:
                        work_should_stop["flag"] = True
                        return f"Stopped at iteration {i} (slot lost)"

                    await asyncio.sleep(0.3)  # Shorter interval for faster detection

                return "Completed all iterations"

            # Start work in background
            work_task = asyncio.create_task(do_work_with_heartbeat())

            # Wait a bit then simulate slot loss (admin intervention or TTL expiry)
            await asyncio.sleep(1.0)
            sem_key = f"nexus:semaphore:{zone_id}:{path}"
            await client_admin.client.zrem(sem_key, lock_a)

            # Wait for work to detect and stop
            result = await work_task

            assert work_should_stop["flag"] is True, "Work should have detected slot loss"
            assert "Stopped" in result, f"Work should stop early: {result}"

        finally:
            await client_a.disconnect()
            await client_admin.disconnect()

    @pytest.mark.asyncio
    async def test_extend_failure_does_not_affect_other_holders(self):
        """Test that one holder's extend failure doesn't affect others."""
        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        client_a = DragonflyClient(url=get_redis_url())
        client_b = DragonflyClient(url=get_redis_url())
        client_admin = DragonflyClient(url=get_redis_url())
        await client_a.connect()
        await client_b.connect()
        await client_admin.connect()

        lock_mgr_a = RedisLockManager(client_a)
        lock_mgr_b = RedisLockManager(client_b)

        zone_id = "test-zone"
        path = "/test/isolated-failure"
        max_holders = 3

        try:
            # A and B both acquire slots
            lock_a = await lock_mgr_a.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=30.0,
                max_holders=max_holders,
            )
            lock_b = await lock_mgr_b.acquire(
                zone_id=zone_id,
                path=path,
                timeout=5.0,
                ttl=30.0,
                max_holders=max_holders,
            )
            assert lock_a is not None and lock_b is not None

            # Simulate A's slot being lost (admin removes it)
            sem_key = f"nexus:semaphore:{zone_id}:{path}"
            await client_admin.client.zrem(sem_key, lock_a)

            # A's extend should fail
            extended_a = await lock_mgr_a.extend(lock_a, zone_id, path, ttl=30.0)
            assert extended_a is False, "A's extend should fail"

            # B's extend should still succeed
            extended_b = await lock_mgr_b.extend(lock_b, zone_id, path, ttl=30.0)
            assert extended_b is True, "B's extend should still work"

            # B can still release normally
            released_b = await lock_mgr_b.release(lock_b, zone_id, path)
            assert released_b is True

        finally:
            await client_a.disconnect()
            await client_b.disconnect()
            await client_admin.disconnect()
