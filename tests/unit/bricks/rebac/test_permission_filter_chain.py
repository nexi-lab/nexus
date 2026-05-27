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
