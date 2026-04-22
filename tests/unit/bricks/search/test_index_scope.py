"""Unit tests for the per-zone index scope filter (Issue #3698).

These tests are the canonical spec for ``is_path_indexed``. Each row in the
parametrized table corresponds to one of the 10 matching rules from the
architecture review. If you change the helper, update the table first.

Rules under test:
    1. Unknown zone (not registered anywhere) → True (backward compat
       fallback, in case the daemon starts before zones are loaded).
    2. Mode 'all'     → True for every path.
    3. Mode 'scoped' with empty directory set → False (explicit opt-in
       means nothing when nothing is registered).
    4. Exact directory match → True.
    5. Descendant match (path is strictly under the directory) → True.
    6. Prefix-but-not-descendant (``/src`` must NOT match ``/srcX/foo``) → False.
    7. Trailing slash in stored dir is canonicalized to no trailing slash;
       the helper assumes dirs are canonical on input.
    8. Wrong zone (dir registered under zone A, querying zone B with
       scoped mode + empty set) → False.
    9. Overlapping directories (both ``/src`` and ``/src/lib`` registered;
       ``/src/lib/x.py`` matches both; result is still True).
    10. Contract violations (relative path, empty path, empty zone) → raise.
"""

from __future__ import annotations

import pytest

from nexus.bricks.search.index_scope import IndexScope, is_path_indexed


def _scope(
    *,
    zone_modes: dict[str, str] | None = None,
    zone_directories: dict[str, set[str]] | None = None,
) -> IndexScope:
    """Test helper — build an IndexScope snapshot from plain dicts."""
    return IndexScope(
        zone_modes=dict(zone_modes or {}),
        zone_directories={zone: frozenset(dirs) for zone, dirs in (zone_directories or {}).items()},
    )


# =============================================================================
# Rule matrix — the canonical spec
# =============================================================================


@pytest.mark.parametrize(
    "case_id,scope,zone_id,virtual_path,expected",
    [
        # Rule 1 — Unknown zone falls back to 'all' (backward compat).
        (
            "unknown-zone-fallback-all",
            _scope(zone_modes={}, zone_directories={}),
            "never-seen",
            "/foo/bar.txt",
            True,
        ),
        # Rule 2 — Mode 'all' means everything is indexed.
        (
            "mode-all-no-dirs",
            _scope(zone_modes={"zone_a": "all"}, zone_directories={}),
            "zone_a",
            "/foo/bar.txt",
            True,
        ),
        (
            "mode-all-with-dirs-still-true",
            _scope(
                zone_modes={"zone_a": "all"},
                zone_directories={"zone_a": {"/src"}},
            ),
            "zone_a",
            "/docs/README.md",  # not under /src but mode is all
            True,
        ),
        # Rule 3 — Mode 'scoped' with empty set means nothing is indexed.
        (
            "mode-scoped-empty-set",
            _scope(zone_modes={"zone_a": "scoped"}, zone_directories={}),
            "zone_a",
            "/foo/bar.txt",
            False,
        ),
        (
            "mode-scoped-missing-zone-key",
            _scope(
                zone_modes={"zone_a": "scoped"},
                zone_directories={"other_zone": {"/src"}},  # zone_a key missing
            ),
            "zone_a",
            "/src/foo.py",
            False,
        ),
        # Rule 4 — Exact directory match.
        (
            "exact-dir-match",
            _scope(
                zone_modes={"zone_a": "scoped"},
                zone_directories={"zone_a": {"/src"}},
            ),
            "zone_a",
            "/src",
            True,
        ),
        # Rule 5 — Descendant match.
        (
            "descendant-match",
            _scope(
                zone_modes={"zone_a": "scoped"},
                zone_directories={"zone_a": {"/src"}},
            ),
            "zone_a",
            "/src/lib/foo.py",
            True,
        ),
        (
            "descendant-direct-child",
            _scope(
                zone_modes={"zone_a": "scoped"},
                zone_directories={"zone_a": {"/src"}},
            ),
            "zone_a",
            "/src/foo.py",
            True,
        ),
        # Rule 6 — Prefix-but-not-descendant bug guard.
        # /srcX/foo MUST NOT match /src even though startswith('/src') is True.
        (
            "prefix-not-descendant-bug-guard",
            _scope(
                zone_modes={"zone_a": "scoped"},
                zone_directories={"zone_a": {"/src"}},
            ),
            "zone_a",
            "/srcX/foo.py",
            False,
        ),
        (
            "prefix-not-descendant-sibling",
            _scope(
                zone_modes={"zone_a": "scoped"},
                zone_directories={"zone_a": {"/project/src"}},
            ),
            "zone_a",
            "/project/srcextra/foo.py",
            False,
        ),
        # Rule 7 — Wrong zone does not leak across zone boundary.
        (
            "wrong-zone-no-leak",
            _scope(
                zone_modes={"zone_a": "scoped", "zone_b": "scoped"},
                zone_directories={"zone_a": {"/src"}},  # zone_b is scoped w/ no dirs
            ),
            "zone_b",
            "/src/foo.py",
            False,
        ),
        # Rule 8 — Overlapping directories: both match, result is still True.
        (
            "overlapping-dirs-both-match",
            _scope(
                zone_modes={"zone_a": "scoped"},
                zone_directories={"zone_a": {"/src", "/src/lib"}},
            ),
            "zone_a",
            "/src/lib/foo.py",
            True,
        ),
        (
            "overlapping-dirs-only-parent-match",
            _scope(
                zone_modes={"zone_a": "scoped"},
                zone_directories={"zone_a": {"/src", "/src/lib"}},
            ),
            "zone_a",
            "/src/other/foo.py",
            True,
        ),
        # Multi-zone sanity: zone_b is in 'all' mode, zone_a is scoped.
        (
            "multi-zone-mixed-modes-zone_a-scoped",
            _scope(
                zone_modes={"zone_a": "scoped", "zone_b": "all"},
                zone_directories={"zone_a": {"/src"}},
            ),
            "zone_a",
            "/docs/README.md",
            False,
        ),
        (
            "multi-zone-mixed-modes-zone_b-all",
            _scope(
                zone_modes={"zone_a": "scoped", "zone_b": "all"},
                zone_directories={"zone_a": {"/src"}},
            ),
            "zone_b",
            "/docs/README.md",
            True,
        ),
    ],
)
def test_is_path_indexed_matrix(
    case_id: str,
    scope: IndexScope,
    zone_id: str,
    virtual_path: str,
    expected: bool,
) -> None:
    assert is_path_indexed(scope, zone_id, virtual_path) is expected, case_id


