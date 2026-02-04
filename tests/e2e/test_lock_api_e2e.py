"""End-to-end tests for Lock REST API endpoints (Issue #1186).

These tests run against an actual nexus serve process.
"""

from __future__ import annotations

import os
import time

import httpx
import pytest


def _redis_available() -> bool:
    """Check if Redis/Dragonfly is available for testing."""
    redis_url = os.environ.get("NEXUS_DRAGONFLY_URL") or os.environ.get("REDIS_URL")
    if not redis_url:
        return False
    try:
        import redis

        client = redis.from_url(redis_url, socket_connect_timeout=1)
        client.ping()
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    _redis_available(),
    reason="Redis/Dragonfly is available. These tests require no Redis.",
)
class TestLockApiWithoutRedis:
    """Test lock API behavior when Redis/Dragonfly is not configured.

    These tests verify graceful error handling when the lock manager
    is not available.
    """

    def test_acquire_lock_returns_503_without_redis(self, test_app: httpx.Client):
        """Test that POST /api/locks returns 503 when lock manager unavailable."""
        response = test_app.post(
            "/api/locks",
            json={"path": "/test/file.txt", "timeout": 1, "ttl": 30},
        )
        # Should return 503 Service Unavailable
        assert response.status_code == 503
        assert "lock manager" in response.json().get("detail", "").lower()

    def test_list_locks_returns_503_without_redis(self, test_app: httpx.Client):
        """Test that GET /api/locks returns 503 when lock manager unavailable."""
        response = test_app.get("/api/locks")
        assert response.status_code == 503

    def test_get_lock_status_returns_503_without_redis(self, test_app: httpx.Client):
        """Test that GET /api/locks/{path} returns 503 when lock manager unavailable."""
        response = test_app.get("/api/locks/test/file.txt")
        assert response.status_code == 503

    def test_release_lock_returns_503_without_redis(self, test_app: httpx.Client):
        """Test that DELETE /api/locks/{path} returns 503 when lock manager unavailable."""
        response = test_app.delete("/api/locks/test/file.txt?lock_id=fake-id")
        assert response.status_code == 503

    def test_extend_lock_returns_503_without_redis(self, test_app: httpx.Client):
        """Test that PATCH /api/locks/{path} returns 503 when lock manager unavailable."""
        response = test_app.patch(
            "/api/locks/test/file.txt",
            json={"lock_id": "fake-id", "ttl": 60},
        )
        assert response.status_code == 503


