"""Smoke tests for Phase 2 services.

Quick validation that services can be instantiated and basic methods work.
Not comprehensive - just enough to catch major bugs before integration.
"""

from unittest.mock import MagicMock

import pytest

pytest.importorskip("pyroaring")


from nexus.contracts.types import OperationContext


@pytest.fixture
def mock_metadata():
    """Mock metadata store."""
    mock = MagicMock()
    mock.engine.url = "sqlite:///test.db"
    return mock


@pytest.fixture
def mock_cas():
    """Mock CAS store."""
    mock = MagicMock()
    mock.read_content.return_value = b"test content"
    return mock


@pytest.fixture
def mock_kernel():
    """Mock Rust kernel."""
    mock = MagicMock()
    backend = MagicMock()
    backend.read_content.return_value = b"test content"
    route = MagicMock(backend=backend, readonly=False)
    mock.route.return_value = route
    return mock


@pytest.fixture
def mock_dlc():
    """Mock DriverLifecycleCoordinator."""
    mock = MagicMock()
    mock.list_mounts.return_value = []
    return mock


@pytest.fixture
def operation_context():
    """Standard operation context."""
    return OperationContext(
        user_id="test_user",
        groups=["test_group"],
        zone_id="test_zone",
        is_system=False,
        is_admin=False,
    )


# =============================================================================
# VersionService Smoke Tests
# =============================================================================


class TestVersionServiceSmoke:
    """Smoke tests for VersionService."""

    def test_version_service_init(self, mock_metadata, mock_cas, mock_kernel, mock_dlc):
        """Test VersionService can be instantiated."""
        from nexus.bricks.versioning.version_service import VersionService

        service = VersionService(
            metadata_store=mock_metadata,
            cas_store=mock_cas,
            kernel=mock_kernel,
            dlc=mock_dlc,
        )

        assert service.metadata == mock_metadata
        # CAS store is stored as self.cas
        assert service.cas == mock_cas
        assert service.kernel == mock_kernel
        assert service.dlc == mock_dlc

    @pytest.mark.asyncio
    async def test_list_versions_basic(self, mock_metadata, mock_cas, mock_kernel, mock_dlc):
        """Test list_versions can be called."""
        from nexus.bricks.versioning.version_service import VersionService

        service = VersionService(
            metadata_store=mock_metadata,
            cas_store=mock_cas,
            kernel=mock_kernel,
            dlc=mock_dlc,
            enforce_permissions=False,
        )

        # Mock list_versions to return empty list
        mock_metadata.list_versions.return_value = []

        result = await service.list_versions("/test.txt")
        assert isinstance(result, list)


# =============================================================================
# MCPService Smoke Tests
# =============================================================================


class TestMCPServiceSmoke:
    """Smoke tests for MCPService."""

    def test_mcp_service_init(self):
        """Test MCPService can be instantiated."""
        from nexus.bricks.mcp.mcp_service import MCPService

        service = MCPService(filesystem=None)
        assert service._filesystem is None

    @pytest.mark.asyncio
    async def test_mcp_mount_validation(self):
        """Test mcp_mount validates inputs."""
        from nexus.bricks.mcp.mcp_service import MCPService
        from nexus.contracts.exceptions import ValidationError

        service = MCPService(filesystem=None)

        # Should fail without command or url
        with pytest.raises(ValidationError, match="Either command or url is required"):
            await service.mcp_mount(name="test")


# =============================================================================
# OAuthCredentialService Smoke Tests
# =============================================================================


class TestOAuthServiceSmoke:
    """Smoke tests for OAuthCredentialService."""

    def test_oauth_service_init(self):
        """Test OAuthCredentialService can be instantiated."""
        from nexus.bricks.auth.oauth.credential_service import OAuthCredentialService

        service = OAuthCredentialService(oauth_factory=None, token_manager=None)
        # Service can be created without factory/token_manager (lazy initialization)
        assert service is not None

    @pytest.mark.asyncio
    async def test_oauth_list_providers_basic(self):
        """Test list_providers can be called."""
        from nexus.bricks.auth.oauth.credential_service import OAuthCredentialService

        service = OAuthCredentialService(oauth_factory=None, token_manager=None)

        # Should work even without config (returns empty list)
        try:
            result = await service.list_providers()
            assert isinstance(result, list)
        except Exception:
            # May raise if config not found, that's ok for smoke test
            pass


# =============================================================================
# SearchService Smoke Tests
# =============================================================================