# =============================================================================
# Rule 10 — Contract violations must raise, not return False silently.
# =============================================================================


def test_is_path_indexed_rejects_relative_path() -> None:
    scope = _scope(zone_modes={"zone_a": "all"})
    with pytest.raises(ValueError, match="absolute"):
        is_path_indexed(scope, "zone_a", "foo/bar.py")


def test_is_path_indexed_rejects_empty_path() -> None:
    scope = _scope(zone_modes={"zone_a": "all"})
    with pytest.raises(ValueError, match="empty"):
        is_path_indexed(scope, "zone_a", "")


def test_is_path_indexed_rejects_empty_zone() -> None:
    scope = _scope(zone_modes={"zone_a": "all"})
    with pytest.raises(ValueError, match="zone_id"):
        is_path_indexed(scope, "", "/foo/bar.py")


def test_is_path_indexed_rejects_zone_prefixed_path() -> None:
    """Callers must strip /zone/{id}/ before calling the helper."""
    scope = _scope(zone_modes={"zone_a": "all"})
    with pytest.raises(ValueError, match="canonical"):
        is_path_indexed(scope, "zone_a", "/zone/zone_a/src/foo.py")


# =============================================================================
# Canonicalization helper — directory paths are stored without trailing slash.
# =============================================================================


def test_is_path_indexed_does_not_canonicalize_trailing_slash_input() -> None:
    """The helper assumes input dirs are already canonical.

    Callers (CRUD endpoint handlers) are responsible for stripping trailing
    slashes before writing to the DB. The helper itself does NOT canonicalize
    because it runs in a tight loop on the mutation hot path.

    This test documents the contract: if a non-canonical dir ('/src/') ends
    up in the scope, /src/foo.py will NOT match. The match rule compares to
    '/src/' + '/' = '/src//' which does not prefix '/src/foo.py'.
    """
    scope = _scope(
        zone_modes={"zone_a": "scoped"},
        zone_directories={"zone_a": {"/src/"}},  # non-canonical
    )
    # Non-canonical dir is a caller bug; the helper surfaces the bug as a miss.
    assert is_path_indexed(scope, "zone_a", "/src/foo.py") is False


# =============================================================================
# Root-dir special case — '/' is its own canonical form and matches everything.
# =============================================================================


def test_is_path_indexed_root_dir_matches_all() -> None:
    scope = _scope(
        zone_modes={"zone_a": "scoped"},
        zone_directories={"zone_a": {"/"}},
    )
    assert is_path_indexed(scope, "zone_a", "/foo/bar.py") is True
    assert is_path_indexed(scope, "zone_a", "/") is True
    assert is_path_indexed(scope, "zone_a", "/deep/nested/path.txt") is True
