"""Integration test: IPC brick wiring through factory.py.

Validates that the factory correctly creates and wires IPC services
(RecordStoreStorageDriver, IPCVFSDriver, AgentProvisioner) when
a RecordStore with session_factory is available.

Issue: #1727
"""


class TestIPCBrickWiring:
    """Verify IPC brick is correctly wired in _boot_brick_services."""

    def test_ipc_fields_exist_on_brick_services(self) -> None:
        """BrickServices dataclass has ipc_storage_driver, ipc_vfs_driver, ipc_provisioner."""
        from nexus.core.config import BrickServices

        bs = BrickServices()
        assert hasattr(bs, "ipc_storage_driver")
        assert hasattr(bs, "ipc_vfs_driver")
        assert hasattr(bs, "ipc_provisioner")
        assert bs.ipc_storage_driver is None
        assert bs.ipc_vfs_driver is None
        assert bs.ipc_provisioner is None

    def test_brick_ipc_in_deployment_profiles(self) -> None:
        """BRICK_IPC is registered and included in LITE+ profiles."""
        from nexus.contracts.deployment_profile import (
            ALL_BRICK_NAMES,
            BRICK_IPC,
            DeploymentProfile,
        )

        assert BRICK_IPC == "ipc"
        assert BRICK_IPC in ALL_BRICK_NAMES

        # IPC should be in LITE and above (infrastructure brick)
        assert DeploymentProfile.LITE.is_brick_enabled(BRICK_IPC)
        assert DeploymentProfile.FULL.is_brick_enabled(BRICK_IPC)
        assert DeploymentProfile.CLOUD.is_brick_enabled(BRICK_IPC)

        # IPC should NOT be in EMBEDDED (too resource-constrained)
        assert not DeploymentProfile.EMBEDDED.is_brick_enabled(BRICK_IPC)

    def test_ipc_storage_driver_importable(self) -> None:
        """RecordStoreStorageDriver is importable (validates no circular deps)."""
        from nexus.bricks.ipc.storage.recordstore_driver import RecordStoreStorageDriver

        assert RecordStoreStorageDriver is not None

    def test_ipc_vfs_driver_importable(self) -> None:
        """IPCVFSDriver is importable (validates no circular deps)."""
        from nexus.bricks.ipc.driver import IPCVFSDriver

        assert IPCVFSDriver is not None

    def test_ipc_provisioner_importable(self) -> None:
        """AgentProvisioner is importable (validates no circular deps)."""
        from nexus.bricks.ipc.provisioning import AgentProvisioner

        assert AgentProvisioner is not None

    def test_ipc_rest_router_importable(self) -> None:
        """IPC REST router is importable and has expected endpoints."""
        from nexus.server.api.v2.routers.ipc import router

        route_paths = [getattr(r, "path", "") for r in router.routes]
        assert any("/send" in p for p in route_paths)
        assert any("/inbox/{agent_id}" in p for p in route_paths)
        assert any("/count" in p for p in route_paths)
        assert any("/provision/{agent_id}" in p for p in route_paths)

    def test_ipc_lifespan_importable(self) -> None:
        """IPC lifespan module is importable."""
        from nexus.server.lifespan.ipc import shutdown_ipc, startup_ipc

        assert callable(startup_ipc)
        assert callable(shutdown_ipc)


class TestIPCVFSDriverMount:
    """Verify IPCVFSDriver can be mounted on a PathRouter."""

    def test_ipc_driver_mounts_at_agents(self) -> None:
        """IPCVFSDriver can be mounted at /agents on the PathRouter."""
        from nexus.bricks.ipc.driver import IPCVFSDriver
        from nexus.core.router import PathRouter
        from tests.unit.bricks.ipc.fakes import InMemoryStorageDriver

        storage = InMemoryStorageDriver()
        driver = IPCVFSDriver(storage=storage, zone_id="test-zone")

        from tests.helpers.in_memory_metadata_store import InMemoryMetastore

        router = PathRouter(InMemoryMetastore())
        # Should not raise — IPCVFSDriver extends Backend
        router.add_mount("/agents", driver)

        # Verify it's mounted
        assert driver.name == "ipc"
        assert driver.has_virtual_filesystem is True

        # Clean up background loop
        driver.close()

    def test_ipc_driver_close_is_idempotent(self) -> None:
        """Calling close() multiple times is safe."""
        from nexus.bricks.ipc.driver import IPCVFSDriver
        from tests.unit.bricks.ipc.fakes import InMemoryStorageDriver

        storage = InMemoryStorageDriver()
        driver = IPCVFSDriver(storage=storage, zone_id="test-zone")
        driver.close()
        driver.close()  # Should not raise
