"""Unit tests for MountManager duplicate detection (Issue #2754).

Verifies that save_mount checks for existing config before insert and
raises a clean ValueError on duplicate, eliminating the TOCTOU race.

Issue #192: store moved off SQLAlchemy onto MetastoreABC, then again
onto VFS files under ``/__sys__/mounts/``. The behavior tested here is
agnostic to the underlying store — we use a fake NexusFS that implements
just the four sys_* calls the store needs.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus.bricks.mount.metastore_mount_store import MetastoreMountStore
from nexus.bricks.mount.mount_manager import MountManager

# ---------------------------------------------------------------------------
# In-memory NexusFS stub for testing
# ---------------------------------------------------------------------------


class _InMemoryNexusFS:
    """Minimal in-memory NexusFS implementing the four sys_* calls the
    store uses. Mirrors the kernel's VFS contract closely enough to
    exercise the store's full code path without the real kernel."""

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}

    def sys_write(self, path: str, buf: bytes | str, **kwargs: Any) -> dict[str, Any]:
        if isinstance(buf, str):
            buf = buf.encode("utf-8")
        self._files[path] = bytes(buf)
        return {"path": path, "bytes_written": len(buf)}

    def sys_read(self, path: str, **kwargs: Any) -> dict[str, Any]:
        if path not in self._files:
            raise FileNotFoundError(path)
        return {"hit": True, "content": self._files[path]}

    def sys_readdir(self, path: str = "/", recursive: bool = True, **kwargs: Any) -> list[str]:
        # Strip directory prefix, return basenames under it.
        prefix = path if path.endswith("/") else path + "/"
        names: set[str] = set()
        for full in self._files:
            if full.startswith(prefix):
                rest = full[len(prefix) :]
                if not rest:
                    continue
                if recursive or "/" not in rest:
                    names.add(rest.split("/", 1)[0])
        return sorted(names)

    def sys_unlink(self, path: str, **kwargs: Any) -> dict[str, Any]:
        if path not in self._files:
            raise FileNotFoundError(path)
        del self._files[path]
        return {"path": path}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> MetastoreMountStore:
    return MetastoreMountStore(_InMemoryNexusFS())


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
