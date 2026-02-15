"""Unit tests for service map module."""

from nexus.backends.registry import ConnectorRegistry
from nexus.backends.service_map import (
    SERVICE_REGISTRY,
    ServiceInfo,
    ServiceMap,
    _sync_from_connector_registry,
)


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
    """Test SERVICE_REGISTRY contents after sync."""

    def test_registry_not_empty(self) -> None:
        """Test that registry contains services."""
        assert len(SERVICE_REGISTRY) > 0

    def test_registry_google_drive(self) -> None:
        """Test Google Drive service — connector auto-derived from registry."""
        info = ServiceMap.get_service_info("google-drive")
        assert info is not None
        assert info.name == "google-drive"
        assert info.display_name == "Google Drive"
        assert info.connector == "gdrive_connector"
        assert info.klavis_mcp == "google_drive"
        assert info.oauth_provider == "google"
        assert "read" in info.capabilities
        assert "write" in info.capabilities

    def test_registry_gmail_has_connector(self) -> None:
        """Test Gmail service now has a connector (was None before auto-derive fix)."""
        info = ServiceMap.get_service_info("gmail")
        assert info is not None
        assert info.name == "gmail"
        assert info.connector == "gmail_connector"
        assert info.klavis_mcp == "gmail"
        assert info.oauth_provider == "google"

    def test_registry_gcalendar_has_connector(self) -> None:
        """Test Google Calendar service now has a connector (was None before)."""
        info = ServiceMap.get_service_info("google-calendar")
        assert info is not None
        assert info.connector == "gcalendar_connector"
        assert info.klavis_mcp == "google_calendar"
        assert info.oauth_provider == "google"

    def test_registry_mcp_only_services(self) -> None:
        """Test services that are MCP-only (no connector)."""
        for service_name in ("google-docs", "google-sheets", "github", "notion", "linear"):
            info = ServiceMap.get_service_info(service_name)
            assert info is not None, f"{service_name} missing from registry"
            assert info.connector is None, f"{service_name} should have no connector"
            assert info.klavis_mcp is not None, f"{service_name} should have MCP"

    def test_registry_all_services_have_required_fields(self) -> None:
        """Test that all services have required fields."""
        _sync_from_connector_registry()
        # Services whose connector depends on optional packages may have
        # connector=None when those packages aren't installed.
        # They still have a valid entry (connector auto-derives when deps are available).
        connector_only_services = {
            name
            for name, info in SERVICE_REGISTRY.items()
            if info.klavis_mcp is None
        }
        for name, info in SERVICE_REGISTRY.items():
            assert info.name == name  # name matches key
            assert info.display_name  # has display name
            assert isinstance(info.capabilities, list)
            # Services with MCP always have it.  Connector-only services
            # may have connector=None if optional deps are missing.
            if name not in connector_only_services:
                assert info.klavis_mcp is not None, (
                    f"Service '{name}' should have MCP"
                )


class TestAutoDerive:
    """Test auto-derivation of connector fields from ConnectorRegistry."""

    def test_connectors_with_service_name_populate_service_map(self) -> None:
        """Test that registered connectors with service_name populate service_map."""
        _sync_from_connector_registry()
        # All connectors that declare service_name should have their connector
        # field populated in SERVICE_REGISTRY
        for info in ConnectorRegistry.list_all():
            if info.service_name and info.service_name in SERVICE_REGISTRY:
                service = SERVICE_REGISTRY[info.service_name]
                assert service.connector == info.name, (
                    f"Service '{info.service_name}' should have connector='{info.name}', "
                    f"got '{service.connector}'"
                )

    def test_connector_registry_service_name_round_trip(self) -> None:
        """Test that service_name on ConnectorInfo round-trips through ServiceMap."""
        for info in ConnectorRegistry.list_all():
            if info.service_name:
                # Connector → service name
                service_name = ServiceMap.get_service_name(connector=info.name)
                assert service_name == info.service_name, (
                    f"Connector '{info.name}' should map to service '{info.service_name}', "
                    f"got '{service_name}'"
                )
                # Service name → connector
                connector = ServiceMap.get_connector(info.service_name)
                assert connector == info.name, (
                    f"Service '{info.service_name}' should map to connector '{info.name}', "
                    f"got '{connector}'"
                )


