"""Benchmark: permission lease performance gain (Issue #3394, #3398).

Measures the on_pre_write/read/delete() hook latency with and without the
PermissionLeaseTable to quantify the optimization.

Run:
    python -m pytest tests/unit/rebac/bench_permission_lease.py -v -s

This is NOT a pytest-benchmark test (avoids xdist issues). It uses
raw time.perf_counter for direct measurement.
"""

from __future__ import annotations

import statistics
import time
from unittest.mock import MagicMock

import pytest

pytest.importorskip("pyroaring")

from nexus.bricks.rebac.cache.permission_lease import PermissionLeaseTable
from nexus.bricks.rebac.permission_hook import PermissionCheckHook
from nexus.contracts.vfs_hooks import DeleteHookContext, ReadHookContext, WriteHookContext


def _make_context(agent_id: str = "agent-A") -> MagicMock:
    ctx = MagicMock()
    ctx.agent_id = agent_id
    ctx.user_id = "user-1"
    ctx.zone_id = "zone-a"
    return ctx


def _make_write_ctx(path: str, old_metadata: MagicMock | None = None) -> WriteHookContext:
    return WriteHookContext(
        path=path,
        content=b"data",
        context=_make_context(),
        old_metadata=old_metadata,
    )


def _make_read_ctx(path: str) -> ReadHookContext:
    return ReadHookContext(path=path, context=_make_context())


def _make_delete_ctx(path: str) -> DeleteHookContext:
    return DeleteHookContext(path=path, context=_make_context())


