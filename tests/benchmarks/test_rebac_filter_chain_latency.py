"""ReBAC filter-chain latency guardrails for inherited directory grants."""

from __future__ import annotations

from typing import Any, cast

import pytest

from nexus.bricks.rebac.permission_filter_chain import (
    BulkReBACStrategy,
    FilterContext,
    HierarchyPreFilterStrategy,
    run_filter_chain,
)

pytestmark = [
    pytest.mark.benchmark_permissions,
    pytest.mark.benchmark(group="rebac-filter-chain-inheritance", min_rounds=5, max_time=0.5),
]

Check = tuple[tuple[str, str], str, tuple[str, str]]


class _BenchCache:
    def mark_bitmap_complete(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def record_accessible_dirs(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _BenchReBAC:
    def __init__(self, allowed_objects: set[str]) -> None:
        self.allowed_objects = allowed_objects
        self.calls: list[list[Check]] = []

    def rebac_check_bulk(self, checks: list[Check], *, zone_id: str) -> dict[Check, bool]:
        self.calls.append(list(checks))
        return {check: check[2][1] in self.allowed_objects for check in checks}


def test_filter_chain_inherited_grants_stay_bulk(benchmark: Any) -> None:
    """Filtering 1000 descendants of one grant should stay at O(1) bulk calls."""
    paths = [f"/workspace/demo/herb/customers/cust-{idx:04d}.md" for idx in range(1000)]
    rebac = _BenchReBAC(allowed_objects={"/workspace/demo"})
    ctx = FilterContext(
        paths=paths,
        subject=("user", "alice"),
        zone_id="root",
        context=object(),
        cache=cast(Any, _BenchCache()),
        rebac_manager=cast(Any, rebac),
    )
    chain = [HierarchyPreFilterStrategy(), BulkReBACStrategy()]

    def run() -> list[str]:
        rebac.calls.clear()
        return run_filter_chain(ctx, chain=chain)

    allowed = benchmark(run)

    assert allowed == paths
    assert len(rebac.calls) <= 2
    assert sum(len(call) for call in rebac.calls) <= len(paths) + 16
