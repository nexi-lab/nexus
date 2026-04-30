"""Regression test for cold-cache path resolution in get_accessible_paths.

When the Tiger bitmap is loaded from L2/L3 but the in-memory resource map
is cold (process restart, eviction), int IDs must still resolve to paths
via DB fallback in get_resource_id() — otherwise the new path-based
visibility code falsely caches False and skips authoritative checks.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("pyroaring")


def test_get_accessible_paths_cold_int_to_uuid_uses_db_fallback():
    """Int IDs missing from in-memory map must be resolved via get_resource_id (DB)."""
    from nexus.bricks.rebac.cache.tiger.bitmap_cache import TigerCache

    cache = TigerCache.__new__(TigerCache)

    resource_map = MagicMock()
    resource_map._int_to_uuid = {}  # cold L1 — empty in-memory map

    def fake_get_resource_id(int_id, conn=None):
        mapping = {
            42: ("file", "/workspace/data/foo.txt"),
            43: ("file", "/workspace/data/bar.txt"),
        }
        return mapping.get(int_id)

    resource_map.get_resource_id.side_effect = fake_get_resource_id
    cache._resource_map = resource_map

    cache.get_accessible_int_ids = MagicMock(return_value={42, 43})

    paths = cache.get_accessible_paths(
        subject_type="user",
        subject_id="alice",
        permission="read",
        resource_type="file",
    )

    assert paths == {"/workspace/data/foo.txt", "/workspace/data/bar.txt"}
    assert resource_map.get_resource_id.call_count == 2


def test_get_accessible_paths_filters_wrong_resource_type():
    """get_resource_id may return entries for other resource_types — filter them out."""
    from nexus.bricks.rebac.cache.tiger.bitmap_cache import TigerCache

    cache = TigerCache.__new__(TigerCache)
    resource_map = MagicMock()
    resource_map._int_to_uuid = {}
    resource_map.get_resource_id.side_effect = lambda iid, conn=None: {
        1: ("file", "/a/file.txt"),
        2: ("group", "/a/group"),  # wrong resource_type — must drop
    }.get(iid)
    cache._resource_map = resource_map
    cache.get_accessible_int_ids = MagicMock(return_value={1, 2})

    paths = cache.get_accessible_paths(
        subject_type="user",
        subject_id="alice",
        permission="read",
        resource_type="file",
    )

    assert paths == {"/a/file.txt"}


def test_get_accessible_paths_returns_none_when_no_bitmap():
    """When get_accessible_int_ids returns None (no bitmap cached), pass through."""
    from nexus.bricks.rebac.cache.tiger.bitmap_cache import TigerCache

    cache = TigerCache.__new__(TigerCache)
    cache._resource_map = MagicMock()
    cache.get_accessible_int_ids = MagicMock(return_value=None)

    paths = cache.get_accessible_paths(
        subject_type="user",
        subject_id="alice",
        permission="read",
        resource_type="file",
    )

    assert paths is None


def test_get_accessible_paths_drops_unresolvable_ids():
    """If get_resource_id returns None (truly orphaned int_id), drop silently."""
    from nexus.bricks.rebac.cache.tiger.bitmap_cache import TigerCache

    cache = TigerCache.__new__(TigerCache)
    resource_map = MagicMock()
    resource_map._int_to_uuid = {}
    resource_map.get_resource_id.side_effect = lambda iid, conn=None: {
        1: ("file", "/a/file.txt"),
        # int_id 2 unresolvable (DB returns None too)
    }.get(iid)
    cache._resource_map = resource_map
    cache.get_accessible_int_ids = MagicMock(return_value={1, 2})

    paths = cache.get_accessible_paths(
        subject_type="user",
        subject_id="alice",
        permission="read",
        resource_type="file",
    )

    assert paths == {"/a/file.txt"}
