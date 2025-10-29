"""Unit tests for FUSE mount management.

These tests verify the mount lifecycle, mode management, and error handling
for the FUSE mount manager.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.fuse.mount import MountMode, NexusFUSE, mount_nexus


@pytest.fixture
def mock_nexus_fs() -> MagicMock:
    """Create a mock Nexus filesystem."""
    fs = MagicMock()
    return fs


@pytest.fixture
def temp_mount_point(tmp_path: Path) -> Path:
    """Create a temporary mount point directory."""
    mount_dir = tmp_path / "mount"
    mount_dir.mkdir()
    return mount_dir


class TestMountMode:
    """Test MountMode enum."""

    def test_mount_mode_values(self) -> None:
        """Test that MountMode has correct values."""
        assert MountMode.BINARY.value == "binary"
        assert MountMode.TEXT.value == "text"
        assert MountMode.SMART.value == "smart"

    def test_mount_mode_from_string(self) -> None:
        """Test creating MountMode from string."""
        assert MountMode("binary") == MountMode.BINARY
        assert MountMode("text") == MountMode.TEXT
        assert MountMode("smart") == MountMode.SMART


class TestNexusFUSEInit:
    """Test NexusFUSE initialization."""

    def test_init_default_params(self, mock_nexus_fs: MagicMock, temp_mount_point: Path) -> None:
        """Test initialization with default parameters."""
        fuse = NexusFUSE(mock_nexus_fs, str(temp_mount_point))

        assert fuse.nexus_fs == mock_nexus_fs
        assert fuse.mount_point == temp_mount_point
        assert fuse.mode == MountMode.SMART
        assert fuse.cache_config is None
        assert fuse.fuse is None
        assert fuse._mount_thread is None
        assert fuse._mounted is False

    def test_init_custom_params(self, mock_nexus_fs: MagicMock, temp_mount_point: Path) -> None:
        """Test initialization with custom parameters."""
        cache_config = {"attr_cache_size": 2048}
        fuse = NexusFUSE(
            mock_nexus_fs,
            str(temp_mount_point),
            mode=MountMode.BINARY,
            cache_config=cache_config,
        )

        assert fuse.mode == MountMode.BINARY
        assert fuse.cache_config == cache_config

    def test_mount_point_converted_to_path(
        self, mock_nexus_fs: MagicMock, temp_mount_point: Path
    ) -> None:
        """Test that mount_point string is converted to Path."""
        fuse = NexusFUSE(mock_nexus_fs, str(temp_mount_point))
        assert isinstance(fuse.mount_point, Path)
        assert fuse.mount_point == temp_mount_point


class TestNexusFUSEMount:
    """Test mounting functionality."""

    @patch("nexus.fuse.mount.FUSE")
    def test_mount_foreground(
        self, mock_fuse_class: MagicMock, mock_nexus_fs: MagicMock, temp_mount_point: Path
    ) -> None:
        """Test mounting in foreground mode."""
        fuse_manager = NexusFUSE(mock_nexus_fs, str(temp_mount_point))

        # Mock FUSE to not block
        mock_fuse_instance = MagicMock()
        mock_fuse_class.return_value = mock_fuse_instance

        # Mount in foreground
        fuse_manager.mount(foreground=True)

        # Should have called FUSE constructor
        mock_fuse_class.assert_called_once()
        args, kwargs = mock_fuse_class.call_args
        assert args[1] == str(temp_mount_point)
        assert kwargs["foreground"] is True
        assert kwargs["nothreads"] is False

    @patch("nexus.fuse.mount.FUSE")
    @patch("time.sleep")
    def test_mount_background(
        self,
        mock_sleep: MagicMock,
        mock_fuse_class: MagicMock,
        mock_nexus_fs: MagicMock,
        temp_mount_point: Path,
    ) -> None:
        """Test mounting in background mode."""
        import threading

        fuse_manager = NexusFUSE(mock_nexus_fs, str(temp_mount_point))

        # Mock FUSE to block briefly so thread stays alive
        block_event = threading.Event()

        def blocking_fuse(*args, **kwargs):
            block_event.wait(timeout=1)  # Block for up to 1 second

        mock_fuse_class.side_effect = blocking_fuse

        # Mount in background
        fuse_manager.mount(foreground=False)

        try:
            # Should have started a thread
            assert fuse_manager._mount_thread is not None
            assert fuse_manager._mount_thread.is_alive()
            assert fuse_manager._mounted is True
        finally:
            # Clean up - unblock FUSE and wait for thread
            block_event.set()
            fuse_manager._mounted = False
            if fuse_manager._mount_thread:
                fuse_manager._mount_thread.join(timeout=2)

    def test_mount_already_mounted_raises_error(
        self, mock_nexus_fs: MagicMock, temp_mount_point: Path
    ) -> None:
        """Test that mounting when already mounted raises error."""
        fuse_manager = NexusFUSE(mock_nexus_fs, str(temp_mount_point))
        fuse_manager._mounted = True

        with pytest.raises(RuntimeError, match="already mounted"):
            fuse_manager.mount()

    def test_mount_nonexistent_directory_raises_error(
        self, mock_nexus_fs: MagicMock, tmp_path: Path
    ) -> None:
        """Test that mounting nonexistent directory raises error."""
        nonexistent = tmp_path / "nonexistent"
        fuse_manager = NexusFUSE(mock_nexus_fs, str(nonexistent))

        with pytest.raises(FileNotFoundError, match="does not exist"):
            fuse_manager.mount()

    def test_mount_non_directory_raises_error(
        self, mock_nexus_fs: MagicMock, tmp_path: Path
    ) -> None:
        """Test that mounting a file (not directory) raises error."""
        file_path = tmp_path / "file.txt"
        file_path.touch()
        fuse_manager = NexusFUSE(mock_nexus_fs, str(file_path))

        with pytest.raises(ValueError, match="not a directory"):
            fuse_manager.mount()

    @patch("nexus.fuse.mount.FUSE")
    @patch("nexus.fuse.mount.logger")
    def test_mount_non_empty_directory_warns(
        self,
        mock_logger: MagicMock,
        mock_fuse_class: MagicMock,
        mock_nexus_fs: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test that mounting non-empty directory logs warning."""
        mount_dir = tmp_path / "mount"
        mount_dir.mkdir()
        (mount_dir / "existing_file.txt").touch()

        fuse_manager = NexusFUSE(mock_nexus_fs, str(mount_dir))
        fuse_manager.mount(foreground=True)

        # Should have logged a warning
        mock_logger.warning.assert_called_once()
        assert "not empty" in mock_logger.warning.call_args[0][0]

    @patch("nexus.fuse.mount.FUSE")
    def test_mount_with_allow_other(
        self, mock_fuse_class: MagicMock, mock_nexus_fs: MagicMock, temp_mount_point: Path
    ) -> None:
        """Test mounting with allow_other option."""
        fuse_manager = NexusFUSE(mock_nexus_fs, str(temp_mount_point))
        fuse_manager.mount(foreground=True, allow_other=True)

        args, kwargs = mock_fuse_class.call_args
        assert kwargs["allow_other"] is True

    @patch("nexus.fuse.mount.FUSE")
    def test_mount_with_debug(
        self, mock_fuse_class: MagicMock, mock_nexus_fs: MagicMock, temp_mount_point: Path
    ) -> None:
        """Test mounting with debug option."""
        fuse_manager = NexusFUSE(mock_nexus_fs, str(temp_mount_point))
        fuse_manager.mount(foreground=True, debug=True)

        args, kwargs = mock_fuse_class.call_args
        assert kwargs["debug"] is True


