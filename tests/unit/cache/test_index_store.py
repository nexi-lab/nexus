from nexus.cache.index_store import IndexKey, MemoryIndexCache
from nexus.cache.policy import negative_ttl_for_backend


def test_memory_index_cache_expires_positive_entry() -> None:
    now = [100.0]
    cache = MemoryIndexCache(now_fn=lambda: now[0])
    key = IndexKey("path_s3", "zone1", "/bucket/foo", "listing")

    cache.put(key, [".", "..", "a.txt"], ttl_seconds=5)
    assert cache.get(key) == [".", "..", "a.txt"]

    now[0] += 6
    assert cache.get(key) is None


def test_memory_index_cache_invalidates_only_parent_listing() -> None:
    cache = MemoryIndexCache(now_fn=lambda: 100.0)
    root_key = IndexKey("path_s3", "zone1", "/a", "listing")
    child_key = IndexKey("path_s3", "zone1", "/a/b", "listing")

    cache.put(root_key, [".", "..", "b"], ttl_seconds=60)
    cache.put(child_key, [".", "..", "c.txt"], ttl_seconds=60)

    cache.invalidate_parent_listing("path_s3", "zone1", "/a/b/c.txt")

    assert cache.get(root_key) == [".", "..", "b"]
    assert cache.get(child_key) is None


def test_memory_index_cache_uses_literal_relative_parent_listing_key() -> None:
    cache = MemoryIndexCache(now_fn=lambda: 100.0)
    relative_parent_key = IndexKey("path_s3", "zone1", ".", "listing")
    root_key = IndexKey("path_s3", "zone1", "/", "listing")

    cache.put(relative_parent_key, [".", "..", "file.txt"], ttl_seconds=60)
    cache.put(root_key, [".", "..", "file.txt"], ttl_seconds=60)

    cache.invalidate_parent_listing("path_s3", "zone1", "file.txt")

    assert cache.get(relative_parent_key) is None
    assert cache.get(root_key) == [".", "..", "file.txt"]


def test_memory_index_cache_expires_negative_entry_with_short_ttl() -> None:
    now = [100.0]
    cache = MemoryIndexCache(now_fn=lambda: now[0])
    key = IndexKey("path_s3", "zone1", "/bucket/missing.txt", "negative")

    cache.put(key, {"missing": True}, ttl_seconds=negative_ttl_for_backend("path_s3"))
    assert cache.get(key) == {"missing": True}

    now[0] += 6
    assert cache.get(key) is None
