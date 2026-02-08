"""End-to-end tests for Lock REST API endpoints (Issue #1110).

These tests run against an actual nexus serve process with authentication enabled.
Includes tests for both Raft-backed and Redis-backed lock managers.
"""

from __future__ import annotations

import os
import time

import httpx
import pytest


@pytest.fixture
def auth_headers():
    """Provide authentication headers for e2e tests.

    The API key is configured in conftest.py as NEXUS_API_KEY.
    API key auth grants admin privileges.
    """
    return {"Authorization": "Bearer test-e2e-api-key-12345"}


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


# =============================================================================
# Tests without Redis/Dragonfly (503 error handling)
# =============================================================================

# NOTE: With Raft-based locks (Issue #1110), the lock manager is always available
# via local sled storage and doesn't require Redis/Dragonfly. These tests are
# kept for legacy documentation but are permanently skipped.
@pytest.mark.skip(
    reason="Obsolete: Raft-based locks are always available (no Redis required)"
)
class TestLockApiWithoutRedis:
    """Test lock API behavior when Redis/Dragonfly is not configured.

    DEPRECATED: These tests are obsolete. With Raft-based locks, the lock manager
    is always available and doesn't require external dependencies.
    """

    def test_acquire_lock_returns_503_without_redis(self, test_app: httpx.Client, auth_headers):
        """Test that POST /api/locks returns 503 when lock manager unavailable."""
        response = test_app.post(
            "/api/locks",
            json={"path": "/test/file.txt", "timeout": 1, "ttl": 30},
            headers=auth_headers,
        )
        # Should return 503 Service Unavailable
        assert response.status_code == 503
        assert "lock manager" in response.json().get("detail", "").lower()

    def test_list_locks_returns_503_without_redis(self, test_app: httpx.Client, auth_headers):
        """Test that GET /api/locks returns 503 when lock manager unavailable."""
        response = test_app.get("/api/locks", headers=auth_headers)
        assert response.status_code == 503

    def test_get_lock_status_returns_503_without_redis(self, test_app: httpx.Client, auth_headers):
        """Test that GET /api/locks/{path} returns 503 when lock manager unavailable."""
        response = test_app.get("/api/locks/test/file.txt", headers=auth_headers)
        assert response.status_code == 503

    def test_release_lock_returns_503_without_redis(self, test_app: httpx.Client, auth_headers):
        """Test that DELETE /api/locks/{path} returns 503 when lock manager unavailable."""
        response = test_app.delete("/api/locks/test/file.txt?lock_id=fake-id", headers=auth_headers)
        assert response.status_code == 503

    def test_extend_lock_returns_503_without_redis(self, test_app: httpx.Client, auth_headers):
        """Test that PATCH /api/locks/{path} returns 503 when lock manager unavailable."""
        response = test_app.patch(
            "/api/locks/test/file.txt",
            json={"lock_id": "fake-id", "ttl": 60},
            headers=auth_headers,
        )
        assert response.status_code == 503


