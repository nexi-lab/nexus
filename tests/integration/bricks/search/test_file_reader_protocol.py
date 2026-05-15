"""Tests for FileReaderProtocol contract (Issue #1520).

Validates that FileReaderProtocol is runtime_checkable, mock implementations
satisfy isinstance checks, and all method contracts are correct.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from nexus.bricks.search.protocols import FileReaderProtocol

# =============================================================================
# Protocol structural checks
# =============================================================================


class TestFileReaderProtocolStructure:
    """Verify FileReaderProtocol is runtime_checkable and well-formed."""

    def test_protocol_is_runtime_checkable(self) -> None:
        """Protocol must support isinstance checks."""
        assert callable(getattr(FileReaderProtocol, "__instancecheck__", None))

    def test_has_read_text(self) -> None:
        assert hasattr(FileReaderProtocol, "read_text")

    def test_has_get_searchable_text(self) -> None:
        assert hasattr(FileReaderProtocol, "get_searchable_text")

    def test_has_list_files(self) -> None:
        assert hasattr(FileReaderProtocol, "list_files")

    def test_has_get_session(self) -> None:
        assert hasattr(FileReaderProtocol, "get_session")

    def test_has_get_path_id(self) -> None:
        assert hasattr(FileReaderProtocol, "get_path_id")

    def test_has_get_content_hash(self) -> None:
        assert hasattr(FileReaderProtocol, "get_content_hash")


# =============================================================================
# Mock implementation
# =============================================================================


class MockFileReader:
    """Minimal mock satisfying FileReaderProtocol."""

    def __init__(self) -> None:
        self._files: dict[str, str] = {
            "/src/main.py": "def main(): pass",
            "/src/utils.py": "def helper(): pass",
            "/docs/readme.md": "# README",
        }

    def read_text(self, path: str) -> str:
        if path not in self._files:
            raise FileNotFoundError(f"File not found: {path}")
        return self._files[path]

    def get_searchable_text(self, path: str) -> str | None:
        content = self._files.get(path)
        if content is None:
            return None
        # Simulate parsed text (e.g., strip comments)
        return content

    def list_files(self, path: str, recursive: bool = True) -> list[str]:
        prefix = path if path.endswith("/") else path + "/"
        if path == "/":
            prefix = "/"
        return [p for p in self._files if p.startswith(prefix) or path == "/"]

    @contextmanager
    def get_session(self) -> Iterator[Any]:
        """Mock session context manager."""
        yield {"mock": "session"}

    def get_path_id(self, path: str) -> str | None:
        if path in self._files:
            return f"id-{path}"
        return None

    def get_content_hash(self, path: str) -> str | None:
        content = self._files.get(path)
        if content is None:
            return None
        return f"hash-{len(content)}"


class TestMockSatisfiesProtocol:
    """Verify MockFileReader passes isinstance check."""

    def test_isinstance_check(self) -> None:
        reader = MockFileReader()
        assert isinstance(reader, FileReaderProtocol)


# =============================================================================
# Method contract tests
# =============================================================================


class TestReadText:
    """Test read_text() method contract."""

    def test_returns_string(self) -> None:
        reader = MockFileReader()
        result = reader.read_text("/src/main.py")
        assert isinstance(result, str)
        assert "def main" in result

    def test_raises_for_missing_file(self) -> None:
        reader = MockFileReader()
        with pytest.raises(FileNotFoundError):
            reader.read_text("/nonexistent.py")


class TestGetSearchableText:
    """Test get_searchable_text() method contract."""

    def test_returns_string_for_existing_file(self) -> None:
        reader = MockFileReader()
        result = reader.get_searchable_text("/src/main.py")
        assert isinstance(result, str)

    def test_returns_none_for_missing_file(self) -> None:
        reader = MockFileReader()
        result = reader.get_searchable_text("/nonexistent.py")
        assert result is None


class TestListFiles:
    """Test list_files() method contract."""

    def test_returns_list(self) -> None:
        reader = MockFileReader()
        result = reader.list_files("/")
        assert isinstance(result, list)
        assert len(result) == 3

    def test_filters_by_path(self) -> None:
        reader = MockFileReader()
        result = reader.list_files("/src")
        assert all("/src/" in f for f in result)

    def test_recursive_default_true(self) -> None:
        reader = MockFileReader()
        result = reader.list_files("/", recursive=True)
        assert len(result) >= 1


class TestGetSession:
    """Test get_session() context manager contract."""

    def test_returns_context_manager(self) -> None:
        reader = MockFileReader()
        with reader.get_session() as session:
            assert session is not None
            assert isinstance(session, dict)


class TestGetPathId:
    """Test get_path_id() method contract."""

    def test_returns_string_for_existing(self) -> None:
        reader = MockFileReader()
        result = reader.get_path_id("/src/main.py")
        assert isinstance(result, str)

    def test_returns_none_for_missing(self) -> None:
        reader = MockFileReader()
        result = reader.get_path_id("/nonexistent.py")
        assert result is None


class TestGetContentHash:
    """Test get_content_hash() method contract."""

    def test_returns_string_for_existing(self) -> None:
        reader = MockFileReader()
        result = reader.get_content_hash("/src/main.py")
        assert isinstance(result, str)

    def test_returns_none_for_missing(self) -> None:
        reader = MockFileReader()
        result = reader.get_content_hash("/nonexistent.py")
        assert result is None


# =============================================================================
# Negative tests — incomplete implementations
# =============================================================================


class IncompleteFileReader:
    """Missing most methods — should NOT satisfy protocol."""

    def read_text(self, path: str) -> str:
        return ""


class TestIncompleteImplementation:
    """Objects missing methods should not satisfy the protocol."""

    def test_incomplete_fails_isinstance(self) -> None:
        reader = IncompleteFileReader()
        assert not isinstance(reader, FileReaderProtocol)