class TestNexusFUSEUnmount:
    """Test unmounting functionality."""

    @patch("subprocess.run")
    @patch("platform.system", return_value="Darwin")
    def test_unmount_macos(
        self,
        mock_system: MagicMock,
        mock_subprocess: MagicMock,
        mock_nexus_fs: MagicMock,
        temp_mount_point: Path,
    ) -> None:
        """Test unmounting on macOS."""
        fuse_manager = NexusFUSE(mock_nexus_fs, str(temp_mount_point))
        fuse_manager._mounted = True

        mock_subprocess.return_value = MagicMock(returncode=0)

        fuse_manager.unmount()

        # Should have called umount
        mock_subprocess.assert_called_once()
        args = mock_subprocess.call_args[0][0]
        assert args == ["umount", str(temp_mount_point)]
        assert fuse_manager._mounted is False

    @patch("subprocess.run")
    @patch("platform.system", return_value="Linux")
    def test_unmount_linux(
        self,
        mock_system: MagicMock,
        mock_subprocess: MagicMock,
        mock_nexus_fs: MagicMock,
        temp_mount_point: Path,
    ) -> None:
        """Test unmounting on Linux."""
        fuse_manager = NexusFUSE(mock_nexus_fs, str(temp_mount_point))
        fuse_manager._mounted = True

        mock_subprocess.return_value = MagicMock(returncode=0)

        fuse_manager.unmount()

        # Should have called fusermount
        mock_subprocess.assert_called_once()
        args = mock_subprocess.call_args[0][0]
        assert args == ["fusermount", "-u", str(temp_mount_point)]
        assert fuse_manager._mounted is False

    @patch("platform.system", return_value="Windows")
    def test_unmount_unsupported_platform_raises_error(
        self, mock_system: MagicMock, mock_nexus_fs: MagicMock, temp_mount_point: Path
    ) -> None:
        """Test that unmounting on unsupported platform raises error."""
        fuse_manager = NexusFUSE(mock_nexus_fs, str(temp_mount_point))
        fuse_manager._mounted = True

        with pytest.raises(RuntimeError, match="Unsupported platform"):
            fuse_manager.unmount()

    def test_unmount_not_mounted_raises_error(
        self, mock_nexus_fs: MagicMock, temp_mount_point: Path
    ) -> None:
        """Test that unmounting when not mounted raises error."""
        fuse_manager = NexusFUSE(mock_nexus_fs, str(temp_mount_point))

        with pytest.raises(RuntimeError, match="not mounted"):
            fuse_manager.unmount()

    @patch("subprocess.run")
    @patch("platform.system", return_value="Linux")
    def test_unmount_failure_raises_error(
        self,
        mock_system: MagicMock,
        mock_subprocess: MagicMock,
        mock_nexus_fs: MagicMock,
        temp_mount_point: Path,
    ) -> None:
        """Test that unmount failure raises error."""
        import subprocess

        fuse_manager = NexusFUSE(mock_nexus_fs, str(temp_mount_point))
        fuse_manager._mounted = True

        # Mock subprocess to raise CalledProcessError
        mock_subprocess.side_effect = subprocess.CalledProcessError(
            1, "fusermount", stderr=b"unmount failed"
        )

        with pytest.raises(RuntimeError, match="Failed to unmount"):
            fuse_manager.unmount()


