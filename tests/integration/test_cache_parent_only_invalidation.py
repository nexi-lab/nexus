"""Integration test: writing /a/b/new.txt invalidates /a/b/'s listing only."""

from __future__ import annotations

from nexus.cache.index_store import IndexKey, MemoryIndexCache


def test_parent_only_invalidation_does_not_touch_grandparent():
    cache = MemoryIndexCache()
    a_listing = IndexKey("path_local", "default", "/a", "listing")
    a_b_listing = IndexKey("path_local", "default", "/a/b", "listing")

    cache.put(a_listing, ["b"], ttl_seconds=600)
    cache.put(a_b_listing, ["existing.txt"], ttl_seconds=600)

    cache.invalidate_parent_listing("path_local", "default", "/a/b/new.txt")

    assert cache.get(a_b_listing) is None
    assert cache.get(a_listing) == ["b"]


def test_parent_only_invalidation_root_file():
    cache = MemoryIndexCache()
    root_listing = IndexKey("path_local", "default", "/", "listing")
    cache.put(root_listing, ["foo.txt"], ttl_seconds=600)

    cache.invalidate_parent_listing("path_local", "default", "/new.txt")
    assert cache.get(root_listing) is None


def test_parent_only_invalidation_trailing_slash_dir():
    """rmdir of /a/b/ should invalidate listing of /a, not of /a/b."""
    cache = MemoryIndexCache()
    a_listing = IndexKey("path_local", "default", "/a", "listing")
    a_b_listing = IndexKey("path_local", "default", "/a/b", "listing")
    cache.put(a_listing, ["b"], ttl_seconds=600)
    cache.put(a_b_listing, [], ttl_seconds=600)

    cache.invalidate_parent_listing("path_local", "default", "/a/b/")

    assert cache.get(a_listing) is None
    assert cache.get(a_b_listing) == []


def test_rename_documents_caller_contract():
    """Cross-dir rename must be issued as two invalidations by the caller."""
    cache = MemoryIndexCache()
    a_listing = IndexKey("path_local", "default", "/a", "listing")
    b_listing = IndexKey("path_local", "default", "/b", "listing")
    cache.put(a_listing, ["x.txt"], ttl_seconds=600)
    cache.put(b_listing, [], ttl_seconds=600)

    cache.invalidate_parent_listing("path_local", "default", "/a/x.txt")
    assert cache.get(a_listing) is None
    assert cache.get(b_listing) == []

    cache.invalidate_parent_listing("path_local", "default", "/b/x.txt")
    assert cache.get(b_listing) is None
