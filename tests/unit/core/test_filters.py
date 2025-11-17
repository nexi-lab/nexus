"""Unit tests for file filtering utilities."""

from unittest.mock import Mock, patch

from nexus.core import filters


class TestIsOsMetadataFile:
    """Test is_os_metadata_file function."""

    def test_apple_double_files(self) -> None:
        """Test detection of AppleDouble files (._*)."""
        assert filters.is_os_metadata_file("._test.txt") is True
        assert filters.is_os_metadata_file("._hidden") is True
        assert filters.is_os_metadata_file("/path/to/._file") is True
        assert filters.is_os_metadata_file("normal_file.txt") is False

    def test_ds_store(self) -> None:
        """Test detection of .DS_Store files."""
        assert filters.is_os_metadata_file(".DS_Store") is True
        assert filters.is_os_metadata_file("/path/to/.DS_Store") is True
        assert filters.is_os_metadata_file("DS_Store") is False
        assert filters.is_os_metadata_file(".DS_Store.txt") is False

    def test_windows_metadata(self) -> None:
        """Test detection of Windows metadata files."""
        assert filters.is_os_metadata_file("Thumbs.db") is True
        assert filters.is_os_metadata_file("desktop.ini") is True
        assert filters.is_os_metadata_file("/path/to/Thumbs.db") is True
        assert filters.is_os_metadata_file("/path/to/desktop.ini") is True

    def test_macos_system_folders(self) -> None:
        """Test detection of macOS system folders."""
        assert filters.is_os_metadata_file(".Spotlight-V100") is True
        assert filters.is_os_metadata_file(".Trashes") is True
        assert filters.is_os_metadata_file(".fseventsd") is True
        assert filters.is_os_metadata_file(".TemporaryItems") is True
        assert filters.is_os_metadata_file(".VolumeIcon.icns") is True
        assert filters.is_os_metadata_file(".com.apple.timemachine.donotpresent") is True

    def test_normal_files(self) -> None:
        """Test that normal files are not detected as OS metadata."""
        assert filters.is_os_metadata_file("file.txt") is False
        assert filters.is_os_metadata_file("document.pdf") is False
        assert filters.is_os_metadata_file(".gitignore") is False
        assert filters.is_os_metadata_file(".env") is False
        assert filters.is_os_metadata_file("README.md") is False

    def test_hidden_files_not_metadata(self) -> None:
        """Test that hidden files (starting with .) are not all metadata."""
        assert filters.is_os_metadata_file(".bashrc") is False
        assert filters.is_os_metadata_file(".config") is False
        assert filters.is_os_metadata_file(".ssh") is False

    def test_path_extraction(self) -> None:
        """Test that function correctly extracts filename from path."""
        assert filters.is_os_metadata_file("/usr/local/bin/.DS_Store") is True
        assert filters.is_os_metadata_file("/home/user/documents/._test.txt") is True
        assert filters.is_os_metadata_file("/var/tmp/file.txt") is False

    def test_empty_path(self) -> None:
        """Test handling of empty path."""
        assert filters.is_os_metadata_file("") is False


