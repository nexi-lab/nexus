"""Shared fixtures for benchmark tests."""

import asyncio
import uuid

import pytest

from nexus.backends.storage.cas_local import CASLocalBackend
from nexus.core.config import CacheConfig, ParseConfig, PermissionConfig
from nexus.core.metastore import RustMetastoreProxy
from nexus.factory import create_nexus_fs
from nexus.storage.record_store import SQLAlchemyRecordStore


def _build_kernel_metastore(db_path) -> tuple[object, RustMetastoreProxy]:
    """Create a Rust kernel + RustMetastoreProxy pair for benchmarks.

    Mirrors ``nexus.connect()`` — production builds metastore via
    ``RustMetastoreProxy(kernel, redb_path)`` so kernel.sys_write /
    sys_readdir / sys_stat persist through the same store Python reads
    from. Benchmarks that constructed ``RaftMetadataStore.embedded``
    directly bypassed the kernel's metastore wiring and hit empty
    dcache/metastore after F2 C4 delegated writes to ``kernel.sys_write``.
    """
    from nexus_kernel import Kernel as _Kernel

    redb_path = str(db_path).replace(".db", "") + ".redb"
    kernel = _Kernel()
    metastore = RustMetastoreProxy(kernel, redb_path)
    return kernel, metastore


@pytest.fixture
def benchmark_loop():
    """Create a dedicated event loop for factory async calls.

    create_nexus_fs() is still async (factory lifecycle), so we keep a
    dedicated event loop for run_until_complete() in fixture setup.
    All NexusFS methods are now sync — only the factory needs this.
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


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
    return CASLocalBackend(str(storage_path))


@pytest.fixture
def benchmark_nexus(benchmark_backend, benchmark_db, benchmark_loop):
    """Create a NexusFS instance for benchmarks.

    Configured with:
    - Local backend
    - RaftMetadataStore (sled, single-node)
    - Permissions disabled (for raw operation benchmarks)
    - Auto-parse disabled (for raw write benchmarks)
    """
    _, metadata_store = _build_kernel_metastore(benchmark_db)
    record_store = SQLAlchemyRecordStore()  # in-memory SQLite for benchmarks
    nx = benchmark_loop.run_until_complete(
        create_nexus_fs(
            backend=benchmark_backend,
            metadata_store=metadata_store,
            record_store=record_store,
            is_admin=True,
            permissions=PermissionConfig(enforce=False),
            parsing=ParseConfig(auto_parse=False),
            cache=CacheConfig(),
        )
    )
    yield nx
    nx.close()


@pytest.fixture
def benchmark_nexus_with_permissions(benchmark_backend, benchmark_db, benchmark_loop):
    """Create a NexusFS instance with permissions enabled for ReBAC benchmarks."""
    _, metadata_store = _build_kernel_metastore(str(benchmark_db).replace(".db", "") + "_perms.db")
    record_store = SQLAlchemyRecordStore()  # in-memory SQLite for benchmarks
    nx = benchmark_loop.run_until_complete(
        create_nexus_fs(
            backend=benchmark_backend,
            metadata_store=metadata_store,
            record_store=record_store,
            is_admin=False,  # Not admin - will check permissions
            zone_id="benchmark_zone",
            agent_id="benchmark_agent",
            permissions=PermissionConfig(enforce=True),
            parsing=ParseConfig(auto_parse=False),
            cache=CacheConfig(),
        )
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

    # All NexusFS methods are now sync — no event loop needed.
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
    config.addinivalue_line("markers", "benchmark_ci: critical benchmarks run on every PR")
    config.addinivalue_line("markers", "benchmark_fusion: Hybrid search fusion benchmarks")
