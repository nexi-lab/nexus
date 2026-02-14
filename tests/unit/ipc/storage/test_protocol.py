"""Tests for IPCStorageDriver protocol conformance.

Verifies that all storage driver implementations satisfy the
``IPCStorageDriver`` runtime-checkable protocol.
"""

from __future__ import annotations

from nexus.ipc.storage.protocol import IPCStorageDriver


class TestProtocolConformance:
    """Verify all drivers satisfy IPCStorageDriver at runtime."""

    def test_in_memory_storage_satisfies_protocol(self) -> None:
        from tests.unit.ipc.fakes import InMemoryStorageDriver

        driver = InMemoryStorageDriver()
        assert isinstance(driver, IPCStorageDriver)

    def test_in_memory_vfs_satisfies_protocol(self) -> None:
        from tests.unit.ipc.fakes import InMemoryVFS

        vfs = InMemoryVFS()
        assert isinstance(vfs, IPCStorageDriver)

    def test_vfs_storage_driver_satisfies_protocol(self) -> None:
        from nexus.ipc.storage.vfs_driver import VFSStorageDriver
        from tests.unit.ipc.fakes import InMemoryVFS

        driver = VFSStorageDriver(vfs=InMemoryVFS())
        assert isinstance(driver, IPCStorageDriver)

    def test_postgresql_storage_driver_satisfies_protocol(self) -> None:
        from nexus.ipc.storage.postgresql_driver import PostgreSQLStorageDriver

        # Use a mock pool — we only check structural protocol conformance
        driver = PostgreSQLStorageDriver(pool=None)
        assert isinstance(driver, IPCStorageDriver)
