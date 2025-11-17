"""Unit tests for glob_fast Rust acceleration module."""

from unittest.mock import Mock, patch

from nexus.core import glob_fast


class TestGlobMatchBulk:
    """Test glob_match_bulk function."""

    def test_glob_match_bulk_when_rust_unavailable(self) -> None:
        """Test that glob_match_bulk returns None when Rust is unavailable."""
        with patch.object(glob_fast, "RUST_AVAILABLE", False):  # noqa: SIM117
            with patch.object(glob_fast, "_rust_glob_match_bulk", None):
                result = glob_fast.glob_match_bulk(["**/*.py"], ["/src/main.py"])
                assert result is None

    def test_glob_match_bulk_with_rust_available(self) -> None:
        """Test glob_match_bulk when Rust extension is available."""
        mock_rust_fn = Mock(return_value=["/src/main.py", "/tests/test_main.py"])

        with patch.object(glob_fast, "RUST_AVAILABLE", True):  # noqa: SIM117
            with patch.object(glob_fast, "_rust_glob_match_bulk", mock_rust_fn):
                patterns = ["**/*.py", "*.txt"]
                paths = ["/src/main.py", "/README.md", "/tests/test_main.py", "/data.json"]

                result = glob_fast.glob_match_bulk(patterns, paths)

                assert result == ["/src/main.py", "/tests/test_main.py"]
                mock_rust_fn.assert_called_once_with(patterns, paths)

    def test_glob_match_bulk_empty_patterns(self) -> None:
        """Test glob_match_bulk with empty patterns list."""
        mock_rust_fn = Mock(return_value=[])

        with patch.object(glob_fast, "RUST_AVAILABLE", True):  # noqa: SIM117
            with patch.object(glob_fast, "_rust_glob_match_bulk", mock_rust_fn):
                result = glob_fast.glob_match_bulk([], ["/src/main.py"])

                assert result == []
                mock_rust_fn.assert_called_once_with([], ["/src/main.py"])

    def test_glob_match_bulk_empty_paths(self) -> None:
        """Test glob_match_bulk with empty paths list."""
        mock_rust_fn = Mock(return_value=[])

        with patch.object(glob_fast, "RUST_AVAILABLE", True):  # noqa: SIM117
            with patch.object(glob_fast, "_rust_glob_match_bulk", mock_rust_fn):
                result = glob_fast.glob_match_bulk(["**/*.py"], [])

                assert result == []
                mock_rust_fn.assert_called_once_with(["**/*.py"], [])

    def test_glob_match_bulk_handles_rust_exception(self) -> None:
        """Test that glob_match_bulk returns None when Rust function raises exception."""
        mock_rust_fn = Mock(side_effect=RuntimeError("Rust error"))

        with patch.object(glob_fast, "RUST_AVAILABLE", True):  # noqa: SIM117
            with patch.object(glob_fast, "_rust_glob_match_bulk", mock_rust_fn):
                result = glob_fast.glob_match_bulk(["**/*.py"], ["/src/main.py"])

                # Should return None to fallback to Python implementation
                assert result is None

    def test_glob_match_bulk_or_semantics(self) -> None:
        """Test that glob_match_bulk uses OR semantics for multiple patterns."""
        # Mock matches both .py and .txt files
        mock_rust_fn = Mock(return_value=["/src/main.py", "/README.txt"])

        with patch.object(glob_fast, "RUST_AVAILABLE", True):  # noqa: SIM117
            with patch.object(glob_fast, "_rust_glob_match_bulk", mock_rust_fn):
                patterns = ["**/*.py", "*.txt"]
                paths = ["/src/main.py", "/README.txt", "/data.json"]

                result = glob_fast.glob_match_bulk(patterns, paths)

                assert "/src/main.py" in result
                assert "/README.txt" in result
                assert "/data.json" not in result


class TestIsAvailable:
    """Test is_available function."""

    def test_is_available_when_rust_loaded(self) -> None:
        """Test is_available returns True when Rust extension is loaded."""
        with patch.object(glob_fast, "RUST_AVAILABLE", True):
            assert glob_fast.is_available() is True

    def test_is_available_when_rust_not_loaded(self) -> None:
        """Test is_available returns False when Rust extension is not loaded."""
        with patch.object(glob_fast, "RUST_AVAILABLE", False):
            assert glob_fast.is_available() is False


class TestModuleConstants:
    """Test module-level constants and imports."""

    def test_rust_available_is_boolean(self) -> None:
        """Test that RUST_AVAILABLE is a boolean."""
        assert isinstance(glob_fast.RUST_AVAILABLE, bool)

    def test_rust_glob_match_bulk_type(self) -> None:
        """Test that _rust_glob_match_bulk is callable or None."""
        assert glob_fast._rust_glob_match_bulk is None or callable(glob_fast._rust_glob_match_bulk)
