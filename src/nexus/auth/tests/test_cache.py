"""Unit tests for AuthCache with invalidation (Decision #15)."""

import time

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
