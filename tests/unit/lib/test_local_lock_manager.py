"""Unit tests for LocalLockManager (standalone advisory lock manager).

Tests use a mock MetastoreABC with lock support — no real redb needed.
"""

import time
from unittest.mock import MagicMock

import pytest

from nexus.lib.distributed_lock import ExtendResult, LocalLockManager

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_mock_store(*, supports_locks: bool = True) -> MagicMock:
    """Create a mock MetastoreABC with lock method stubs."""
    store = MagicMock()
    store.supports_locks = supports_locks
    # Default: all lock ops succeed
    store.acquire_lock.return_value = True
    store.release_lock.return_value = True
    store.extend_lock.return_value = True
    store.force_release_lock.return_value = True
    store.get_lock_info.return_value = None
    store.list_locks.return_value = []
    return store


@pytest.fixture
def store():
    return _make_mock_store()


@pytest.fixture
def mgr(store):
    return LocalLockManager(store)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_release(mgr, store):
    """acquire returns holder_id, release returns True."""
    holder_id = await mgr.acquire("z1", "/file.txt")
    assert holder_id is not None
    assert isinstance(holder_id, str)
    assert len(holder_id) == 36  # UUID format

    store.acquire_lock.assert_called_once()
    call_args = store.acquire_lock.call_args
    assert call_args[0][0] == "z1:/file.txt"  # lock_key
    assert call_args[0][1] == holder_id  # holder_id

    released = await mgr.release(holder_id, "z1", "/file.txt")
    assert released is True
    store.release_lock.assert_called_once_with("z1:/file.txt", holder_id)


@pytest.mark.asyncio
async def test_acquire_timeout_zero(mgr, store):
    """acquire with timeout=0 returns None on contention."""
    store.acquire_lock.return_value = False

    result = await mgr.acquire("z1", "/busy", timeout=0)
    assert result is None
    store.acquire_lock.assert_called_once()


@pytest.mark.asyncio
async def test_acquire_retry_succeeds(mgr, store):
    """acquire retries until success within timeout."""
    # Fail twice, succeed on third attempt
    store.acquire_lock.side_effect = [False, False, True]

    holder_id = await mgr.acquire("z1", "/contested", timeout=1.0)
    assert holder_id is not None
    assert store.acquire_lock.call_count == 3


@pytest.mark.asyncio
async def test_acquire_retry_timeout(mgr, store):
    """acquire returns None when timeout expires during retries."""
    store.acquire_lock.return_value = False

    t0 = time.monotonic()
    result = await mgr.acquire("z1", "/held", timeout=0.15)
    elapsed = time.monotonic() - t0

    assert result is None
    assert elapsed >= 0.1  # at least a couple retries at 50ms
    assert store.acquire_lock.call_count >= 2


@pytest.mark.asyncio
async def test_extend(mgr, store):
    """extend delegates to store, returns ExtendResult."""
    store.extend_lock.return_value = True
    store.get_lock_info.return_value = {
        "path": "z1:/file.txt",
        "max_holders": 1,
        "holders": [
            {
                "lock_id": "abc",
                "holder_info": "",
                "acquired_at": 1000.0,
                "expires_at": 1060.0,
            }
        ],
    }

    result = await mgr.extend("abc", "z1", "/file.txt", ttl=60.0)
    assert isinstance(result, ExtendResult)
    assert result.success is True
    assert result.lock_info is not None
    assert result.lock_info.path == "/file.txt"

    store.extend_lock.assert_called_once_with("z1:/file.txt", "abc", 60)


@pytest.mark.asyncio
async def test_extend_failure(mgr, store):
    """extend returns success=False when store rejects."""
    store.extend_lock.return_value = False

    result = await mgr.extend("bad-id", "z1", "/file.txt")
    assert result.success is False
    assert result.lock_info is None


@pytest.mark.asyncio
async def test_release_unknown(mgr, store):
    """release unknown lock_id returns False."""
    store.release_lock.return_value = False
    released = await mgr.release("nonexistent", "z1", "/file.txt")
    assert released is False


@pytest.mark.asyncio
async def test_semaphore_mode(mgr, store):
    """acquire with max_holders>1 passes through to store."""
    holder_id = await mgr.acquire("z1", "/slots", max_holders=5)
    assert holder_id is not None

    call_kwargs = store.acquire_lock.call_args[1]
    assert call_kwargs["max_holders"] == 5


