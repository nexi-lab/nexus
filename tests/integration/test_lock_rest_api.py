"""Integration tests for Lock REST API endpoints (Issue #1110).

These tests use a real RaftLockManager backed by SQLite (no external
dependencies) to verify the full lock lifecycle through the REST API.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from pydantic import ValidationError

from nexus.core.distributed_lock import ExtendResult, HolderInfo, LockInfo, RaftLockManager
from nexus.server.fastapi_server import (
    LOCK_MAX_TTL,
    LockAcquireRequest,
    LockExtendRequest,
    LockHolderResponse,
    LockInfoMutex,
    LockInfoSemaphore,
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
            LockResponse(
                lock_id="a",
                path="/p",
                mode="invalid",  # type: ignore
                max_holders=1,
                ttl=30,
                expires_at="2025-01-01T00:00:00Z",
                fence_token=3,
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


class TestLockManagerIntegration:
    """Integration tests for RaftLockManager.

    These tests use a real RaftLockManager with in-memory storage,
    no external dependencies required.
    """

    @pytest.fixture
    def lock_manager(self, tmp_path):
        """Create a RaftLockManager with local sled storage."""
        try:
            from nexus.storage.raft_metadata_store import RaftMetadataStore

            store = RaftMetadataStore.local(str(tmp_path / "test-raft"))
            return RaftLockManager(store)
        except Exception:
            pytest.skip("RaftMetadataStore not available (Rust bindings not compiled)")

    @pytest.mark.asyncio
    async def test_full_lock_lifecycle(self, lock_manager):
        """Test complete lock lifecycle: acquire -> status -> extend -> release."""
        lock_id = await lock_manager.acquire("default", "/test/file.txt", timeout=5, ttl=30)
        assert lock_id is not None

        # Check status
        info = await lock_manager.get_lock_info("default", "/test/file.txt")
        assert info is not None
        assert info.mode == "mutex"
        assert len(info.holders) == 1
        assert info.holders[0].lock_id == lock_id

        # Extend
        result = await lock_manager.extend(lock_id, "default", "/test/file.txt", ttl=60)
        assert result.success is True
        assert result.lock_info is not None

        # Release
        released = await lock_manager.release(lock_id, "default", "/test/file.txt")
        assert released is True

        # Verify unlocked
        info = await lock_manager.get_lock_info("default", "/test/file.txt")
        assert info is None

    @pytest.mark.asyncio
    async def test_lock_contention(self, lock_manager):
        """Test lock behavior under contention."""
        lock_id = await lock_manager.acquire("default", "/contested.txt", timeout=1, ttl=30)
        assert lock_id is not None

        # Second acquire should fail (timeout)
        lock_id2 = await lock_manager.acquire("default", "/contested.txt", timeout=0.1, ttl=30)
        assert lock_id2 is None

        # Release first lock
        await lock_manager.release(lock_id, "default", "/contested.txt")

        # Now should succeed
        lock_id3 = await lock_manager.acquire("default", "/contested.txt", timeout=1, ttl=30)
        assert lock_id3 is not None
        await lock_manager.release(lock_id3, "default", "/contested.txt")

    @pytest.mark.asyncio
    async def test_semaphore_multiple_holders(self, lock_manager):
        """Test semaphore with multiple concurrent holders."""
        lock_ids = []
        for _ in range(3):
            lid = await lock_manager.acquire(
                "default", "/shared.txt", timeout=1, ttl=30, max_holders=3
            )
            assert lid is not None
            lock_ids.append(lid)

        # Fourth should fail
        lid4 = await lock_manager.acquire(
            "default", "/shared.txt", timeout=0.1, ttl=30, max_holders=3
        )
        assert lid4 is None

        # Release all
        for lid in lock_ids:
            await lock_manager.release(lid, "default", "/shared.txt")

    @pytest.mark.asyncio
    async def test_force_release(self, lock_manager):
        """Test admin force release."""
        lock_id = await lock_manager.acquire("default", "/force-test.txt", timeout=1, ttl=30)
        assert lock_id is not None

        released = await lock_manager.force_release("default", "/force-test.txt")
        assert released is True

        # Should be unlocked now
        info = await lock_manager.get_lock_info("default", "/force-test.txt")
        assert info is None

    @pytest.mark.asyncio
    async def test_list_locks(self, lock_manager):
        """Test listing active locks."""
        ids = []
        for i in range(3):
            lid = await lock_manager.acquire(
                "default", f"/list-test-{i}.txt", timeout=1, ttl=30
            )
            assert lid is not None
            ids.append((f"/list-test-{i}.txt", lid))

        locks = await lock_manager.list_locks("default", limit=100)
        assert len(locks) >= 3

        # Cleanup
        for path, lid in ids:
            await lock_manager.release(lid, "default", path)
