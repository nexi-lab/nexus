"""Service Name Mapping for unified connector and MCP naming.

This module provides a centralized mapping between:
- Unified service names (e.g., "google-drive")
- Nexus connector types (e.g., "gdrive_connector")
- Klavis MCP server names (e.g., "google_drive")

The unified service name is used for:
- Skill folder paths: /skills/{tier}/{service_name}/
- SKILL.md generation
- OAuth token mapping

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

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


@dataclass
class ServiceInfo:
    """Information about a service."""

    name: str  # Unified service name
    display_name: str  # Human-readable name
    connector: str | None  # Nexus connector type (if exists)
    klavis_mcp: str | None  # Klavis MCP server name (if exists)
    oauth_provider: str | None  # OAuth provider name (e.g., "google")
    capabilities: list[str]  # ["read", "write", "list", "delete", "tools"]
    description: str = ""


# Unified service registry
# Key: unified service name
# Value: ServiceInfo
SERVICE_REGISTRY: dict[str, ServiceInfo] = {
    # Google services
    "google-drive": ServiceInfo(
        name="google-drive",
        display_name="Google Drive",
        connector="gdrive_connector",
        klavis_mcp="google_drive",
        oauth_provider="google",
        capabilities=["read", "write", "list", "delete", "tools"],
        description="Google Drive files and folders",
    ),
    "gmail": ServiceInfo(
        name="gmail",
        display_name="Gmail",
        connector=None,
        klavis_mcp="gmail",
        oauth_provider="google",
        capabilities=["tools"],
        description="Gmail MCP integration. Read, send, and manage Gmail messages, threads, and labels via MCP tools.",
    ),
    "google-docs": ServiceInfo(
        name="google-docs",
        display_name="Google Docs",
        connector=None,
        klavis_mcp="google_docs",
        oauth_provider="google",
        capabilities=["tools"],
        description="Google Docs MCP integration. Create, read, and edit Google Docs documents via MCP tools.",
    ),
    "google-sheets": ServiceInfo(
        name="google-sheets",
        display_name="Google Sheets",
        connector=None,
        klavis_mcp="google_sheets",
        oauth_provider="google",
        capabilities=["tools"],
        description="Google Sheets spreadsheets",
    ),
    "google-calendar": ServiceInfo(
        name="google-calendar",
        display_name="Google Calendar",
        connector=None,
        klavis_mcp="google_calendar",
        oauth_provider="google",
        capabilities=["tools"],
        description="Google Calendar events",
    ),
    # Cloud storage
    "gcs": ServiceInfo(
        name="gcs",
        display_name="Google Cloud Storage",
        connector="gcs_connector",
        klavis_mcp=None,
        oauth_provider="google",
        capabilities=["read", "write", "list", "delete"],
        description="Google Cloud Storage buckets and objects",
    ),
    "s3": ServiceInfo(
        name="s3",
        display_name="Amazon S3",
        connector="s3_connector",
        klavis_mcp=None,
        oauth_provider=None,  # Uses AWS credentials, not OAuth
        capabilities=["read", "write", "list", "delete"],
        description="Amazon S3 buckets and objects",
    ),
    # Social/Dev platforms
    "github": ServiceInfo(
        name="github",
        display_name="GitHub",
        connector=None,
        klavis_mcp="github",
        oauth_provider="github",
        capabilities=["tools"],
        description="GitHub repositories, issues, pull requests",
    ),
    "slack": ServiceInfo(
        name="slack",
        display_name="Slack",
        connector=None,
        klavis_mcp="slack",
        oauth_provider="slack",
        capabilities=["tools"],
        description="Slack messages, channels, users",
    ),
    "notion": ServiceInfo(
        name="notion",
        display_name="Notion",
        connector=None,
        klavis_mcp="notion",
        oauth_provider="notion",
        capabilities=["tools"],
        description="Notion pages, databases, blocks",
    ),
    "linear": ServiceInfo(
        name="linear",
        display_name="Linear",
        connector=None,
        klavis_mcp="linear",
        oauth_provider="linear",
        capabilities=["tools"],
        description="Linear issues, projects, teams",
    ),
    "x": ServiceInfo(
        name="x",
        display_name="X (Twitter)",
        connector="x_connector",
        klavis_mcp=None,  # Klavis doesn't support X yet
        oauth_provider="twitter",
        capabilities=["read", "write", "list"],
        description="X timeline, posts, users",
    ),
    # Read-only services
    "hackernews": ServiceInfo(
        name="hackernews",
        display_name="Hacker News",
        connector="hn_connector",
        klavis_mcp=None,
        oauth_provider=None,  # No auth needed
        capabilities=["read", "list"],
        description="Hacker News stories, comments, jobs",
    ),
}

# Reverse lookup maps (built from SERVICE_REGISTRY)
_CONNECTOR_TO_SERVICE: dict[str, str] = {}
_MCP_TO_SERVICE: dict[str, str] = {}

for _service_name, _info in SERVICE_REGISTRY.items():
    if _info.connector:
        _CONNECTOR_TO_SERVICE[_info.connector] = _service_name
    if _info.klavis_mcp:
        _MCP_TO_SERVICE[_info.klavis_mcp] = _service_name


class ServiceMap:
    """Helper class for service name lookups."""

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
        return SERVICE_REGISTRY.get(service_name)

    @staticmethod
    def get_connector(service_name: str) -> str | None:
        """Get connector type for a service.

        Args:
            service_name: Unified service name

        Returns:
            Connector type or None if no connector exists
        """
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
        info = SERVICE_REGISTRY.get(service_name)
        return info.oauth_provider if info else None

    @staticmethod
    def list_services() -> list[str]:
        """List all unified service names.

        Returns:
            List of service names
        """
        return list(SERVICE_REGISTRY.keys())

    @staticmethod
    def list_services_with_connector() -> list[str]:
        """List services that have a Nexus connector.

        Returns:
            List of service names with connectors
        """
        return [name for name, info in SERVICE_REGISTRY.items() if info.connector]

    @staticmethod
    def list_services_with_mcp() -> list[str]:
        """List services that have a Klavis MCP.

        Returns:
            List of service names with MCPs
        """
        return [name for name, info in SERVICE_REGISTRY.items() if info.klavis_mcp]

    @staticmethod
    def has_both(service_name: str) -> bool:
        """Check if service has both connector and MCP.

        Args:
            service_name: Unified service name

        Returns:
            True if service has both connector and MCP
        """
        info = SERVICE_REGISTRY.get(service_name)
        return bool(info and info.connector and info.klavis_mcp)
