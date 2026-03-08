"""Test NexusFS service composition (Phase 2).

Issue #643: Services are now created by factory._boot_wired_services(),
not by NexusFS.__init__. Tests use create_nexus_fs() factory entry point.
"""

from pathlib import Path

import pytest

from nexus.backends.storage.cas_local import CASLocalBackend
from nexus.core.config import PermissionConfig
from nexus.core.nexus_fs import NexusFS

try:
    from nexus.storage.raft_metadata_store import RaftMetadataStore

    RaftMetadataStore.embedded("/tmp/_raft_probe")  # noqa: S108
    _raft_available = True
except Exception:
    _raft_available = False

pytestmark = pytest.mark.skipif(not _raft_available, reason="Raft metastore not available")


def _make_fs(tmp_path: Path, *, enforce_permissions: bool = True) -> NexusFS:
    """Create NexusFS via factory (includes two-phase wired services)."""
    from nexus.factory import create_nexus_fs
    from nexus.storage.record_store import SQLAlchemyRecordStore

    backend_path = tmp_path / "storage"
    backend_path.mkdir(exist_ok=True)
    db_path = tmp_path / "metadata"

    backend = CASLocalBackend(str(backend_path))
    metadata_store = RaftMetadataStore.embedded(str(db_path))
    record_store = SQLAlchemyRecordStore(db_path=str(tmp_path / "nexus.db"))

    return create_nexus_fs(
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        permissions=PermissionConfig(enforce=enforce_permissions),
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
        assert fs.search_service is not None
        assert fs.share_link_service is not None
        assert fs.events_service is not None

    def test_service_dependencies_correct(self, tmp_path: Path):
        """Test that services receive correct dependencies."""
        fs = _make_fs(tmp_path)

        # VersionService dependencies (injected by _make_fs, mimicking factory)
        assert fs.version_service.metadata == fs.metadata
        assert fs.version_service.cas == fs.router.route("/").backend

        # ReBACService should have rebac_manager
        assert fs.rebac_service._rebac_manager == fs._rebac_manager

        # MountService should have router and mount_manager
        assert fs.mount_service.router == fs.router
        assert fs.mount_service.mount_manager == fs.mount_manager

        # Services that take filesystem should have it
        assert fs.mcp_service._filesystem == fs
        assert fs.llm_service.nexus_fs == fs
        # SearchService should have metadata and permission_enforcer
        assert fs.search_service.metadata == fs.metadata
        assert fs.search_service._permission_enforcer == fs._permission_enforcer

        # ShareLinkService should have gateway
        assert fs.share_link_service._gw is not None

        # EventsService should exist
        assert fs.events_service is not None

    def test_version_service_delegation(self, tmp_path: Path):
        """Test that VersionService is available on NexusFS."""
        fs = _make_fs(tmp_path, enforce_permissions=False)

        # Verify version_service exists and is not None
        assert hasattr(fs, "version_service"), "VersionService not instantiated"
        assert fs.version_service is not None
