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
    return tmp_path


def _make_mock_nx_fs():
    """Create a mock NexusFS that records sys_write calls and has a stub router."""
    writes: dict[str, bytes] = {}
    mounts: dict[str, object] = {}

    class _MockRouter:
        def add_mount(self, path, backend, **kwargs):
            mounts[path] = backend

    class _MockNexusFS:
        router = _MockRouter()

        def sys_write(self, path: str, content: bytes) -> int:
            writes[path] = content
            return len(content)

    return _MockNexusFS(), writes, mounts


class TestMountEtc:
    """Test _mount_etc() function."""

    def test_mounts_path_local_backend_and_writes(self, state_dir: Path) -> None:
        from nexus.__init__ import _mount_etc
        from nexus.backends.storage.path_local import PathLocalBackend

        nx_fs, writes, mounts = _make_mock_nx_fs()
        _mount_etc(nx_fs, str(state_dir))

        # PathLocalBackend mounted at /etc
        assert "/etc" in mounts
        assert isinstance(mounts["/etc"], PathLocalBackend)

        # Files sys_written to create metastore entries
        assert "/etc/conf.d/mounts" in writes
        assert b"auto_sync = true" in writes["/etc/conf.d/mounts"]
        assert "/etc/conf.d/cache" in writes

    def test_seeds_defaults_when_no_etc_dir(self, tmp_path: Path) -> None:
        from nexus.__init__ import _mount_etc

        nx_fs, writes, mounts = _make_mock_nx_fs()
        _mount_etc(nx_fs, str(tmp_path))

        # Defaults are seeded from repo etc/conf.d/
        assert len(writes) > 0
        assert "/etc" in mounts
        assert (tmp_path / "etc" / "conf.d").is_dir()

    def test_ignores_directories(self, state_dir: Path) -> None:
        from nexus.__init__ import _mount_etc

        # Add a subdirectory (not a file)
        (state_dir / "etc" / "conf.d" / "subdir").mkdir()

        nx_fs, writes, _mounts = _make_mock_nx_fs()
        _mount_etc(nx_fs, str(state_dir))

        # Only files are written, not directories
        assert all("subdir" not in p for p in writes)
        assert len(writes) == 2  # mounts + cache


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
