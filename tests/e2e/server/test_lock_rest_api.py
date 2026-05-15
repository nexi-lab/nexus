"""Integration tests for Lock REST API endpoints (Issue #1110).

Tests verify lock model validation and REST API endpoint contracts.
Lock operations are backed by Rust kernel LockManager (sys_lock/sys_unlock).
"""

import pytest
from pydantic import ValidationError

from nexus.server.api.v2.models.locks import (
    LOCK_MAX_TTL,
    LockAcquireRequest,
    LockExtendRequest,
    LockInfoMutex,
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
            fence_token=1,
        )
        assert resp.lock_id == "abc-123"
        assert resp.mode == "mutex"
        assert resp.max_holders == 1
        assert resp.ttl == 30
        assert resp.fence_token == 1

    def test_lock_response_semaphore_mode(self):
        """Test LockResponse for semaphore lock."""
        resp = LockResponse(
            lock_id="def-456",
            path="/shared/room",
            mode="semaphore",
            max_holders=5,
            ttl=60,
            expires_at="2025-01-01T00:01:00+00:00",
            fence_token=2,
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
            fence_token=42,
        )
        json_data = resp.model_dump()
        assert json_data["lock_id"] == "abc-123"
        assert json_data["mode"] == "mutex"
        assert json_data["fence_token"] == 42

    def test_lock_status_response_locked(self):
        """Test LockStatusResponse when locked."""
        status = LockStatusResponse(
            path="/test/file.txt",
            locked=True,
            lock_info=LockInfoMutex(
                lock_id="abc-123",
                acquired_at=1700000000.0,
                expires_at=1700000030.0,
                fence_token=1,
            ),
        )
        assert status.locked is True
        assert status.lock_info is not None
        assert status.lock_info.mode == "mutex"

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
        resp = LockListResponse(
            locks=[
                LockInfoMutex(
                    lock_id="l1",
                    acquired_at=1700000000.0,
                    expires_at=1700000030.0,
                    fence_token=1,
                ),
            ],
            count=1,
        )
        assert resp.count == 1
        assert len(resp.locks) == 1

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
            LockAcquireRequest.model_validate({})

    def test_lock_extend_request_requires_lock_id(self):
        """Test that lock_id is required."""
        with pytest.raises(ValidationError):
            LockExtendRequest.model_validate({})

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
            fence_token=1,
        )
        LockResponse(
            lock_id="a",
            path="/p",
            mode="semaphore",
            max_holders=5,
            ttl=30,
            expires_at="2025-01-01T00:00:00Z",
            fence_token=2,
        )

        # Invalid mode should raise
        with pytest.raises(ValidationError):
            LockResponse.model_validate(
                {
                    "lock_id": "a",
                    "path": "/p",
                    "mode": "invalid",
                    "max_holders": 1,
                    "ttl": 30,
                    "expires_at": "2025-01-01T00:00:00Z",
                    "fence_token": 3,
                }
            )

    def test_ttl_must_be_positive(self):
        """Test that TTL must be >= 1."""
        with pytest.raises(ValidationError):
            LockAcquireRequest(path="/test", ttl=0)

        with pytest.raises(ValidationError):
            LockAcquireRequest(path="/test", ttl=-1)

    def test_ttl_must_be_within_max(self):
        """Test that TTL must be <= LOCK_MAX_TTL."""
        with pytest.raises(ValidationError):
            LockAcquireRequest(path="/test", ttl=LOCK_MAX_TTL + 1)

    def test_max_holders_must_be_positive(self):
        """Test that max_holders must be >= 1."""
        with pytest.raises(ValidationError):
            LockAcquireRequest(path="/test", max_holders=0)

        with pytest.raises(ValidationError):
            LockAcquireRequest(path="/test", max_holders=-1)

    def test_timeout_must_be_non_negative(self):
        """Test that timeout must be >= 0."""
        with pytest.raises(ValidationError):
            LockAcquireRequest(path="/test", timeout=-1)