class TestNexusFUSEStatus:
    """Test status checking functionality."""

    def test_is_mounted_true(self, mock_nexus_fs: MagicMock, temp_mount_point: Path) -> None:
        """Test is_mounted returns True when mounted."""
        fuse_manager = NexusFUSE(mock_nexus_fs, str(temp_mount_point))
        fuse_manager._mounted = True

        assert fuse_manager.is_mounted() is True

    def test_is_mounted_false(self, mock_nexus_fs: MagicMock, temp_mount_point: Path) -> None:
        """Test is_mounted returns False when not mounted."""
        fuse_manager = NexusFUSE(mock_nexus_fs, str(temp_mount_point))
        fuse_manager._mounted = False

        assert fuse_manager.is_mounted() is False


class TestNexusFUSEWait:
    """Test wait functionality."""

    @patch("nexus.fuse.mount.FUSE")
    @patch("time.sleep")
    def test_wait_for_background_mount(
        self,
        mock_sleep: MagicMock,
        mock_fuse_class: MagicMock,
        mock_nexus_fs: MagicMock,
        temp_mount_point: Path,
    ) -> None:
        """Test waiting for background mount thread."""
        fuse_manager = NexusFUSE(mock_nexus_fs, str(temp_mount_point))

        # Start background mount
        mock_fuse_instance = MagicMock()
        mock_fuse_class.return_value = mock_fuse_instance

        fuse_manager.mount(foreground=False)

        # Mock the thread to finish quickly using an Event instead of sleep
        finish_event = threading.Event()

        def finish_thread() -> None:
            finish_event.set()
            fuse_manager._mounted = False

        thread = threading.Thread(target=finish_thread)
        fuse_manager._mount_thread = thread
        thread.start()

        # Wait for thread to signal completion
        finish_event.wait(timeout=1.0)

        # Wait should block until thread finishes
        fuse_manager.wait()

        assert not thread.is_alive()

    def test_wait_with_no_thread(self, mock_nexus_fs: MagicMock, temp_mount_point: Path) -> None:
        """Test that wait does nothing when no mount thread exists."""
        fuse_manager = NexusFUSE(mock_nexus_fs, str(temp_mount_point))

        # Should not raise any errors
        fuse_manager.wait()


