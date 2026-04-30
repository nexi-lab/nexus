"""Perf regression guard for prefix helpers (Issue #3951).

Not a microbenchmark — asserts that large-scale calls complete within
generous wall-clock bounds. Run in CI with: pytest tests/unit/rebac/bench_prefix_helpers.py -v
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("pyroaring")


def _make_paths(n: int) -> list[str]:
    """Generate n distinct file paths under /workspace/."""
    return [f"/workspace/user_{i % 1000}/project_{i // 1000}/file_{i}.txt" for i in range(n)]


def _make_prefixes(n: int) -> list[str]:
    """Generate n distinct directory prefixes."""
    return [f"/workspace/user_{i}" for i in range(n)]


# ---------------------------------------------------------------------------
# any_path_under_prefix
# ---------------------------------------------------------------------------


def test_any_path_under_prefix_50k_paths_under_500ms():
    from nexus.bricks.rebac.cache._prefix_helpers import any_path_under_prefix

    paths = _make_paths(50_000)
    prefix = "/workspace/user_999"  # match near the end

    start = time.perf_counter()
    result = any_path_under_prefix(paths, prefix)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert result is True
    assert elapsed_ms < 500, (
        f"any_path_under_prefix over 50K paths took {elapsed_ms:.1f}ms (limit 500ms)"
    )


def test_any_path_under_prefix_python_fallback_50k_paths_under_500ms(monkeypatch):
    import nexus.bricks.rebac.cache._prefix_helpers as ph

    monkeypatch.setattr(ph, "_rust_any", None)

    paths = _make_paths(50_000)
    prefix = "/workspace/user_999"

    start = time.perf_counter()
    result = ph.any_path_under_prefix(paths, prefix)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert result is True
    assert elapsed_ms < 500, f"Python fallback over 50K paths took {elapsed_ms:.1f}ms (limit 500ms)"


# ---------------------------------------------------------------------------
# batch_paths_under_prefixes
# ---------------------------------------------------------------------------


def test_batch_paths_under_prefixes_100k_paths_50_prefixes_under_500ms():
    from nexus.bricks.rebac.cache._prefix_helpers import batch_paths_under_prefixes

    paths = _make_paths(100_000)
    prefixes = _make_prefixes(50)

    start = time.perf_counter()
    results = batch_paths_under_prefixes(paths, prefixes)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert len(results) == 50
    assert elapsed_ms < 500, (
        f"batch_paths_under_prefixes 100K×50 took {elapsed_ms:.1f}ms (limit 500ms)"
    )


def test_batch_paths_under_prefixes_python_fallback_100k_paths_50_prefixes_under_2000ms(
    monkeypatch,
):
    import nexus.bricks.rebac.cache._prefix_helpers as ph

    monkeypatch.setattr(ph, "_rust_batch", None)

    paths = _make_paths(100_000)
    prefixes = _make_prefixes(50)

    start = time.perf_counter()
    results = ph.batch_paths_under_prefixes(paths, prefixes)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert len(results) == 50
    # Python fallback is O(N×M) — generous 2s limit to avoid CI flakiness
    assert elapsed_ms < 2000, f"Python fallback 100K×50 took {elapsed_ms:.1f}ms (limit 2000ms)"
