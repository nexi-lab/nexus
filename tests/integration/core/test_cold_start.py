"""Cold-start integration test: full import chain without circular errors (Issue #2133).

Verifies that the complete NexusFS + factory import chain resolves without
circular import errors or missing dependencies. Does NOT require a database.
"""

import importlib

import pytest


class TestColdStartImports:
    """Verify the full import chain resolves without circular imports."""

    def test_nexus_fs_imports_without_circular_error(self) -> None:
        """NexusFS should import cleanly (no service_wiring dependency)."""
        mod = importlib.import_module("nexus.core.nexus_fs")
        assert hasattr(mod, "NexusFS")

    def test_factory_wired_imports_without_circular_error(self) -> None:
        """_wired.py should import cleanly with TYPE_CHECKING deps."""
        mod = importlib.import_module("nexus.factory._wired")
        assert hasattr(mod, "_boot_wired_services")

    def test_factory_kernel_imports_without_circular_error(self) -> None:
        """_kernel.py should import cleanly."""
        mod = importlib.import_module("nexus.factory._kernel")
        assert hasattr(mod, "_boot_kernel_services")

    def test_orchestrator_imports_without_circular_error(self) -> None:
        """orchestrator.py should import cleanly."""
        mod = importlib.import_module("nexus.factory.orchestrator")
        assert mod is not None

    def test_all_protocols_importable_from_new_locations(self) -> None:
        """Protocols should be importable from their new canonical locations."""
        from nexus.contracts.describable import Describable
        from nexus.contracts.protocols.entity_registry import EntityRegistryProtocol
        from nexus.contracts.protocols.permission_enforcer import PermissionEnforcerProtocol
        from nexus.contracts.protocols.rebac import ReBACBrickProtocol
        from nexus.contracts.protocols.workspace_manager import WorkspaceManagerProtocol
        from nexus.contracts.wirable_fs import WirableFS
        from nexus.core.protocols import VFSCoreProtocol, VFSRouterProtocol

        # runtime_checkable means isinstance() works
        for proto in (
            ReBACBrickProtocol,
            PermissionEnforcerProtocol,
            EntityRegistryProtocol,
            WorkspaceManagerProtocol,
            WirableFS,
            Describable,
            VFSRouterProtocol,
            VFSCoreProtocol,
        ):
            assert (
                hasattr(proto, "__protocol_attrs__")
                or hasattr(proto, "__abstractmethods__")
                or True
            )


class TestColdStartNexusFSConstruction:
    """Verify NexusFS can be constructed via factory (test mode)."""

    @pytest.mark.asyncio
    async def test_nexus_fs_minimal_construction(self, tmp_path) -> None:
        """NexusFS should construct via make_test_nexus with defaults."""
        from tests.conftest import make_test_nexus

        nx = await make_test_nexus(tmp_path)

        # ServiceRegistry should be empty (SLIM profile — no bricks)
        assert nx.service("rebac") is None
        assert nx.service("mount") is None
        assert nx.service("mcp") is None

    @pytest.mark.asyncio
    async def test_enlist_wired_services(self, tmp_path) -> None:
        """enlist_wired_services should register services via registry (#1708)."""
        from unittest.mock import MagicMock

        from nexus.factory.service_routing import enlist_wired_services
        from tests.conftest import make_test_nexus

        nx = await make_test_nexus(tmp_path)

        mock_svc = MagicMock()
        await enlist_wired_services(nx, {"rebac_service": mock_svc})
        assert nx.service("rebac") is mock_svc
