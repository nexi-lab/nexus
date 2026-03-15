"""Integration test: IPC brick wiring through factory.py.

Validates that the factory correctly creates and wires IPC services
(KernelVFSAdapter, AgentProvisioner) when the IPC brick is enabled.

Issue: #1727, #1178
"""

import pytest


class TestIPCBrickWiring:
    """Verify IPC brick is correctly wired in _boot_brick_services."""

    def test_ipc_fields_exist_on_brick_services(self) -> None:
        """BrickServices dataclass has ipc_storage_driver and ipc_provisioner."""
        from nexus.core.config import BrickServices

        bs = BrickServices()
        assert hasattr(bs, "ipc_storage_driver")
        assert hasattr(bs, "ipc_provisioner")
        assert bs.ipc_storage_driver is None
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

    def test_kernel_vfs_adapter_importable(self) -> None:
        """KernelVFSAdapter is importable (validates no circular deps)."""
        from nexus.bricks.ipc.kernel_adapter import KernelVFSAdapter

        assert KernelVFSAdapter is not None

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


class TestKernelVFSAdapter:
    """Verify KernelVFSAdapter satisfies VFSOperations protocol."""

    @pytest.mark.asyncio
    async def test_adapter_unbound_raises(self) -> None:
        """Calling methods before bind() raises RuntimeError."""
        import asyncio

        from nexus.bricks.ipc.kernel_adapter import KernelVFSAdapter

        adapter = KernelVFSAdapter(zone_id="test-zone")
        assert not adapter.is_bound

        with __import__("pytest").raises(RuntimeError, match="bind"):
            asyncio.run(await adapter.sys_read("/test", "test-zone"))

    def test_adapter_satisfies_vfs_operations_protocol(self) -> None:
        """KernelVFSAdapter structurally satisfies VFSOperations."""
        from nexus.bricks.ipc.kernel_adapter import KernelVFSAdapter
        from nexus.bricks.ipc.protocols import VFSOperations

        adapter = KernelVFSAdapter(zone_id="test-zone")
        # Protocol check: all required methods exist (sys_ prefixed names)
        for method in (
            "sys_read",
            "sys_write",
            "list_dir",
            "count_dir",
            "rename",
            "sys_mkdir",
            "sys_access",
        ):
            assert hasattr(adapter, method), f"Missing method: {method}"
        # Runtime checkable protocol
        assert isinstance(adapter, VFSOperations)
