"""Micro-benchmark: L3 persistent view restore vs cold ReBAC rebuild (Issue #1265).

Measures:
1. Cold rebuild time: L2 miss → L3 miss → full rebac_list_objects() rebuild
2. L3 restore time: L2 miss → L3 hit → mount table restoration
3. Asserts L3 restore is faster than cold rebuild

Run with: uv run pytest tests/unit/core/test_persistent_view_benchmark.py -v -s --tb=short
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import create_engine

from nexus.cache.persistent_view_postgres import PostgresPersistentViewStore
from nexus.core.namespace_manager import NamespaceManager
from nexus.storage.models import Base


@pytest.fixture
def engine():
    """Create in-memory SQLite database for benchmarking."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def rebac_manager(engine):
    """Create an EnhancedReBACManager with 100 grants."""
    from nexus.core.rebac_manager_enhanced import EnhancedReBACManager

    manager = EnhancedReBACManager(
        engine=engine,
        cache_ttl_seconds=300,
        max_depth=10,
    )

    # Create 100 file grants for a single subject
    for i in range(100):
        manager.rebac_write(
            subject=("user", "bench-user"),
            relation="direct_viewer",
            object=("file", f"/workspace/project-{i:03d}/data.csv"),
            zone_id=None,
        )

    yield manager
    manager.close()


class TestL3Benchmark:
    """Micro-benchmark comparing L3 restore vs cold ReBAC rebuild."""

    def test_l3_restore_faster_than_cold_rebuild(self, engine, rebac_manager):
        """L3 restore should be faster than full ReBAC rebuild.

        This test:
        1. Creates 100 ReBAC grants for a subject
        2. Measures cold rebuild time (no L2, no L3 → full rebac_list_objects)
        3. Stores result to L3 (via the rebuild)
        4. Clears L2 cache
        5. Measures L3 restore time (no L2, valid L3)
        6. Asserts L3 restore < cold rebuild
        """
        store = PostgresPersistentViewStore(engine)

        # --- Cold rebuild (no L2, no L3) ---
        ns_cold = NamespaceManager(
            rebac_manager=rebac_manager,
            cache_maxsize=100,
            cache_ttl=60,
            revision_window=10,
            persistent_store=store,
        )

        start = time.perf_counter()
        entries_cold = ns_cold.get_mount_table(("user", "bench-user"))
        cold_rebuild_ms = (time.perf_counter() - start) * 1000

        assert len(entries_cold) > 0, "Should have mount entries from 100 grants"

        # --- L3 restore (clear L2, L3 has valid view) ---
        ns_l3 = NamespaceManager(
            rebac_manager=rebac_manager,
            cache_maxsize=100,
            cache_ttl=60,
            revision_window=10,
            persistent_store=store,
        )

        start = time.perf_counter()
        entries_l3 = ns_l3.get_mount_table(("user", "bench-user"))
        l3_restore_ms = (time.perf_counter() - start) * 1000

        assert len(entries_l3) == len(entries_cold), "L3 restore should return same entries"
        assert ns_l3.metrics["l3_hits"] == 1, "Should have exactly 1 L3 hit"

        # Print for CI visibility
        print("\n--- L3 Benchmark (100 grants) ---")
        print(f"Cold ReBAC rebuild: {cold_rebuild_ms:.2f}ms")
        print(f"L3 restore:         {l3_restore_ms:.2f}ms")
        print(f"Speedup:            {cold_rebuild_ms / max(l3_restore_ms, 0.001):.1f}x")

        # Assert L3 restore is faster (with generous margin for CI variance)
        # In practice, L3 restore is 5-50x faster than cold rebuild
        assert l3_restore_ms < cold_rebuild_ms, (
            f"L3 restore ({l3_restore_ms:.2f}ms) should be faster than "
            f"cold rebuild ({cold_rebuild_ms:.2f}ms)"
        )
