"""Unit tests for MountManager duplicate detection (Issue #2754).

Verifies that save_mount relies on metastore uniqueness check instead of
check-then-insert, eliminating the TOCTOU race condition.

Issue #192: Updated for metastore-backed MountManager.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus.bricks.mount.metastore_mount_store import MetastoreMountStore
from nexus.bricks.mount.mount_manager import MountManager
from nexus.contracts.metadata import FileMetadata

# ---------------------------------------------------------------------------
# In-memory metastore stub for testing
# ---------------------------------------------------------------------------


class _InMemoryMetastore:
    """Minimal in-memory metastore for testing MetastoreMountStore."""

    def __init__(self) -> None:
        self._data: dict[str, FileMetadata] = {}

    def get(self, path: str) -> FileMetadata | None:
        return self._data.get(path)

    def put(self, metadata: FileMetadata) -> int | None:
        self._data[metadata.path] = metadata
        return None

    def delete(self, path: str) -> dict[str, Any] | None:
        if path in self._data:
            del self._data[path]
            return {"path": path}
        return None

    def list(self, prefix: str = "", recursive: bool = True, **kwargs: Any) -> list[FileMetadata]:
        return [fm for k, fm in sorted(self._data.items()) if k.startswith(prefix)]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> MetastoreMountStore:
    return MetastoreMountStore(_InMemoryMetastore())


@pytest.fixture
def manager(store: MetastoreMountStore) -> MountManager:
    return MountManager(store)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSaveMountDuplicateDetection:
    """Tests for race-free duplicate mount detection."""

    def test_save_mount_success(self, manager: MountManager) -> None:
        """First save_mount succeeds and returns a mount_id."""
        mount_id = manager.save_mount(
            mount_point="/mnt/test",
            backend_type="cas_local",
            backend_config={"data_dir": "/tmp"},
        )
        assert mount_id is not None
        assert len(mount_id) > 0

    def test_duplicate_mount_raises_value_error(self, manager: MountManager) -> None:
        """Second save_mount with same mount_point raises ValueError."""
        manager.save_mount(
            mount_point="/mnt/test",
            backend_type="cas_local",
            backend_config={"data_dir": "/tmp"},
        )

        with pytest.raises(ValueError, match="Mount already exists at /mnt/test"):
            manager.save_mount(
                mount_point="/mnt/test",
                backend_type="gcs",
                backend_config={"bucket": "other"},
            )

    def test_different_mount_points_both_succeed(self, manager: MountManager) -> None:
        """Different mount_points can both be saved."""
        id1 = manager.save_mount(
            mount_point="/mnt/a",
            backend_type="cas_local",
            backend_config={"data_dir": "/tmp/a"},
        )
        id2 = manager.save_mount(
            mount_point="/mnt/b",
            backend_type="cas_local",
            backend_config={"data_dir": "/tmp/b"},
        )
        assert id1 != id2

    def test_duplicate_does_not_corrupt_existing(self, manager: MountManager) -> None:
        """Failed duplicate insert does not corrupt the existing row."""
        manager.save_mount(
            mount_point="/mnt/test",
            backend_type="cas_local",
            backend_config={"data_dir": "/original"},
        )

        with pytest.raises(ValueError):
            manager.save_mount(
                mount_point="/mnt/test",
                backend_type="gcs",
                backend_config={"bucket": "overwrite-attempt"},
            )

        # Original entry is intact
        config = manager.get_mount("/mnt/test")
        assert config is not None
        assert config["backend_type"] == "cas_local"
        assert config["backend_config"]["data_dir"] == "/original"

    def test_can_save_after_remove(self, manager: MountManager) -> None:
        """After removing a mount, the same mount_point can be re-saved."""
        manager.save_mount(
            mount_point="/mnt/test",
            backend_type="cas_local",
            backend_config={"data_dir": "/tmp"},
        )
        manager.remove_mount("/mnt/test")

        mount_id = manager.save_mount(
            mount_point="/mnt/test",
            backend_type="gcs",
            backend_config={"bucket": "new"},
        )
        assert mount_id is not None
