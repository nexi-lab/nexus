"""Unit tests for LocksRPCService — lock_acquire, lock_extend.

Issue #1528.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from nexus.server.rpc.services.locks_rpc import LocksRPCService


@dataclass
class _FakeLockInfo:
    lock_id: str = "lk-001"
    path: str = "/test/file.txt"
    mode: str = "exclusive"
    ttl: float = 60.0


@dataclass
class _FakeExtendResult:
    success: bool = True
    lock_info: _FakeLockInfo | None = None


@pytest.fixture()
def lock_manager():
    mgr = AsyncMock()
    mgr.acquire = AsyncMock(return_value="lk-001")
    mgr.get_lock_info = AsyncMock(return_value=_FakeLockInfo())
    mgr.extend = AsyncMock(return_value=_FakeExtendResult(success=True, lock_info=_FakeLockInfo()))
    return mgr


@pytest.fixture()
def svc(lock_manager):
    return LocksRPCService(lock_manager)


class TestLockAcquire:
    @pytest.mark.asyncio
    async def test_acquire_success(self, svc, lock_manager):
        result = await svc.lock_acquire(path="/test/file.txt")
        assert result["acquired"] is True
        assert result["lock_id"] == "lk-001"
        assert result["lock_info"]["lock_id"] == "lk-001"
        lock_manager.acquire.assert_awaited_once_with(
            path="/test/file.txt",
            mode="exclusive",
            timeout=30.0,
            ttl=60.0,
            max_holders=1,
        )

    @pytest.mark.asyncio
    async def test_acquire_failure(self, svc, lock_manager):
        lock_manager.acquire.return_value = None
        result = await svc.lock_acquire(path="/locked")
        assert result["acquired"] is False
        assert result["lock_id"] is None

    @pytest.mark.asyncio
    async def test_acquire_custom_params(self, svc, lock_manager):
        await svc.lock_acquire(
            path="/x",
            mode="shared",
            timeout=5.0,
            ttl=120.0,
            max_holders=3,
        )
        lock_manager.acquire.assert_awaited_once_with(
            path="/x",
            mode="shared",
            timeout=5.0,
            ttl=120.0,
            max_holders=3,
        )


class TestLockExtend:
    @pytest.mark.asyncio
    async def test_extend_success(self, svc, lock_manager):
        result = await svc.lock_extend(lock_id="lk-001", path="/test/file.txt", ttl=120.0)
        assert result["success"] is True
        assert result["lock_info"] is not None
        lock_manager.extend.assert_awaited_once_with(
            lock_id="lk-001",
            path="/test/file.txt",
            ttl=120.0,
        )

    @pytest.mark.asyncio
    async def test_extend_failure(self, svc, lock_manager):
        lock_manager.extend.return_value = _FakeExtendResult(success=False, lock_info=None)
        result = await svc.lock_extend(lock_id="bad", path="/x")
        assert result["success"] is False
        assert result["lock_info"] is None
