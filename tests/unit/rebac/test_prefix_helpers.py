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


def test_any_path_under_prefix_python_fallback(monkeypatch):
    import nexus.bricks.rebac.cache._prefix_helpers as ph

    monkeypatch.setattr(ph, "_rust_any", None)
    assert ph.any_path_under_prefix(["/a/b/c"], "/a/b") is True
    assert ph.any_path_under_prefix(["/a/bc"], "/a/b") is False


def test_batch_paths_under_prefixes_python_fallback(monkeypatch):
    import nexus.bricks.rebac.cache._prefix_helpers as ph

    monkeypatch.setattr(ph, "_rust_batch", None)
    result = ph.batch_paths_under_prefixes(["/a/b/c"], ["/a/b", "/z"])
    assert result == [True, False]


def test_python_fallback_no_partial_match(monkeypatch):
    import nexus.bricks.rebac.cache._prefix_helpers as ph

    monkeypatch.setattr(ph, "_rust_any", None)
    assert ph.any_path_under_prefix(["/a/bc"], "/a/b") is False
