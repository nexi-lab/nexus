"""Unit tests for rebac cache prefix helpers (Issue #3951)."""

from __future__ import annotations

import pytest

pytest.importorskip("pyroaring")  # matches rebac test convention


# ---------------------------------------------------------------------------
# any_path_under_prefix
# ---------------------------------------------------------------------------


def test_any_path_under_prefix_descendant():
    from nexus.bricks.rebac.cache._prefix_helpers import any_path_under_prefix

    assert any_path_under_prefix(["/a/b/c", "/x/y"], "/a/b") is True


def test_any_path_under_prefix_exact_match():
    from nexus.bricks.rebac.cache._prefix_helpers import any_path_under_prefix

    assert any_path_under_prefix(["/a/b", "/x/y"], "/a/b") is True


def test_any_path_under_prefix_no_match():
    from nexus.bricks.rebac.cache._prefix_helpers import any_path_under_prefix

    assert any_path_under_prefix(["/x/y", "/z"], "/a/b") is False


def test_any_path_under_prefix_root():
    from nexus.bricks.rebac.cache._prefix_helpers import any_path_under_prefix

    assert any_path_under_prefix(["/a/b"], "/") is True


def test_any_path_under_prefix_trailing_slash_prefix():
    from nexus.bricks.rebac.cache._prefix_helpers import any_path_under_prefix

    assert any_path_under_prefix(["/a/b/c"], "/a/b/") is True


def test_any_path_under_prefix_set_input():
    from nexus.bricks.rebac.cache._prefix_helpers import any_path_under_prefix

    assert any_path_under_prefix({"/a/b/c"}, "/a/b") is True


def test_any_path_under_prefix_empty_paths():
    from nexus.bricks.rebac.cache._prefix_helpers import any_path_under_prefix

    assert any_path_under_prefix([], "/a/b") is False


def test_any_path_under_prefix_no_partial_match():
    from nexus.bricks.rebac.cache._prefix_helpers import any_path_under_prefix

    # "/a/bc" must NOT match prefix "/a/b"
    assert any_path_under_prefix(["/a/bc"], "/a/b") is False


# ---------------------------------------------------------------------------
# batch_paths_under_prefixes
# ---------------------------------------------------------------------------


def test_batch_paths_under_prefixes_basic():
    from nexus.bricks.rebac.cache._prefix_helpers import batch_paths_under_prefixes

    result = batch_paths_under_prefixes(["/a/b/c", "/x/y"], ["/a/b", "/z"])
    assert result == [True, False]


def test_batch_paths_under_prefixes_order_preserved():
    from nexus.bricks.rebac.cache._prefix_helpers import batch_paths_under_prefixes

    result = batch_paths_under_prefixes(["/a/b/c"], ["/z", "/a", "/b"])
    assert result == [False, True, False]


def test_batch_paths_under_prefixes_empty_paths():
    from nexus.bricks.rebac.cache._prefix_helpers import batch_paths_under_prefixes

    assert batch_paths_under_prefixes([], ["/a", "/b"]) == [False, False]


def test_batch_paths_under_prefixes_empty_prefixes():
    from nexus.bricks.rebac.cache._prefix_helpers import batch_paths_under_prefixes

    assert batch_paths_under_prefixes(["/a/b"], []) == []


def test_batch_paths_under_prefixes_result_length_matches_prefixes():
    from nexus.bricks.rebac.cache._prefix_helpers import batch_paths_under_prefixes

    prefixes = ["/a", "/b", "/c", "/d"]
    result = batch_paths_under_prefixes(["/a/x"], prefixes)
    assert len(result) == len(prefixes)


# ---------------------------------------------------------------------------
# Python fallback paths (mock Rust to None)
# ---------------------------------------------------------------------------


def test_any_path_under_prefix_python_fallback():
    """any_path_under_prefix is intentionally Python-only (round 8) for early
    exit; this test pins the semantic against any future Rust regression."""
    import nexus.bricks.rebac.cache._prefix_helpers as ph

    assert ph.any_path_under_prefix(["/a/b/c"], "/a/b") is True
    assert ph.any_path_under_prefix(["/a/bc"], "/a/b") is False


def test_batch_paths_under_prefixes_python_fallback(monkeypatch):
    import nexus.bricks.rebac.cache._prefix_helpers as ph

    monkeypatch.setattr(ph, "_rust_batch", None)
    result = ph.batch_paths_under_prefixes(["/a/b/c"], ["/a/b", "/z"])
    assert result == [True, False]


def test_python_no_partial_match():
    """Confirm "/a/bc" does NOT match prefix "/a/b" (descendant match needs /)."""
    import nexus.bricks.rebac.cache._prefix_helpers as ph

    assert ph.any_path_under_prefix(["/a/bc"], "/a/b") is False


# ---------------------------------------------------------------------------
# DirectoryVisibilityCache.compute_from_tiger_bitmap — refactor contract
# ---------------------------------------------------------------------------


def test_compute_from_tiger_bitmap_calls_get_accessible_paths():
    """After refactor, method must use get_accessible_paths (not get_accessible_resources)."""
    from unittest.mock import MagicMock

    from nexus.bricks.rebac.cache.visibility import DirectoryVisibilityCache

    tiger_cache = MagicMock()
    tiger_cache.get_accessible_paths.return_value = {"/a/b/c", "/a/b/d"}
    # If old code runs it calls get_accessible_resources — return empty set so
    # the wrong path produces the wrong (False) answer.
    tiger_cache.get_accessible_resources.return_value = set()

    cache = DirectoryVisibilityCache(tiger_cache=tiger_cache)
    result = cache.compute_from_tiger_bitmap("z1", "user", "u1", "/a/b", "read")

    assert result is True
    tiger_cache.get_accessible_paths.assert_called_once_with(
        subject_type="user",
        subject_id="u1",
        permission="read",
        resource_type="file",
        zone_id="z1",
    )