@pytest.mark.asyncio
async def test_lock_key_format(mgr, store):
    """lock key is {zone_id}:{path}."""
    await mgr.acquire("zone-abc", "/deep/path/file.txt")
    lock_key = store.acquire_lock.call_args[0][0]
    assert lock_key == "zone-abc:/deep/path/file.txt"


@pytest.mark.asyncio
async def test_health_check(mgr):
    """health_check always returns True for local store."""
    assert await mgr.health_check() is True


@pytest.mark.asyncio
async def test_force_release(mgr, store):
    """force_release delegates to store."""
    store.force_release_lock.return_value = True
    result = await mgr.force_release("z1", "/locked")
    assert result is True
    store.force_release_lock.assert_called_once_with("z1:/locked")


@pytest.mark.asyncio
async def test_force_release_no_lock(mgr, store):
    """force_release returns False when no lock exists."""
    store.force_release_lock.return_value = False
    result = await mgr.force_release("z1", "/not-locked")
    assert result is False


@pytest.mark.asyncio
async def test_ttl_enforced_minimum(mgr, store):
    """ttl is always >= 1 second (prevents zero/negative TTL)."""
    await mgr.acquire("z1", "/file.txt", ttl=0.1)
    call_kwargs = store.acquire_lock.call_args[1]
    assert call_kwargs["ttl_secs"] >= 1


@pytest.mark.asyncio
async def test_get_lock_info_none(mgr, store):
    """get_lock_info returns None when not locked."""
    store.get_lock_info.return_value = None
    info = await mgr.get_lock_info("z1", "/file.txt")
    assert info is None


@pytest.mark.asyncio
async def test_get_lock_info_with_data(mgr, store):
    """get_lock_info converts store dict to LockInfo."""
    store.get_lock_info.return_value = {
        "path": "z1:/file.txt",
        "max_holders": 1,
        "holders": [
            {
                "lock_id": "h1",
                "holder_info": "test",
                "acquired_at": 1000.0,
                "expires_at": 1030.0,
            }
        ],
    }
    info = await mgr.get_lock_info("z1", "/file.txt")
    assert info is not None
    assert info.path == "/file.txt"
    assert info.mode == "mutex"
    assert len(info.holders) == 1
    assert info.holders[0].lock_id == "h1"


@pytest.mark.asyncio
async def test_get_lock_info_semaphore(mgr, store):
    """get_lock_info reports mode='semaphore' when max_holders>1."""
    store.get_lock_info.return_value = {
        "path": "z1:/slots",
        "max_holders": 3,
        "holders": [],
    }
    info = await mgr.get_lock_info("z1", "/slots")
    assert info is not None
    assert info.mode == "semaphore"


@pytest.mark.asyncio
async def test_list_locks_empty(mgr, store):
    """list_locks returns empty list when no locks."""
    store.list_locks.return_value = []
    locks = await mgr.list_locks("z1")
    assert locks == []
    store.list_locks.assert_called_once_with(prefix="z1:", limit=100)


@pytest.mark.asyncio
async def test_list_locks_with_pattern(mgr, store):
    """list_locks filters by pattern."""
    store.list_locks.return_value = [
        {"path": "z1:/a/file.txt", "max_holders": 1, "holders": []},
        {"path": "z1:/b/other.txt", "max_holders": 1, "holders": []},
    ]
    locks = await mgr.list_locks("z1", pattern="/a/")
    assert len(locks) == 1
    assert locks[0].path == "/a/file.txt"


@pytest.mark.asyncio
async def test_is_locked(mgr, store):
    """is_locked delegates to get_lock_info (inherited from LockManagerBase)."""
    store.get_lock_info.return_value = None
    assert await mgr.is_locked("z1", "/file.txt") is False

    store.get_lock_info.return_value = {
        "path": "z1:/file.txt",
        "max_holders": 1,
        "holders": [{"lock_id": "h1", "acquired_at": 0, "expires_at": 0}],
    }
    assert await mgr.is_locked("z1", "/file.txt") is True


@pytest.mark.asyncio
async def test_max_holders_validation(mgr):
    """max_holders < 1 raises ValueError."""
    with pytest.raises(ValueError, match="max_holders must be >= 1"):
        await mgr.acquire("z1", "/file.txt", max_holders=0)