@pytest.mark.skipif(
    not _redis_available(),
    reason="Redis/Dragonfly not available. Set NEXUS_DRAGONFLY_URL or REDIS_URL.",
)
class TestLockApiWithRedis:
    """Full e2e tests for lock API with Redis/Dragonfly backend.

    These tests require Redis/Dragonfly to be running and configured
    via NEXUS_DRAGONFLY_URL or REDIS_URL environment variable.
    """

    @pytest.fixture(autouse=True)
    def setup_redis_env(self, monkeypatch):
        """Ensure Redis URL is available to the server."""
        redis_url = os.environ.get("NEXUS_DRAGONFLY_URL") or os.environ.get("REDIS_URL")
        if redis_url:
            monkeypatch.setenv("NEXUS_DRAGONFLY_URL", redis_url)

    def test_full_lock_lifecycle(self, test_app: httpx.Client):
        """Test complete lock lifecycle: acquire -> status -> extend -> release."""
        path = f"/test/lock-lifecycle-{time.time()}.txt"

        # 1. Acquire lock
        response = test_app.post(
            "/api/locks",
            json={"path": path, "timeout": 5, "ttl": 30},
        )
        assert response.status_code == 201
        data = response.json()
        assert "lock_id" in data
        assert data["path"] == path
        assert data["mode"] == "mutex"
        lock_id = data["lock_id"]

        # 2. Check status - should be locked
        response = test_app.get(f"/api/locks{path}")
        assert response.status_code == 200
        data = response.json()
        assert data["locked"] is True
        assert data["lock_info"]["lock_id"] == lock_id

        # 3. Extend TTL
        response = test_app.patch(
            f"/api/locks{path}",
            json={"lock_id": lock_id, "ttl": 60},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ttl"] == 60

        # 4. Release lock
        response = test_app.delete(f"/api/locks{path}?lock_id={lock_id}")
        assert response.status_code == 200
        assert response.json()["released"] is True

        # 5. Verify unlocked
        response = test_app.get(f"/api/locks{path}")
        assert response.status_code == 200
        assert response.json()["locked"] is False

    def test_lock_contention_blocking(self, test_app: httpx.Client):
        """Test that second lock request blocks until first is released."""
        path = f"/test/contention-{time.time()}.txt"

        # Acquire first lock
        response = test_app.post(
            "/api/locks",
            json={"path": path, "timeout": 1, "ttl": 2},
        )
        assert response.status_code == 201
        lock_id = response.json()["lock_id"]

        # Try to acquire second lock with short timeout - should fail
        response = test_app.post(
            "/api/locks",
            json={"path": path, "timeout": 0.5, "ttl": 30},
        )
        assert response.status_code == 409  # Conflict - timeout

        # Release first lock
        test_app.delete(f"/api/locks{path}?lock_id={lock_id}")

    def test_non_blocking_lock_acquisition(self, test_app: httpx.Client):
        """Test non-blocking lock acquisition returns immediately."""
        path = f"/test/non-blocking-{time.time()}.txt"

        # Acquire first lock
        response = test_app.post(
            "/api/locks",
            json={"path": path, "timeout": 5, "ttl": 30},
        )
        assert response.status_code == 201
        lock_id = response.json()["lock_id"]

        # Try non-blocking acquisition - should return immediately with 409
        start = time.time()
        response = test_app.post(
            "/api/locks",
            json={"path": path, "timeout": 10, "ttl": 30, "blocking": False},
        )
        elapsed = time.time() - start

        assert response.status_code == 409
        assert elapsed < 1.0  # Should be nearly instant
        assert "non-blocking" in response.json().get("detail", "").lower()

        # Cleanup
        test_app.delete(f"/api/locks{path}?lock_id={lock_id}")

    def test_release_wrong_lock_id_fails(self, test_app: httpx.Client):
        """Test that releasing with wrong lock_id fails with 403."""
        path = f"/test/wrong-id-{time.time()}.txt"

        # Acquire lock
        response = test_app.post(
            "/api/locks",
            json={"path": path, "timeout": 5, "ttl": 30},
        )
        assert response.status_code == 201
        real_lock_id = response.json()["lock_id"]

        # Try to release with wrong ID
        response = test_app.delete(f"/api/locks{path}?lock_id=wrong-id-12345")
        assert response.status_code == 403

        # Cleanup with real ID
        test_app.delete(f"/api/locks{path}?lock_id={real_lock_id}")

    def test_semaphore_multiple_holders(self, test_app: httpx.Client):
        """Test semaphore mode allows multiple holders."""
        path = f"/test/semaphore-{time.time()}.txt"
        max_holders = 3

        lock_ids = []

        # Acquire 3 semaphore slots
        for i in range(max_holders):
            response = test_app.post(
                "/api/locks",
                json={"path": path, "timeout": 5, "ttl": 30, "max_holders": max_holders},
            )
            assert response.status_code == 201, f"Failed to acquire slot {i + 1}"
            data = response.json()
            assert data["mode"] == "semaphore"
            lock_ids.append(data["lock_id"])

        # Fourth request should fail (semaphore full)
        response = test_app.post(
            "/api/locks",
            json={"path": path, "timeout": 0.5, "ttl": 30, "max_holders": max_holders},
        )
        assert response.status_code == 409

        # Check status
        response = test_app.get(f"/api/locks{path}")
        assert response.status_code == 200
        data = response.json()
        assert data["locked"] is True
        assert data["lock_info"]["mode"] == "semaphore"
        assert data["lock_info"]["holders"] == max_holders

        # Cleanup
        for lock_id in lock_ids:
            test_app.delete(f"/api/locks{path}?lock_id={lock_id}")

    def test_list_locks(self, test_app: httpx.Client):
        """Test listing active locks."""
        paths = [f"/test/list-{time.time()}-{i}.txt" for i in range(3)]
        lock_ids = []

        # Acquire multiple locks
        for path in paths:
            response = test_app.post(
                "/api/locks",
                json={"path": path, "timeout": 5, "ttl": 60},
            )
            assert response.status_code == 201
            lock_ids.append((path, response.json()["lock_id"]))

        # List locks
        response = test_app.get("/api/locks?limit=100")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] >= 3

        # Cleanup
        for path, lock_id in lock_ids:
            test_app.delete(f"/api/locks{path}?lock_id={lock_id}")

    def test_force_release_requires_admin(self, test_app: httpx.Client):
        """Test that force=true requires admin privileges."""
        path = f"/test/force-release-{time.time()}.txt"

        # Acquire lock
        response = test_app.post(
            "/api/locks",
            json={"path": path, "timeout": 5, "ttl": 30},
        )
        assert response.status_code == 201
        lock_id = response.json()["lock_id"]

        # Try force release without admin - should fail
        response = test_app.delete(f"/api/locks{path}?lock_id={lock_id}&force=true")
        assert response.status_code == 403
        assert "admin" in response.json().get("detail", "").lower()

        # Normal release should work
        response = test_app.delete(f"/api/locks{path}?lock_id={lock_id}")
        assert response.status_code == 200