class TestFilterOsMetadata:
    """Test filter_os_metadata function."""

    def test_filter_with_python_fallback(self) -> None:
        """Test filtering using Python fallback (< 10 files)."""
        files = [
            "file1.txt",
            "._file2.txt",
            ".DS_Store",
            "file3.pdf",
            "Thumbs.db",
        ]

        result = filters.filter_os_metadata(files)

        assert result == ["file1.txt", "file3.pdf"]
        assert "._file2.txt" not in result
        assert ".DS_Store" not in result
        assert "Thumbs.db" not in result

    def test_filter_empty_list(self) -> None:
        """Test filtering empty list."""
        result = filters.filter_os_metadata([])
        assert result == []

    def test_filter_all_valid_files(self) -> None:
        """Test filtering list with no OS metadata files."""
        files = ["file1.txt", "file2.pdf", "file3.md"]
        result = filters.filter_os_metadata(files)
        assert result == files

    def test_filter_all_metadata_files(self) -> None:
        """Test filtering list with only OS metadata files."""
        files = ["._file1", ".DS_Store", "Thumbs.db"]
        result = filters.filter_os_metadata(files)
        assert result == []

    def test_filter_with_rust_available(self) -> None:
        """Test filtering using Rust acceleration (>= 10 files)."""
        files = [f"file{i}.txt" for i in range(8)] + ["._hidden", ".DS_Store", "Thumbs.db"]
        expected_filtered = [f"file{i}.txt" for i in range(8)]

        mock_rust_fn = Mock(return_value=expected_filtered)
        mock_nexus_fast = Mock()
        mock_nexus_fast.filter_paths = mock_rust_fn

        with patch.object(filters, "RUST_AVAILABLE", True):  # noqa: SIM117
            with patch("nexus.core.filters.nexus_fast", mock_nexus_fast, create=True):
                result = filters.filter_os_metadata(files)

                assert result == expected_filtered
                mock_rust_fn.assert_called_once_with(files, filters.OS_METADATA_PATTERNS)

    def test_filter_rust_exception_fallback(self) -> None:
        """Test fallback to Python when Rust raises exception."""
        files = [
            "file1.txt",
            "._file2.txt",
            ".DS_Store",
            "file3.pdf",
            "Thumbs.db",
            "file4.txt",
            "file5.txt",
            "file6.txt",
            "file7.txt",
            "file8.txt",
            "file9.txt",
        ]

        mock_rust_fn = Mock(side_effect=RuntimeError("Rust error"))
        mock_nexus_fast = Mock()
        mock_nexus_fast.filter_paths = mock_rust_fn

        with patch.object(filters, "RUST_AVAILABLE", True):  # noqa: SIM117
            with patch("nexus.core.filters.nexus_fast", mock_nexus_fast, create=True):
                result = filters.filter_os_metadata(files)

                # Should fall back to Python implementation
                assert "file1.txt" in result
                assert "._file2.txt" not in result
                assert ".DS_Store" not in result
                assert "Thumbs.db" not in result

    def test_filter_threshold_9_files_uses_python(self) -> None:
        """Test that < 10 files uses Python even if Rust available."""
        files = [f"file{i}.txt" for i in range(9)]

        mock_rust_fn = Mock(return_value=files)
        mock_nexus_fast = Mock()
        mock_nexus_fast.filter_paths = mock_rust_fn

        with patch.object(filters, "RUST_AVAILABLE", True):  # noqa: SIM117
            with patch("nexus.core.filters.nexus_fast", mock_nexus_fast, create=True):
                result = filters.filter_os_metadata(files)

                # Should NOT call Rust with only 9 files
                mock_rust_fn.assert_not_called()
                assert result == files

    def test_filter_threshold_10_files_uses_rust(self) -> None:
        """Test that >= 10 files uses Rust if available."""
        files = [f"file{i}.txt" for i in range(10)]

        mock_rust_fn = Mock(return_value=files)
        mock_nexus_fast = Mock()
        mock_nexus_fast.filter_paths = mock_rust_fn

        with patch.object(filters, "RUST_AVAILABLE", True):  # noqa: SIM117
            with patch("nexus.core.filters.nexus_fast", mock_nexus_fast, create=True):
                result = filters.filter_os_metadata(files)

                # Should call Rust with 10+ files
                mock_rust_fn.assert_called_once()
                assert result == files

    def test_filter_preserves_order(self) -> None:
        """Test that filtering preserves original file order."""
        files = [
            "zebra.txt",
            "._meta",
            "apple.txt",
            ".DS_Store",
            "banana.txt",
        ]

        result = filters.filter_os_metadata(files)

        assert result == ["zebra.txt", "apple.txt", "banana.txt"]


