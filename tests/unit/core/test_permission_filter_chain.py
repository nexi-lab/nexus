"""Tests for permission filter chain strategies (Issue #899).

Tests the composable filter strategy chain used by filter_list() to decompose
permission checks into TigerBitmap -> LeopardIndex -> HierarchyPreFilter ->
ZonePreFilter -> BulkReBAC stages.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.core.permissions import OperationContext
from nexus.services.permissions.permission_filter_chain import (
    BulkReBACStrategy,
    FilterContext,
    FilterResult,
    HierarchyPreFilterStrategy,
    LeopardIndexStrategy,
    TigerBitmapStrategy,
    ZonePreFilterStrategy,
    run_filter_chain,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_cache():
    cache = MagicMock()
    cache.try_bitmap_filter.return_value = None
    cache.is_bitmap_complete.return_value = False
    cache.try_leopard_lookup.return_value = ([], [])
    cache.record_accessible_dirs = MagicMock()
    cache.mark_bitmap_complete = MagicMock()
    return cache


@pytest.fixture
def mock_rebac():
    rebac = MagicMock()
    rebac.rebac_check_bulk.return_value = {}
    return rebac


@pytest.fixture
def ctx(mock_cache, mock_rebac):
    context = OperationContext(user="alice", groups=[], zone_id="default")
    return FilterContext(
        paths=["/workspace/a.txt", "/workspace/b.txt"],
        subject=("user", "alice"),
        zone_id="default",
        context=context,
        cache=mock_cache,
        rebac_manager=mock_rebac,
    )


def _make_ctx(
    paths: list[str],
    mock_cache: MagicMock,
    mock_rebac: MagicMock,
    zone_id: str = "default",
    router: object | None = None,
) -> FilterContext:
    """Helper to build a FilterContext with custom paths."""
    context = OperationContext(user="alice", groups=[], zone_id=zone_id)
    return FilterContext(
        paths=paths,
        subject=("user", "alice"),
        zone_id=zone_id,
        context=context,
        cache=mock_cache,
        rebac_manager=mock_rebac,
        router=router,
    )


# ---------------------------------------------------------------------------
# Strategy 1: TigerBitmapStrategy
# ---------------------------------------------------------------------------


class TestTigerBitmapStrategy:
    """Tests for TigerBitmapStrategy — O(1) bitmap cache filtering."""

    def test_bitmap_hit(self, ctx):
        """When the bitmap cache returns a split, the strategy returns allowed + remaining."""
        ctx.cache.try_bitmap_filter.return_value = (
            ["/workspace/a.txt"],
            ["/workspace/b.txt"],
        )

        strategy = TigerBitmapStrategy()
        result = strategy.apply(ctx, list(ctx.paths))

        assert result.allowed == ["/workspace/a.txt"]
        assert result.remaining == ["/workspace/b.txt"]
        assert result.short_circuit is False

    def test_bitmap_miss(self, ctx):
        """When the bitmap cache returns None, all paths pass through as remaining."""
        ctx.cache.try_bitmap_filter.return_value = None

        strategy = TigerBitmapStrategy()
        result = strategy.apply(ctx, list(ctx.paths))

        assert result.allowed == []
        assert result.remaining == list(ctx.paths)
        assert result.short_circuit is False

    def test_bitmap_complete_short_circuits(self, ctx):
        """When bitmap is complete, remaining paths are dropped and chain short-circuits."""
        ctx.cache.try_bitmap_filter.return_value = (
            ["/workspace/a.txt"],
            ["/workspace/b.txt"],
        )
        ctx.cache.is_bitmap_complete.return_value = True

        strategy = TigerBitmapStrategy()
        result = strategy.apply(ctx, list(ctx.paths))

        assert result.allowed == ["/workspace/a.txt"]
        assert result.remaining == []
        assert result.short_circuit is True


# ---------------------------------------------------------------------------
# Strategy 2: LeopardIndexStrategy
# ---------------------------------------------------------------------------


class TestLeopardIndexStrategy:
    """Tests for LeopardIndexStrategy — cached accessible directory index."""

    def test_leopard_hit(self, ctx):
        """When leopard lookup resolves some paths, they become allowed."""
        ctx.cache.try_leopard_lookup.return_value = (
            ["/workspace/a.txt"],
            ["/workspace/b.txt"],
        )

        strategy = LeopardIndexStrategy()
        result = strategy.apply(ctx, list(ctx.paths))

        assert result.allowed == ["/workspace/a.txt"]
        assert result.remaining == ["/workspace/b.txt"]

    def test_leopard_miss(self, ctx):
        """When leopard lookup resolves nothing, all paths remain."""
        ctx.cache.try_leopard_lookup.return_value = (
            [],
            ["/workspace/a.txt", "/workspace/b.txt"],
        )

        strategy = LeopardIndexStrategy()
        result = strategy.apply(ctx, list(ctx.paths))

        assert result.allowed == []
        assert result.remaining == ["/workspace/a.txt", "/workspace/b.txt"]


# ---------------------------------------------------------------------------
# Strategy 3: HierarchyPreFilterStrategy
# ---------------------------------------------------------------------------


class TestHierarchyPreFilterStrategy:
    """Tests for HierarchyPreFilterStrategy — batch ancestor pruning."""

    def test_small_set_skipped(self, ctx):
        """Sets with <=100 paths skip the hierarchy check (not worth overhead)."""
        paths = [f"/workspace/file_{i}.txt" for i in range(50)]

        strategy = HierarchyPreFilterStrategy()
        result = strategy.apply(ctx, paths)

        assert result.allowed == []
        assert result.remaining == paths
        # rebac_check_bulk should NOT be called for small sets
        ctx.rebac_manager.rebac_check_bulk.assert_not_called()

    def test_parent_denied(self, mock_cache, mock_rebac):
        """When a parent directory is denied, its children are removed from remaining."""
        # Generate >100 paths to trigger the hierarchy check
        # Use two parent directories: /workspace/allowed/ and /workspace/denied/
        paths = [f"/workspace/allowed/file_{i}.txt" for i in range(60)] + [
            f"/workspace/denied/file_{i}.txt" for i in range(60)
        ]
        ctx = _make_ctx(paths, mock_cache, mock_rebac)

        subject = ("user", "alice")
        # Only /workspace/allowed is accessible; /workspace/denied is not
        allowed_check = (subject, "read", ("file", "/workspace/allowed"))
        mock_rebac.rebac_check_bulk.return_value = {
            allowed_check: True,
            # /workspace/denied check absent -> defaults to False via .get()
        }

        strategy = HierarchyPreFilterStrategy()
        result = strategy.apply(ctx, paths)

        assert result.allowed == []
        # Only paths under /workspace/allowed should remain
        for p in result.remaining:
            assert p.startswith("/workspace/allowed/")
        assert len(result.remaining) == 60

    def test_parent_allowed(self, mock_cache, mock_rebac):
        """When all parent directories are accessible, all children stay in remaining."""
        paths = [f"/workspace/src/file_{i}.txt" for i in range(60)] + [
            f"/workspace/lib/file_{i}.txt" for i in range(60)
        ]
        ctx = _make_ctx(paths, mock_cache, mock_rebac)

        subject = ("user", "alice")
        src_check = (subject, "read", ("file", "/workspace/src"))
        lib_check = (subject, "read", ("file", "/workspace/lib"))
        mock_rebac.rebac_check_bulk.return_value = {
            src_check: True,
            lib_check: True,
        }

        strategy = HierarchyPreFilterStrategy()
        result = strategy.apply(ctx, paths)

        assert result.allowed == []
        assert len(result.remaining) == 120


# ---------------------------------------------------------------------------
# Strategy 4: ZonePreFilterStrategy
# ---------------------------------------------------------------------------


class TestZonePreFilterStrategy:
    """Tests for ZonePreFilterStrategy — cross-zone path elimination."""

    def test_cross_zone_skipped(self, mock_cache, mock_rebac):
        """Paths in /zones/<other_zone>/ are removed from remaining."""
        paths = [
            "/zones/other_zone/secret.txt",
            "/workspace/ok.txt",
            "/zones/default/mine.txt",
        ]
        ctx = _make_ctx(paths, mock_cache, mock_rebac, zone_id="default")

        strategy = ZonePreFilterStrategy()
        result = strategy.apply(ctx, paths)

        assert result.allowed == []
        assert "/zones/other_zone/secret.txt" not in result.remaining
        assert "/workspace/ok.txt" in result.remaining
        assert "/zones/default/mine.txt" in result.remaining
        assert len(result.remaining) == 2

    def test_same_zone_kept(self, mock_cache, mock_rebac):
        """Paths in /zones/<my_zone>/ are kept in remaining."""
        paths = [
            "/zones/my_zone/a.txt",
            "/zones/my_zone/b.txt",
            "/workspace/c.txt",
        ]
        ctx = _make_ctx(paths, mock_cache, mock_rebac, zone_id="my_zone")

        strategy = ZonePreFilterStrategy()
        result = strategy.apply(ctx, paths)

        assert result.allowed == []
        assert result.remaining == paths


# ---------------------------------------------------------------------------
# Strategy 5: BulkReBACStrategy
# ---------------------------------------------------------------------------


class TestBulkReBACStrategy:
    """Tests for BulkReBACStrategy — final fallback via rebac_check_bulk()."""

    def test_bulk_success(self, ctx):
        """When rebac_check_bulk returns results, allowed is populated."""
        subject = ("user", "alice")
        check_a = (subject, "read", ("file", "/workspace/a.txt"))
        check_b = (subject, "read", ("file", "/workspace/b.txt"))
        ctx.rebac_manager.rebac_check_bulk.return_value = {
            check_a: True,
            check_b: False,
        }

        strategy = BulkReBACStrategy()
        result = strategy.apply(ctx, list(ctx.paths))

        assert result.allowed == ["/workspace/a.txt"]
        assert result.remaining == []
        assert result.short_circuit is True

    def test_bulk_retry_on_transient_failure(self, ctx):
        """First rebac_check_bulk call fails with transient I/O error, second succeeds."""
        subject = ("user", "alice")
        check_a = (subject, "read", ("file", "/workspace/a.txt"))
        check_b = (subject, "read", ("file", "/workspace/b.txt"))

        ctx.rebac_manager.rebac_check_bulk.side_effect = [
            ConnectionError("transient failure"),
            {check_a: True, check_b: True},
        ]

        strategy = BulkReBACStrategy()
        result = strategy.apply(ctx, list(ctx.paths))

        assert result.allowed == ["/workspace/a.txt", "/workspace/b.txt"]
        assert result.remaining == []
        assert result.short_circuit is True
        assert ctx.rebac_manager.rebac_check_bulk.call_count == 2

    def test_bulk_fail_fast_on_non_retryable(self, ctx):
        """Non-retryable error (e.g. TypeError) fails immediately without retry."""
        ctx.rebac_manager.rebac_check_bulk.side_effect = TypeError("bad args")

        strategy = BulkReBACStrategy()
        result = strategy.apply(ctx, list(ctx.paths))

        assert result.allowed == []
        assert result.remaining == []
        assert result.short_circuit is True
        assert ctx.rebac_manager.rebac_check_bulk.call_count == 1

    def test_bulk_fail_fast_on_double_transient(self, ctx):
        """Both transient I/O calls fail; returns empty with short_circuit."""
        ctx.rebac_manager.rebac_check_bulk.side_effect = [
            ConnectionError("first failure"),
            TimeoutError("second failure"),
        ]

        strategy = BulkReBACStrategy()
        result = strategy.apply(ctx, list(ctx.paths))

        assert result.allowed == []
        assert result.remaining == []
        assert result.short_circuit is True
        assert ctx.rebac_manager.rebac_check_bulk.call_count == 2


# ---------------------------------------------------------------------------
# Chain Composition
# ---------------------------------------------------------------------------


class TestFilterChainComposition:
    """Tests for run_filter_chain() composing strategies together."""

    def test_chain_short_circuits(self, ctx):
        """When an early strategy short-circuits, later strategies are not called."""
        # Bitmap returns everything as allowed and short-circuits
        ctx.cache.try_bitmap_filter.return_value = (
            ["/workspace/a.txt", "/workspace/b.txt"],
            ["/workspace/b.txt"],
        )
        ctx.cache.is_bitmap_complete.return_value = True

        # Track whether BulkReBACStrategy gets called
        spy_strategy = MagicMock()
        spy_strategy.apply.return_value = FilterResult(allowed=[], remaining=[], short_circuit=True)

        chain = [TigerBitmapStrategy(), spy_strategy]
        allowed = run_filter_chain(ctx, chain=chain)

        # Bitmap resolved /workspace/a.txt and /workspace/b.txt, short-circuited
        assert "/workspace/a.txt" in allowed
        assert "/workspace/b.txt" in allowed
        # The spy strategy should NOT have been called
        spy_strategy.apply.assert_not_called()

    def test_chain_completes(self, ctx):
        """All strategies run when none short-circuit, results are combined."""
        # Bitmap resolves a.txt, leaves b.txt as remaining
        ctx.cache.try_bitmap_filter.return_value = (
            ["/workspace/a.txt"],
            ["/workspace/b.txt"],
        )
        ctx.cache.is_bitmap_complete.return_value = False

        # Leopard resolves nothing
        ctx.cache.try_leopard_lookup.return_value = (
            [],
            ["/workspace/b.txt"],
        )

        # BulkReBAC resolves b.txt
        subject = ("user", "alice")
        check_b = (subject, "read", ("file", "/workspace/b.txt"))
        ctx.rebac_manager.rebac_check_bulk.return_value = {
            check_b: True,
        }

        chain = [
            TigerBitmapStrategy(),
            LeopardIndexStrategy(),
            BulkReBACStrategy(),
        ]
        allowed = run_filter_chain(ctx, chain=chain)

        assert sorted(allowed) == ["/workspace/a.txt", "/workspace/b.txt"]
