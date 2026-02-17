"""Tests for BrickLifecycleManager factory wiring (Issue #1704).

Phase 4 TDD: integration with factory.py boot sequence.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.services.brick_lifecycle import BrickLifecycleManager
from nexus.services.protocols.brick_lifecycle import BrickState

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_lifecycle_brick(name: str = "test") -> MagicMock:
    brick = AsyncMock()
    brick.start = AsyncMock(return_value=None)
    brick.stop = AsyncMock(return_value=None)
    brick.health_check = AsyncMock(return_value=True)
    brick.__class__.__name__ = f"{name.capitalize()}Brick"
    return brick


# ---------------------------------------------------------------------------
# Factory integration: BrickLifecycleManager in KernelServices
# ---------------------------------------------------------------------------


class TestKernelServicesIntegration:
    """Verify BrickLifecycleManager is a first-class KernelServices field."""

    def test_kernel_services_has_brick_lifecycle_manager_field(self) -> None:
        """KernelServices should have a brick_lifecycle_manager field."""
        from nexus.core.config import KernelServices

        ks = KernelServices()
        assert hasattr(ks, "brick_lifecycle_manager")
        assert ks.brick_lifecycle_manager is None  # Default is None

    def test_kernel_services_accepts_lifecycle_manager(self) -> None:
        """KernelServices should accept a BrickLifecycleManager instance."""
        from nexus.core.config import KernelServices

        manager = BrickLifecycleManager()
        ks = KernelServices(brick_lifecycle_manager=manager)
        assert ks.brick_lifecycle_manager is manager


# ---------------------------------------------------------------------------
# Boot integration: lifecycle manager created during system boot
# ---------------------------------------------------------------------------


class TestBootIntegration:
    """Verify lifecycle manager is created during factory boot."""

    def test_lifecycle_manager_creation(self) -> None:
        """BrickLifecycleManager should be constructable at boot time."""
        manager = BrickLifecycleManager()
        assert isinstance(manager, BrickLifecycleManager)

    @pytest.mark.asyncio
    async def test_lifecycle_manager_register_and_mount_pattern(self) -> None:
        """Verify the factory pattern: register bricks → mount_all → health."""
        manager = BrickLifecycleManager()

        # Register brick services (as factory would)
        wallet = _make_lifecycle_brick("wallet")
        search = _make_lifecycle_brick("search")
        manifest = _make_lifecycle_brick("manifest")

        manager.register("wallet_provisioner", wallet, protocol_name="WalletProtocol")
        manager.register(
            "search_service",
            search,
            protocol_name="SearchProtocol",
        )
        manager.register(
            "manifest_resolver",
            manifest,
            protocol_name="ManifestProtocol",
        )

        # Mount all bricks
        report = await manager.mount_all()

        assert report.total == 3
        assert report.active == 3
        assert report.failed == 0

    @pytest.mark.asyncio
    async def test_lifecycle_manager_with_dependency_chain(self) -> None:
        """Factory pattern with dependencies between bricks."""
        manager = BrickLifecycleManager()

        infra = _make_lifecycle_brick("infra")
        search = _make_lifecycle_brick("search")
        rag = _make_lifecycle_brick("rag")

        manager.register("infra", infra, protocol_name="InfraProtocol")
        manager.register("search", search, protocol_name="SearchProtocol", depends_on=("infra",))
        manager.register("rag", rag, protocol_name="RAGProtocol", depends_on=("search",))

        report = await manager.mount_all()
        assert report.active == 3

        # Shutdown in reverse order
        report = await manager.unmount_all()
        assert report.active == 0

    @pytest.mark.asyncio
    async def test_lifecycle_manager_handles_brick_failure_gracefully(self) -> None:
        """Factory boot should continue even if one brick fails."""
        manager = BrickLifecycleManager()

        good = _make_lifecycle_brick("good")
        bad = _make_lifecycle_brick("bad")
        bad.start = AsyncMock(side_effect=RuntimeError("service unavailable"))
        also_good = _make_lifecycle_brick("also_good")

        manager.register("good", good, protocol_name="GP")
        manager.register("bad", bad, protocol_name="BP")
        manager.register("also_good", also_good, protocol_name="AGP")

        report = await manager.mount_all()

        assert report.total == 3
        assert report.active == 2
        assert report.failed == 1
        assert manager.get_status("bad").state == BrickState.FAILED  # type: ignore[union-attr]