# =============================================================================
# Tests with Redis/Dragonfly (full e2e)
# =============================================================================


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

    def test_full_lock_lifecycle(self, test_app: httpx.Client, auth_headers):
        """Test complete lock lifecycle: acquire -> status -> extend -> release."""
        path = f"/test/lock-lifecycle-{time.time()}.txt"

        # 1. Acquire lock
        response = test_app.post(
            "/api/locks",
            json={"path": path, "timeout": 5, "ttl": 30},
            headers=auth_headers,
        )
        assert response.status_code == 201
        data = response.json()
        assert "lock_id" in data
        assert data["path"] == path
        assert data["mode"] == "mutex"
        assert "fence_token" in data
        lock_id = data["lock_id"]

        # 2. Check status - should be locked
        response = test_app.get(f"/api/locks{path}", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["locked"] is True
        assert data["lock_info"]["mode"] == "mutex"

        # 3. Extend TTL
        response = test_app.patch(
            f"/api/locks{path}",
            json={"lock_id": lock_id, "ttl": 60},
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["ttl"] == 60

        # 4. Release lock
        response = test_app.delete(f"/api/locks{path}?lock_id={lock_id}", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["released"] is True

        # 5. Verify unlocked
        response = test_app.get(f"/api/locks{path}", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["locked"] is False

    def test_lock_contention_blocking(self, test_app: httpx.Client, auth_headers):
        """Test that second lock request blocks until first is released."""
        path = f"/test/contention-{time.time()}.txt"

        # Acquire first lock
        response = test_app.post(
            "/api/locks",
            json={"path": path, "timeout": 1, "ttl": 2},
            headers=auth_headers,
        )
        assert response.status_code == 201
        lock_id = response.json()["lock_id"]

        # Try to acquire second lock with short timeout - should fail
        response = test_app.post(
            "/api/locks",
            json={"path": path, "timeout": 0.5, "ttl": 30},
            headers=auth_headers,
        )
        assert response.status_code == 409  # Conflict - timeout

        # Release first lock
        test_app.delete(f"/api/locks{path}?lock_id={lock_id}", headers=auth_headers)

    def test_non_blocking_lock_acquisition(self, test_app: httpx.Client, auth_headers):
        """Test non-blocking lock acquisition returns immediately."""
        path = f"/test/non-blocking-{time.time()}.txt"

        # Acquire first lock
        response = test_app.post(
            "/api/locks",
            json={"path": path, "timeout": 5, "ttl": 30},
            headers=auth_headers,
        )
        assert response.status_code == 201
        lock_id = response.json()["lock_id"]

        # Try non-blocking acquisition - should return immediately with 409
        start = time.time()
        response = test_app.post(
            "/api/locks",
            json={"path": path, "timeout": 10, "ttl": 30, "blocking": False},
            headers=auth_headers,
        )
        elapsed = time.time() - start

        assert response.status_code == 409
        assert elapsed < 1.0  # Should be nearly instant
        assert "non-blocking" in response.json().get("detail", "").lower()

        # Cleanup
        test_app.delete(f"/api/locks{path}?lock_id={lock_id}", headers=auth_headers)

    def test_release_wrong_lock_id_fails(self, test_app: httpx.Client, auth_headers):
        """Test that releasing with wrong lock_id fails with 403."""
        path = f"/test/wrong-id-{time.time()}.txt"

        # Acquire lock
        response = test_app.post(
            "/api/locks",
            json={"path": path, "timeout": 5, "ttl": 30},
            headers=auth_headers,
        )
        assert response.status_code == 201
        real_lock_id = response.json()["lock_id"]

        # Try to release with wrong ID
        response = test_app.delete(f"/api/locks{path}?lock_id=wrong-id-12345", headers=auth_headers)
        assert response.status_code == 403

        # Cleanup with real ID
        test_app.delete(f"/api/locks{path}?lock_id={real_lock_id}", headers=auth_headers)

    def test_semaphore_multiple_holders(self, test_app: httpx.Client, auth_headers):
        """Test semaphore mode allows multiple holders."""
        path = f"/test/semaphore-{time.time()}.txt"
        max_holders = 3

        lock_ids = []

        # Acquire 3 semaphore slots
        for i in range(max_holders):
            response = test_app.post(
                "/api/locks",
                json={"path": path, "timeout": 5, "ttl": 30, "max_holders": max_holders},
                headers=auth_headers,
        )
            assert response.status_code == 201, f"Failed to acquire slot {i + 1}"
            data = response.json()
            assert data["mode"] == "semaphore"
            lock_ids.append(data["lock_id"])

        # Fourth request should fail (semaphore full)
        response = test_app.post(
            "/api/locks",
            json={"path": path, "timeout": 0.5, "ttl": 30, "max_holders": max_holders},
            headers=auth_headers,
        )
        assert response.status_code == 409

        # Cleanup
        for lock_id in lock_ids:
            test_app.delete(f"/api/locks{path}?lock_id={lock_id}", headers=auth_headers)

    def test_list_locks(self, test_app: httpx.Client, auth_headers):
        """Test listing active locks."""
        paths = [f"/test/list-{time.time()}-{i}.txt" for i in range(3)]
        lock_ids = []

        # Acquire multiple locks
        for path in paths:
            response = test_app.post(
                "/api/locks",
                json={"path": path, "timeout": 5, "ttl": 60},
                headers=auth_headers,
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
            test_app.delete(f"/api/locks{path}?lock_id={lock_id}", headers=auth_headers)

    def test_force_release_requires_admin(self, test_app: httpx.Client, auth_headers):
        """Test that force=true requires admin privileges."""
        path = f"/test/force-release-{time.time()}.txt"

        # Acquire lock
        response = test_app.post(
            "/api/locks",
            json={"path": path, "timeout": 5, "ttl": 30},
            headers=auth_headers,
        )
        assert response.status_code == 201
        lock_id = response.json()["lock_id"]

        # Try force release without admin - should fail
        response = test_app.delete(f"/api/locks{path}?lock_id={lock_id}&force=true")
        assert response.status_code == 403
        assert "admin" in response.json().get("detail", "").lower()

        # Normal release should work
        response = test_app.delete(f"/api/locks{path}?lock_id={lock_id}", headers=auth_headers)
        assert response.status_code == 200


# =============================================================================
# Edge Case Tests (Input Validation)
# =============================================================================


class TestLockApiEdgeCases:
    """Edge case tests for lock API input validation.

    These tests verify proper handling of boundary inputs and
    potential security concerns like path traversal.
    """

    def test_ttl_too_high_returns_422(self, test_app: httpx.Client, auth_headers):
        """Test that TTL > max returns 422 Unprocessable Entity."""
        response = test_app.post(
            "/api/locks",
            json={"path": "/test/file.txt", "ttl": 999999999},
            headers=auth_headers,
        )
        assert response.status_code in (401, 422)

    def test_ttl_zero_returns_422(self, test_app: httpx.Client, auth_headers):
        """Test that TTL = 0 returns 422."""
        response = test_app.post(
            "/api/locks",
            json={"path": "/test/file.txt", "ttl": 0},
            headers=auth_headers,
        )
        assert response.status_code in (401, 422)

    def test_ttl_negative_returns_422(self, test_app: httpx.Client, auth_headers):
        """Test that TTL < 0 returns 422."""
        response = test_app.post(
            "/api/locks",
            json={"path": "/test/file.txt", "ttl": -1},
            headers=auth_headers,
        )
        assert response.status_code in (401, 422)

    def test_max_holders_zero_returns_422(self, test_app: httpx.Client, auth_headers):
        """Test that max_holders = 0 returns 422."""
        response = test_app.post(
            "/api/locks",
            json={"path": "/test/file.txt", "max_holders": 0},
            headers=auth_headers,
        )
        assert response.status_code in (401, 422)

    def test_max_holders_negative_returns_422(self, test_app: httpx.Client, auth_headers):
        """Test that max_holders < 0 returns 422."""
        response = test_app.post(
            "/api/locks",
            json={"path": "/test/file.txt", "max_holders": -5},
            headers=auth_headers,
        )
        assert response.status_code in (401, 422)

    def test_timeout_negative_returns_422(self, test_app: httpx.Client, auth_headers):
        """Test that timeout < 0 returns 422."""
        response = test_app.post(
            "/api/locks",
            json={"path": "/test/file.txt", "timeout": -1},
            headers=auth_headers,
        )
        assert response.status_code in (401, 422)

    def test_extend_ttl_zero_returns_422(self, test_app: httpx.Client, auth_headers):
        """Test that extend with TTL = 0 returns 422."""
        response = test_app.patch(
            "/api/locks/test/file.txt",
            json={"lock_id": "some-id", "ttl": 0},
            headers=auth_headers,
        )
        assert response.status_code in (401, 422)

    def test_path_traversal_normalized(self, test_app: httpx.Client, auth_headers):
        """Test that path traversal patterns are handled safely.

        The lock path is used as a key, not as a filesystem path.
        Path normalization ensures leading slash consistency.
        """
        # These should not cause errors (paths are just string keys)
        response = test_app.get("/api/locks/../../../etc/passwd")
        # Should get 401 (no auth) or 200/503 (depending on lock manager state)
        # but NOT a 500 internal server error
        assert response.status_code != 500

    def test_empty_path_in_url(self, test_app: httpx.Client, auth_headers):
        """Test that empty path segments in URL are handled."""
        response = test_app.get("/api/locks/")
        # FastAPI may route this to list_locks (GET /api/locks) or return 404
        assert response.status_code != 500

    def test_unicode_path(self, test_app: httpx.Client, auth_headers):
        """Test that Unicode paths are handled."""
        response = test_app.post(
            "/api/locks",
            json={"path": "/test/文件.txt", "ttl": 30},
            headers=auth_headers,
        )
        # Should not crash; 401 (no auth) or 201/503 are all acceptable
        assert response.status_code != 500

    def test_very_long_path(self, test_app: httpx.Client, auth_headers):
        """Test that very long paths don't crash the server."""
        long_path = "/test/" + "a" * 10000 + ".txt"
        response = test_app.post(
            "/api/locks",
            json={"path": long_path, "ttl": 30},
            headers=auth_headers,
        )
        assert response.status_code != 500

    def test_missing_lock_id_in_release(self, test_app: httpx.Client, auth_headers):
        """Test that DELETE without lock_id returns 422."""
        response = test_app.delete("/api/locks/test/file.txt")
        assert response.status_code == 422

    def test_missing_lock_id_in_extend(self, test_app: httpx.Client, auth_headers):
        """Test that PATCH without lock_id in body returns 422."""
        response = test_app.patch(
            "/api/locks/test/file.txt",
            json={"ttl": 60},
            headers=auth_headers,
        )
        assert response.status_code == 422
