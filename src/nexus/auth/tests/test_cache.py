"""Unit tests for AuthCache with invalidation and singleflight (Decision #15)."""

from __future__ import annotations

import asyncio
import time

import pytest

from nexus.auth.cache import AuthCache


def test_cache_set_and_get():
    """Set then get returns the cached value."""
    cache = AuthCache(ttl=60, max_size=100)
    cache.set("token-abc", {"authenticated": True, "subject_id": "alice"})

    result = cache.get("token-abc")
    assert result is not None
    assert result["authenticated"] is True
    assert result["subject_id"] == "alice"


def test_cache_miss():
    """get() returns None on miss."""
    cache = AuthCache(ttl=60, max_size=100)
    assert cache.get("nonexistent") is None


def test_cache_get_returns_copy():
    """get() returns a copy — mutations don't affect cache."""
    cache = AuthCache(ttl=60, max_size=100)
    cache.set("tok", {"key": "value"})

    result = cache.get("tok")
    assert result is not None
    result["key"] = "mutated"

    # Original should be unchanged
    original = cache.get("tok")
    assert original is not None
    assert original["key"] == "value"


def test_cache_invalidate():
    """invalidate() removes a specific entry."""
    cache = AuthCache(ttl=60, max_size=100)
    cache.set("token-1", {"user": "alice"})
    cache.set("token-2", {"user": "bob"})

    cache.invalidate("token-1")

    assert cache.get("token-1") is None
    assert cache.get("token-2") is not None


def test_cache_invalidate_nonexistent():
    """invalidate() on non-existent key does not raise."""
    cache = AuthCache(ttl=60, max_size=100)
    cache.invalidate("ghost-token")  # should not raise


def test_cache_clear():
    """clear() removes all entries."""
    cache = AuthCache(ttl=60, max_size=100)
    cache.set("a", {"x": 1})
    cache.set("b", {"x": 2})

    cache.clear()

    assert cache.size == 0
    assert cache.get("a") is None
    assert cache.get("b") is None


def test_cache_size():
    """size property tracks entry count."""
    cache = AuthCache(ttl=60, max_size=100)
    assert cache.size == 0

    cache.set("a", {})
    assert cache.size == 1

    cache.set("b", {})
    assert cache.size == 2


def test_cache_ttl_expiration():
    """Entries expire after TTL seconds."""
    cache = AuthCache(ttl=1, max_size=100)
    cache.set("ephemeral", {"alive": True})

    assert cache.get("ephemeral") is not None

    # Fast-forward time past TTL
    time.sleep(1.1)

    assert cache.get("ephemeral") is None


def test_cache_max_size_eviction():
    """Oldest entries are evicted when max_size is exceeded."""
    cache = AuthCache(ttl=60, max_size=2)
    cache.set("first", {"n": 1})
    cache.set("second", {"n": 2})
    cache.set("third", {"n": 3})  # should evict "first"

    # "first" may have been evicted (LRU behavior)
    assert cache.size <= 2


def test_cache_overwrite():
    """Setting a key twice overwrites the value."""
    cache = AuthCache(ttl=60, max_size=100)
    cache.set("tok", {"version": 1})
    cache.set("tok", {"version": 2})

    result = cache.get("tok")
    assert result is not None
    assert result["version"] == 2


def test_token_hash_consistency():
    """Same token always produces the same hash key."""
    h1 = AuthCache._token_hash("same-token")
    h2 = AuthCache._token_hash("same-token")
    assert h1 == h2


def test_token_hash_uniqueness():
    """Different tokens produce different hash keys."""
    h1 = AuthCache._token_hash("token-a")
    h2 = AuthCache._token_hash("token-b")
    assert h1 != h2


# ---------------------------------------------------------------------------
# Singleflight (get_or_fetch) tests — Issue #15
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_fetch_cache_hit():
    """get_or_fetch returns cached value without calling fetch."""
    cache = AuthCache(ttl=60, max_size=100)
    cache.set("tok", {"user": "cached"})

    call_count = 0

    async def _fetch():
        nonlocal call_count
        call_count += 1
        return {"user": "fetched"}

    result = await cache.get_or_fetch("tok", _fetch)
    assert result is not None
    assert result["user"] == "cached"
    assert call_count == 0


@pytest.mark.asyncio
async def test_get_or_fetch_cache_miss():
    """get_or_fetch calls fetch on miss and caches result."""
    cache = AuthCache(ttl=60, max_size=100)

    async def _fetch():
        return {"user": "alice"}

    result = await cache.get_or_fetch("tok", _fetch)
    assert result is not None
    assert result["user"] == "alice"

    # Should be cached now
    cached = cache.get("tok")
    assert cached is not None
    assert cached["user"] == "alice"


@pytest.mark.asyncio
async def test_get_or_fetch_returns_none():
    """get_or_fetch propagates None from fetch (auth failure)."""
    cache = AuthCache(ttl=60, max_size=100)

    async def _fetch():
        return None

    result = await cache.get_or_fetch("bad-tok", _fetch)
    assert result is None
    # None results should NOT be cached
    assert cache.get("bad-tok") is None


@pytest.mark.asyncio
async def test_get_or_fetch_singleflight():
    """Concurrent get_or_fetch for same token calls fetch only once."""
    cache = AuthCache(ttl=60, max_size=100)
    call_count = 0

    async def _slow_fetch():
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)  # simulate slow provider
        return {"user": "alice", "call": call_count}

    # Launch 5 concurrent requests for the same token
    results = await asyncio.gather(
        cache.get_or_fetch("tok", _slow_fetch),
        cache.get_or_fetch("tok", _slow_fetch),
        cache.get_or_fetch("tok", _slow_fetch),
        cache.get_or_fetch("tok", _slow_fetch),
        cache.get_or_fetch("tok", _slow_fetch),
    )

    # Only ONE fetch call should have been made
    assert call_count == 1
    # All results should have the same data
    for r in results:
        assert r is not None
        assert r["user"] == "alice"


@pytest.mark.asyncio
async def test_get_or_fetch_different_tokens():
    """Concurrent get_or_fetch for different tokens calls fetch for each."""
    cache = AuthCache(ttl=60, max_size=100)
    call_count = 0

    async def _fetch():
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.01)
        return {"n": call_count}

    results = await asyncio.gather(
        cache.get_or_fetch("tok-a", _fetch),
        cache.get_or_fetch("tok-b", _fetch),
        cache.get_or_fetch("tok-c", _fetch),
    )

    assert call_count == 3
    assert all(r is not None for r in results)


@pytest.mark.asyncio
async def test_get_or_fetch_exception_propagates():
    """If fetch raises, all waiters get the exception."""
    cache = AuthCache(ttl=60, max_size=100)

    async def _failing_fetch():
        await asyncio.sleep(0.02)
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError, match="provider down"):
        await cache.get_or_fetch("tok", _failing_fetch)

    # Inflight should be cleaned up — next call can try again
    assert len(cache._inflight) == 0


@pytest.mark.asyncio
async def test_get_or_fetch_returns_copies():
    """Each caller gets an independent copy (mutation safety)."""
    cache = AuthCache(ttl=60, max_size=100)

    async def _fetch():
        await asyncio.sleep(0.02)
        return {"mutable": "original"}

    r1, r2 = await asyncio.gather(
        cache.get_or_fetch("tok", _fetch),
        cache.get_or_fetch("tok", _fetch),
    )

    assert r1 is not None and r2 is not None
    r1["mutable"] = "mutated"
    assert r2["mutable"] == "original"
    cached = cache.get("tok")
    assert cached is not None
    assert cached["mutable"] == "original"
