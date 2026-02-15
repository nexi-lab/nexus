"""Service Name Mapping for unified connector and MCP naming.

This module provides a centralized mapping between:
- Unified service names (e.g., "google-drive")
- Nexus connector types (e.g., "gdrive_connector")
- Klavis MCP server names (e.g., "google_drive")

The unified service name is used for:
- Skill folder paths: /skills/{tier}/{service_name}/
- SKILL.md generation
- OAuth token mapping

Connector fields are auto-derived from ConnectorRegistry via the
``service_name`` metadata on each ``@register_connector`` decorator.
This eliminates manual synchronization between the two registries.

Example:
    >>> from nexus.backends.service_map import ServiceMap
    >>>
    >>> # Get unified name from connector
    >>> ServiceMap.get_service_name(connector="gdrive_connector")
    'google-drive'
    >>>
    >>> # Get unified name from MCP
    >>> ServiceMap.get_service_name(mcp="google_drive")
    'google-drive'
    >>>
    >>> # Get connector for a service
    >>> ServiceMap.get_connector("google-drive")
    'gdrive_connector'
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ServiceInfo:
    """Information about a service."""

    name: str  # Unified service name
    display_name: str  # Human-readable name
    connector: str | None  # Nexus connector type (auto-derived from registry)
    klavis_mcp: str | None  # Klavis MCP server name (if exists)
    oauth_provider: str | None  # OAuth provider name (e.g., "google")
    capabilities: list[str]  # ["read", "write", "list", "delete", "tools"]
    description: str = ""


# Unified service registry
# Key: unified service name
# Value: ServiceInfo
#
# NOTE: The `connector` field is intentionally set to None for services that
# have connectors. It is auto-populated by _sync_from_connector_registry()
# using the `service_name` metadata from @register_connector decorators.
# This eliminates the class of bugs where connector fields go stale.
SERVICE_REGISTRY: dict[str, ServiceInfo] = {
    # Google services — connector fields auto-derived from ConnectorRegistry
    "google-drive": ServiceInfo(
        name="google-drive",
        display_name="Google Drive",
        connector=None,  # auto-derived: gdrive_connector
        klavis_mcp="google_drive",
        oauth_provider="google",
        capabilities=["read", "write", "list", "delete", "tools"],
        description="Google Drive files and folders",
    ),
    "gmail": ServiceInfo(
        name="gmail",
        display_name="Gmail",
        connector=None,  # auto-derived: gmail_connector
        klavis_mcp="gmail",
        oauth_provider="google",
        capabilities=["read", "list", "tools"],
        description="Gmail emails with OAuth 2.0 authentication",
    ),
    "google-docs": ServiceInfo(
        name="google-docs",
        display_name="Google Docs",
        connector=None,  # MCP-only, no connector
        klavis_mcp="google_docs",
        oauth_provider="google",
        capabilities=["tools"],
        description="Google Docs MCP integration. Create, read, and edit Google Docs documents via MCP tools.",
    ),
    "google-sheets": ServiceInfo(
        name="google-sheets",
        display_name="Google Sheets",
        connector=None,  # MCP-only, no connector
        klavis_mcp="google_sheets",
        oauth_provider="google",
        capabilities=["tools"],
        description="Google Sheets spreadsheets",
    ),
    "google-calendar": ServiceInfo(
        name="google-calendar",
        display_name="Google Calendar",
        connector=None,  # auto-derived: gcalendar_connector
        klavis_mcp="google_calendar",
        oauth_provider="google",
        capabilities=["read", "write", "list", "delete", "tools"],
        description="Google Calendar events with full CRUD support",
    ),
    # Cloud storage — connector fields auto-derived from ConnectorRegistry
    "gcs": ServiceInfo(
        name="gcs",
        display_name="Google Cloud Storage",
        connector=None,  # auto-derived: gcs_connector
        klavis_mcp=None,
        oauth_provider="google",
        capabilities=["read", "write", "list", "delete"],
        description="Google Cloud Storage buckets and objects",
    ),
    "s3": ServiceInfo(
        name="s3",
        display_name="Amazon S3",
        connector=None,  # auto-derived: s3_connector
        klavis_mcp=None,
        oauth_provider=None,  # Uses AWS credentials, not OAuth
        capabilities=["read", "write", "list", "delete"],
        description="Amazon S3 buckets and objects",
    ),
    # Social/Dev platforms
    "github": ServiceInfo(
        name="github",
        display_name="GitHub",
        connector=None,  # MCP-only, no connector
        klavis_mcp="github",
        oauth_provider="github",
        capabilities=["tools"],
        description="GitHub repositories, issues, pull requests",
    ),
    "slack": ServiceInfo(
        name="slack",
        display_name="Slack",
        connector="slack_connector",  # TODO: add @register_connector to slack_connector.py
        klavis_mcp="slack",
        oauth_provider="slack",
        capabilities=["read", "write", "list", "tools"],
        description="Slack messages, channels, users",
    ),
    "notion": ServiceInfo(
        name="notion",
        display_name="Notion",
        connector=None,  # MCP-only, no connector
        klavis_mcp="notion",
        oauth_provider="notion",
        capabilities=["tools"],
        description="Notion pages, databases, blocks",
    ),
    "linear": ServiceInfo(
        name="linear",
        display_name="Linear",
        connector=None,  # MCP-only, no connector
        klavis_mcp="linear",
        oauth_provider="linear",
        capabilities=["tools"],
        description="Linear issues, projects, teams",
    ),
    "x": ServiceInfo(
        name="x",
        display_name="X (Twitter)",
        connector=None,  # auto-derived: x_connector
        klavis_mcp=None,  # Klavis doesn't support X yet
        oauth_provider="twitter",
        capabilities=["read", "write", "list"],
        description="X timeline, posts, users",
    ),
    # Read-only services
    "hackernews": ServiceInfo(
        name="hackernews",
        display_name="Hacker News",
        connector=None,  # auto-derived: hn_connector
        klavis_mcp=None,
        oauth_provider=None,  # No auth needed
        capabilities=["read", "list"],
        description="Hacker News stories, comments, jobs",
    ),
}

# Reverse lookup maps — rebuilt by _sync_from_connector_registry()
_CONNECTOR_TO_SERVICE: dict[str, str] = {}
_MCP_TO_SERVICE: dict[str, str] = {}
_synced = False


def _sync_from_connector_registry() -> None:
    """Auto-derive connector fields from ConnectorRegistry.

    Iterates all registered connectors that declare a ``service_name`` and
    populates the corresponding ``connector`` field in SERVICE_REGISTRY.
    Then rebuilds reverse lookup maps.

    This is called lazily on first ServiceMap access, ensuring optional
    backends have been registered via ``_register_optional_backends()``.
    """
    global _synced
    if _synced:
        return
    _synced = True

    # Ensure optional backends are imported (triggers @register_connector)
    from nexus.backends import _register_optional_backends

    _register_optional_backends()

    # Auto-populate connector fields from ConnectorRegistry
    from nexus.backends.registry import ConnectorRegistry

    for info in ConnectorRegistry.list_all():
        if info.service_name and info.service_name in SERVICE_REGISTRY:
            service = SERVICE_REGISTRY[info.service_name]
            if service.connector is not None and service.connector != info.name:
                logger.warning(
                    "Service '%s' connector mismatch: static=%s, registry=%s. "
                    "Using registry value.",
                    info.service_name,
                    service.connector,
                    info.name,
                )
            service.connector = info.name

    # Rebuild reverse lookup maps
    _CONNECTOR_TO_SERVICE.clear()
    _MCP_TO_SERVICE.clear()
    for service_name, service_info in SERVICE_REGISTRY.items():
        if service_info.connector:
            _CONNECTOR_TO_SERVICE[service_info.connector] = service_name
        if service_info.klavis_mcp:
            _MCP_TO_SERVICE[service_info.klavis_mcp] = service_name


class ServiceMap:
    """Helper class for service name lookups.

    All methods trigger lazy sync from ConnectorRegistry on first access,
    ensuring connector fields are always up-to-date.
    """

    @staticmethod
    def get_service_name(
        connector: str | None = None,
        mcp: str | None = None,
    ) -> str | None:
        """Get unified service name from connector or MCP name.

        Args:
            connector: Nexus connector type (e.g., "gdrive_connector")
            mcp: Klavis MCP server name (e.g., "google_drive")

        Returns:
            Unified service name or None if not found
        """
        _sync_from_connector_registry()
        if connector:
            return _CONNECTOR_TO_SERVICE.get(connector)
        if mcp:
            return _MCP_TO_SERVICE.get(mcp)
        return None

    @staticmethod
    def get_service_info(service_name: str) -> ServiceInfo | None:
        """Get full service info by unified name.

        Args:
            service_name: Unified service name

        Returns:
            ServiceInfo or None if not found
        """
        _sync_from_connector_registry()
        return SERVICE_REGISTRY.get(service_name)

    @staticmethod
    def get_connector(service_name: str) -> str | None:
        """Get connector type for a service.

        Args:
            service_name: Unified service name

        Returns:
            Connector type or None if no connector exists
        """
        _sync_from_connector_registry()
        info = SERVICE_REGISTRY.get(service_name)
        return info.connector if info else None

    @staticmethod
    def get_mcp(service_name: str) -> str | None:
        """Get Klavis MCP name for a service.

        Args:
            service_name: Unified service name

        Returns:
            MCP name or None if no MCP exists
        """
        _sync_from_connector_registry()
        info = SERVICE_REGISTRY.get(service_name)
        return info.klavis_mcp if info else None

    @staticmethod
    def get_oauth_provider(service_name: str) -> str | None:
        """Get OAuth provider for a service.

        Args:
            service_name: Unified service name

        Returns:
            OAuth provider name or None
        """
        _sync_from_connector_registry()
        info = SERVICE_REGISTRY.get(service_name)
        return info.oauth_provider if info else None

    @staticmethod
    def list_services() -> list[str]:
        """List all unified service names.

        Returns:
            List of service names
        """
        _sync_from_connector_registry()
        return list(SERVICE_REGISTRY.keys())

    @staticmethod
    def list_services_with_connector() -> list[str]:
        """List services that have a Nexus connector.

        Returns:
            List of service names with connectors
        """
        _sync_from_connector_registry()
        return [name for name, info in SERVICE_REGISTRY.items() if info.connector]

    @staticmethod
    def list_services_with_mcp() -> list[str]:
        """List services that have a Klavis MCP.

        Returns:
            List of service names with MCPs
        """
        _sync_from_connector_registry()
        return [name for name, info in SERVICE_REGISTRY.items() if info.klavis_mcp]

    @staticmethod
    def has_both(service_name: str) -> bool:
        """Check if service has both connector and MCP.

        Args:
            service_name: Unified service name

        Returns:
            True if service has both connector and MCP
        """
        _sync_from_connector_registry()
        info = SERVICE_REGISTRY.get(service_name)
        return bool(info and info.connector and info.klavis_mcp)