class TestFilterOsMetadataDicts:
    """Test filter_os_metadata_dicts function."""

    def test_filter_dicts_with_path_key(self) -> None:
        """Test filtering dictionaries with 'path' key."""
        files = [
            {"path": "file1.txt", "size": 100},
            {"path": "._file2.txt", "size": 50},
            {"path": ".DS_Store", "size": 10},
            {"path": "file3.pdf", "size": 200},
        ]

        result = filters.filter_os_metadata_dicts(files)

        assert len(result) == 2
        assert result[0]["path"] == "file1.txt"
        assert result[1]["path"] == "file3.pdf"

    def test_filter_dicts_empty_list(self) -> None:
        """Test filtering empty dictionary list."""
        result = filters.filter_os_metadata_dicts([])
        assert result == []

    def test_filter_dicts_missing_path_key(self) -> None:
        """Test filtering dictionaries with missing 'path' key."""
        files = [
            {"name": "file1.txt"},
            {"path": "._file2.txt"},
            {"path": "file3.txt"},
        ]

        result = filters.filter_os_metadata_dicts(files)

        # Dict without 'path' key gets empty string, which is not OS metadata
        assert len(result) == 2
        assert {"name": "file1.txt"} in result
        assert {"path": "file3.txt"} in result

    def test_filter_dicts_all_valid_files(self) -> None:
        """Test filtering dictionaries with no OS metadata."""
        files = [
            {"path": "file1.txt"},
            {"path": "file2.pdf"},
            {"path": "file3.md"},
        ]

        result = filters.filter_os_metadata_dicts(files)
        assert result == files

    def test_filter_dicts_all_metadata(self) -> None:
        """Test filtering dictionaries with only OS metadata."""
        files = [
            {"path": "._file1"},
            {"path": ".DS_Store"},
            {"path": "Thumbs.db"},
        ]

        result = filters.filter_os_metadata_dicts(files)
        assert result == []

    def test_filter_dicts_preserves_structure(self) -> None:
        """Test that filtering preserves dictionary structure."""
        files = [
            {"path": "file1.txt", "size": 100, "mtime": 123456},
            {"path": "._meta", "size": 10, "mtime": 789012},
            {"path": "file2.txt", "size": 200, "mtime": 345678},
        ]

        result = filters.filter_os_metadata_dicts(files)

        assert len(result) == 2
        assert result[0] == {"path": "file1.txt", "size": 100, "mtime": 123456}
        assert result[1] == {"path": "file2.txt", "size": 200, "mtime": 345678}

    def test_filter_dicts_with_nested_paths(self) -> None:
        """Test filtering dictionaries with nested file paths."""
        files = [
            {"path": "/usr/local/file1.txt"},
            {"path": "/usr/local/.DS_Store"},
            {"path": "/home/user/._hidden"},
            {"path": "/var/tmp/file2.txt"},
        ]

        result = filters.filter_os_metadata_dicts(files)

        assert len(result) == 2
        assert result[0]["path"] == "/usr/local/file1.txt"
        assert result[1]["path"] == "/var/tmp/file2.txt"


class TestModuleConstants:
    """Test module-level constants and imports."""

    def test_rust_available_is_boolean(self) -> None:
        """Test that RUST_AVAILABLE is a boolean."""
        assert isinstance(filters.RUST_AVAILABLE, bool)

    def test_os_metadata_patterns_defined(self) -> None:
        """Test that OS_METADATA_PATTERNS is defined and has expected entries."""
        assert isinstance(filters.OS_METADATA_PATTERNS, list)
        assert len(filters.OS_METADATA_PATTERNS) > 0

        # Verify key patterns are present
        assert "._*" in filters.OS_METADATA_PATTERNS
        assert ".DS_Store" in filters.OS_METADATA_PATTERNS
        assert "Thumbs.db" in filters.OS_METADATA_PATTERNS
        assert "desktop.ini" in filters.OS_METADATA_PATTERNS

    def test_os_metadata_patterns_immutable(self) -> None:
        """Test that OS_METADATA_PATTERNS list contains expected patterns."""
        expected_patterns = {
            "._*",
            ".DS_Store",
            "Thumbs.db",
            "desktop.ini",
            ".Spotlight-V100",
            ".Trashes",
            ".fseventsd",
            ".TemporaryItems",
            ".VolumeIcon.icns",
            ".com.apple.timemachine.donotpresent",
        }

        assert set(filters.OS_METADATA_PATTERNS) == expected_patterns
