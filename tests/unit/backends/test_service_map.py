"""Unit tests for service map module."""

from nexus.backends.service_map import SERVICE_REGISTRY, ServiceInfo, ServiceMap


class TestServiceInfo:
    """Test ServiceInfo dataclass."""

    def test_service_info_creation(self) -> None:
        """Test creating ServiceInfo instance."""
        info = ServiceInfo(
            name="test-service",
            display_name="Test Service",
            connector="test_connector",
            klavis_mcp="test_mcp",
            oauth_provider="test_oauth",
            capabilities=["read", "write"],
            description="Test description",
        )

        assert info.name == "test-service"
        assert info.display_name == "Test Service"
        assert info.connector == "test_connector"
        assert info.klavis_mcp == "test_mcp"
        assert info.oauth_provider == "test_oauth"
        assert info.capabilities == ["read", "write"]
        assert info.description == "Test description"

    def test_service_info_minimal(self) -> None:
        """Test ServiceInfo with minimal fields."""
        info = ServiceInfo(
            name="minimal",
            display_name="Minimal",
            connector=None,
            klavis_mcp=None,
            oauth_provider=None,
            capabilities=[],
        )

        assert info.name == "minimal"
        assert info.connector is None
        assert info.klavis_mcp is None
        assert info.oauth_provider is None
        assert info.description == ""  # Default value


class TestServiceRegistry:
    """Test SERVICE_REGISTRY contents."""

    def test_registry_not_empty(self) -> None:
        """Test that registry contains services."""
        assert len(SERVICE_REGISTRY) > 0

    def test_registry_google_drive(self) -> None:
        """Test Google Drive service in registry."""
        assert "google-drive" in SERVICE_REGISTRY

        info = SERVICE_REGISTRY["google-drive"]
        assert info.name == "google-drive"
        assert info.display_name == "Google Drive"
        assert info.connector == "gdrive_connector"
        assert info.klavis_mcp == "google_drive"
        assert info.oauth_provider == "google"
        assert "read" in info.capabilities
        assert "write" in info.capabilities

    def test_registry_gmail(self) -> None:
        """Test Gmail service in registry."""
        assert "gmail" in SERVICE_REGISTRY

        info = SERVICE_REGISTRY["gmail"]
        assert info.name == "gmail"
        assert info.connector is None  # Gmail has no connector, only MCP
        assert info.klavis_mcp == "gmail"
        assert info.oauth_provider == "google"

    def test_registry_s3(self) -> None:
        """Test S3 service in registry."""
        assert "s3" in SERVICE_REGISTRY

        info = SERVICE_REGISTRY["s3"]
        assert info.name == "s3"
        assert info.connector == "s3_connector"
        assert info.klavis_mcp is None
        assert info.oauth_provider is None  # S3 uses AWS credentials, not OAuth

    def test_registry_all_services_have_required_fields(self) -> None:
        """Test that all services have required fields."""
        for name, info in SERVICE_REGISTRY.items():
            assert info.name == name  # name matches key
            assert info.display_name  # has display name
            assert isinstance(info.capabilities, list)
            # At least one of connector or klavis_mcp should exist
            assert info.connector or info.klavis_mcp


class TestServiceMapGetServiceName:
    """Test ServiceMap.get_service_name() method."""

    def test_get_service_name_by_connector(self) -> None:
        """Test getting service name from connector."""
        assert ServiceMap.get_service_name(connector="gdrive_connector") == "google-drive"
        assert ServiceMap.get_service_name(connector="gcs_connector") == "gcs"
        assert ServiceMap.get_service_name(connector="s3_connector") == "s3"
        assert ServiceMap.get_service_name(connector="x_connector") == "x"
        assert ServiceMap.get_service_name(connector="hn_connector") == "hackernews"

    def test_get_service_name_by_mcp(self) -> None:
        """Test getting service name from MCP."""
        assert ServiceMap.get_service_name(mcp="google_drive") == "google-drive"
        assert ServiceMap.get_service_name(mcp="gmail") == "gmail"
        assert ServiceMap.get_service_name(mcp="github") == "github"
        assert ServiceMap.get_service_name(mcp="slack") == "slack"

    def test_get_service_name_not_found(self) -> None:
        """Test getting service name for non-existent service."""
        assert ServiceMap.get_service_name(connector="nonexistent") is None
        assert ServiceMap.get_service_name(mcp="nonexistent") is None

    def test_get_service_name_no_args(self) -> None:
        """Test calling without arguments."""
        assert ServiceMap.get_service_name() is None


