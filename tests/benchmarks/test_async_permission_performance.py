"""Performance benchmark for AsyncNexusFS permission enforcement (Issue #940).

Compares performance:
1. Without permission enforcement (enforce_permissions=False)
2. With permission enforcement (enforce_permissions=True)

Goal: Permission enforcement should add <20ms overhead per operation.

Converted to pytest-benchmark for CI integration (Issue #1304).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nexus.core.async_nexus_fs import AsyncNexusFS
from nexus.core.permissions import OperationContext
from nexus.rebac.async_permissions import AsyncPermissionEnforcer
from nexus.storage.raft_metadata_store import RaftMetadataStore


@pytest.fixture
def metadata_store(tmp_path):
    """Create a local RaftMetadataStore for benchmarks."""
    store = RaftMetadataStore.embedded(str(tmp_path / "raft"))
    yield store
    store.close()


@pytest.fixture
def mock_rebac_manager():
    """Create mock ReBAC manager that always allows."""
    mock = AsyncMock()
    mock.rebac_check.return_value = True
    mock.rebac_check_bulk.return_value = {}
    return mock


def test_write_performance_without_permissions(benchmark, tmp_path: Path, metadata_store):
    """Benchmark write operations WITHOUT permission enforcement."""
    loop = asyncio.new_event_loop()
    try:
        fs = AsyncNexusFS(
            backend_root=tmp_path / "backend",
            metadata_store=metadata_store,
            enforce_permissions=False,
        )
        loop.run_until_complete(fs.initialize())

        counter = [0]

        def write_op():
            counter[0] += 1
            content = f"Content {counter[0]}".encode() * 100  # ~1KB
            path = f"/benchmark/file_{counter[0]}.txt"
            loop.run_until_complete(fs.write(path, content))

        benchmark(write_op)
        loop.run_until_complete(fs.close())
    finally:
        loop.close()


def test_write_performance_with_permissions(
    benchmark, tmp_path: Path, metadata_store, mock_rebac_manager: AsyncMock
):
    """Benchmark write operations WITH permission enforcement."""
    loop = asyncio.new_event_loop()
    try:
        enforcer = AsyncPermissionEnforcer(rebac_manager=mock_rebac_manager)
        fs = AsyncNexusFS(
            backend_root=tmp_path / "backend",
            metadata_store=metadata_store,
            enforce_permissions=True,
            permission_enforcer=enforcer,
        )
        loop.run_until_complete(fs.initialize())

        context = OperationContext(user="alice", groups=[], zone_id="test-tenant")
        counter = [0]

        def write_op():
            counter[0] += 1
            content = f"Content {counter[0]}".encode() * 100
            path = f"/benchmark/file_{counter[0]}.txt"
            loop.run_until_complete(fs.write(path, content, context=context))

        benchmark(write_op)
        loop.run_until_complete(fs.close())
    finally:
        loop.close()


def test_read_performance_without_permissions(benchmark, tmp_path: Path, metadata_store):
    """Benchmark read operations WITHOUT permission enforcement."""
    loop = asyncio.new_event_loop()
    try:
        fs = AsyncNexusFS(
            backend_root=tmp_path / "backend",
            metadata_store=metadata_store,
            enforce_permissions=False,
        )
        loop.run_until_complete(fs.initialize())

        # Setup: write files first
        num_files = 50
        for i in range(num_files):
            content = f"Content {i}".encode() * 100
            loop.run_until_complete(fs.write(f"/benchmark/file_{i}.txt", content))

        counter = [0]

        def read_op():
            idx = counter[0] % num_files
            counter[0] += 1
            loop.run_until_complete(fs.read(f"/benchmark/file_{idx}.txt"))

        benchmark(read_op)
        loop.run_until_complete(fs.close())
    finally:
        loop.close()


def test_read_performance_with_permissions(
    benchmark, tmp_path: Path, metadata_store, mock_rebac_manager: AsyncMock
):
    """Benchmark read operations WITH permission enforcement."""
    loop = asyncio.new_event_loop()
    try:
        enforcer = AsyncPermissionEnforcer(rebac_manager=mock_rebac_manager)
        fs = AsyncNexusFS(
            backend_root=tmp_path / "backend",
            metadata_store=metadata_store,
            enforce_permissions=True,
            permission_enforcer=enforcer,
        )
        loop.run_until_complete(fs.initialize())

        context = OperationContext(user="alice", groups=[], zone_id="test-tenant")

        # Setup: write files first
        num_files = 50
        for i in range(num_files):
            content = f"Content {i}".encode() * 100
            loop.run_until_complete(fs.write(f"/benchmark/file_{i}.txt", content, context=context))

        counter = [0]

        def read_op():
            idx = counter[0] % num_files
            counter[0] += 1
            loop.run_until_complete(fs.read(f"/benchmark/file_{idx}.txt", context=context))

        benchmark(read_op)
        loop.run_until_complete(fs.close())
    finally:
        loop.close()


@pytest.mark.benchmark_ci
def test_permission_overhead_acceptable(
    benchmark, tmp_path: Path, metadata_store, mock_rebac_manager: AsyncMock
):
    """Verify permission overhead is acceptable (<20ms per write+read cycle).

    Benchmarks the WITH-permissions path. The median cycle time must stay under
    20ms to ensure permission enforcement does not regress latency.
    """
    loop = asyncio.new_event_loop()
    try:
        enforcer = AsyncPermissionEnforcer(rebac_manager=mock_rebac_manager)
        fs_with_perm = AsyncNexusFS(
            backend_root=tmp_path / "backend_with_perm",
            metadata_store=metadata_store,
            enforce_permissions=True,
            permission_enforcer=enforcer,
        )
        loop.run_until_complete(fs_with_perm.initialize())

        context = OperationContext(user="alice", groups=[], zone_id="test-tenant")
        counter = [0]

        def write_read_with_perms():
            counter[0] += 1
            content = f"Content {counter[0]}".encode() * 100
            path = f"/bench/file_{counter[0]}.txt"
            loop.run_until_complete(fs_with_perm.write(path, content, context=context))
            loop.run_until_complete(fs_with_perm.read(path, context=context))

        benchmark(write_read_with_perms)
        loop.run_until_complete(fs_with_perm.close())
    finally:
        loop.close()

    # Validate: median benchmark time should be under 20ms per cycle
    median_s = benchmark.stats.get("median")
    assert median_s is not None, "benchmark stats not populated"
    median_ms = median_s * 1000
    assert median_ms < 20.0, (
        f"Permission write+read cycle too slow: {median_ms:.2f}ms (expected <20ms)"
    )