class TestNexusFUSEContextManager:
    """Test context manager functionality."""

    @patch("subprocess.run")
    @patch("platform.system", return_value="Linux")
    def test_context_manager_unmounts_on_exit(
        self,
        mock_system: MagicMock,
        mock_subprocess: MagicMock,
        mock_nexus_fs: MagicMock,
        temp_mount_point: Path,
    ) -> None:
        """Test that context manager unmounts on exit."""
        mock_subprocess.return_value = MagicMock(returncode=0)

        fuse_manager = NexusFUSE(mock_nexus_fs, str(temp_mount_point))

        with fuse_manager as fuse:
            fuse._mounted = True
            assert fuse is fuse_manager

        # Should have unmounted
        assert fuse_manager._mounted is False

    @patch("nexus.fuse.mount.logger")
    def test_context_manager_handles_unmount_error(
        self, mock_logger: MagicMock, mock_nexus_fs: MagicMock, temp_mount_point: Path
    ) -> None:
        """Test that context manager handles unmount errors."""
        fuse_manager = NexusFUSE(mock_nexus_fs, str(temp_mount_point))

        with fuse_manager as fuse:
            fuse._mounted = True
            # Mock unmount to raise error
            with patch.object(fuse, "unmount", side_effect=RuntimeError("unmount failed")):
                pass  # Exit context

        # Should have logged error (may log multiple times)
        assert mock_logger.error.called

    def test_context_manager_does_nothing_if_not_mounted(
        self, mock_nexus_fs: MagicMock, temp_mount_point: Path
    ) -> None:
        """Test that context manager does nothing if not mounted."""
        fuse_manager = NexusFUSE(mock_nexus_fs, str(temp_mount_point))

        with fuse_manager:
            pass  # Not mounted

        # Should not raise any errors


class TestMountNexusFunction:
    """Test mount_nexus convenience function."""

    @patch("nexus.fuse.mount.NexusFUSE")
    def test_mount_nexus_default_params(
        self, mock_nexus_fuse_class: MagicMock, mock_nexus_fs: MagicMock, temp_mount_point: Path
    ) -> None:
        """Test mount_nexus with default parameters."""
        mock_instance = MagicMock()
        mock_nexus_fuse_class.return_value = mock_instance

        result = mount_nexus(mock_nexus_fs, str(temp_mount_point))

        # Should have created NexusFUSE instance
        mock_nexus_fuse_class.assert_called_once_with(
            mock_nexus_fs,
            str(temp_mount_point),
            mode=MountMode.SMART,
            cache_config=None,
        )

        # Should have called mount
        mock_instance.mount.assert_called_once_with(foreground=True, allow_other=False, debug=False)

        assert result == mock_instance

    @patch("nexus.fuse.mount.NexusFUSE")
    def test_mount_nexus_custom_params(
        self, mock_nexus_fuse_class: MagicMock, mock_nexus_fs: MagicMock, temp_mount_point: Path
    ) -> None:
        """Test mount_nexus with custom parameters."""
        mock_instance = MagicMock()
        mock_nexus_fuse_class.return_value = mock_instance

        cache_config = {"attr_cache_size": 2048}

        mount_nexus(
            mock_nexus_fs,
            str(temp_mount_point),
            mode="binary",
            foreground=False,
            allow_other=True,
            debug=True,
            cache_config=cache_config,
        )

        # Should have created NexusFUSE with correct params
        mock_nexus_fuse_class.assert_called_once_with(
            mock_nexus_fs,
            str(temp_mount_point),
            mode=MountMode.BINARY,
            cache_config=cache_config,
        )

        # Should have called mount with correct params
        mock_instance.mount.assert_called_once_with(foreground=False, allow_other=True, debug=True)

    @patch("nexus.fuse.mount.NexusFUSE")
    def test_mount_nexus_mode_conversion(
        self, mock_nexus_fuse_class: MagicMock, mock_nexus_fs: MagicMock, temp_mount_point: Path
    ) -> None:
        """Test that mount_nexus converts mode string to enum."""
        mock_instance = MagicMock()
        mock_nexus_fuse_class.return_value = mock_instance

        mount_nexus(mock_nexus_fs, str(temp_mount_point), mode="text")

        call_args = mock_nexus_fuse_class.call_args
        assert call_args[1]["mode"] == MountMode.TEXT
