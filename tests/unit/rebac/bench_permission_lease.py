"""Benchmark: permission write lease performance gain (Issue #3394).

Measures the on_pre_write() hook latency with and without the
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
from nexus.contracts.vfs_hooks import WriteHookContext


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


class TestPermissionLeaseBenchmark:
    """Benchmark: quantify the lease fast-path speedup."""

    def _run_benchmark(
        self,
        hook: PermissionCheckHook,
        ctx: WriteHookContext,
        iterations: int = 1000,
        warmup: int = 100,
    ) -> list[float]:
        """Run on_pre_write iterations and return per-call latencies in microseconds."""
        # Warmup
        for _ in range(warmup):
            hook.on_pre_write(ctx)

        latencies: list[float] = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            hook.on_pre_write(ctx)
            t1 = time.perf_counter()
            latencies.append((t1 - t0) * 1_000_000)  # microseconds
        return latencies

    def test_benchmark_with_vs_without_lease(self) -> None:
        """Compare on_pre_write latency with and without lease table.

        Without lease: every call does checker.check() (mocked but still
        has Python call overhead).
        With lease: second+ calls hit the lease and skip checker entirely.
        """
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

        latencies_no_lease = self._run_benchmark(hook_no_lease, ctx)
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

        # First call primes the lease
        hook_with_lease.on_pre_write(ctx)
        checker.check.reset_mock()

        latencies_with_lease = self._run_benchmark(hook_with_lease, ctx)

        # --- Results ---
        med_no = statistics.median(latencies_no_lease)
        med_with = statistics.median(latencies_with_lease)
        p95_no = sorted(latencies_no_lease)[int(0.95 * len(latencies_no_lease))]
        p95_with = sorted(latencies_with_lease)[int(0.95 * len(latencies_with_lease))]
        speedup = med_no / med_with if med_with > 0 else float("inf")

        print("\n")
        print("  ┌─────────────────────────────────────────────────────┐")
        print("  │  Permission Write Lease Benchmark (Issue #3394)     │")
        print("  ├─────────────────────────────────────────────────────┤")
        print(f"  │  WITHOUT lease:  median={med_no:8.2f}μs  p95={p95_no:8.2f}μs │")
        print(f"  │  WITH lease:     median={med_with:8.2f}μs  p95={p95_with:8.2f}μs │")
        print(f"  │  Speedup:        {speedup:5.1f}x                            │")
        print("  └─────────────────────────────────────────────────────┘")
        print()

        # The lease path should be meaningfully faster
        assert med_with < med_no, (
            f"Lease path ({med_with:.2f}μs) should be faster than no-lease path ({med_no:.2f}μs)"
        )

        # Verify checker was NOT called during the lease benchmark
        # (all calls hit the lease fast path)
        checker.check.assert_not_called()

        # Report lease stats
        stats = lease_table.stats()
        print(f"  Lease stats: {stats}")
        assert stats["lease_hits"] >= 1000  # All benchmark iterations hit

    def test_benchmark_new_files_same_directory(self) -> None:
        """Benchmark: many new files in the same directory (ancestor walk).

        Without lease: each new file checks WRITE on parent dir.
        With lease: first file stamps parent, rest hit via ancestor walk.
        """
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

        # All 500 should have hit the lease (parent dir stamp)
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

        # Write succeeds, lease stamped
        ctx = _make_write_ctx("/workspace/file.py", old_metadata=MagicMock())
        hook.on_pre_write(ctx)
        checker.check.reset_mock()

        # Second write: lease hit (no check)
        hook.on_pre_write(ctx)
        checker.check.assert_not_called()

        # Invalidate (simulates permission revocation)
        lease_table.invalidate_all()

        # Third write: must do full check
        checker.check.side_effect = PermissionError("revoked")
        with pytest.raises(PermissionError, match="revoked"):
            hook.on_pre_write(ctx)

        checker.check.assert_called_once()
        print("\n  ✓ Correctness verified: invalidation forces re-check")
