"""Shared fixtures for benchmark tests."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from nexus import NexusFS


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def get_backend_combinations() -> list[str]:
    """Get available backend/metadata combinations for testing.

    Architecture:
    - Storage Backend: Where file CONTENT is stored (Local, GCS, S3)
    - Metadata Store: Where file METADATA is stored (SQLite, PostgreSQL)

    Returns list of combinations to test:
    - local-sqlite: LocalBackend + SQLite (always available)
    - local-postgres: LocalBackend + PostgreSQL (if DB URL set)
    - local_fs: Raw filesystem baseline (no Nexus, always available)
    - gcs-sqlite: GCSBackend + SQLite (if GCS credentials available)
    - gcs-postgres: GCSBackend + PostgreSQL (if both available)
    """
    combinations = ["local-sqlite", "local_fs"]

    # Add PostgreSQL metadata combinations if DB URL is available
    if os.getenv("NEXUS_DATABASE_URL") or os.getenv("POSTGRES_URL"):
        combinations.append("local-postgres")

    # Add GCS backend combinations if credentials available
    if os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or os.getenv("GCS_BUCKET"):
        combinations.append("gcs-sqlite")
        if os.getenv("NEXUS_DATABASE_URL") or os.getenv("POSTGRES_URL"):
            combinations.append("gcs-postgres")

    return combinations


@pytest.fixture(params=get_backend_combinations())
def backend_type(request: pytest.FixtureRequest) -> str:
    """Parametrize backend/metadata combinations for testing.

    Format: {storage_backend}-{metadata_store}
    Examples:
    - local-sqlite: LocalBackend + SQLite metadata
    - local-postgres: LocalBackend + PostgreSQL metadata
    - gcs-sqlite: GCSBackend + SQLite metadata
    - local_fs: Raw filesystem (no Nexus)
    """
    return request.param  # type: ignore


@pytest.fixture
def nexus_backend(backend_type: str, temp_dir: Path) -> Generator[NexusFS | Path, None, None]:
    """Fixture that provides various backend/metadata combinations.

    Tests Nexus with different storage backends and metadata stores:
    - Storage Backend: Where file CONTENT lives (Local disk, GCS, S3)
    - Metadata Store: Where file METADATA lives (SQLite, PostgreSQL)

    Args:
        backend_type: Format "{storage}-{metadata}" or "local_fs"
                     Examples: "local-sqlite", "local-postgres", "gcs-sqlite"
        temp_dir: Temporary directory for test data

    Yields:
        NexusFS instance for Nexus backends, or Path for local filesystem

    Environment Variables:
        NEXUS_DATABASE_URL or POSTGRES_URL: PostgreSQL connection string
        GOOGLE_APPLICATION_CREDENTIALS: Path to GCS service account key
        GCS_BUCKET: GCS bucket name
    """
    if backend_type == "local_fs":
        # Raw filesystem baseline (no Nexus)
        fs_dir = temp_dir / "local-fs"
        fs_dir.mkdir(exist_ok=True)
        yield fs_dir
        return

    # Parse backend_type: format is "{storage}-{metadata}"
    parts = backend_type.split("-")
    storage_backend = parts[0]  # local, gcs, s3
    metadata_store = parts[1] if len(parts) > 1 else "sqlite"  # sqlite, postgres

    # Create storage backend
    if storage_backend == "local":
        from nexus import LocalBackend

        backend = LocalBackend(temp_dir / "nexus-data")
    elif storage_backend == "gcs":
        from nexus.backends.gcs import GCSBackend

        bucket = os.getenv("GCS_BUCKET")
        credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if not bucket:
            pytest.skip("GCS_BUCKET not configured")

        backend = GCSBackend(
            bucket_name=bucket,
            credentials_path=credentials_path,
            base_path=f"benchmark-{temp_dir.name}",
        )
    else:
        pytest.skip(f"Unknown storage backend: {storage_backend}")

    # Create metadata store path
    if metadata_store == "sqlite":
        db_path = temp_dir / "metadata.db"
    elif metadata_store == "postgres":
        db_url = os.getenv("NEXUS_DATABASE_URL") or os.getenv("POSTGRES_URL")
        if not db_url:
            pytest.skip("PostgreSQL database URL not configured")
        db_path = db_url
    else:
        pytest.skip(f"Unknown metadata store: {metadata_store}")

    # Create NexusFS with the combination
    nx = NexusFS(
        backend=backend,
        db_path=db_path,
        auto_parse=False,  # Disable parsing for performance tests
    )
    yield nx
    nx.close()


@pytest.fixture
def backend_name(backend_type: str) -> str:
    """Get current backend name for reporting."""
    return backend_type


class BackendAdapter:
    """Adapter to provide uniform interface for different backends."""

    def __init__(self, backend: NexusFS | Path):
        """Initialize adapter with backend."""
        self.backend = backend
        self.is_nexus = isinstance(backend, NexusFS)

    def write(self, path: str, data: bytes) -> None:
        """Write data to path."""
        if self.is_nexus:
            self.backend.write(path, data)  # type: ignore
        else:
            # Local filesystem
            full_path = self.backend / path.lstrip("/")  # type: ignore
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_bytes(data)

    def read(self, path: str) -> bytes:
        """Read data from path."""
        if self.is_nexus:
            return self.backend.read(path)  # type: ignore
        else:
            # Local filesystem
            full_path = self.backend / path.lstrip("/")  # type: ignore
            return full_path.read_bytes()

    def exists(self, path: str) -> bool:
        """Check if path exists."""
        if self.is_nexus:
            return self.backend.exists(path)  # type: ignore
        else:
            # Local filesystem
            full_path = self.backend / path.lstrip("/")  # type: ignore
            return full_path.exists()

    def delete(self, path: str) -> None:
        """Delete path."""
        if self.is_nexus:
            self.backend.delete(path)  # type: ignore
        else:
            # Local filesystem
            full_path = self.backend / path.lstrip("/")  # type: ignore
            if full_path.exists():
                full_path.unlink()

    def list_dir(self, path: str) -> list[str]:
        """List directory contents."""
        if self.is_nexus:
            entries = self.backend.list(path)  # type: ignore
            # Handle both string paths and FileMetadata objects
            if entries and hasattr(entries[0], "name"):
                return [entry.name for entry in entries]
            else:
                # If list() returns paths as strings
                return [str(entry).split("/")[-1] for entry in entries]
        else:
            # Local filesystem
            full_path = self.backend / path.lstrip("/")  # type: ignore
            if full_path.exists():
                return [p.name for p in full_path.iterdir()]
            return []


@pytest.fixture
def backend_adapter(nexus_backend: NexusFS | Path) -> BackendAdapter:
    """Create a backend adapter for uniform testing interface."""
    return BackendAdapter(nexus_backend)
