"""Test NexusFS service composition (Phase 2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.backends.local import LocalBackend
from nexus.core.config import KernelServices, PermissionConfig
from nexus.core.nexus_fs import NexusFS
from nexus.services.version_service import VersionService

try:
    from nexus.storage.raft_metadata_store import RaftMetadataStore

    RaftMetadataStore.embedded("/tmp/_raft_probe")  # noqa: S108
    _raft_available = True
except Exception:
    _raft_available = False

pytestmark = pytest.mark.skipif(not _raft_available, reason="Raft metastore not available")


def _make_fs(tmp_path: Path, *, enforce_permissions: bool = True) -> NexusFS:
    """Create NexusFS with VersionService injected (mimics factory)."""
    backend_path = tmp_path / "storage"
    backend_path.mkdir(exist_ok=True)
    db_path = tmp_path / "metadata"

    backend = LocalBackend(str(backend_path))
    metadata_store = RaftMetadataStore.embedded(str(db_path))
    # VersionService is created by factory; for unit tests we inject it manually
    version_service = VersionService(
        metadata_store=metadata_store,
        cas_store=backend,
        enforce_permissions=False,
    )
    return NexusFS(
        backend=backend,
        metadata_store=metadata_store,
        permissions=PermissionConfig(enforce=enforce_permissions),
        services=KernelServices(version_service=version_service),
    )


class TestNexusFSServiceComposition:
    """Test that NexusFS correctly instantiates all services."""

    def test_all_services_instantiated(self, tmp_path: Path):
        """Test that all services are created during NexusFS initialization."""
        fs = _make_fs(tmp_path, enforce_permissions=False)

        # Verify all services are instantiated
        assert hasattr(fs, "version_service"), "VersionService not instantiated"
        assert hasattr(fs, "rebac_service"), "ReBACService not instantiated"
        assert hasattr(fs, "mount_service"), "MountService not instantiated"
        assert hasattr(fs, "mcp_service"), "MCPService not instantiated"
        assert hasattr(fs, "llm_service"), "LLMService not instantiated"
        assert hasattr(fs, "oauth_service"), "OAuthService not instantiated"
        assert hasattr(fs, "skill_service"), "SkillService not instantiated"
        assert hasattr(fs, "search_service"), "SearchService not instantiated"
        assert hasattr(fs, "share_link_service"), "ShareLinkService not instantiated"
        assert hasattr(fs, "events_service"), "EventsService not instantiated"

        # Verify services are not None
        assert fs.version_service is not None
        assert fs.rebac_service is not None
        assert fs.mount_service is not None
        assert fs.mcp_service is not None
        assert fs.llm_service is not None
        assert fs.oauth_service is not None
        assert fs.skill_service is not None
        assert fs.search_service is not None
        assert fs.share_link_service is not None
        assert fs.events_service is not None

    def test_service_dependencies_correct(self, tmp_path: Path):
        """Test that services receive correct dependencies."""
        fs = _make_fs(tmp_path)

        # VersionService dependencies (injected by _make_fs, mimicking factory)
        assert fs.version_service.metadata == fs.metadata
        assert fs.version_service.cas == fs.backend

        # ReBACService should have rebac_manager
        assert fs.rebac_service._rebac_manager == fs._rebac_manager

        # MountService should have router and mount_manager
        assert fs.mount_service.router == fs.router
        assert fs.mount_service.mount_manager == fs.mount_manager

        # Services that take nexus_fs should have it
        assert fs.mcp_service.nexus_fs == fs
        assert fs.llm_service.nexus_fs == fs
        # SkillService is directly instantiated
        assert fs.skill_service is not None

        # SearchService should have metadata and permission_enforcer
        assert fs.search_service.metadata == fs.metadata
        assert fs.search_service._permission_enforcer == fs._permission_enforcer

        # ShareLinkService should have gateway
        assert fs.share_link_service._gw is not None

        # EventsService should have backend
        assert fs.events_service._backend == fs.backend

    def test_version_service_delegation(self, tmp_path: Path):
        """Test that VersionService delegation methods work correctly."""
        fs = _make_fs(tmp_path, enforce_permissions=False)

        # Verify sync methods exist (with @rpc_expose, wrap async methods)
        assert hasattr(fs, "get_version")
        assert hasattr(fs, "list_versions")
        assert hasattr(fs, "rollback")
        assert hasattr(fs, "diff_versions")

        # Verify async delegation methods exist (with "a" prefix)
        assert hasattr(fs, "aget_version")
        assert hasattr(fs, "alist_versions")
        assert hasattr(fs, "arollback")
        assert hasattr(fs, "adiff_versions")

        # Verify async methods are coroutine functions
        import inspect

        assert inspect.iscoroutinefunction(fs.aget_version)
        assert inspect.iscoroutinefunction(fs.alist_versions)
        assert inspect.iscoroutinefunction(fs.arollback)
        assert inspect.iscoroutinefunction(fs.adiff_versions)

        # Verify sync methods are NOT coroutine functions (they wrap async)
        assert not inspect.iscoroutinefunction(fs.get_version)
        assert not inspect.iscoroutinefunction(fs.list_versions)
        assert not inspect.iscoroutinefunction(fs.rollback)
        assert not inspect.iscoroutinefunction(fs.diff_versions)

        # Verify sync methods have @rpc_expose decorator
        assert hasattr(fs.get_version, "_rpc_exposed")
        assert hasattr(fs.list_versions, "_rpc_exposed")
        assert hasattr(fs.rollback, "_rpc_exposed")
        assert hasattr(fs.diff_versions, "_rpc_exposed")
