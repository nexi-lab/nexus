"""Test NexusFS service composition (Phase 2)."""

from __future__ import annotations

from pathlib import Path

from nexus.backends.local import LocalBackend
from nexus.core.nexus_fs import NexusFS


class TestNexusFSServiceComposition:
    """Test that NexusFS correctly instantiates all services."""

    def test_all_services_instantiated(self, tmp_path: Path):
        """Test that all 8 services are created during NexusFS initialization."""
        # Create temporary backend and database
        backend_path = tmp_path / "storage"
        backend_path.mkdir()
        db_path = tmp_path / "metadata.db"

        # Initialize NexusFS
        backend = LocalBackend(str(backend_path))
        fs = NexusFS(backend=backend, db_path=str(db_path), enforce_permissions=False)

        # Verify all services are instantiated
        assert hasattr(fs, "version_service"), "VersionService not instantiated"
        assert hasattr(fs, "rebac_service"), "ReBACService not instantiated"
        assert hasattr(fs, "mount_service"), "MountService not instantiated"
        assert hasattr(fs, "mcp_service"), "MCPService not instantiated"
        assert hasattr(fs, "llm_service"), "LLMService not instantiated"
        assert hasattr(fs, "oauth_service"), "OAuthService not instantiated"
        assert hasattr(fs, "skill_service"), "SkillService not instantiated"
        assert hasattr(fs, "search_service"), "SearchService not instantiated"

        # Verify services are not None
        assert fs.version_service is not None
        assert fs.rebac_service is not None
        assert fs.mount_service is not None
        assert fs.mcp_service is not None
        assert fs.llm_service is not None
        assert fs.oauth_service is not None
        assert fs.skill_service is not None
        assert fs.search_service is not None

    def test_service_dependencies_correct(self, tmp_path: Path):
        """Test that services receive correct dependencies."""
        backend_path = tmp_path / "storage"
        backend_path.mkdir()
        db_path = tmp_path / "metadata.db"

        backend = LocalBackend(str(backend_path))
        fs = NexusFS(backend=backend, db_path=str(db_path))

        # VersionService should have metadata, cas, and router
        assert fs.version_service.metadata == fs.metadata
        assert fs.version_service.cas == fs.backend
        assert fs.version_service.router == fs.router

        # ReBACService should have rebac_manager
        assert fs.rebac_service._rebac_manager == fs._rebac_manager

        # MountService should have router and mount_manager
        assert fs.mount_service.router == fs.router
        assert fs.mount_service.mount_manager == fs.mount_manager

        # Services that take nexus_fs should have it
        assert fs.mcp_service.nexus_fs == fs
        assert fs.llm_service.nexus_fs == fs
        assert fs.skill_service.nexus_fs == fs

        # SearchService should have metadata and permission_enforcer
        assert fs.search_service.metadata == fs.metadata
        assert fs.search_service._permission_enforcer == fs._permission_enforcer
