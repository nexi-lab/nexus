"""Performance benchmark for AsyncNexusFS permission enforcement (Issue #940).

Compares performance:
1. Without permission enforcement (enforce_permissions=False)
2. With permission enforcement (enforce_permissions=True)

Goal: Permission enforcement should add <10ms overhead per operation.
"""

import os
import statistics
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nexus.core.async_nexus_fs import AsyncNexusFS
from nexus.core.async_permissions import AsyncPermissionEnforcer
from nexus.core.permissions import OperationContext

# Test database URL
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://scorpio:scorpio@localhost:5432/scorpio",
)


@pytest_asyncio.fixture
async def engine():
    """Create async engine."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)

    from sqlalchemy import text

    from nexus.storage.models import DirectoryEntryModel, FilePathModel, VersionHistoryModel

    async with engine.begin() as conn:
        for table in [
            FilePathModel.__table__,
            DirectoryEntryModel.__table__,
            VersionHistoryModel.__table__,
        ]:
            await conn.run_sync(lambda c, t=table: t.create(c, checkfirst=True))
        try:
            await conn.execute(
                text("TRUNCATE file_paths, directory_entries, version_history CASCADE")
            )
        except Exception:
            pass

    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def mock_rebac_manager():
    """Create mock ReBAC manager that always allows."""
    mock = AsyncMock()
    mock.rebac_check.return_value = True
    mock.rebac_check_bulk.return_value = {}
    return mock


@pytest.mark.asyncio
async def test_write_performance_without_permissions(tmp_path: Path, engine: AsyncEngine):
    """Benchmark write operations WITHOUT permission enforcement."""
    fs = AsyncNexusFS(
        backend_root=tmp_path / "backend",
        engine=engine,
        enforce_permissions=False,
    )
    await fs.initialize()

    try:
        latencies = []
        num_ops = 50

        for i in range(num_ops):
            content = f"Content {i}".encode() * 100  # ~1KB
            path = f"/benchmark/file_{i}.txt"

            start = time.perf_counter()
            await fs.write(path, content)
            latency = (time.perf_counter() - start) * 1000  # ms
            latencies.append(latency)

        avg = statistics.mean(latencies)
        p50 = statistics.median(latencies)
        p95 = sorted(latencies)[int(0.95 * len(latencies))]

        print(f"\n[NO PERMS] Write Performance ({num_ops} ops):")
        print(f"  Avg: {avg:.2f}ms, P50: {p50:.2f}ms, P95: {p95:.2f}ms")

        # Store results for comparison
        return {"avg": avg, "p50": p50, "p95": p95}
    finally:
        await fs.close()


@pytest.mark.asyncio
async def test_write_performance_with_permissions(
    tmp_path: Path,
    engine: AsyncEngine,
    mock_rebac_manager: AsyncMock,
):
    """Benchmark write operations WITH permission enforcement."""
    enforcer = AsyncPermissionEnforcer(rebac_manager=mock_rebac_manager)

    fs = AsyncNexusFS(
        backend_root=tmp_path / "backend",
        engine=engine,
        enforce_permissions=True,
        permission_enforcer=enforcer,
    )
    await fs.initialize()

    context = OperationContext(user="alice", groups=[], tenant_id="test-tenant")

    try:
        latencies = []
        num_ops = 50

        for i in range(num_ops):
            content = f"Content {i}".encode() * 100  # ~1KB
            path = f"/benchmark/file_{i}.txt"

            start = time.perf_counter()
            await fs.write(path, content, context=context)
            latency = (time.perf_counter() - start) * 1000  # ms
            latencies.append(latency)

        avg = statistics.mean(latencies)
        p50 = statistics.median(latencies)
        p95 = sorted(latencies)[int(0.95 * len(latencies))]

        print(f"\n[WITH PERMS] Write Performance ({num_ops} ops):")
        print(f"  Avg: {avg:.2f}ms, P50: {p50:.2f}ms, P95: {p95:.2f}ms")

        return {"avg": avg, "p50": p50, "p95": p95}
    finally:
        await fs.close()


@pytest.mark.asyncio
async def test_read_performance_without_permissions(tmp_path: Path, engine: AsyncEngine):
    """Benchmark read operations WITHOUT permission enforcement."""
    fs = AsyncNexusFS(
        backend_root=tmp_path / "backend",
        engine=engine,
        enforce_permissions=False,
    )
    await fs.initialize()

    try:
        # Setup: write files first
        num_ops = 50
        for i in range(num_ops):
            content = f"Content {i}".encode() * 100
            await fs.write(f"/benchmark/file_{i}.txt", content)

        # Benchmark reads
        latencies = []
        for i in range(num_ops):
            path = f"/benchmark/file_{i}.txt"

            start = time.perf_counter()
            await fs.read(path)
            latency = (time.perf_counter() - start) * 1000
            latencies.append(latency)

        avg = statistics.mean(latencies)
        p50 = statistics.median(latencies)
        p95 = sorted(latencies)[int(0.95 * len(latencies))]

        print(f"\n[NO PERMS] Read Performance ({num_ops} ops):")
        print(f"  Avg: {avg:.2f}ms, P50: {p50:.2f}ms, P95: {p95:.2f}ms")

        return {"avg": avg, "p50": p50, "p95": p95}
    finally:
        await fs.close()


@pytest.mark.asyncio
async def test_read_performance_with_permissions(
    tmp_path: Path,
    engine: AsyncEngine,
    mock_rebac_manager: AsyncMock,
):
    """Benchmark read operations WITH permission enforcement."""
    enforcer = AsyncPermissionEnforcer(rebac_manager=mock_rebac_manager)

    fs = AsyncNexusFS(
        backend_root=tmp_path / "backend",
        engine=engine,
        enforce_permissions=True,
        permission_enforcer=enforcer,
    )
    await fs.initialize()

    context = OperationContext(user="alice", groups=[], tenant_id="test-tenant")

    try:
        # Setup: write files first
        num_ops = 50
        for i in range(num_ops):
            content = f"Content {i}".encode() * 100
            await fs.write(f"/benchmark/file_{i}.txt", content, context=context)

        # Benchmark reads
        latencies = []
        for i in range(num_ops):
            path = f"/benchmark/file_{i}.txt"

            start = time.perf_counter()
            await fs.read(path, context=context)
            latency = (time.perf_counter() - start) * 1000
            latencies.append(latency)

        avg = statistics.mean(latencies)
        p50 = statistics.median(latencies)
        p95 = sorted(latencies)[int(0.95 * len(latencies))]

        print(f"\n[WITH PERMS] Read Performance ({num_ops} ops):")
        print(f"  Avg: {avg:.2f}ms, P50: {p50:.2f}ms, P95: {p95:.2f}ms")

        return {"avg": avg, "p50": p50, "p95": p95}
    finally:
        await fs.close()


@pytest.mark.asyncio
async def test_permission_overhead_acceptable(
    tmp_path: Path,
    engine: AsyncEngine,
    mock_rebac_manager: AsyncMock,
):
    """
    MAIN TEST: Verify permission overhead is acceptable.

    This test proves no significant performance regression from permission enforcement.

    Note: Results vary based on system load. We use median (P50) to reduce noise impact
    and set a generous threshold to account for environmental variance.
    """
    # --- Without permissions ---
    fs_no_perm = AsyncNexusFS(
        backend_root=tmp_path / "backend_no_perm",
        engine=engine,
        enforce_permissions=False,
    )
    await fs_no_perm.initialize()

    no_perm_latencies = []
    num_ops = 50  # More samples for better statistics

    for i in range(num_ops):
        content = f"Content {i}".encode() * 100
        path = f"/bench/file_{i}.txt"

        start = time.perf_counter()
        await fs_no_perm.write(path, content)
        _ = await fs_no_perm.read(path)
        latency = (time.perf_counter() - start) * 1000
        no_perm_latencies.append(latency)

    await fs_no_perm.close()

    # --- With permissions ---
    enforcer = AsyncPermissionEnforcer(rebac_manager=mock_rebac_manager)
    fs_with_perm = AsyncNexusFS(
        backend_root=tmp_path / "backend_with_perm",
        engine=engine,
        enforce_permissions=True,
        permission_enforcer=enforcer,
    )
    await fs_with_perm.initialize()

    context = OperationContext(user="alice", groups=[], tenant_id="test-tenant")
    with_perm_latencies = []

    for i in range(num_ops):
        content = f"Content {i}".encode() * 100
        path = f"/bench/file_{i}.txt"

        start = time.perf_counter()
        await fs_with_perm.write(path, content, context=context)
        _ = await fs_with_perm.read(path, context=context)
        latency = (time.perf_counter() - start) * 1000
        with_perm_latencies.append(latency)

    await fs_with_perm.close()

    # --- Analysis using P50 (median) for noise reduction ---
    no_perm_p50 = statistics.median(no_perm_latencies)
    with_perm_p50 = statistics.median(with_perm_latencies)
    overhead_p50 = with_perm_p50 - no_perm_p50

    no_perm_avg = statistics.mean(no_perm_latencies)
    with_perm_avg = statistics.mean(with_perm_latencies)
    overhead_avg = with_perm_avg - no_perm_avg

    print(f"\n{'=' * 60}")
    print(f"PERMISSION OVERHEAD ANALYSIS ({num_ops} write+read cycles)")
    print(f"{'=' * 60}")
    print(f"Without permissions: P50={no_perm_p50:.2f}ms, Avg={no_perm_avg:.2f}ms")
    print(f"With permissions:    P50={with_perm_p50:.2f}ms, Avg={with_perm_avg:.2f}ms")
    print(f"Overhead:            P50={overhead_p50:.2f}ms, Avg={overhead_avg:.2f}ms")
    print(f"{'=' * 60}")

    # ASSERTION: Use P50 for stability, generous 20ms threshold for system variance
    # Permission checking adds ~2-10ms per cycle depending on system load
    # Each cycle = write (1 perm check) + read (1 perm check)
    assert overhead_p50 < 20.0, (
        f"Permission P50 overhead too high: {overhead_p50:.2f}ms (expected <20ms)"
    )

    print(f"✓ Permission overhead acceptable: P50={overhead_p50:.2f}ms (threshold: 20ms)")
    print(f"  Per-operation overhead: ~{overhead_p50 / 2:.2f}ms (write or read)")
