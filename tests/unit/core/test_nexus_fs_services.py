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
        assert fs.service("version_service") is not None, "VersionService not instantiated"
        assert fs.service("rebac") is not None, "ReBACService not instantiated"
        assert fs.service("mount") is not None, "MountService not instantiated"
        assert fs.service("mcp") is not None, "MCPService not instantiated"
        assert fs.service("oauth") is not None, "OAuthService not instantiated"
        assert fs.service("search") is not None, "SearchService not instantiated"
        assert fs.service("share_link") is not None, "ShareLinkService not instantiated"

        # Verify services are not None
        assert fs.service("version_service") is not None
        assert fs.service("rebac") is not None
        assert fs.service("mount") is not None
        assert fs.service("mcp") is not None
        assert fs.service("oauth") is not None
        assert fs.service("search") is not None
        assert fs.service("share_link") is not None

    def test_service_dependencies_correct(self, tmp_path: Path):
        """Test that services receive correct dependencies."""
        fs = _make_fs(tmp_path)

        # VersionService dependencies (injected by _make_fs, mimicking factory)
        assert fs.service("version_service").metadata == fs.metadata
        assert fs.service("version_service").cas == fs.router.route("/").backend

        # ReBACService should have a rebac_manager (may be proxy-wrapped)
        assert fs.service("rebac")._rebac_manager is not None
        assert fs.service("rebac_manager") is not None

        # MountService should have router and mount_manager
        assert fs.service("mount").router == fs.router
        assert fs.service("mount").mount_manager == fs.service("mount_manager")

        # Services that take filesystem should have it
        assert fs.service("mcp")._filesystem == fs
        # SearchService should have metadata and permission_enforcer
        assert fs.service("search").metadata == fs.metadata
        assert fs.service("search")._permission_enforcer == fs.service("permission_enforcer")

        # ShareLinkService should have gateway
        assert fs.service("share_link")._gw is not None

    def test_version_service_delegation(self, tmp_path: Path):
        """Test that VersionService is available on NexusFS."""
        fs = _make_fs(tmp_path, enforce_permissions=False)

        # Verify version_service exists and is not None
        assert fs.service("version_service") is not None, "VersionService not instantiated"
