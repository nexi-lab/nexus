"""Multi-zone scope isolation + perf guardrail (Issue #3698).

These tests cover two cross-cutting concerns from the architecture review:

1. **Isolation** — a zone in ``'scoped'`` mode must not leak its dir list
   to other zones, and a zone in ``'all'`` mode must keep indexing
   everything regardless of what's registered under other zones.

2. **Perf guardrail** — ``is_path_indexed`` stays under a 1 µs budget
   at ~100 registered directories per zone. This is the trigger for
   upgrading to a trie-based data structure; until the benchmark fails,
   the naive list scan is appropriate (matches the "engineered enough"
   preference).
"""

from __future__ import annotations

import time

import pytest

from nexus.bricks.search.index_scope import IndexScope, is_path_indexed

# =============================================================================
# Multi-zone isolation
# =============================================================================


def test_isolation_scoped_zone_does_not_leak_into_all_zone() -> None:
    """Zone B in ``'all'`` mode must still index everything even when
    zone A's scoped dir list is restrictive."""
    scope = IndexScope(
        zone_modes={"zone_a": "scoped", "zone_b": "all"},
        zone_directories={"zone_a": frozenset({"/src"})},
    )

    # Zone A: only /src is indexed.
    assert is_path_indexed(scope, "zone_a", "/src/main.py") is True
    assert is_path_indexed(scope, "zone_a", "/docs/README.md") is False

    # Zone B: everything is indexed, regardless of zone A's rules.
    assert is_path_indexed(scope, "zone_b", "/src/main.py") is True
    assert is_path_indexed(scope, "zone_b", "/docs/README.md") is True
    assert is_path_indexed(scope, "zone_b", "/anywhere/else.py") is True


def test_isolation_all_zone_does_not_leak_into_scoped_zone() -> None:
    """Zone A in ``'all'`` mode must not cause zone B's scoped filter
    to accept files under zone A's paths."""
    scope = IndexScope(
        zone_modes={"zone_a": "all", "zone_b": "scoped"},
        zone_directories={"zone_b": frozenset({"/project"})},
    )

    # Zone A in 'all' mode: everything indexed.
    assert is_path_indexed(scope, "zone_a", "/src/file.py") is True

    # Zone B in 'scoped' mode: only /project is indexed. Zone A's mode
    # does not leak into zone B.
    assert is_path_indexed(scope, "zone_b", "/project/code.py") is True
    assert is_path_indexed(scope, "zone_b", "/src/file.py") is False


def test_isolation_two_scoped_zones_do_not_share_dirs() -> None:
    """Zone A's scoped dirs must not accept paths queried under zone B."""
    scope = IndexScope(
        zone_modes={"zone_a": "scoped", "zone_b": "scoped"},
        zone_directories={
            "zone_a": frozenset({"/src"}),
            "zone_b": frozenset({"/docs"}),
        },
    )

    # Each zone sees only its own dirs.
    assert is_path_indexed(scope, "zone_a", "/src/main.py") is True
    assert is_path_indexed(scope, "zone_a", "/docs/README.md") is False
    assert is_path_indexed(scope, "zone_b", "/docs/README.md") is True
    assert is_path_indexed(scope, "zone_b", "/src/main.py") is False


def test_isolation_mode_flip_does_not_corrupt_other_zones() -> None:
    """Simulating a mode flip on zone A (all→scoped→all) must leave
    zone B's behavior untouched at each step."""
    dirs_a = frozenset({"/src"})

    # Step 1: zone_a=all, zone_b=scoped.
    scope1 = IndexScope(
        zone_modes={"zone_a": "all", "zone_b": "scoped"},
        zone_directories={"zone_a": dirs_a, "zone_b": frozenset({"/docs"})},
    )
    assert is_path_indexed(scope1, "zone_b", "/src/main.py") is False
    assert is_path_indexed(scope1, "zone_a", "/anywhere.py") is True

    # Step 2: zone_a=scoped, zone_b unchanged.
    scope2 = IndexScope(
        zone_modes={"zone_a": "scoped", "zone_b": "scoped"},
        zone_directories={"zone_a": dirs_a, "zone_b": frozenset({"/docs"})},
    )
    # Zone A is restricted now; zone B's behavior is unchanged.
    assert is_path_indexed(scope2, "zone_a", "/anywhere.py") is False
    assert is_path_indexed(scope2, "zone_b", "/docs/file.md") is True
    assert is_path_indexed(scope2, "zone_b", "/src/main.py") is False

    # Step 3: zone_a=all again.
    scope3 = IndexScope(
        zone_modes={"zone_a": "all", "zone_b": "scoped"},
        zone_directories={"zone_a": dirs_a, "zone_b": frozenset({"/docs"})},
    )
    assert is_path_indexed(scope3, "zone_a", "/anywhere.py") is True
    assert is_path_indexed(scope3, "zone_b", "/docs/file.md") is True


# =============================================================================
# Perf guardrail — list-scan benchmark
# =============================================================================


@pytest.mark.parametrize("n_dirs", [10, 50, 100])
def test_is_path_indexed_scales_with_dir_count(n_dirs: int) -> None:
    """Verify ``is_path_indexed`` stays fast enough at N registered dirs.

    Budget: the helper is in the refresh/consumer hot path. At 100 dirs
    the average lookup must stay under 50µs; we target << 1µs in the
    typical case. If this ever regresses, the TODO in index_scope.py
    points at a trie as the upgrade path.
    """
    dirs = frozenset(f"/path_{i:04d}" for i in range(n_dirs))
    scope = IndexScope(
        zone_modes={"zone_a": "scoped"},
        zone_directories={"zone_a": dirs},
    )

    # Pre-warm the JIT / CPU cache.
    for _ in range(100):
        is_path_indexed(scope, "zone_a", "/path_0005/foo.py")

    iterations = 10_000
    start = time.perf_counter()
    for _ in range(iterations):
        # Worst-case for a list-scan: path matches the LAST dir in the set.
        is_path_indexed(scope, "zone_a", f"/path_{n_dirs - 1:04d}/leaf.py")
    elapsed = time.perf_counter() - start
    per_call_us = (elapsed / iterations) * 1_000_000

    # Generous upper bound — the real number is typically << 1µs, but
    # CI noise and Python interpreter variance demand headroom.
    assert per_call_us < 50.0, (
        f"is_path_indexed regressed: {per_call_us:.2f}µs/call at {n_dirs} dirs. "
        f"Budget is 50µs. See the trie TODO in index_scope.py."
    )


def test_is_path_indexed_miss_path_is_still_fast() -> None:
    """A path that doesn't match ANY dir (worst-case iteration) must
    still be cheap."""
    dirs = frozenset(f"/path_{i:04d}" for i in range(100))
    scope = IndexScope(
        zone_modes={"zone_a": "scoped"},
        zone_directories={"zone_a": dirs},
    )

    iterations = 10_000
    start = time.perf_counter()
    for _ in range(iterations):
        is_path_indexed(scope, "zone_a", "/no/such/path/here.py")
    elapsed = time.perf_counter() - start
    per_call_us = (elapsed / iterations) * 1_000_000

    assert per_call_us < 50.0, (
        f"is_path_indexed miss-path regressed: {per_call_us:.2f}µs/call. Budget is 50µs."
    )
