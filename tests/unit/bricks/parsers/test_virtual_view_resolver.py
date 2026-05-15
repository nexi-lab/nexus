"""Tests for VirtualViewResolver — VFSPathResolver behavioral contract (#1305).

Verifies:
- try_read uses single kernel lookup (no double lookup)
- try_read returns parsed content for virtual view paths
- try_read returns None for non-virtual paths
- try_write / try_delete reject virtual views with error
"""

from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest

from nexus.bricks.parsers.virtual_view_resolver import VirtualViewResolver


@pytest.fixture
def metadata() -> MagicMock:
    """Mock kernel-shaped metastore.

    Post-C10a the resolver stores ``self._kernel = metadata`` directly
    and calls ``sys_stat`` (try_read) and ``access`` (try_write/try_delete)
    instead of the deleted ``metastore_get`` / ``metastore_exists``.
    """
    mock = MagicMock()
    # sys_stat returns a dict (truthy) or None (falsy) — used by try_read
    mock.sys_stat.return_value = {
        "path": "/file.xlsx",
        "content_id": "hash123",
        "size": 100,
        "entry_type": 0,
    }
    # access returns bool — used by try_write / try_delete
    mock.access.return_value = True
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
    """Behavioral contract: try_read must use at most one kernel call."""

    def test_try_read_single_lookup(
        self, resolver: VirtualViewResolver, metadata: MagicMock
    ) -> None:
        """try_read should call sys_stat exactly once, not access+sys_stat."""
        resolver.try_read("/file_parsed.xlsx.md")

        # kernel.sys_stat called exactly once (by parse_virtual_path)
        assert metadata.sys_stat.call_count == 1
        assert metadata.sys_stat.call_args == call("/file.xlsx", "root")

        # kernel.access must NOT be called during try_read
        metadata.access.assert_not_called()

    def test_try_read_nonvirtual_single_lookup(
        self, resolver: VirtualViewResolver, metadata: MagicMock
    ) -> None:
        """Non-virtual path: sys_stat should not be called at all."""
        result = resolver.try_read("/normal_file.txt")

        assert result is None
        metadata.sys_stat.assert_not_called()
        metadata.access.assert_not_called()


class TestTryRead:
    def test_returns_parsed_content(self, resolver: VirtualViewResolver) -> None:
        result = resolver.try_read("/file_parsed.xlsx.md")
        assert result == b"# Parsed markdown"

    def test_returns_none_for_non_virtual(self, resolver: VirtualViewResolver) -> None:
        assert resolver.try_read("/normal.txt") is None

    def test_returns_none_when_original_missing(
        self, resolver: VirtualViewResolver, metadata: MagicMock
    ) -> None:
        metadata.sys_stat.return_value = None
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