def test_compute_from_tiger_bitmap_cache_miss_returns_none():
    from unittest.mock import MagicMock

    from nexus.bricks.rebac.cache.visibility import DirectoryVisibilityCache

    tiger_cache = MagicMock()
    tiger_cache.get_accessible_paths.return_value = None

    cache = DirectoryVisibilityCache(tiger_cache=tiger_cache)
    result = cache.compute_from_tiger_bitmap("z1", "user", "u1", "/a/b", "read")
    assert result is None


def test_compute_from_tiger_bitmap_no_accessible_returns_false():
    from unittest.mock import MagicMock

    from nexus.bricks.rebac.cache.visibility import DirectoryVisibilityCache

    tiger_cache = MagicMock()
    tiger_cache.get_accessible_paths.return_value = set()

    cache = DirectoryVisibilityCache(tiger_cache=tiger_cache)
    result = cache.compute_from_tiger_bitmap("z1", "user", "u1", "/a/b", "read")
    assert result is False


def test_compute_from_tiger_bitmap_no_descendants_returns_false():
    from unittest.mock import MagicMock

    from nexus.bricks.rebac.cache.visibility import DirectoryVisibilityCache

    tiger_cache = MagicMock()
    tiger_cache.get_accessible_paths.return_value = {"/x/y/z"}

    cache = DirectoryVisibilityCache(tiger_cache=tiger_cache)
    result = cache.compute_from_tiger_bitmap("z1", "user", "u1", "/a/b", "read")
    assert result is False


# ---------------------------------------------------------------------------
# DirectoryVisibilityCache.compute_batch_visibility — refactor contract
# ---------------------------------------------------------------------------


def test_compute_batch_visibility_correct_results():
    from unittest.mock import MagicMock

    from nexus.bricks.rebac.cache.visibility import DirectoryVisibilityCache

    tiger_cache = MagicMock()
    tiger_cache.get_accessible_paths.return_value = {"/a/b/c", "/x/y/z"}
    tiger_cache.get_accessible_resources.return_value = set()  # wrong result if called

    cache = DirectoryVisibilityCache(tiger_cache=tiger_cache)
    result = cache.compute_batch_visibility("z1", "user", "u1", ["/a/b", "/x/y", "/nope"], "read")

    assert result == {"/a/b": True, "/x/y": True, "/nope": False}


def test_compute_batch_visibility_cache_miss_returns_empty():
    from unittest.mock import MagicMock

    from nexus.bricks.rebac.cache.visibility import DirectoryVisibilityCache

    tiger_cache = MagicMock()
    tiger_cache.get_accessible_paths.return_value = None

    cache = DirectoryVisibilityCache(tiger_cache=tiger_cache)
    result = cache.compute_batch_visibility("z1", "user", "u1", ["/a/b"], "read")
    assert result == {}


def test_compute_batch_visibility_no_tiger_cache_returns_empty():
    from nexus.bricks.rebac.cache.visibility import DirectoryVisibilityCache

    cache = DirectoryVisibilityCache(tiger_cache=None)
    result = cache.compute_batch_visibility("z1", "user", "u1", ["/a/b"], "read")
    assert result == {}


# ---------------------------------------------------------------------------
# DescendantAccessChecker.has_access — Tiger fallback uses get_accessible_paths
# ---------------------------------------------------------------------------


def test_has_access_tiger_fallback_uses_get_accessible_paths():
    """Tiger fallback must call get_accessible_paths, not loop _resource_map."""
    from unittest.mock import MagicMock

    from nexus.services.namespace.descendant_access import DescendantAccessChecker

    tiger_cache = MagicMock()
    tiger_cache.get_accessible_paths.return_value = {"/workspace/joe/file.txt"}

    # spec=[] means only explicitly-set attributes exist.
    # hasattr() returns False for everything else, so the faster optimisation
    # paths (tiger_check_access, rebac_check_bulk, rebac_check_bulk_sync,
    # tiger_get_accessible_resources) are all skipped, letting the code reach
    # the Tiger Cache bitmap fallback at line 305.
    rebac_manager = MagicMock(spec=[])
    rebac_manager._tiger_cache = tiger_cache

    rebac_service = MagicMock(spec=[])
    rebac_service.rebac_check_sync = MagicMock(return_value=False)  # direct access denied

    ctx = MagicMock()
    ctx.is_admin = False
    ctx.is_system = False
    ctx.subject_id = "joe"
    ctx.subject_type = "user"
    ctx.zone_id = "z1"

    metadata_store = MagicMock()
    metadata_store.list.return_value = []

    checker = DescendantAccessChecker(
        rebac_manager=rebac_manager,
        rebac_service=rebac_service,
        dir_visibility_cache=None,
        permission_enforcer=MagicMock(),
        metadata_store=metadata_store,
    )

    from nexus.contracts.types import Permission

    result = checker.has_access("/workspace/joe", Permission.READ, ctx)

    assert result is True
    tiger_cache.get_accessible_paths.assert_called_once()
