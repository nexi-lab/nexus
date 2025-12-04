"""Tests for sync operations module."""

from __future__ import annotations

from pathlib import Path

from nexus import connect
from nexus.sync import (
    SyncStats,
    copy_file,
    copy_recursive,
    is_local_path,
    list_local_files,
    move_file,
    sync_directories,
)


class TestSyncHelpers:
    """Test helper functions."""

    def test_is_local_path(self) -> None:
        """Test local path detection."""
        # Local paths
        assert is_local_path("./data")
        assert is_local_path("data")
        assert is_local_path("../data")

        # Nexus paths
        assert not is_local_path("/workspace/data")
        assert not is_local_path("/workspace/file.txt")

    def test_list_local_files(self, tmp_path: Path) -> None:
        """Test listing local files."""
        # Create test files
        (tmp_path / "file1.txt").write_text("content1")
        (tmp_path / "file2.txt").write_text("content2")
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "file3.txt").write_text("content3")

        # List all files recursively
        files = list_local_files(str(tmp_path))
        assert len(files) == 3
        assert all(Path(f).is_file() for f in files)

        # List single file
        single_file = list_local_files(str(tmp_path / "file1.txt"))
        assert len(single_file) == 1
        assert str(tmp_path / "file1.txt") in single_file


class TestCopyOperations:
    """Test copy operations."""

    def test_copy_file_local_to_nexus(self, tmp_path: Path) -> None:
        """Test copying from local to Nexus."""
        # Create local file
        local_file = tmp_path / "source.txt"
        local_file.write_text("test content")

        # Create Nexus instance (disable permission enforcement for sync tests)
        nx = connect(
            config={
                "data_dir": str(tmp_path / "nexus-data"),
                "enforce_permissions": False,
                "backend": "local",
            }
        )

        try:
            # Copy to Nexus
            bytes_copied = copy_file(nx, str(local_file), "/workspace/dest.txt", True, False, False)

            assert bytes_copied > 0
            assert nx.exists("/workspace/dest.txt")
            assert nx.read("/workspace/dest.txt") == b"test content"

        finally:
            nx.close()

    def test_copy_file_nexus_to_local(self, tmp_path: Path) -> None:
        """Test copying from Nexus to local."""
        # Create Nexus instance and file
        nx = connect(
            config={
                "data_dir": str(tmp_path / "nexus-data"),
                "enforce_permissions": False,
                "backend": "local",
            }
        )

        try:
            nx.write("/workspace/source.txt", b"test content")

            # Copy to local
            dest_file = tmp_path / "dest.txt"
            bytes_copied = copy_file(
                nx, "/workspace/source.txt", str(dest_file), False, True, False
            )

            assert bytes_copied > 0
            assert dest_file.exists()
            assert dest_file.read_text() == "test content"

        finally:
            nx.close()

    def test_copy_file_with_checksum_skip(self, tmp_path: Path) -> None:
        """Test that identical files are skipped with checksum enabled."""
        nx = connect(
            config={
                "data_dir": str(tmp_path / "nexus-data"),
                "enforce_permissions": False,
                "backend": "local",
            }
        )

        try:
            # Create source and destination with same content
            nx.write("/workspace/source.txt", b"test content")
            nx.write("/workspace/dest.txt", b"test content")

            # Copy with checksum enabled
            bytes_copied = copy_file(
                nx, "/workspace/source.txt", "/workspace/dest.txt", False, False, True
            )

            # Should skip because content is identical
            assert bytes_copied == 0

        finally:
            nx.close()

    def test_copy_recursive(self, tmp_path: Path) -> None:
        """Test recursive copy operation."""
        # Create local source directory
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        (source_dir / "file1.txt").write_text("content1")
        (source_dir / "file2.txt").write_text("content2")
        (source_dir / "subdir").mkdir()
        (source_dir / "subdir" / "file3.txt").write_text("content3")

        # Create Nexus instance (disable permission enforcement for sync tests)
        nx = connect(
            config={
                "data_dir": str(tmp_path / "nexus-data"),
                "enforce_permissions": False,
                "backend": "local",
            }
        )

        try:
            # Copy recursively to workspace
            stats = copy_recursive(
                nx, str(source_dir), "/workspace/backup", checksum=False, progress=False
            )

            assert stats.files_checked == 3
            assert stats.files_copied == 3
            assert stats.files_skipped == 0

            # Verify files exist in Nexus
            assert nx.exists("/workspace/backup/file1.txt")
            assert nx.exists("/workspace/backup/file2.txt")
            assert nx.exists("/workspace/backup/subdir/file3.txt")

        finally:
            nx.close()


