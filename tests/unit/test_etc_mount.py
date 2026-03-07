"""Tests for /etc VFS mount functionality."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    """Create a temporary STATE_DIR with etc/conf.d/ populated."""
    etc = tmp_path / "etc" / "conf.d"
    etc.mkdir(parents=True)
    (etc / "mounts").write_text("auto_sync = true\n")
    (etc / "cache").write_text('backend = "dragonfly"\n')
    # Also need data dir and metastore for connect()
    (tmp_path / "data").mkdir()
    return tmp_path


class TestMountEtc:
    """Test _mount_etc() function."""

    def test_mount_etc_creates_readonly_mount(self, state_dir: Path) -> None:
        from nexus.__init__ import _mount_etc

        # Create a minimal NexusFS-like object with a router
        from nexus.backends.storage.path_local import PathLocalBackend
        from nexus.core.router import PathRouter
        from nexus.storage.raft_metadata_store import RaftMetadataStore

        meta = RaftMetadataStore.embedded(str(state_dir / "metastore"))
        router = PathRouter(meta)
        data_backend = PathLocalBackend(root_path=state_dir / "data")
        router.add_mount("/", data_backend)

        # Create a mock nx_fs with the router
        class MockNexusFS:
            pass

        nx_fs = MockNexusFS()
        nx_fs.router = router

        _mount_etc(nx_fs, str(state_dir))

        # Verify /etc is mounted
        assert router.has_mount("/etc")

        # Verify /etc is readonly
        route = router.route("/etc/conf.d/mounts", is_admin=True)
        assert route.readonly is True

    def test_mount_etc_skips_when_no_etc_dir(self, tmp_path: Path) -> None:
        from nexus.__init__ import _mount_etc

        class MockNexusFS:
            pass

        nx_fs = MockNexusFS()
        nx_fs.router = None  # Should not be accessed

        # No error when etc/ doesn't exist
        _mount_etc(nx_fs, str(tmp_path))

    def test_etc_files_routable(self, state_dir: Path) -> None:
        from nexus.__init__ import _mount_etc
        from nexus.backends.storage.path_local import PathLocalBackend
        from nexus.core.router import PathRouter
        from nexus.storage.raft_metadata_store import RaftMetadataStore

        meta = RaftMetadataStore.embedded(str(state_dir / "metastore"))
        router = PathRouter(meta)
        data_backend = PathLocalBackend(root_path=state_dir / "data")
        router.add_mount("/", data_backend)

        class MockNexusFS:
            pass

        nx_fs = MockNexusFS()
        nx_fs.router = router

        _mount_etc(nx_fs, str(state_dir))

        # Route resolves to the /etc mount with correct backend path
        route = router.route("/etc/conf.d/mounts", is_admin=True)
        assert route.mount_point == "/etc"
        assert route.backend_path == "conf.d/mounts"
        assert isinstance(route.backend, PathLocalBackend)


class TestRestoreMountsWithConfd:
    """Test that _restore_mounts reads from conf.d/mounts."""

    def test_auto_sync_from_confd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """auto_sync from conf.d/mounts takes precedence over env var."""
        confd = tmp_path / "etc" / "conf.d"
        confd.mkdir(parents=True)
        (confd / "mounts").write_text("auto_sync = true\n")

        monkeypatch.setenv("NEXUS_STATE_DIR", str(tmp_path))
        monkeypatch.delenv("NEXUS_AUTO_SYNC_MOUNTS", raising=False)

        from nexus.etc import get_brick_config

        cfg = get_brick_config("mounts", state_dir=tmp_path)
        assert cfg.get("auto_sync") is True

    def test_env_fallback_when_no_confd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Falls back to env var when conf.d/mounts doesn't exist."""
        monkeypatch.setenv("NEXUS_STATE_DIR", str(tmp_path))

        from nexus.etc import get_brick_config

        cfg = get_brick_config("mounts", state_dir=tmp_path)
        assert cfg == {}  # Empty — caller should fall back to env var