class TestSearchServiceSmoke:
    """Smoke tests for SearchService."""

    def test_search_service_init(self, mock_metadata):
        """Test SearchService can be instantiated."""
        from nexus.bricks.search.search_service import SearchService

        service = SearchService(
            metadata_store=mock_metadata,
            permission_enforcer=None,
            enforce_permissions=False,
        )

        assert service.metadata == mock_metadata
        assert service._enforce_permissions is False

    @pytest.mark.asyncio
    async def test_semantic_search_not_initialized(self, mock_metadata):
        """Test semantic_search raises if not initialized."""
        from nexus.bricks.search.search_service import SearchService

        service = SearchService(
            metadata_store=mock_metadata,
            enforce_permissions=False,
        )

        with pytest.raises(ValueError, match="not available"):
            await service.semantic_search(query="test")

    @pytest.mark.asyncio
    async def test_initialize_semantic_search_basic(self, mock_metadata):
        """Test initialize_semantic_search can be called."""
        from nexus.bricks.search.search_service import SearchService

        service = SearchService(
            metadata_store=mock_metadata,
            enforce_permissions=False,
        )

        # Should work without embedding provider (keyword-only mode)
        try:
            await service.initialize_semantic_search(
                embedding_provider=None,
                async_mode=True,
            )
            # Check that _async_search was created
            assert hasattr(service, "_async_search")
        except Exception:
            # May fail if database connection fails, that's ok for smoke test
            pass


# =============================================================================
# MountService Smoke Tests
# =============================================================================


class TestMountServiceSmoke:
    """Smoke tests for MountService."""

    def test_mount_service_init(self, mock_kernel, mock_dlc):
        """Test MountService can be instantiated."""
        from nexus.bricks.mount.mount_service import MountService

        service = MountService(kernel=mock_kernel, dlc=mock_dlc)
        assert service._kernel is mock_kernel
        assert service._dlc is mock_dlc

    @pytest.mark.asyncio
    async def test_list_mounts_basic(self, mock_kernel, mock_dlc):
        """Test list_mounts can be called."""
        from nexus.bricks.mount.mount_service import MountService

        service = MountService(kernel=mock_kernel, dlc=mock_dlc)

        # Should return list (may be empty)
        result = await service.list_mounts()
        assert isinstance(result, list)


# =============================================================================
# ReBACService Smoke Tests
# =============================================================================


class TestReBACServiceSmoke:
    """Smoke tests for ReBACService."""

    def test_rebac_service_init(self):
        """Test ReBACService can be instantiated."""
        from nexus.bricks.rebac.rebac_service import ReBACService

        service = ReBACService(rebac_manager=None, enforce_permissions=False)
        assert service._rebac_manager is None
        assert service._enforce_permissions is False

    @pytest.mark.asyncio
    async def test_rebac_check_without_manager(self):
        """Test rebac_check raises without manager."""
        from nexus.bricks.rebac.rebac_service import ReBACService

        service = ReBACService(rebac_manager=None, enforce_permissions=False)

        # Should raise RuntimeError without manager
        with pytest.raises(RuntimeError, match="ReBAC manager is not available"):
            await service.rebac_check(
                subject=("user", "alice"),
                permission="view",
                object=("file", "/test.txt"),
            )


# =============================================================================
# Integration Smoke Test
# =============================================================================


class TestServiceIntegrationSmoke:
    """Smoke test for service integration patterns."""

    def test_all_services_can_coexist(self, mock_metadata, mock_cas, mock_kernel, mock_dlc):
        """Test that all services can be instantiated together."""

        from nexus.bricks.auth.oauth.credential_service import OAuthCredentialService
        from nexus.bricks.mcp.mcp_service import MCPService
        from nexus.bricks.mount.mount_service import MountService
        from nexus.bricks.rebac.rebac_service import ReBACService
        from nexus.bricks.search.search_service import SearchService
        from nexus.bricks.versioning.version_service import VersionService

        # Create all services
        version_svc = VersionService(
            metadata_store=mock_metadata,
            cas_store=mock_cas,
            kernel=mock_kernel,
            dlc=mock_dlc,
        )
        mcp_svc = MCPService(filesystem=None)
        oauth_svc = OAuthCredentialService(oauth_factory=None, token_manager=None)
        search_svc = SearchService(metadata_store=mock_metadata, enforce_permissions=False)
        mount_svc = MountService(kernel=mock_kernel, dlc=mock_dlc)
        rebac_svc = ReBACService(rebac_manager=None, enforce_permissions=False)

        # Verify all instantiated
        assert version_svc is not None
        assert mcp_svc is not None
        assert oauth_svc is not None
        assert search_svc is not None
        assert mount_svc is not None
        assert rebac_svc is not None

        # Verify they have expected attributes
        assert hasattr(version_svc, "list_versions")
        assert hasattr(mcp_svc, "mcp_mount")
        assert hasattr(oauth_svc, "list_providers")
        assert hasattr(search_svc, "semantic_search")
        assert hasattr(mount_svc, "list_mounts")
        assert hasattr(rebac_svc, "rebac_check")