class TestServiceMapGetServiceInfo:
    """Test ServiceMap.get_service_info() method."""

    def test_get_service_info_exists(self) -> None:
        """Test getting full service info."""
        info = ServiceMap.get_service_info("google-drive")
        assert info is not None
        assert info.name == "google-drive"
        assert info.connector == "gdrive_connector"
        assert info.klavis_mcp == "google_drive"

    def test_get_service_info_not_found(self) -> None:
        """Test getting info for non-existent service."""
        assert ServiceMap.get_service_info("nonexistent") is None


class TestServiceMapGetConnector:
    """Test ServiceMap.get_connector() method."""

    def test_get_connector_exists(self) -> None:
        """Test getting connector for service."""
        assert ServiceMap.get_connector("google-drive") == "gdrive_connector"
        assert ServiceMap.get_connector("gcs") == "gcs_connector"
        assert ServiceMap.get_connector("s3") == "s3_connector"

    def test_get_connector_none(self) -> None:
        """Test getting connector for service that has none."""
        assert ServiceMap.get_connector("gmail") is None
        assert ServiceMap.get_connector("github") is None

    def test_get_connector_not_found(self) -> None:
        """Test getting connector for non-existent service."""
        assert ServiceMap.get_connector("nonexistent") is None


class TestServiceMapGetMcp:
    """Test ServiceMap.get_mcp() method."""

    def test_get_mcp_exists(self) -> None:
        """Test getting MCP for service."""
        assert ServiceMap.get_mcp("google-drive") == "google_drive"
        assert ServiceMap.get_mcp("gmail") == "gmail"
        assert ServiceMap.get_mcp("github") == "github"

    def test_get_mcp_none(self) -> None:
        """Test getting MCP for service that has none."""
        assert ServiceMap.get_mcp("s3") is None
        assert ServiceMap.get_mcp("hackernews") is None

    def test_get_mcp_not_found(self) -> None:
        """Test getting MCP for non-existent service."""
        assert ServiceMap.get_mcp("nonexistent") is None


class TestServiceMapGetOAuthProvider:
    """Test ServiceMap.get_oauth_provider() method."""

    def test_get_oauth_provider_exists(self) -> None:
        """Test getting OAuth provider for service."""
        assert ServiceMap.get_oauth_provider("google-drive") == "google"
        assert ServiceMap.get_oauth_provider("gmail") == "google"
        assert ServiceMap.get_oauth_provider("github") == "github"
        assert ServiceMap.get_oauth_provider("x") == "twitter"

    def test_get_oauth_provider_none(self) -> None:
        """Test getting OAuth provider for service that has none."""
        assert ServiceMap.get_oauth_provider("s3") is None
        assert ServiceMap.get_oauth_provider("hackernews") is None

    def test_get_oauth_provider_not_found(self) -> None:
        """Test getting OAuth provider for non-existent service."""
        assert ServiceMap.get_oauth_provider("nonexistent") is None


class TestServiceMapListServices:
    """Test ServiceMap.list_services() method."""

    def test_list_services(self) -> None:
        """Test listing all services."""
        services = ServiceMap.list_services()
        assert isinstance(services, list)
        assert len(services) > 0
        assert "google-drive" in services
        assert "gmail" in services
        assert "s3" in services


class TestServiceMapListServicesWithConnector:
    """Test ServiceMap.list_services_with_connector() method."""

    def test_list_services_with_connector(self) -> None:
        """Test listing services with connectors."""
        services = ServiceMap.list_services_with_connector()
        assert isinstance(services, list)
        assert "google-drive" in services  # Has gdrive_connector
        assert "gcs" in services  # Has gcs_connector
        assert "s3" in services  # Has s3_connector
        assert "gmail" not in services  # No connector
        assert "github" not in services  # No connector


class TestServiceMapListServicesWithMcp:
    """Test ServiceMap.list_services_with_mcp() method."""

    def test_list_services_with_mcp(self) -> None:
        """Test listing services with MCP."""
        services = ServiceMap.list_services_with_mcp()
        assert isinstance(services, list)
        assert "google-drive" in services  # Has google_drive MCP
        assert "gmail" in services  # Has gmail MCP
        assert "github" in services  # Has github MCP
        # S3 and HN don't have MCP
        assert "hackernews" not in services


class TestServiceMapHasBoth:
    """Test ServiceMap.has_both() method."""

    def test_has_both_true(self) -> None:
        """Test service that has both connector and MCP."""
        assert ServiceMap.has_both("google-drive") is True

    def test_has_both_connector_only(self) -> None:
        """Test service with only connector."""
        assert ServiceMap.has_both("s3") is False
        assert ServiceMap.has_both("hackernews") is False

    def test_has_both_mcp_only(self) -> None:
        """Test service with only MCP."""
        assert ServiceMap.has_both("gmail") is False
        assert ServiceMap.has_both("github") is False

    def test_has_both_not_found(self) -> None:
        """Test non-existent service."""
        assert ServiceMap.has_both("nonexistent") is False
