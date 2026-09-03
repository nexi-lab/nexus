"""Permission filter chain regressions."""

from __future__ import annotations

from typing import Any, cast

from nexus.bricks.rebac.permission_filter_chain import (
    BulkReBACStrategy,
    FilterContext,
    HierarchyPreFilterStrategy,
    run_filter_chain,
)

Check = tuple[tuple[str, str], str, tuple[str, str]]


class _FakeCache:
    def __init__(self) -> None:
        self.recorded_dirs: list[set[str]] = []

    def mark_bitmap_complete(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def record_accessible_dirs(
        self,
        dirs: set[str],
        *_args: Any,
        **_kwargs: Any,
    ) -> None:
        self.recorded_dirs.append(set(dirs))


class _DirectOnlyBulkReBAC:
    def __init__(self, allowed_objects: set[str]) -> None:
        self.allowed_objects = allowed_objects
        self.calls: list[list[Check]] = []

    def rebac_check_bulk(self, checks: list[Check], *, zone_id: str) -> dict[Check, bool]:
        self.calls.append(list(checks))
        return {check: check[2][1] in self.allowed_objects for check in checks}


def test_bulk_rebac_strategy_checks_parent_grants_without_search_fallback() -> None:
    paths = [
        "/workspace/demo/herb/customers/cust-001.md",
        "/workspace/demo/herb/customers/cust-002.md",
        "/workspace/private/secret.md",
    ]
    rebac = _DirectOnlyBulkReBAC(
        allowed_objects={
            "/workspace/demo/herb/customers",
        },
    )
    ctx = FilterContext(
        paths=paths,
        subject=("user", "alice"),
        zone_id="root",
        context=object(),
        cache=cast(Any, _FakeCache()),
        rebac_manager=cast(Any, rebac),
    )

    result = BulkReBACStrategy().apply(ctx, paths)

    assert result.allowed == paths[:2]
    assert result.remaining == []
    assert result.short_circuit is True
    assert len(rebac.calls) == 1

    bulk_checks = rebac.calls[0]
    assert (
        ("user", "alice"),
        "read",
        ("file", "/workspace/demo/herb/customers"),
    ) in bulk_checks


def test_hierarchy_prefilter_keeps_subtree_when_grandparent_grants_read() -> None:
    paths = [f"/workspace/demo/herb/customers/cust-{idx:03d}.md" for idx in range(101)]
    rebac = _DirectOnlyBulkReBAC(
        allowed_objects={
            "/workspace/demo",
        },
    )
    ctx = FilterContext(
        paths=paths,
        subject=("user", "alice"),
        zone_id="root",
        context=object(),
        cache=cast(Any, _FakeCache()),
        rebac_manager=cast(Any, rebac),
    )

    allowed = run_filter_chain(
        ctx,
        chain=[HierarchyPreFilterStrategy(), BulkReBACStrategy()],
    )

    assert allowed == paths
    assert len(rebac.calls) <= 2


def test_hierarchy_prefilter_does_not_drop_direct_leaf_grants() -> None:
    paths = [f"/workspace/demo/herb/customers/cust-{idx:03d}.md" for idx in range(101)]
    rebac = _DirectOnlyBulkReBAC(
        allowed_objects={
            paths[0],
        },
    )
    ctx = FilterContext(
        paths=paths,
        subject=("user", "alice"),
        zone_id="root",
        context=object(),
        cache=cast(Any, _FakeCache()),
        rebac_manager=cast(Any, rebac),
    )

    allowed = run_filter_chain(
        ctx,
        chain=[HierarchyPreFilterStrategy(), BulkReBACStrategy()],
    )

    assert allowed == [paths[0]]


# ---------------------------------------------------------------------------
# Issue #4739: consistency="strong" — authoritative chain, no cache strategies
# ---------------------------------------------------------------------------


class _RecordingBulkReBAC:
    """Bulk checker that records the kwargs it was called with."""

    def __init__(self, allowed_objects: set[str]) -> None:
        self.allowed_objects = allowed_objects
        self.kwargs: list[dict[str, Any]] = []

    def rebac_check_bulk(self, checks: list[Check], **kwargs: Any) -> dict[Check, bool]:
        self.kwargs.append(dict(kwargs))
        return {check: check[2][1] in self.allowed_objects for check in checks}


class _StaleTigerCache(_FakeCache):
    """Cache whose Tiger bitmap still allows everything (stale after a revoke)."""

    def __init__(self) -> None:
        super().__init__()
        self.tiger_calls = 0
        self.leopard_calls = 0

    def try_bitmap_filter(self, paths: list[str], *_args: Any, **_kwargs: Any) -> Any:
        self.tiger_calls += 1
        return (list(paths), [])

    def try_leopard_lookup(self, paths: list[str], *_args: Any, **_kwargs: Any) -> Any:
        self.leopard_calls += 1
        return ([], list(paths))

    def is_bitmap_complete(self, *_args: Any, **_kwargs: Any) -> bool:
        return True


def _ctx_with(cache: Any, rebac: Any, paths: list[str], consistency: str | None) -> FilterContext:
    return FilterContext(
        paths=paths,
        subject=("user", "alice"),
        zone_id="root",
        context=object(),
        cache=cast(Any, cache),
        rebac_manager=cast(Any, rebac),
        consistency=consistency,
    )


def test_strong_chain_skips_tiger_and_leopard_and_forwards_consistency() -> None:
    paths = ["/workspace/a.md", "/workspace/b.md"]
    cache = _StaleTigerCache()
    rebac = _RecordingBulkReBAC(allowed_objects={"/workspace/a.md"})

    allowed = run_filter_chain(_ctx_with(cache, rebac, paths, "strong"))

    # The stale bitmap would have allowed both; the tuple store allows one.
    assert allowed == ["/workspace/a.md"]
    assert cache.tiger_calls == 0
    assert cache.leopard_calls == 0
    assert rebac.kwargs == [{"zone_id": "root", "consistency": "strong"}]


def test_default_chain_still_uses_tiger_and_omits_consistency_kwarg() -> None:
    paths = ["/workspace/a.md", "/workspace/b.md"]
    cache = _StaleTigerCache()
    rebac = _RecordingBulkReBAC(allowed_objects=set())

    allowed = run_filter_chain(_ctx_with(cache, rebac, paths, None))

    assert allowed == paths  # served by the (stale) bitmap
    assert cache.tiger_calls == 1
    assert rebac.kwargs == []  # bulk never reached; and if it were, no consistency kwarg


def test_strong_chain_keeps_zone_prefilter() -> None:
    paths = ["/zone/other/secret.md", "/workspace/a.md"]
    rebac = _RecordingBulkReBAC(allowed_objects={"/zone/other/secret.md", "/workspace/a.md"})

    allowed = run_filter_chain(_ctx_with(_StaleTigerCache(), rebac, paths, "strong"))

    assert allowed == ["/workspace/a.md"]


def test_bulk_kwargs_only_forward_strong() -> None:
    rebac = _RecordingBulkReBAC(allowed_objects=set())
    assert _ctx_with(_FakeCache(), rebac, [], None).bulk_kwargs() == {"zone_id": "root"}
    assert _ctx_with(_FakeCache(), rebac, [], "eventual").bulk_kwargs() == {"zone_id": "root"}
    assert _ctx_with(_FakeCache(), rebac, [], "strong").bulk_kwargs() == {
        "zone_id": "root",
        "consistency": "strong",
    }
