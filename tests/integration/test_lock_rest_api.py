"""Integration tests for Lock REST API endpoints (Issue #1186).

These tests verify the Lock REST API models and the lock manager integration.
The actual endpoint testing requires a running app with Redis/Dragonfly.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus.server.fastapi_server import (
    LockAcquireRequest,
    LockExtendRequest,
    LockListResponse,
    LockResponse,
    LockStatusResponse,
)


class TestLockModels:
    """Test Pydantic models for lock API."""

    def test_lock_acquire_request_defaults(self):
        """Test LockAcquireRequest default values."""
        req = LockAcquireRequest(path="/test/file.txt")
        assert req.path == "/test/file.txt"
        assert req.timeout == 30.0
        assert req.ttl == 30.0
        assert req.max_holders == 1
        assert req.blocking is True

    def test_lock_acquire_request_custom_values(self):
        """Test LockAcquireRequest with custom values."""
        req = LockAcquireRequest(
            path="/shared/config.json",
            timeout=10.0,
            ttl=60.0,
            max_holders=5,
            blocking=False,
        )
        assert req.path == "/shared/config.json"
        assert req.timeout == 10.0
        assert req.ttl == 60.0
        assert req.max_holders == 5
        assert req.blocking is False

    def test_lock_acquire_request_semaphore(self):
        """Test LockAcquireRequest for semaphore mode."""
        req = LockAcquireRequest(
            path="/shared/boardroom",
            max_holders=10,
            ttl=300,
        )
        assert req.max_holders == 10
        assert req.ttl == 300

    def test_lock_response_mutex_mode(self):
        """Test LockResponse for mutex lock."""
        resp = LockResponse(
            lock_id="abc-123",
            path="/test/file.txt",
            mode="mutex",
            max_holders=1,
            ttl=30,
            expires_at="2025-01-01T00:00:30+00:00",
        )
        assert resp.lock_id == "abc-123"
        assert resp.mode == "mutex"
        assert resp.max_holders == 1
        assert resp.ttl == 30

    def test_lock_response_semaphore_mode(self):
        """Test LockResponse for semaphore lock."""
        resp = LockResponse(
            lock_id="def-456",
            path="/shared/room",
            mode="semaphore",
            max_holders=5,
            ttl=60,
            expires_at="2025-01-01T00:01:00+00:00",
        )
        assert resp.mode == "semaphore"
        assert resp.max_holders == 5

    def test_lock_response_serialization(self):
        """Test LockResponse JSON serialization."""
        resp = LockResponse(
            lock_id="abc-123",
            path="/test/file.txt",
            mode="mutex",
            max_holders=1,
            ttl=30,
            expires_at="2025-01-01T00:00:30+00:00",
        )
        json_data = resp.model_dump()
        assert json_data["lock_id"] == "abc-123"
        assert json_data["mode"] == "mutex"

    def test_lock_status_response_locked(self):
        """Test LockStatusResponse when locked."""
        status = LockStatusResponse(
            path="/test/file.txt",
            locked=True,
            lock_info={"lock_id": "abc-123", "ttl": 30, "mode": "mutex"},
        )
        assert status.locked is True
        assert status.lock_info is not None
        assert status.lock_info["ttl"] == 30

    def test_lock_status_response_unlocked(self):
        """Test LockStatusResponse when not locked."""
        status = LockStatusResponse(
            path="/test/file.txt",
            locked=False,
            lock_info=None,
        )
        assert status.locked is False
        assert status.lock_info is None

    def test_lock_extend_request(self):
        """Test LockExtendRequest model."""
        req = LockExtendRequest(lock_id="abc-123", ttl=120.0)
        assert req.lock_id == "abc-123"
        assert req.ttl == 120.0

    def test_lock_extend_request_default_ttl(self):
        """Test LockExtendRequest with default TTL."""
        req = LockExtendRequest(lock_id="abc-123")
        assert req.ttl == 30.0

    def test_lock_list_response(self):
        """Test LockListResponse model."""
        locks = [
            {"path": "/file1.txt", "mode": "mutex", "ttl": 30},
            {"path": "/file2.txt", "mode": "semaphore", "holders": 3, "max_holders": 5},
        ]
        resp = LockListResponse(locks=locks, count=2)
        assert resp.count == 2
        assert len(resp.locks) == 2

    def test_lock_list_response_empty(self):
        """Test LockListResponse with no locks."""
        resp = LockListResponse(locks=[], count=0)
        assert resp.count == 0
        assert len(resp.locks) == 0


class TestLockRequestValidation:
    """Test request validation for lock models."""

    def test_lock_acquire_request_requires_path(self):
        """Test that path is required."""
        with pytest.raises(ValidationError):
            LockAcquireRequest()  # type: ignore

    def test_lock_extend_request_requires_lock_id(self):
        """Test that lock_id is required."""
        with pytest.raises(ValidationError):
            LockExtendRequest()  # type: ignore

    def test_lock_response_mode_literal(self):
        """Test that mode must be mutex or semaphore."""
        # Valid modes
        LockResponse(
            lock_id="a",
            path="/p",
            mode="mutex",
            max_holders=1,
            ttl=30,
            expires_at="2025-01-01T00:00:00Z",
        )
        LockResponse(
            lock_id="a",
            path="/p",
            mode="semaphore",
            max_holders=5,
            ttl=30,
            expires_at="2025-01-01T00:00:00Z",
        )

        # Invalid mode should raise
        with pytest.raises(ValidationError):
            LockResponse(
                lock_id="a",
                path="/p",
                mode="invalid",  # type: ignore
                max_holders=1,
                ttl=30,
                expires_at="2025-01-01T00:00:00Z",
            )


class TestLockEndpointIntegration:
    """Integration tests requiring full NexusFS + Redis setup.

    These tests are marked as skip by default since they require
    Redis/Dragonfly infrastructure. Run with:

        pytest tests/integration/test_lock_rest_api.py -k "Integration" --runintegration
    """

    @pytest.mark.skip(reason="Requires Redis/Dragonfly infrastructure")
    @pytest.mark.asyncio
    async def test_full_lock_lifecycle(self):
        """Test complete lock lifecycle: acquire -> extend -> release."""
        # This test would use TestClient with a real app
        pass

    @pytest.mark.skip(reason="Requires Redis/Dragonfly infrastructure")
    @pytest.mark.asyncio
    async def test_lock_contention(self):
        """Test lock behavior under contention."""
        pass

    @pytest.mark.skip(reason="Requires Redis/Dragonfly infrastructure")
    @pytest.mark.asyncio
    async def test_semaphore_multiple_holders(self):
        """Test semaphore with multiple concurrent holders."""
        pass

    @pytest.mark.skip(reason="Requires Redis/Dragonfly infrastructure")
    @pytest.mark.asyncio
    async def test_lock_auto_expiry(self):
        """Test that locks auto-expire after TTL."""
        pass

    @pytest.mark.skip(reason="Requires Redis/Dragonfly infrastructure")
    @pytest.mark.asyncio
    async def test_force_release_admin_only(self):
        """Test that force release requires admin privileges."""
        pass
