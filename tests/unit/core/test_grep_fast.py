"""Unit tests for grep_fast Rust acceleration module."""

from unittest.mock import Mock, patch

from nexus.core import grep_fast


class TestGrepBulk:
    """Test grep_bulk function."""

    def test_grep_bulk_when_rust_unavailable(self) -> None:
        """Test that grep_bulk returns None when Rust is unavailable."""
        with patch.object(grep_fast, "RUST_AVAILABLE", False):  # noqa: SIM117
            with patch.object(grep_fast, "_rust_grep_bulk", None):
                result = grep_fast.grep_bulk("pattern", {"file.txt": b"content"})
                assert result is None

    def test_grep_bulk_with_rust_available(self) -> None:
        """Test grep_bulk when Rust extension is available."""
        expected_matches = [
            {
                "file": "/src/main.py",
                "line": 10,
                "content": "def hello():",
                "match": "hello",
            },
            {
                "file": "/src/main.py",
                "line": 11,
                "content": "    print('hello world')",
                "match": "hello",
            },
        ]
        mock_rust_fn = Mock(return_value=expected_matches)

        with patch.object(grep_fast, "RUST_AVAILABLE", True):  # noqa: SIM117
            with patch.object(grep_fast, "_rust_grep_bulk", mock_rust_fn):
                file_contents = {
                    "/src/main.py": b"def hello():\n    print('hello world')\n",
                }

                result = grep_fast.grep_bulk("hello", file_contents)

                assert result == expected_matches
                mock_rust_fn.assert_called_once_with("hello", file_contents, False, 1000)

    def test_grep_bulk_with_ignore_case(self) -> None:
        """Test grep_bulk with case-insensitive search."""
        mock_rust_fn = Mock(return_value=[{"file": "test.txt", "line": 1}])

        with patch.object(grep_fast, "RUST_AVAILABLE", True):  # noqa: SIM117
            with patch.object(grep_fast, "_rust_grep_bulk", mock_rust_fn):
                file_contents = {"test.txt": b"HELLO world"}

                result = grep_fast.grep_bulk("hello", file_contents, ignore_case=True)

                assert result is not None
                mock_rust_fn.assert_called_once_with("hello", file_contents, True, 1000)

    def test_grep_bulk_with_custom_max_results(self) -> None:
        """Test grep_bulk with custom max_results."""
        mock_rust_fn = Mock(return_value=[])

        with patch.object(grep_fast, "RUST_AVAILABLE", True):  # noqa: SIM117
            with patch.object(grep_fast, "_rust_grep_bulk", mock_rust_fn):
                file_contents = {"test.txt": b"content"}

                result = grep_fast.grep_bulk("pattern", file_contents, max_results=500)

                assert result == []
                mock_rust_fn.assert_called_once_with("pattern", file_contents, False, 500)

    def test_grep_bulk_with_all_parameters(self) -> None:
        """Test grep_bulk with all parameters specified."""
        mock_rust_fn = Mock(return_value=[{"file": "a.txt", "line": 1}])

        with patch.object(grep_fast, "RUST_AVAILABLE", True):  # noqa: SIM117
            with patch.object(grep_fast, "_rust_grep_bulk", mock_rust_fn):
                file_contents = {
                    "a.txt": b"Test Content",
                    "b.txt": b"More Content",
                }

                result = grep_fast.grep_bulk(
                    "test",
                    file_contents,
                    ignore_case=True,
                    max_results=100,
                )

                assert result == [{"file": "a.txt", "line": 1}]
                mock_rust_fn.assert_called_once_with("test", file_contents, True, 100)

    def test_grep_bulk_empty_file_contents(self) -> None:
        """Test grep_bulk with empty file contents."""
        mock_rust_fn = Mock(return_value=[])

        with patch.object(grep_fast, "RUST_AVAILABLE", True):  # noqa: SIM117
            with patch.object(grep_fast, "_rust_grep_bulk", mock_rust_fn):
                result = grep_fast.grep_bulk("pattern", {})

                assert result == []
                mock_rust_fn.assert_called_once_with("pattern", {}, False, 1000)

    def test_grep_bulk_handles_rust_exception(self) -> None:
        """Test that grep_bulk returns None when Rust function raises exception."""
        mock_rust_fn = Mock(side_effect=RuntimeError("Rust regex error"))

        with patch.object(grep_fast, "RUST_AVAILABLE", True):  # noqa: SIM117
            with patch.object(grep_fast, "_rust_grep_bulk", mock_rust_fn):
                file_contents = {"test.txt": b"content"}

                result = grep_fast.grep_bulk("pattern", file_contents)

                # Should return None to fallback to Python implementation
                assert result is None

    def test_grep_bulk_match_structure(self) -> None:
        """Test that grep_bulk returns matches with correct structure."""
        matches = [
            {
                "file": "/project/README.md",
                "line": 5,
                "content": "This is a sample README file",
                "match": "sample",
            }
        ]
        mock_rust_fn = Mock(return_value=matches)

        with patch.object(grep_fast, "RUST_AVAILABLE", True):  # noqa: SIM117
            with patch.object(grep_fast, "_rust_grep_bulk", mock_rust_fn):
                file_contents = {"/project/README.md": b"This is a sample README file"}

                result = grep_fast.grep_bulk("sample", file_contents)

                assert result is not None
                assert len(result) == 1
                match = result[0]
                assert "file" in match
                assert "line" in match
                assert "content" in match
                assert "match" in match
                assert match["file"] == "/project/README.md"
                assert match["line"] == 5
                assert match["match"] == "sample"

    def test_grep_bulk_default_parameters(self) -> None:
        """Test grep_bulk uses correct default parameter values."""
        mock_rust_fn = Mock(return_value=[])

        with patch.object(grep_fast, "RUST_AVAILABLE", True):  # noqa: SIM117
            with patch.object(grep_fast, "_rust_grep_bulk", mock_rust_fn):
                file_contents = {"test.txt": b"content"}

                # Call with minimal parameters
                grep_fast.grep_bulk("pattern", file_contents)

                # Verify defaults: ignore_case=False, max_results=1000
                mock_rust_fn.assert_called_once_with(
                    "pattern",
                    file_contents,
                    False,  # ignore_case default
                    1000,  # max_results default
                )


class TestIsAvailable:
    """Test is_available function."""

    def test_is_available_when_rust_loaded(self) -> None:
        """Test is_available returns True when Rust extension is loaded."""
        with patch.object(grep_fast, "RUST_AVAILABLE", True):
            assert grep_fast.is_available() is True

    def test_is_available_when_rust_not_loaded(self) -> None:
        """Test is_available returns False when Rust extension is not loaded."""
        with patch.object(grep_fast, "RUST_AVAILABLE", False):
            assert grep_fast.is_available() is False


class TestModuleConstants:
    """Test module-level constants and imports."""

    def test_rust_available_is_boolean(self) -> None:
        """Test that RUST_AVAILABLE is a boolean."""
        assert isinstance(grep_fast.RUST_AVAILABLE, bool)

    def test_rust_grep_bulk_type(self) -> None:
        """Test that _rust_grep_bulk is callable or None."""
        assert grep_fast._rust_grep_bulk is None or callable(grep_fast._rust_grep_bulk)