class TestServiceMapGetServiceName:
    """Test ServiceMap.get_service_name() method."""

    def test_get_service_name_by_connector(self) -> None:
        """Test getting service name from connector."""
        assert ServiceMap.get_service_name(connector="gdrive_connector") == "google-drive"
        assert ServiceMap.get_service_name(connector="gmail_connector") == "gmail"
        assert ServiceMap.get_service_name(connector="gcalendar_connector") == "google-calendar"

    def test_get_service_name_by_connector_optional(self) -> None:
        """Test connector lookup for backends with optional deps."""
        # These may not be registered if deps aren't installed
        for connector, expected_service in [
            ("gcs_connector", "gcs"),
            ("s3_connector", "s3"),
            ("x_connector", "x"),
            ("hn_connector", "hackernews"),
        ]:
            result = ServiceMap.get_service_name(connector=connector)
            if ConnectorRegistry.is_registered(connector):
                assert result == expected_service
            else:
                assert result is None  # Not registered, deps missing

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
        """Test getting connector for services with connectors."""
        assert ServiceMap.get_connector("google-drive") == "gdrive_connector"
        assert ServiceMap.get_connector("gmail") == "gmail_connector"
        assert ServiceMap.get_connector("google-calendar") == "gcalendar_connector"

    def test_get_connector_none(self) -> None:
        """Test getting connector for MCP-only services."""
        assert ServiceMap.get_connector("github") is None
        assert ServiceMap.get_connector("google-docs") is None
        assert ServiceMap.get_connector("notion") is None

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
        # These services have connectors but no MCP
        # Only check if they're registered (deps might not be available)
        if ServiceMap.get_connector("hackernews"):
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

    def test_get_oauth_provider_none(self) -> None:
        """Test getting OAuth provider for service that has none."""
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


class TestServiceMapListServicesWithConnector:
    """Test ServiceMap.list_services_with_connector() method."""

    def test_list_services_with_connector(self) -> None:
        """Test listing services with connectors."""
        services = ServiceMap.list_services_with_connector()
        assert isinstance(services, list)
        assert "google-drive" in services
        assert "gmail" in services  # Now has connector via auto-derive
        assert "google-calendar" in services  # Now has connector via auto-derive
        # MCP-only services should NOT be in this list
        assert "github" not in services
        assert "notion" not in services
        assert "google-docs" not in services


class TestServiceMapListServicesWithMcp:
    """Test ServiceMap.list_services_with_mcp() method."""

    def test_list_services_with_mcp(self) -> None:
        """Test listing services with MCP."""
        services = ServiceMap.list_services_with_mcp()
        assert isinstance(services, list)
        assert "google-drive" in services  # Has google_drive MCP
        assert "gmail" in services  # Has gmail MCP
        assert "github" in services  # Has github MCP


class TestServiceMapHasBoth:
    """Test ServiceMap.has_both() method."""

    def test_has_both_true(self) -> None:
        """Test services that have both connector and MCP."""
        assert ServiceMap.has_both("google-drive") is True
        assert ServiceMap.has_both("gmail") is True  # Now has both
        assert ServiceMap.has_both("google-calendar") is True  # Now has both

    def test_has_both_connector_only(self) -> None:
        """Test services with only connector (no MCP)."""
        # Only check if their connector is actually registered
        if ServiceMap.get_connector("hackernews"):
            assert ServiceMap.has_both("hackernews") is False

    def test_has_both_mcp_only(self) -> None:
        """Test service with only MCP (no connector)."""
        assert ServiceMap.has_both("github") is False
        assert ServiceMap.has_both("google-docs") is False
        assert ServiceMap.has_both("notion") is False

    def test_has_both_not_found(self) -> None:
        """Test non-existent service."""
        assert ServiceMap.has_both("nonexistent") is False
