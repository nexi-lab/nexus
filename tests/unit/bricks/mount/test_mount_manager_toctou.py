"""Unit tests for MountManager duplicate detection (Issue #2754).

Verifies that save_mount relies on the DB UNIQUE constraint instead of
check-then-insert, eliminating the TOCTOU race condition.
"""

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from nexus.bricks.mount.mount_manager import MountManager
from nexus.storage.models._base import Base

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def session_factory(temp_dir: Path) -> sessionmaker[Session]:
    db_path = temp_dir / "test_mount.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def manager(session_factory: sessionmaker[Session]) -> MountManager:
    record_store = type("FakeRS", (), {"session_factory": session_factory})()
    return MountManager(record_store)


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

    def test_duplicate_does_not_corrupt_existing(
        self, manager: MountManager, session_factory: sessionmaker[Session]
    ) -> None:
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

        # Original row is intact
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