class TestSyncOperations:
    """Test sync operations."""

    def test_sync_basic(self, tmp_path: Path) -> None:
        """Test basic sync operation."""
        # Create source directory
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        (source_dir / "file1.txt").write_text("content1")
        (source_dir / "file2.txt").write_text("content2")

        # Create Nexus instance (disable permission enforcement for sync tests)
        nx = connect(
            config={
                "data_dir": str(tmp_path / "nexus-data"),
                "enforce_permissions": False,
                "backend": "local",
            }
        )

        try:
            # Sync to Nexus workspace
            stats = sync_directories(
                nx,
                str(source_dir),
                "/workspace/sync",
                delete=False,
                dry_run=False,
                checksum=False,
                progress=False,
            )

            assert stats.files_checked == 2
            assert stats.files_copied == 2
            assert stats.files_deleted == 0

        finally:
            nx.close()

    def test_sync_with_delete(self, tmp_path: Path) -> None:
        """Test sync with delete option."""
        # Create source directory
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        (source_dir / "file1.txt").write_text("content1")

        # Create Nexus instance with existing file (disable permission enforcement for sync tests)
        nx = connect(
            config={
                "data_dir": str(tmp_path / "nexus-data"),
                "enforce_permissions": False,
                "backend": "local",
            }
        )

        try:
            # Create files in destination
            nx.write("/workspace/dest/file1.txt", b"old content")
            nx.write("/workspace/dest/file2.txt", b"to be deleted")

            # Sync with delete
            stats = sync_directories(
                nx,
                str(source_dir),
                "/workspace/dest",
                delete=True,
                dry_run=False,
                checksum=False,
                progress=False,
            )

            assert stats.files_deleted == 1
            assert not nx.exists("/workspace/dest/file2.txt")
            assert nx.exists("/workspace/dest/file1.txt")

        finally:
            nx.close()

    def test_sync_dry_run(self, tmp_path: Path) -> None:
        """Test sync dry run mode."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        (source_dir / "file1.txt").write_text("content1")

        nx = connect(config={"data_dir": str(tmp_path / "nexus-data")})

        try:
            # Dry run sync
            stats = sync_directories(
                nx,
                str(source_dir),
                "/workspace/dest",
                delete=False,
                dry_run=True,
                checksum=False,
                progress=False,
            )

            assert stats.files_checked == 1
            assert stats.files_copied == 1  # Would copy

            # But file should not actually exist
            assert not nx.exists("/workspace/dest/file1.txt")

        finally:
            nx.close()


class TestMoveOperations:
    """Test move operations."""

    def test_move_file_within_nexus(self, tmp_path: Path) -> None:
        """Test moving file within Nexus."""
        nx = connect(
            config={
                "data_dir": str(tmp_path / "nexus-data"),
                "enforce_permissions": False,
                "backend": "local",
            }
        )

        try:
            # Create source file
            nx.write("/workspace/source.txt", b"test content")

            # Move file
            success = move_file(nx, "/workspace/source.txt", "/workspace/dest.txt")

            assert success
            assert not nx.exists("/workspace/source.txt")
            assert nx.exists("/workspace/dest.txt")
            assert nx.read("/workspace/dest.txt") == b"test content"

        finally:
            nx.close()

    def test_move_file_local_to_local(self, tmp_path: Path) -> None:
        """Test moving file locally."""
        source_file = tmp_path / "source.txt"
        source_file.write_text("test content")

        dest_file = tmp_path / "dest.txt"

        # Create minimal Nexus instance (not actually used for local-to-local move)
        nx = connect(config={"data_dir": str(tmp_path / "nexus-data")})

        try:
            # Move file
            success = move_file(nx, str(source_file), str(dest_file))

            assert success
            assert not source_file.exists()
            assert dest_file.exists()
            assert dest_file.read_text() == "test content"

        finally:
            nx.close()


class TestSyncStats:
    """Test SyncStats class."""

    def test_stats_initialization(self) -> None:
        """Test stats initialization."""
        stats = SyncStats()

        assert stats.files_checked == 0
        assert stats.files_copied == 0
        assert stats.files_skipped == 0
        assert stats.files_deleted == 0
        assert stats.bytes_transferred == 0
        assert stats.errors == []

    def test_stats_tracking(self) -> None:
        """Test stats tracking during operations."""
        stats = SyncStats()

        stats.files_checked = 10
        stats.files_copied = 7
        stats.files_skipped = 3
        stats.bytes_transferred = 1024

        assert stats.files_checked == 10
        assert stats.files_copied == 7
        assert stats.files_skipped == 3
        assert stats.bytes_transferred == 1024
