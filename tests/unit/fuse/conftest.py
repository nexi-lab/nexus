"""FUSE-specific test fixtures."""

from enum import Enum
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


class _MockMountMode(Enum):
    BINARY = "binary"
    TEXT = "text"
    SMART = "smart"


@pytest.fixture()
def mock_nexus_fs() -> MagicMock:
    """Mock NexusFS with standard methods."""
    fs = MagicMock()
    # NexusFS syscalls are sync def — use MagicMock
    fs.access = MagicMock(return_value=True)
    fs.is_directory = MagicMock(return_value=False)
    fs.sys_read = MagicMock(return_value=b"hello world")
    fs.sys_write = MagicMock(return_value=None)
    fs.write = MagicMock(return_value=None)
    fs.sys_readdir = MagicMock(return_value=[])
    fs.sys_stat = MagicMock(return_value=None)
    fs.sys_setattr = MagicMock(return_value=None)
    fs.sys_unlink = MagicMock(return_value=None)
    fs.sys_rename = MagicMock(return_value=None)
    fs.mkdir = MagicMock(return_value=None)
    fs.rmdir = MagicMock(return_value=None)
    fs.zone_id = "test-zone"
    return fs


@pytest.fixture()
def mock_cache() -> MagicMock:
    """Mock FUSECacheManager."""
    cache = MagicMock()
    cache.get_attr.return_value = None
    cache.get_content.return_value = None
    cache.get_parsed.return_value = None
    return cache


@pytest.fixture()
def mock_mode() -> _MockMountMode:
    """Default mount mode (binary)."""
    return _MockMountMode.BINARY


@pytest.fixture()
def fuse_ops(mock_nexus_fs: MagicMock, mock_mode: _MockMountMode) -> Any:
    """Create a NexusFUSEOperations instance with mocked dependencies.

    Patches out heavyweight imports (readahead, local disk cache, event bus)
    so the test only exercises FUSE logic.
    """
    with (
        patch("nexus.fuse.operations.HAS_READAHEAD", False),
        patch("nexus.fuse.operations.HAS_LOCAL_DISK_CACHE", False),
        patch("nexus.fuse.operations.HAS_EVENT_BUS", False),
    ):
        from nexus.fuse.operations import NexusFUSEOperations

        ops = NexusFUSEOperations(
            nexus_fs=mock_nexus_fs,
            mode=mock_mode,
            cache_config={"attr_cache_ttl": 60},
        )
    return ops


@pytest.fixture()
def fuse_ops_with_context(mock_nexus_fs: MagicMock, mock_mode: _MockMountMode) -> Any:
    """FUSE ops with a namespace context set."""
    with (
        patch("nexus.fuse.operations.HAS_READAHEAD", False),
        patch("nexus.fuse.operations.HAS_LOCAL_DISK_CACHE", False),
        patch("nexus.fuse.operations.HAS_EVENT_BUS", False),
    ):
        from nexus.fuse.operations import NexusFUSEOperations

        ctx = MagicMock()
        ctx.get_subject.return_value = ("agent", "agent_001")
        ctx.zone_id = "zone1"

        ops = NexusFUSEOperations(
            nexus_fs=mock_nexus_fs,
            mode=mock_mode,
            cache_config={"attr_cache_ttl": 60},
            context=ctx,
        )
    return ops
