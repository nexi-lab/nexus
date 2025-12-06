"""Shared fixtures for benchmark tests."""

from __future__ import annotations

import uuid

import pytest

from nexus.backends.local import LocalBackend
from nexus.core.nexus_fs import NexusFS


@pytest.fixture
def benchmark_db(tmp_path, monkeypatch):
    """Create an isolated database path for benchmarks.

    Clears environment variables that could override the database path.
    """
    # Clear environment variables that would override db_path
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    unique_id = str(uuid.uuid4())[:8]
    db_path = tmp_path / f"benchmark_db_{unique_id}.db"
    yield db_path


@pytest.fixture
def benchmark_backend(tmp_path):
    """Create a local backend for benchmarks."""
    storage_path = tmp_path / "storage"
    storage_path.mkdir(parents=True, exist_ok=True)
    return LocalBackend(str(storage_path))


@pytest.fixture
def benchmark_nexus(benchmark_backend, benchmark_db):
    """Create a NexusFS instance for benchmarks.

    Configured with:
    - Local backend
    - Permissions disabled (for raw operation benchmarks)
    - Auto-parse disabled (for raw write benchmarks)
    """
    nx = NexusFS(
        backend=benchmark_backend,
        db_path=benchmark_db,
        is_admin=True,
        enforce_permissions=False,  # Disable for pure operation benchmarks
        auto_parse=False,  # Disable for pure write benchmarks
        enable_metadata_cache=True,
        enable_content_cache=True,
    )
    yield nx
    nx.close()


@pytest.fixture
def benchmark_nexus_with_permissions(benchmark_backend, benchmark_db):
    """Create a NexusFS instance with permissions enabled for ReBAC benchmarks."""
    nx = NexusFS(
        backend=benchmark_backend,
        db_path=benchmark_db,
        is_admin=False,  # Not admin - will check permissions
        tenant_id="benchmark_tenant",
        agent_id="benchmark_agent",
        enforce_permissions=True,
        auto_parse=False,
        enable_metadata_cache=True,
        enable_content_cache=True,
    )
    yield nx
    nx.close()


@pytest.fixture
def sample_files():
    """Generate sample file data of various sizes."""
    return {
        "tiny": b"Hello, World!",  # 13 bytes
        "small": b"x" * 1024,  # 1 KB
        "medium": b"y" * (64 * 1024),  # 64 KB
        "large": b"z" * (1024 * 1024),  # 1 MB
        "xlarge": b"w" * (10 * 1024 * 1024),  # 10 MB
    }


@pytest.fixture
def populated_nexus(benchmark_nexus, sample_files):
    """Create a NexusFS with pre-populated files for read benchmarks."""
    nx = benchmark_nexus

    # Create directory structure
    for i in range(10):
        nx.mkdir(f"/dir_{i}", parents=True)
        for j in range(10):
            nx.mkdir(f"/dir_{i}/subdir_{j}", parents=True)

    # Create files of various sizes
    for size_name, content in sample_files.items():
        if size_name != "xlarge":  # Skip xlarge for setup speed
            nx.write(f"/test_{size_name}.bin", content)
            # Create copies in subdirectories
            for i in range(5):
                nx.write(f"/dir_{i}/test_{size_name}.bin", content)

    # Create many small files for glob/list benchmarks
    for i in range(100):
        nx.write(f"/many_files/file_{i:04d}.txt", f"Content {i}".encode())
        nx.write(f"/many_files/file_{i:04d}.py", f"# Python {i}".encode())
        nx.write(f"/many_files/file_{i:04d}.json", f'{{"id": {i}}}'.encode())

    yield nx


@pytest.fixture
def deep_directory_nexus(benchmark_nexus):
    """Create a NexusFS with deep directory structure for path resolution benchmarks."""
    nx = benchmark_nexus

    # Create deep nested directories
    current_path = ""
    for i in range(20):
        current_path += f"/level_{i}"
        nx.mkdir(current_path, parents=True)
        nx.write(f"{current_path}/file.txt", f"Content at depth {i}".encode())

    yield nx


# Benchmark group markers
def pytest_configure(config):
    """Register custom markers for benchmark categories."""
    config.addinivalue_line("markers", "benchmark_file_ops: File operation benchmarks")
    config.addinivalue_line("markers", "benchmark_glob: Glob and listing benchmarks")
    config.addinivalue_line("markers", "benchmark_hash: Content hashing benchmarks")
    config.addinivalue_line("markers", "benchmark_metadata: Metadata query benchmarks")
    config.addinivalue_line("markers", "benchmark_permissions: Permission check benchmarks")
