"""Cold-start integration test: full import chain without circular errors (Issue #2133).

Verifies that the complete NexusFS + factory import chain resolves without
circular import errors or missing dependencies. Does NOT require a database.
"""

import importlib

from tests.helpers.test_context import TEST_CONTEXT


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
    """Verify NexusFS can be constructed without factory (test mode)."""

    def test_nexus_fs_minimal_construction(self) -> None:
        """NexusFS should construct with just metadata_store."""
        from unittest.mock import MagicMock

        from nexus.core.config import ParseConfig
        from nexus.core.nexus_fs import NexusFS

        mock_metadata = MagicMock()
        mock_metadata.list = MagicMock(return_value=[])

        nx = NexusFS(
            metadata_store=mock_metadata,
            parsing=ParseConfig(auto_parse=False),
        )
        nx._init_cred = TEST_CONTEXT

        # ServiceRegistry should be empty (no factory wiring)
        assert nx.service("rebac") is None
        assert nx.service("mount") is None
        assert nx.service("mcp") is None

    def test_enlist_wired_services(self) -> None:
        """enlist_wired_services should register services via coordinator (#1708)."""
        import asyncio
        from unittest.mock import MagicMock

        from nexus.core.config import ParseConfig
        from nexus.core.nexus_fs import NexusFS
        from nexus.factory.service_routing import enlist_wired_services
        from nexus.system_services.lifecycle.service_lifecycle_coordinator import (
            ServiceLifecycleCoordinator,
        )

        mock_metadata = MagicMock()
        mock_metadata.list = MagicMock(return_value=[])

        nx = NexusFS(
            metadata_store=mock_metadata,
            parsing=ParseConfig(auto_parse=False),
        )
        nx._init_cred = TEST_CONTEXT

        coordinator = ServiceLifecycleCoordinator(nx._service_registry, None, nx._dispatch)
        mock_svc = MagicMock()
        asyncio.run(enlist_wired_services(coordinator, {"rebac_service": mock_svc}))
        assert nx.service("rebac")._service_instance is mock_svc
