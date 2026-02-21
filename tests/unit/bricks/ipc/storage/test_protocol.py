"""Tests for IPCStorageDriver protocol conformance.

Verifies that all storage driver implementations satisfy the
``IPCStorageDriver`` runtime-checkable protocol.
"""

from __future__ import annotations

from nexus.bricks.ipc.storage.protocol import IPCStorageDriver


class TestProtocolConformance:
    """Verify all drivers satisfy IPCStorageDriver at runtime."""

    def test_in_memory_storage_satisfies_protocol(self) -> None:
        from tests.unit.bricks.ipc.fakes import InMemoryStorageDriver

        driver = InMemoryStorageDriver()
        assert isinstance(driver, IPCStorageDriver)

    def test_in_memory_vfs_satisfies_protocol(self) -> None:
        from tests.unit.bricks.ipc.fakes import InMemoryVFS

        vfs = InMemoryVFS()
        assert isinstance(vfs, IPCStorageDriver)

    def test_vfs_storage_driver_satisfies_protocol(self) -> None:
        from nexus.bricks.ipc.storage.vfs_driver import VFSStorageDriver
        from tests.unit.bricks.ipc.fakes import InMemoryVFS

        driver = VFSStorageDriver(vfs=InMemoryVFS())
        assert isinstance(driver, IPCStorageDriver)

    def test_recordstore_storage_driver_satisfies_protocol(self) -> None:
        from unittest.mock import MagicMock

        from nexus.bricks.ipc.storage.recordstore_driver import RecordStoreStorageDriver

        # Use a mock record_store — we only check structural protocol conformance
        mock_rs = MagicMock()
        mock_rs.session_factory = None
        driver = RecordStoreStorageDriver(record_store=mock_rs)
        assert isinstance(driver, IPCStorageDriver)
