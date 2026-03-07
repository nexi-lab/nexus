"""Tests for /etc VFS write functionality."""

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


class TestMountEtc:
    """Test _mount_etc() function."""

    def test_writes_etc_files_to_vfs(self, state_dir: Path) -> None:
        from nexus.__init__ import _mount_etc

        writes: dict[str, bytes] = {}

        class MockNexusFS:
            def sys_write(self, path: str, content: bytes) -> int:
                writes[path] = content
                return len(content)

        nx_fs = MockNexusFS()
        _mount_etc(nx_fs, str(state_dir))

        assert "/etc/conf.d/mounts" in writes
        assert b"auto_sync = true" in writes["/etc/conf.d/mounts"]
        assert "/etc/conf.d/cache" in writes
        assert b'backend = "dragonfly"' in writes["/etc/conf.d/cache"]

    def test_seeds_defaults_when_no_etc_dir(self, tmp_path: Path) -> None:
        from nexus.__init__ import _mount_etc

        writes: dict[str, bytes] = {}

        class MockNexusFS:
            def sys_write(self, path: str, content: bytes) -> int:
                writes[path] = content
                return len(content)

        nx_fs = MockNexusFS()
        _mount_etc(nx_fs, str(tmp_path))

        # Defaults are seeded from repo etc/conf.d/
        assert len(writes) > 0
        assert (tmp_path / "etc" / "conf.d").is_dir()

    def test_ignores_directories(self, state_dir: Path) -> None:
        from nexus.__init__ import _mount_etc

        # Add a subdirectory (not a file)
        (state_dir / "etc" / "conf.d" / "subdir").mkdir()

        writes: dict[str, bytes] = {}

        class MockNexusFS:
            def sys_write(self, path: str, content: bytes) -> int:
                writes[path] = content
                return len(content)

        nx_fs = MockNexusFS()
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
