"""Cross-platform distributed lock tests.

Tests distributed lock coordination between Windows (host) and Linux (Docker container).
Both platforms connect to the same Dragonfly instance to verify true distributed locking.

Test Matrix:
- Win → Linux: Windows holds lock, Linux waits
- Linux → Win: Linux holds lock, Windows waits
- Concurrent: Both try to acquire at the same time

Prerequisites:
- Docker containers running: docker compose --profile test up -d
- NEXUS_DRAGONFLY_COORDINATION_URL=redis://localhost:6380

Usage:
    # Run from Windows host
    pytest tests/integration/test_distributed_lock_cross_platform.py -v --tb=short
"""

import asyncio
import os
import subprocess
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


pytestmark = [
    pytest.mark.skipif(
        not is_redis_available(),
        reason="Redis not available (set NEXUS_DRAGONFLY_COORDINATION_URL)",
    ),
    pytest.mark.skipif(
        not is_linux_container_available(),
        reason="Linux container not running (docker compose --profile test up -d)",
    ),
]


class TestCrossPlatformLocking:
    """Tests for cross-platform distributed lock coordination."""

    @pytest.mark.asyncio
    async def test_win_holds_linux_waits(self):
        """Test that when Windows holds a lock, Linux cannot acquire it.

        Scenario:
        1. Windows acquires lock on /shared-resource
        2. Linux tries to acquire the same lock (should timeout)
        3. Windows releases lock
        4. Linux can now acquire the lock
        """
        from nexus.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        redis_url = os.environ.get(
            "NEXUS_DRAGONFLY_COORDINATION_URL",
            os.environ.get("NEXUS_REDIS_URL", "redis://localhost:6380"),
        )

        # Windows: acquire lock
        win_client = DragonflyClient(url=redis_url)
        await win_client.connect()
        win_lock_mgr = RedisLockManager(win_client)

        zone_id = "test-zone"
        resource_path = "/cross-platform-test-1"

        try:
            # Step 1: Windows acquires lock
            win_lock_id = await win_lock_mgr.acquire(
                zone_id=zone_id,
                path=resource_path,
                timeout=5.0,
                ttl=30.0,
            )
            assert win_lock_id is not None, "Windows should acquire lock"

            # Step 2: Linux tries to acquire (should timeout)
            # Run in Docker container - connection URL uses internal network
            linux_script = f'''
import asyncio
import sys
sys.path.insert(0, "/app/src")
from nexus.cache.dragonfly import DragonflyClient
from nexus.core.distributed_lock import RedisLockManager

async def try_acquire():
    # Inside Docker, use the internal network address
    client = DragonflyClient(url="redis://dragonfly-coordination:6379")
    await client.connect()
    lock_mgr = RedisLockManager(client)

    # Try to acquire with short timeout (should fail)
    lock_id = await lock_mgr.acquire(
        zone_id="{zone_id}",
        path="{resource_path}",
        timeout=2.0,  # Short timeout - should fail
        ttl=30.0,
    )
    await client.disconnect()

    if lock_id is None:
        print("LOCK_BLOCKED")  # Expected - Windows holds the lock
    else:
        print("LOCK_ACQUIRED")  # Unexpected - should have been blocked

asyncio.run(try_acquire())
'''
            result = subprocess.run(
                ["docker", "exec", "nexus-linux-test", "python", "-c", linux_script],
                capture_output=True,
                text=True,
                timeout=30,
            )

            assert "LOCK_BLOCKED" in result.stdout, (
                f"Linux should be blocked by Windows lock. "
                f"stdout: {result.stdout}, stderr: {result.stderr}"
            )

            # Step 3: Windows releases lock
            await win_lock_mgr.release(win_lock_id, zone_id, resource_path)

            # Step 4: Linux can now acquire
            linux_script_acquire = f'''
import asyncio
import sys
sys.path.insert(0, "/app/src")
from nexus.cache.dragonfly import DragonflyClient
from nexus.core.distributed_lock import RedisLockManager

async def acquire_and_release():
    client = DragonflyClient(url="redis://dragonfly-coordination:6379")
    await client.connect()
    lock_mgr = RedisLockManager(client)

    lock_id = await lock_mgr.acquire(
        zone_id="{zone_id}",
        path="{resource_path}",
        timeout=5.0,
        ttl=30.0,
    )

    if lock_id is not None:
        print("LOCK_ACQUIRED")
        await lock_mgr.release(lock_id, "{zone_id}", "{resource_path}")
    else:
        print("LOCK_FAILED")

asyncio.run(acquire_and_release())
'''
            result = subprocess.run(
                ["docker", "exec", "nexus-linux-test", "python", "-c", linux_script_acquire],
                capture_output=True,
                text=True,
                timeout=30,
            )

            assert "LOCK_ACQUIRED" in result.stdout, (
                f"Linux should acquire lock after Windows release. "
                f"stdout: {result.stdout}, stderr: {result.stderr}"
            )

        finally:
            await win_client.disconnect()

    @pytest.mark.asyncio
    async def test_linux_holds_win_waits(self):
        """Test that when Linux holds a lock, Windows cannot acquire it.

        Scenario:
        1. Linux acquires lock on /shared-resource
        2. Windows tries to acquire the same lock (should timeout)
        3. Linux releases lock
        4. Windows can now acquire the lock
        """
        from nexus.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        redis_url = os.environ.get(
            "NEXUS_DRAGONFLY_COORDINATION_URL",
            os.environ.get("NEXUS_REDIS_URL", "redis://localhost:6380"),
        )

        zone_id = "test-zone"
        resource_path = "/cross-platform-test-2"

        # Step 1: Linux acquires lock (start in background)
        linux_script_hold = f'''
import asyncio
import sys
import time
sys.path.insert(0, "/app/src")
from nexus.cache.dragonfly import DragonflyClient
from nexus.core.distributed_lock import RedisLockManager

async def hold_lock():
    client = DragonflyClient(url="redis://dragonfly-coordination:6379")
    await client.connect()
    lock_mgr = RedisLockManager(client)

    lock_id = await lock_mgr.acquire(
        zone_id="{zone_id}",
        path="{resource_path}",
        timeout=5.0,
        ttl=30.0,
    )

    if lock_id is not None:
        print("LINUX_ACQUIRED", flush=True)
        # Hold lock for 5 seconds
        await asyncio.sleep(5)
        await lock_mgr.release(lock_id, "{zone_id}", "{resource_path}")
        print("LINUX_RELEASED", flush=True)
    else:
        print("LINUX_FAILED", flush=True)

    await client.disconnect()

asyncio.run(hold_lock())
'''
        # Start Linux lock holder in background
        linux_proc = subprocess.Popen(
            ["docker", "exec", "nexus-linux-test", "python", "-c", linux_script_hold],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            # Wait for Linux to acquire lock
            time.sleep(1)

            # Verify Linux acquired the lock
            # (we can't read stdout easily while process is running, so we check by trying to acquire)

            # Step 2: Windows tries to acquire (should timeout)
            win_client = DragonflyClient(url=redis_url)
            await win_client.connect()
            win_lock_mgr = RedisLockManager(win_client)

            try:
                win_lock_id = await win_lock_mgr.acquire(
                    zone_id=zone_id,
                    path=resource_path,
                    timeout=2.0,  # Short timeout
                    ttl=30.0,
                )

                assert win_lock_id is None, "Windows should be blocked by Linux lock"

                # Step 3-4: Wait for Linux to release and then Windows acquires
                # Wait for Linux process to finish (it will release after 5 seconds)
                linux_proc.wait(timeout=10)
                stdout, stderr = linux_proc.communicate()
                assert "LINUX_ACQUIRED" in stdout, f"Linux should have acquired: {stdout}, {stderr}"
                assert "LINUX_RELEASED" in stdout, f"Linux should have released: {stdout}, {stderr}"

                # Now Windows should be able to acquire
                win_lock_id = await win_lock_mgr.acquire(
                    zone_id=zone_id,
                    path=resource_path,
                    timeout=5.0,
                    ttl=30.0,
                )
                assert win_lock_id is not None, "Windows should acquire lock after Linux release"

                await win_lock_mgr.release(win_lock_id, zone_id, resource_path)

            finally:
                await win_client.disconnect()

        finally:
            # Ensure Linux process is terminated
            if linux_proc.poll() is None:
                linux_proc.terminate()
                linux_proc.wait(timeout=5)

    @pytest.mark.asyncio
    async def test_concurrent_lock_acquisition(self):
        """Test concurrent lock acquisition from Windows and Linux.

        Both platforms try to acquire the same lock at the same time.
        Only one should succeed, the other should wait and then acquire.
        """
        from nexus.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        redis_url = os.environ.get(
            "NEXUS_DRAGONFLY_COORDINATION_URL",
            os.environ.get("NEXUS_REDIS_URL", "redis://localhost:6380"),
        )

        zone_id = "test-zone"
        resource_path = "/cross-platform-test-3"
        results = {"win": None, "linux": None}

        # Linux script that tries to acquire, hold briefly, then release
        linux_script = f'''
import asyncio
import sys
import time
sys.path.insert(0, "/app/src")
from nexus.cache.dragonfly import DragonflyClient
from nexus.core.distributed_lock import RedisLockManager

async def compete():
    client = DragonflyClient(url="redis://dragonfly-coordination:6379")
    await client.connect()
    lock_mgr = RedisLockManager(client)

    start = time.time()
    lock_id = await lock_mgr.acquire(
        zone_id="{zone_id}",
        path="{resource_path}",
        timeout=10.0,  # Longer timeout to wait for other side
        ttl=30.0,
    )
    acquire_time = time.time() - start

    if lock_id is not None:
        print(f"LINUX_ACQUIRED_AT:{{acquire_time:.2f}}", flush=True)
        await asyncio.sleep(1)  # Hold briefly
        await lock_mgr.release(lock_id, "{zone_id}", "{resource_path}")
        print("LINUX_RELEASED", flush=True)
    else:
        print("LINUX_FAILED", flush=True)

    await client.disconnect()

asyncio.run(compete())
'''

        async def win_acquire():
            client = DragonflyClient(url=redis_url)
            await client.connect()
            lock_mgr = RedisLockManager(client)

            start = time.time()
            lock_id = await lock_mgr.acquire(
                zone_id=zone_id,
                path=resource_path,
                timeout=10.0,
                ttl=30.0,
            )
            acquire_time = time.time() - start

            if lock_id is not None:
                results["win"] = ("acquired", acquire_time)
                await asyncio.sleep(1)  # Hold briefly
                await lock_mgr.release(lock_id, zone_id, resource_path)
            else:
                results["win"] = ("failed", acquire_time)

            await client.disconnect()

        # Start both at approximately the same time
        linux_proc = subprocess.Popen(
            ["docker", "exec", "nexus-linux-test", "python", "-c", linux_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            # Run Windows acquisition concurrently
            await win_acquire()

            # Wait for Linux to finish
            linux_proc.wait(timeout=15)
            stdout, stderr = linux_proc.communicate()

            # Parse Linux result
            if "LINUX_ACQUIRED_AT:" in stdout:
                linux_time = float(stdout.split("LINUX_ACQUIRED_AT:")[1].split()[0])
                results["linux"] = ("acquired", linux_time)
            else:
                results["linux"] = ("failed", None)

            # Both should have acquired (sequentially, not simultaneously)
            assert results["win"][0] == "acquired", f"Windows should acquire: {results}"
            assert results["linux"][0] == "acquired", f"Linux should acquire: {stdout}, {stderr}"

            # One should have acquired quickly (first), one should have waited
            win_time = results["win"][1]
            linux_time = results["linux"][1]

            # At least one should have waited (acquire time > 0.5s means it waited)
            first_acquired_quickly = win_time < 0.5 or linux_time < 0.5
            assert first_acquired_quickly, (
                f"At least one should acquire quickly. "
                f"Win: {win_time:.2f}s, Linux: {linux_time:.2f}s"
            )

        finally:
            if linux_proc.poll() is None:
                linux_proc.terminate()
                linux_proc.wait(timeout=5)

    @pytest.mark.asyncio
    async def test_cross_platform_atomic_counter(self):
        """Test atomic counter increment from both Windows and Linux.

        Uses a shared file and distributed lock to ensure atomic increments.
        Each platform increments the counter 5 times.
        Final count should be exactly 10 (no lost updates).
        """
        from nexus.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        redis_url = os.environ.get(
            "NEXUS_DRAGONFLY_COORDINATION_URL",
            os.environ.get("NEXUS_REDIS_URL", "redis://localhost:6380"),
        )

        zone_id = "test-zone"
        resource_path = "/cross-platform-counter"
        num_increments = 5

        # Create shared counter file in a temp directory that can be accessed
        # We'll use Redis to store the counter value instead of a file
        # (simpler than sharing a file between host and container)
        counter_key = f"test:counter:{resource_path}"

        # Initialize counter in Redis
        win_client = DragonflyClient(url=redis_url)
        await win_client.connect()
        await win_client._client.set(counter_key, "0")

        win_lock_mgr = RedisLockManager(win_client)

        # Linux script that increments counter
        linux_script = f'''
import asyncio
import sys
sys.path.insert(0, "/app/src")
from nexus.cache.dragonfly import DragonflyClient
from nexus.core.distributed_lock import RedisLockManager

async def increment():
    client = DragonflyClient(url="redis://dragonfly-coordination:6379")
    await client.connect()
    lock_mgr = RedisLockManager(client)

    for i in range({num_increments}):
        lock_id = await lock_mgr.acquire(
            zone_id="{zone_id}",
            path="{resource_path}",
            timeout=30.0,
            ttl=30.0,
        )
        if lock_id is not None:
            try:
                # Read counter from Redis
                val = await client._client.get("{counter_key}")
                count = int(val) if val else 0
                count += 1
                await client._client.set("{counter_key}", str(count))
                print(f"LINUX_INCREMENT:{{i}}->{{count}}", flush=True)
            finally:
                await lock_mgr.release(lock_id, "{zone_id}", "{resource_path}")
        await asyncio.sleep(0.05)  # Small delay between increments

    await client.disconnect()
    print("LINUX_DONE", flush=True)

asyncio.run(increment())
'''

        async def win_increment():
            for _ in range(num_increments):
                lock_id = await win_lock_mgr.acquire(
                    zone_id=zone_id,
                    path=resource_path,
                    timeout=30.0,
                    ttl=30.0,
                )
                if lock_id is not None:
                    try:
                        # Read counter from Redis
                        val = await win_client._client.get(counter_key)
                        count = int(val) if val else 0
                        count += 1
                        await win_client._client.set(counter_key, str(count))
                    finally:
                        await win_lock_mgr.release(lock_id, zone_id, resource_path)
                await asyncio.sleep(0.05)

        # Start Linux incrementer
        linux_proc = subprocess.Popen(
            ["docker", "exec", "nexus-linux-test", "python", "-c", linux_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            # Run Windows incrementer concurrently
            await win_increment()

            # Wait for Linux to finish
            linux_proc.wait(timeout=60)
            stdout, stderr = linux_proc.communicate()

            assert "LINUX_DONE" in stdout, f"Linux should complete: {stdout}, {stderr}"

            # Verify final counter value
            final_val = await win_client._client.get(counter_key)
            final_count = int(final_val) if final_val else 0

            expected_count = num_increments * 2  # Both platforms increment
            assert final_count == expected_count, (
                f"Counter should be {expected_count}, got {final_count}. "
                f"Some increments were lost due to race condition."
            )

        finally:
            # Cleanup
            await win_client._client.delete(counter_key)
            await win_client.disconnect()

            if linux_proc.poll() is None:
                linux_proc.terminate()
                linux_proc.wait(timeout=5)

    @pytest.mark.asyncio
    async def test_cross_platform_lock_recovery_after_crash(self):
        """Test lock recovery when one platform "crashes" without releasing.

        Scenario:
        1. Windows acquires lock with SHORT TTL (2 seconds)
        2. Windows "crashes" (doesn't release the lock)
        3. Linux waits and should acquire after TTL expires
        4. Verify Linux successfully acquired the lock

        This tests the distributed system's resilience to node failures.
        """
        from nexus.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        redis_url = os.environ.get(
            "NEXUS_DRAGONFLY_COORDINATION_URL",
            os.environ.get("NEXUS_REDIS_URL", "redis://localhost:6380"),
        )

        zone_id = "test-zone"
        resource_path = "/crash-recovery-test"
        SHORT_TTL = 2.0  # Short TTL for faster test

        # Windows: acquire lock and "crash" (don't release)
        win_client = DragonflyClient(url=redis_url)
        await win_client.connect()
        win_lock_mgr = RedisLockManager(win_client)

        try:
            win_lock_id = await win_lock_mgr.acquire(
                zone_id=zone_id,
                path=resource_path,
                timeout=5.0,
                ttl=SHORT_TTL,  # Short TTL
            )
            assert win_lock_id is not None, "Windows should acquire lock"

            # Windows "crashes" - we intentionally DON'T release the lock
            # Just disconnect without releasing
            await win_client.disconnect()

            # Linux: try to acquire - should succeed after TTL expires
            linux_script = f'''
import asyncio
import sys
import time
sys.path.insert(0, "/app/src")
from nexus.cache.dragonfly import DragonflyClient
from nexus.core.distributed_lock import RedisLockManager

async def acquire_after_crash():
    client = DragonflyClient(url="redis://dragonfly-coordination:6379")
    await client.connect()
    lock_mgr = RedisLockManager(client)

    start = time.time()
    # Wait up to 10 seconds (TTL is {SHORT_TTL}s, so should acquire after ~{SHORT_TTL}s)
    lock_id = await lock_mgr.acquire(
        zone_id="{zone_id}",
        path="{resource_path}",
        timeout=10.0,
        ttl=30.0,
    )
    acquire_time = time.time() - start

    if lock_id is not None:
        print(f"LINUX_ACQUIRED_AFTER:{{acquire_time:.2f}}", flush=True)
        await lock_mgr.release(lock_id, "{zone_id}", "{resource_path}")
        print("LINUX_RELEASED", flush=True)
    else:
        print("LINUX_FAILED", flush=True)

    await client.disconnect()

asyncio.run(acquire_after_crash())
'''
            result = subprocess.run(
                ["docker", "exec", "nexus-linux-test", "python", "-c", linux_script],
                capture_output=True,
                text=True,
                timeout=30,
            )

            assert "LINUX_ACQUIRED_AFTER:" in result.stdout, (
                f"Linux should acquire lock after TTL. stdout: {result.stdout}, stderr: {result.stderr}"
            )

            # Verify Linux acquired after approximately TTL seconds
            acquire_time_str = result.stdout.split("LINUX_ACQUIRED_AFTER:")[1].split()[0]
            acquire_time = float(acquire_time_str)

            # Should have waited at least TTL-0.5 seconds (some tolerance)
            assert acquire_time >= SHORT_TTL - 0.5, (
                f"Linux should have waited for TTL ({SHORT_TTL}s), but acquired in {acquire_time:.2f}s"
            )

        finally:
            # Cleanup: ensure lock is released (reconnect if needed)
            try:
                cleanup_client = DragonflyClient(url=redis_url)
                await cleanup_client.connect()
                lock_key = f"nexus:lock:{zone_id}:{resource_path}"
                await cleanup_client._client.delete(lock_key)
                await cleanup_client.disconnect()
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_lock_heartbeat_extend(self):
        """Test lock extension (heartbeat) for long-running operations.

        Scenario:
        1. Windows acquires lock with SHORT TTL (2 seconds)
        2. Windows extends the lock every 1 second (heartbeat)
        3. Linux tries to acquire - should be blocked for entire duration
        4. Windows releases after 5 seconds
        5. Linux should then acquire

        This tests that extend() properly keeps locks alive.
        """
        from nexus.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        redis_url = os.environ.get(
            "NEXUS_DRAGONFLY_COORDINATION_URL",
            os.environ.get("NEXUS_REDIS_URL", "redis://localhost:6380"),
        )

        zone_id = "test-zone"
        resource_path = "/heartbeat-test"
        SHORT_TTL = 2.0
        HOLD_TIME = 5.0  # Hold for 5 seconds (longer than TTL)

        win_client = DragonflyClient(url=redis_url)
        await win_client.connect()
        win_lock_mgr = RedisLockManager(win_client)

        try:
            # Windows: acquire lock with short TTL
            win_lock_id = await win_lock_mgr.acquire(
                zone_id=zone_id,
                path=resource_path,
                timeout=5.0,
                ttl=SHORT_TTL,
            )
            assert win_lock_id is not None

            # Start heartbeat task
            async def heartbeat():
                """Extend lock every second."""
                for _ in range(int(HOLD_TIME)):
                    await asyncio.sleep(1.0)
                    extended = await win_lock_mgr.extend(
                        lock_id=win_lock_id,
                        zone_id=zone_id,
                        path=resource_path,
                        ttl=SHORT_TTL,
                    )
                    if not extended:
                        print("Heartbeat failed!")
                        break

            # Linux: try to acquire (should be blocked)
            linux_script = f'''
import asyncio
import sys
import time
sys.path.insert(0, "/app/src")
from nexus.cache.dragonfly import DragonflyClient
from nexus.core.distributed_lock import RedisLockManager

async def try_acquire():
    client = DragonflyClient(url="redis://dragonfly-coordination:6379")
    await client.connect()
    lock_mgr = RedisLockManager(client)

    start = time.time()
    lock_id = await lock_mgr.acquire(
        zone_id="{zone_id}",
        path="{resource_path}",
        timeout=10.0,  # Wait up to 10s
        ttl=30.0,
    )
    acquire_time = time.time() - start

    if lock_id is not None:
        print(f"LINUX_ACQUIRED_AFTER:{{acquire_time:.2f}}", flush=True)
        await lock_mgr.release(lock_id, "{zone_id}", "{resource_path}")
    else:
        print("LINUX_TIMEOUT", flush=True)

    await client.disconnect()

asyncio.run(try_acquire())
'''

            # Run Linux acquisition in background
            linux_proc = subprocess.Popen(
                ["docker", "exec", "nexus-linux-test", "python", "-c", linux_script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            # Run heartbeat for HOLD_TIME seconds
            await heartbeat()

            # Release lock
            await win_lock_mgr.release(win_lock_id, zone_id, resource_path)

            # Wait for Linux
            linux_proc.wait(timeout=15)
            stdout, stderr = linux_proc.communicate()

            assert "LINUX_ACQUIRED_AFTER:" in stdout, (
                f"Linux should acquire after Windows releases. stdout: {stdout}, stderr: {stderr}"
            )

            # Linux should have waited approximately HOLD_TIME
            acquire_time_str = stdout.split("LINUX_ACQUIRED_AFTER:")[1].split()[0]
            acquire_time = float(acquire_time_str)

            # Should have waited at least HOLD_TIME - 1s (tolerance for timing)
            assert acquire_time >= HOLD_TIME - 1.0, (
                f"Linux should have waited ~{HOLD_TIME}s due to heartbeat, but waited {acquire_time:.2f}s"
            )

        finally:
            await win_client.disconnect()
            if linux_proc.poll() is None:
                linux_proc.terminate()


class TestHighContention:
    """Tests for high contention scenarios with many concurrent acquirers."""

    @pytest.mark.asyncio
    async def test_many_threads_one_lock(self):
        """Test N threads competing for the same lock.

        All threads try to increment a counter atomically.
        With proper locking, final count should equal total increments.
        """
        import threading

        from nexus.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        redis_url = os.environ.get(
            "NEXUS_DRAGONFLY_COORDINATION_URL",
            os.environ.get("NEXUS_REDIS_URL", "redis://localhost:6380"),
        )

        zone_id = "test-zone"
        resource_path = "/high-contention-test"
        counter_key = "test:high-contention-counter"
        NUM_THREADS = 5
        INCREMENTS_PER_THREAD = 10

        # Initialize counter
        init_client = DragonflyClient(url=redis_url)
        await init_client.connect()
        await init_client._client.set(counter_key, "0")
        await init_client.disconnect()

        errors = []
        successful_increments = []

        def worker(thread_id):
            """Worker that increments counter with lock."""

            async def _work():
                client = DragonflyClient(url=redis_url)
                await client.connect()
                lock_mgr = RedisLockManager(client)

                try:
                    for i in range(INCREMENTS_PER_THREAD):
                        lock_id = await lock_mgr.acquire(
                            zone_id=zone_id,
                            path=resource_path,
                            timeout=60.0,  # Long timeout for high contention
                            ttl=30.0,
                        )
                        if lock_id:
                            try:
                                val = await client._client.get(counter_key)
                                count = int(val) if val else 0
                                count += 1
                                await client._client.set(counter_key, str(count))
                                successful_increments.append((thread_id, i))
                            finally:
                                await lock_mgr.release(lock_id, zone_id, resource_path)
                        else:
                            errors.append(f"T{thread_id}: Lock acquisition failed at {i}")
                finally:
                    await client.disconnect()

            try:
                asyncio.run(_work())
            except Exception as e:
                errors.append(f"T{thread_id}: {e}")

        # Start all threads
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(NUM_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)  # 2 minute timeout

        # Verify
        assert len(errors) == 0, f"Errors: {errors}"

        # Check final counter
        check_client = DragonflyClient(url=redis_url)
        await check_client.connect()
        final_val = await check_client._client.get(counter_key)
        final_count = int(final_val) if final_val else 0
        await check_client._client.delete(counter_key)
        await check_client.disconnect()

        expected = NUM_THREADS * INCREMENTS_PER_THREAD
        assert final_count == expected, (
            f"Lost updates! Expected {expected}, got {final_count}. "
            f"Successful increments: {len(successful_increments)}"
        )

    @pytest.mark.asyncio
    async def test_cross_platform_high_contention(self):
        """Test high contention across Windows and Linux.

        Multiple Windows threads + multiple Linux processes competing.
        """
        import threading

        from nexus.cache.dragonfly import DragonflyClient
        from nexus.core.distributed_lock import RedisLockManager

        redis_url = os.environ.get(
            "NEXUS_DRAGONFLY_COORDINATION_URL",
            os.environ.get("NEXUS_REDIS_URL", "redis://localhost:6380"),
        )

        zone_id = "test-zone"
        resource_path = "/cross-platform-high-contention"
        counter_key = "test:cross-platform-contention-counter"
        WIN_THREADS = 3
        LINUX_WORKERS = 2
        INCREMENTS_EACH = 5

        # Initialize counter
        init_client = DragonflyClient(url=redis_url)
        await init_client.connect()
        await init_client._client.set(counter_key, "0")
        await init_client.disconnect()

        errors = []

        def win_worker(thread_id):
            """Windows worker."""

            async def _work():
                client = DragonflyClient(url=redis_url)
                await client.connect()
                lock_mgr = RedisLockManager(client)
                try:
                    for _ in range(INCREMENTS_EACH):
                        lock_id = await lock_mgr.acquire(
                            zone_id=zone_id,
                            path=resource_path,
                            timeout=60.0,
                            ttl=30.0,
                        )
                        if lock_id:
                            try:
                                val = await client._client.get(counter_key)
                                count = int(val) if val else 0
                                count += 1
                                await client._client.set(counter_key, str(count))
                            finally:
                                await lock_mgr.release(lock_id, zone_id, resource_path)
                finally:
                    await client.disconnect()

            try:
                asyncio.run(_work())
            except Exception as e:
                errors.append(f"Win-T{thread_id}: {e}")

        # Linux script for workers
        linux_script = f'''
import asyncio
import sys
sys.path.insert(0, "/app/src")
from nexus.cache.dragonfly import DragonflyClient
from nexus.core.distributed_lock import RedisLockManager

async def work():
    client = DragonflyClient(url="redis://dragonfly-coordination:6379")
    await client.connect()
    lock_mgr = RedisLockManager(client)

    try:
        for _ in range({INCREMENTS_EACH}):
            lock_id = await lock_mgr.acquire(
                zone_id="{zone_id}",
                path="{resource_path}",
                timeout=60.0,
                ttl=30.0,
            )
            if lock_id:
                try:
                    val = await client._client.get("{counter_key}")
                    count = int(val) if val else 0
                    count += 1
                    await client._client.set("{counter_key}", str(count))
                finally:
                    await lock_mgr.release(lock_id, "{zone_id}", "{resource_path}")
    finally:
        await client.disconnect()

    print("LINUX_DONE", flush=True)

asyncio.run(work())
'''

        # Start Windows threads
        win_threads = [threading.Thread(target=win_worker, args=(i,)) for i in range(WIN_THREADS)]
        for t in win_threads:
            t.start()

        # Start Linux workers
        linux_procs = []
        for _ in range(LINUX_WORKERS):
            proc = subprocess.Popen(
                ["docker", "exec", "nexus-linux-test", "python", "-c", linux_script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            linux_procs.append(proc)

        # Wait for all
        for t in win_threads:
            t.join(timeout=120)

        for proc in linux_procs:
            proc.wait(timeout=120)
            stdout, _ = proc.communicate()
            if "LINUX_DONE" not in stdout:
                errors.append(f"Linux worker failed: {stdout}")

        # Verify
        assert len(errors) == 0, f"Errors: {errors}"

        check_client = DragonflyClient(url=redis_url)
        await check_client.connect()
        final_val = await check_client._client.get(counter_key)
        final_count = int(final_val) if final_val else 0
        await check_client._client.delete(counter_key)
        await check_client.disconnect()

        expected = (WIN_THREADS + LINUX_WORKERS) * INCREMENTS_EACH
        assert final_count == expected, f"Lost updates! Expected {expected}, got {final_count}"
