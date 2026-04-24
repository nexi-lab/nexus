"""Tests for VirtualViewResolver — VFSPathResolver behavioral contract (#1305).

Verifies:
- try_read uses single metastore lookup (no double lookup)
- try_read returns parsed content for virtual view paths
- try_read returns None for non-virtual paths
- try_write / try_delete reject virtual views with error
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, call

import pytest

from nexus.bricks.parsers.virtual_view_resolver import VirtualViewResolver


@dataclass
class FakeFileMetadata:
    path: str
    etag: str = "abc123"
    version: str = "1"
    size: int = 100
    owner: str = "user"


@pytest.fixture
def metadata() -> MagicMock:
    mock = MagicMock()
    # metadata.get returns FakeFileMetadata for existing files, None otherwise
    mock.get.return_value = FakeFileMetadata(path="/file.xlsx", etag="hash123")
    mock.exists.return_value = True
    return mock


@pytest.fixture
def dlc() -> MagicMock:
    backend = MagicMock()
    backend.read_content.return_value = b"raw xlsx bytes"
    mock = MagicMock()
    mock.resolve_path.return_value = (backend, "/file.xlsx", "/")
    return mock


@pytest.fixture
def permission_checker() -> MagicMock:
    return MagicMock()


@pytest.fixture
def resolver(
    metadata: MagicMock,
    dlc: MagicMock,
    permission_checker: MagicMock,
) -> VirtualViewResolver:
    return VirtualViewResolver(
        metadata=metadata,
        dlc=dlc,
        permission_checker=permission_checker,
        parse_fn=lambda content, path: b"# Parsed markdown",
    )


class TestSingleMetastoreLookup:
    """Behavioral contract: try_read must use at most one metastore call."""

    def test_try_read_single_lookup(
        self, resolver: VirtualViewResolver, metadata: MagicMock
    ) -> None:
        """try_read should call metadata.get exactly once, not exists+get."""
        resolver.try_read("/file_parsed.xlsx.md")

        # metadata.get called exactly once (by parse_virtual_path)
        assert metadata.get.call_count == 1
        assert metadata.get.call_args == call("/file.xlsx")

        # metadata.exists must NOT be called during try_read
        metadata.exists.assert_not_called()

    def test_try_read_nonvirtual_single_lookup(
        self, resolver: VirtualViewResolver, metadata: MagicMock
    ) -> None:
        """Non-virtual path: metadata.get should not be called at all."""
        result = resolver.try_read("/normal_file.txt")

        assert result is None
        metadata.get.assert_not_called()
        metadata.exists.assert_not_called()


class TestTryRead:
    def test_returns_parsed_content(self, resolver: VirtualViewResolver) -> None:
        result = resolver.try_read("/file_parsed.xlsx.md")
        assert result == b"# Parsed markdown"

    def test_returns_none_for_non_virtual(self, resolver: VirtualViewResolver) -> None:
        assert resolver.try_read("/normal.txt") is None

    def test_returns_none_when_original_missing(
        self, resolver: VirtualViewResolver, metadata: MagicMock
    ) -> None:
        metadata.get.return_value = None
        assert resolver.try_read("/missing_parsed.xlsx.md") is None


class TestTryWriteDelete:
    def test_write_rejects_virtual_view(self, resolver: VirtualViewResolver) -> None:
        with pytest.raises(Exception, match="Cannot write"):
            resolver.try_write("/file_parsed.xlsx.md", b"data")

    def test_write_returns_none_for_non_virtual(
        self,
        resolver: VirtualViewResolver,
    ) -> None:
        assert resolver.try_write("/normal.txt", b"data") is None

    def test_delete_rejects_virtual_view(self, resolver: VirtualViewResolver) -> None:
        with pytest.raises(Exception, match="Cannot delete"):
            resolver.try_delete("/file_parsed.xlsx.md")

    def test_delete_returns_none_for_non_virtual(
        self,
        resolver: VirtualViewResolver,
    ) -> None:
        assert resolver.try_delete("/normal.txt") is None
