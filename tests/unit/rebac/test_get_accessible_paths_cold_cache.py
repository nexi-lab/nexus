"""Regression test for cold-cache path resolution in get_accessible_paths.

When the Tiger bitmap is loaded from L2/L3 but the in-memory resource map
is cold (process restart, eviction), int IDs must still resolve to paths
via DB fallback in bulk_get_resource_ids() — otherwise the new path-based
visibility code falsely caches False and skips authoritative checks.

The fallback must also use a single batched DB query (not N per-id round
trips) so a large bitmap doesn't saturate the DB on hot auth paths.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("pyroaring")


def test_get_accessible_paths_cold_int_to_uuid_uses_bulk_fallback():
    """Cold in-memory map must resolve all int IDs via bulk_get_resource_ids."""
    from nexus.bricks.rebac.cache.tiger.bitmap_cache import TigerCache

    cache = TigerCache.__new__(TigerCache)

    resource_map = MagicMock()
    resource_map._int_to_uuid = {}  # cold L1 — empty in-memory map

    resource_map.bulk_get_resource_ids.return_value = {
        42: ("file", "/workspace/data/foo.txt"),
        43: ("file", "/workspace/data/bar.txt"),
    }
    cache._resource_map = resource_map
    cache.get_accessible_int_ids = MagicMock(return_value={42, 43})

    paths = cache.get_accessible_paths(
        subject_type="user",
        subject_id="alice",
        permission="read",
        resource_type="file",
    )

    assert paths == {"/workspace/data/foo.txt", "/workspace/data/bar.txt"}
    # Must be a single bulk call — not N per-id calls
    assert resource_map.bulk_get_resource_ids.call_count == 1


def test_get_accessible_paths_filters_wrong_resource_type():
    """bulk_get_resource_ids may include other resource_types — filter them out."""
    from nexus.bricks.rebac.cache.tiger.bitmap_cache import TigerCache

    cache = TigerCache.__new__(TigerCache)
    resource_map = MagicMock()
    resource_map.bulk_get_resource_ids.return_value = {
        1: ("file", "/a/file.txt"),
        2: ("group", "/a/group"),  # wrong resource_type — must drop
    }
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
    cache._resource_map.bulk_get_resource_ids.assert_not_called()


def test_get_accessible_paths_drops_unresolvable_ids():
    """If bulk_get_resource_ids omits an int_id (truly orphaned), drop silently."""
    from nexus.bricks.rebac.cache.tiger.bitmap_cache import TigerCache

    cache = TigerCache.__new__(TigerCache)
    resource_map = MagicMock()
    # int_id 2 omitted — DB also has no row for it
    resource_map.bulk_get_resource_ids.return_value = {1: ("file", "/a/file.txt")}
    cache._resource_map = resource_map
    cache.get_accessible_int_ids = MagicMock(return_value={1, 2})

    paths = cache.get_accessible_paths(
        subject_type="user",
        subject_id="alice",
        permission="read",
        resource_type="file",
    )

    assert paths == {"/a/file.txt"}


def test_get_accessible_paths_large_bitmap_single_bulk_call():
    """50K cold int IDs must produce one bulk_get_resource_ids call, not 50K."""
    from nexus.bricks.rebac.cache.tiger.bitmap_cache import TigerCache

    cache = TigerCache.__new__(TigerCache)
    resource_map = MagicMock()
    resource_map.bulk_get_resource_ids.return_value = {
        i: ("file", f"/workspace/file_{i}.txt") for i in range(50_000)
    }
    cache._resource_map = resource_map
    cache.get_accessible_int_ids = MagicMock(return_value=set(range(50_000)))

    paths = cache.get_accessible_paths(
        subject_type="user",
        subject_id="alice",
        permission="read",
        resource_type="file",
    )

    assert len(paths) == 50_000
    # Hot-path invariant: one bulk call regardless of bitmap size
    assert resource_map.bulk_get_resource_ids.call_count == 1


def test_bulk_get_resource_ids_uses_memory_cache_when_warm():
    """When _int_to_uuid is fully populated, bulk_get_resource_ids must skip DB."""
    from sqlalchemy import create_engine

    from nexus.bricks.rebac.cache.tiger.resource_map import TigerResourceMap

    engine = create_engine("sqlite:///:memory:")
    rmap = TigerResourceMap(engine)
    rmap._int_to_uuid = {
        1: ("file", "/a"),
        2: ("file", "/b"),
        3: ("file", "/c"),
    }
    rmap._uuid_to_int = {v: k for k, v in rmap._int_to_uuid.items()}

    # No connection — would crash if it tried to hit DB
    result = rmap.bulk_get_resource_ids({1, 2, 3})

    assert result == {1: ("file", "/a"), 2: ("file", "/b"), 3: ("file", "/c")}


def test_bulk_get_resource_ids_empty_input():
    """Empty input must return empty dict without DB access."""
    from sqlalchemy import create_engine

    from nexus.bricks.rebac.cache.tiger.resource_map import TigerResourceMap

    engine = create_engine("sqlite:///:memory:")
    rmap = TigerResourceMap(engine)

    assert rmap.bulk_get_resource_ids(set()) == {}
    assert rmap.bulk_get_resource_ids([]) == {}