class TestPermissionLeaseBenchmark:
    """Benchmark: quantify the lease fast-path speedup."""

    def _run_benchmark(
        self,
        fn,
        ctx,
        iterations: int = 1000,
        warmup: int = 100,
    ) -> list[float]:
        """Run hook iterations and return per-call latencies in microseconds."""
        for _ in range(warmup):
            fn(ctx)

        latencies: list[float] = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            fn(ctx)
            t1 = time.perf_counter()
            latencies.append((t1 - t0) * 1_000_000)
        return latencies

    def _report(self, label: str, latencies_no: list[float], latencies_with: list[float]) -> None:
        med_no = statistics.median(latencies_no)
        med_with = statistics.median(latencies_with)
        p95_no = sorted(latencies_no)[int(0.95 * len(latencies_no))]
        p95_with = sorted(latencies_with)[int(0.95 * len(latencies_with))]
        speedup = med_no / med_with if med_with > 0 else float("inf")

        print("\n  ┌─────────────────────────────────────────────────────┐")
        print(f"  │  {label:<51} │")
        print("  ├─────────────────────────────────────────────────────┤")
        print(f"  │  WITHOUT lease:  median={med_no:8.2f}μs  p95={p95_no:8.2f}μs │")
        print(f"  │  WITH lease:     median={med_with:8.2f}μs  p95={p95_with:8.2f}μs │")
        print(f"  │  Speedup:        {speedup:5.1f}x                            │")
        print("  └─────────────────────────────────────────────────────┘\n")

    def test_benchmark_write_with_vs_without_lease(self) -> None:
        """Compare on_pre_write latency with and without lease table."""
        checker = MagicMock()
        metadata_store = MagicMock()
        default_ctx = _make_context()
        old_meta = MagicMock()

        # --- WITHOUT lease table ---
        hook_no_lease = PermissionCheckHook(
            checker=checker,
            metadata_store=metadata_store,
            default_context=default_ctx,
            enforce_permissions=True,
            lease_table=None,
        )
        ctx = _make_write_ctx("/workspace/src/file.py", old_metadata=old_meta)
        latencies_no = self._run_benchmark(hook_no_lease.on_pre_write, ctx)
        checker.check.reset_mock()

        # --- WITH lease table ---
        lease_table = PermissionLeaseTable()
        hook_with_lease = PermissionCheckHook(
            checker=checker,
            metadata_store=metadata_store,
            default_context=default_ctx,
            enforce_permissions=True,
            lease_table=lease_table,
        )
        hook_with_lease.on_pre_write(ctx)  # prime the lease
        checker.check.reset_mock()
        latencies_with = self._run_benchmark(hook_with_lease.on_pre_write, ctx)

        self._report("Write Lease Benchmark (Issue #3394)", latencies_no, latencies_with)

        med_with = statistics.median(latencies_with)
        med_no = statistics.median(latencies_no)
        assert med_with < med_no
        checker.check.assert_not_called()
        assert lease_table.stats()["lease_hits"] >= 1000

    def test_benchmark_read_with_vs_without_lease(self) -> None:
        """Compare on_pre_read latency with and without lease table."""
        checker = MagicMock()
        metadata_store = MagicMock()
        default_ctx = _make_context()

        hook_no_lease = PermissionCheckHook(
            checker=checker,
            metadata_store=metadata_store,
            default_context=default_ctx,
            enforce_permissions=True,
            lease_table=None,
        )
        ctx = _make_read_ctx("/workspace/src/file.py")
        latencies_no = self._run_benchmark(hook_no_lease.on_pre_read, ctx)
        checker.check.reset_mock()

        lease_table = PermissionLeaseTable()
        hook_with_lease = PermissionCheckHook(
            checker=checker,
            metadata_store=metadata_store,
            default_context=default_ctx,
            enforce_permissions=True,
            lease_table=lease_table,
        )
        hook_with_lease.on_pre_read(ctx)
        checker.check.reset_mock()
        latencies_with = self._run_benchmark(hook_with_lease.on_pre_read, ctx)

        self._report("Read Lease Benchmark (Issue #3398)", latencies_no, latencies_with)

        assert statistics.median(latencies_with) < statistics.median(latencies_no)
        checker.check.assert_not_called()

    def test_benchmark_delete_with_vs_without_lease(self) -> None:
        """Compare on_pre_delete latency with and without lease table."""
        checker = MagicMock()
        metadata_store = MagicMock()
        default_ctx = _make_context()

        hook_no_lease = PermissionCheckHook(
            checker=checker,
            metadata_store=metadata_store,
            default_context=default_ctx,
            enforce_permissions=True,
            lease_table=None,
        )
        ctx = _make_delete_ctx("/workspace/src/file.py")
        latencies_no = self._run_benchmark(hook_no_lease.on_pre_delete, ctx)
        checker.check.reset_mock()

        lease_table = PermissionLeaseTable()
        hook_with_lease = PermissionCheckHook(
            checker=checker,
            metadata_store=metadata_store,
            default_context=default_ctx,
            enforce_permissions=True,
            lease_table=lease_table,
        )
        hook_with_lease.on_pre_delete(ctx)
        checker.check.reset_mock()
        latencies_with = self._run_benchmark(hook_with_lease.on_pre_delete, ctx)

        self._report("Delete Lease Benchmark (Issue #3398)", latencies_no, latencies_with)

        assert statistics.median(latencies_with) < statistics.median(latencies_no)
        checker.check.assert_not_called()

    def test_benchmark_new_files_same_directory(self) -> None:
        """Benchmark: many new files in the same directory (ancestor walk)."""
        checker = MagicMock()
        metadata_store = MagicMock()
        default_ctx = _make_context()

        lease_table = PermissionLeaseTable()
        hook = PermissionCheckHook(
            checker=checker,
            metadata_store=metadata_store,
            default_context=default_ctx,
            enforce_permissions=True,
            lease_table=lease_table,
        )

        # First new file: stamps parent dir /workspace/src
        ctx1 = _make_write_ctx("/workspace/src/file0.py", old_metadata=None)
        hook.on_pre_write(ctx1)
        assert checker.check.call_count == 1
        checker.check.reset_mock()

        # Subsequent new files in same dir: lease hit on parent
        iterations = 500
        latencies: list[float] = []
        for i in range(1, iterations + 1):
            ctx = _make_write_ctx(f"/workspace/src/file{i}.py", old_metadata=None)
            t0 = time.perf_counter()
            hook.on_pre_write(ctx)
            t1 = time.perf_counter()
            latencies.append((t1 - t0) * 1_000_000)

        med = statistics.median(latencies)
        p95 = sorted(latencies)[int(0.95 * len(latencies))]

        print("\n")
        print("  ┌─────────────────────────────────────────────────────┐")
        print("  │  New Files in Same Dir (Ancestor Walk Benchmark)    │")
        print("  ├─────────────────────────────────────────────────────┤")
        print(f"  │  {iterations} new files in /workspace/src/                  │")
        print(f"  │  median={med:8.2f}μs  p95={p95:8.2f}μs                  │")
        print(f"  │  Full ReBAC checks skipped: {iterations}                     │")
        print("  └─────────────────────────────────────────────────────┘")
        print()

        checker.check.assert_not_called()
        assert lease_table.stats()["lease_hits"] >= iterations

    def test_correctness_permission_denied_after_invalidation(self) -> None:
        """Correctness: after invalidation, writes are re-checked."""
        checker = MagicMock()
        lease_table = PermissionLeaseTable()
        hook = PermissionCheckHook(
            checker=checker,
            metadata_store=MagicMock(),
            default_context=_make_context(),
            enforce_permissions=True,
            lease_table=lease_table,
        )

        ctx = _make_write_ctx("/workspace/file.py", old_metadata=MagicMock())
        hook.on_pre_write(ctx)
        checker.check.reset_mock()

        hook.on_pre_write(ctx)
        checker.check.assert_not_called()

        lease_table.invalidate_all()

        checker.check.side_effect = PermissionError("revoked")
        with pytest.raises(PermissionError, match="revoked"):
            hook.on_pre_write(ctx)

        checker.check.assert_called_once()
        print("\n  ✓ Correctness verified: invalidation forces re-check")
