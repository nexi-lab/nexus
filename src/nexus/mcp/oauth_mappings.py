"""OAuth to Klavis MCP Mappings.

This module provides mappings between our local OAuth providers/connectors
and Klavis MCP servers that can reuse the same tokens.

When a user authenticates with our OAuth (e.g., for gdrive_connector),
we can pass that token to Klavis via set_auth() instead of requiring
a separate OAuth flow.

Example:
    >>> from nexus.mcp.oauth_mappings import OAuthKlavisMappings
    >>>
    >>> mappings = OAuthKlavisMappings.load_default()
    >>>
    >>> # Check if gmail MCP can reuse our google token
    >>> oauth_provider = mappings.get_oauth_provider_for_klavis_mcp("gmail")
    >>> # Returns "google"
    >>>
    >>> # Get required scopes for gmail
    >>> scopes = mappings.get_required_scopes("gmail")
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class OAuthProviderMapping:
    """Mapping for an OAuth provider to Klavis MCPs."""

    name: str  # e.g., "google", "microsoft"
    local_providers: list[str] = field(default_factory=list)  # Our oauth.yaml provider names
    connectors: list[str] = field(default_factory=list)  # Our connector names
    klavis_mcps: list[str] = field(default_factory=list)  # Klavis MCP server names
    mcp_scopes: dict[str, list[str]] = field(default_factory=dict)  # Required scopes per MCP

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> OAuthProviderMapping:
        """Create from dictionary."""
        return cls(
            name=name,
            local_providers=data.get("local_providers", []),
            connectors=data.get("connectors", []),
            klavis_mcps=data.get("klavis_mcps", []),
            mcp_scopes=data.get("mcp_scopes", {}),
        )


class OAuthKlavisMappings:
    """Registry for OAuth to Klavis MCP mappings.

    This class helps determine when we can reuse our OAuth tokens
    for Klavis MCP servers instead of doing a separate OAuth flow.
    """

    def __init__(self, providers: dict[str, OAuthProviderMapping] | None = None):
        """Initialize mappings.

        Args:
            providers: Dict mapping OAuth provider names to their mappings
        """
        self._providers: dict[str, OAuthProviderMapping] = providers or {}

        # Build reverse lookup: klavis_mcp -> oauth_provider
        self._klavis_to_oauth: dict[str, str] = {}
        for name, mapping in self._providers.items():
            for klavis_mcp in mapping.klavis_mcps:
                self._klavis_to_oauth[klavis_mcp] = name

        # Build reverse lookup: connector -> oauth_provider
        self._connector_to_oauth: dict[str, str] = {}
        for name, mapping in self._providers.items():
            for connector in mapping.connectors:
                self._connector_to_oauth[connector] = name

        # Build reverse lookup: local_provider -> oauth_provider
        self._local_to_oauth: dict[str, str] = {}
        for name, mapping in self._providers.items():
            for local_provider in mapping.local_providers:
                self._local_to_oauth[local_provider] = name

    @classmethod
    def from_yaml(cls, path: str | Path) -> OAuthKlavisMappings:
        """Load mappings from YAML file.

        Args:
            path: Path to YAML config file

        Returns:
            OAuthKlavisMappings instance
        """
        path = Path(path)
        if not path.exists():
            logger.warning(f"OAuth mappings config not found: {path}")
            return cls()

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        providers = {}
        for name, config in data.get("oauth_providers", {}).items():
            providers[name] = OAuthProviderMapping.from_dict(name, config)

        return cls(providers=providers)

    @classmethod
    def load_default(cls) -> OAuthKlavisMappings:
        """Load default mappings.

        Searches for oauth-klavis-mappings.yaml in:
        1. NEXUS_OAUTH_MAPPINGS_PATH environment variable
        2. /app/configs/oauth-klavis-mappings.yaml (Docker)
        3. configs/oauth-klavis-mappings.yaml (relative to package)

        Returns:
            OAuthKlavisMappings with default mappings
        """
        tried_paths = []

        # Try environment variable
        env_path = os.getenv("NEXUS_OAUTH_MAPPINGS_PATH")
        if env_path:
            tried_paths.append(env_path)
            if Path(env_path).exists():
                return cls.from_yaml(env_path)

        # Try Docker path
        docker_path = Path("/app/configs/oauth-klavis-mappings.yaml")
        tried_paths.append(str(docker_path))
        if docker_path.exists():
            return cls.from_yaml(docker_path)

        # Try relative to package
        package_dir = Path(__file__).parent.parent.parent.parent
        config_path = package_dir / "configs" / "oauth-klavis-mappings.yaml"
        tried_paths.append(str(config_path))
        if config_path.exists():
            return cls.from_yaml(config_path)

        # Return empty mappings with built-in defaults
        logger.warning(
            f"No oauth-klavis-mappings.yaml found, using built-in defaults. Tried: {tried_paths}"
        )
        return cls.with_builtin_defaults()

    @classmethod
    def with_builtin_defaults(cls) -> OAuthKlavisMappings:
        """Create mappings with built-in defaults.

        Returns:
            OAuthKlavisMappings with common provider mappings
        """
        providers = {
            "google": OAuthProviderMapping(
                name="google",
                local_providers=["google-drive", "gmail", "google-cloud-storage"],
                connectors=["gdrive_connector"],
                klavis_mcps=[
                    "gmail",
                    "google_drive",
                    "google_docs",
                    "google_sheets",
                    "google_calendar",
                ],
                mcp_scopes={
                    "gmail": ["https://www.googleapis.com/auth/gmail.readonly"],
                    "google_drive": ["https://www.googleapis.com/auth/drive"],
                    "google_docs": ["https://www.googleapis.com/auth/drive"],
                    "google_sheets": ["https://www.googleapis.com/auth/drive"],
                    "google_calendar": ["https://www.googleapis.com/auth/calendar"],
                },
            ),
            "microsoft": OAuthProviderMapping(
                name="microsoft",
                local_providers=["microsoft-onedrive"],
                connectors=[],
                klavis_mcps=["outlook", "onedrive"],
                mcp_scopes={
                    "outlook": ["Mail.Read"],
                    "onedrive": ["Files.ReadWrite.All"],
                },
            ),
            "twitter": OAuthProviderMapping(
                name="twitter",
                local_providers=["x"],
                connectors=["x_connector"],
                klavis_mcps=[],  # Klavis doesn't support X yet
                mcp_scopes={},
            ),
        }
        return cls(providers=providers)

    def get_oauth_provider_for_klavis_mcp(self, klavis_mcp: str) -> str | None:
        """Get the OAuth provider name for a Klavis MCP server.

        Args:
            klavis_mcp: Klavis MCP server name (e.g., "gmail", "google_drive")

        Returns:
            OAuth provider name (e.g., "google") or None if not mapped
        """
        return self._klavis_to_oauth.get(klavis_mcp)

    def get_oauth_provider_for_connector(self, connector: str) -> str | None:
        """Get the OAuth provider name for a connector.

        Args:
            connector: Connector name (e.g., "gdrive_connector")

        Returns:
            OAuth provider name (e.g., "google") or None if not mapped
        """
        return self._connector_to_oauth.get(connector)

    def get_oauth_provider_for_local_provider(self, local_provider: str) -> str | None:
        """Get the OAuth provider name for a local OAuth provider.

        Args:
            local_provider: Local provider name from oauth.yaml (e.g., "google-drive")

        Returns:
            OAuth provider name (e.g., "google") or None if not mapped
        """
        return self._local_to_oauth.get(local_provider)

    def get_required_scopes(self, klavis_mcp: str) -> list[str]:
        """Get required scopes for a Klavis MCP server.

        Args:
            klavis_mcp: Klavis MCP server name

        Returns:
            List of required OAuth scopes
        """
        oauth_provider = self._klavis_to_oauth.get(klavis_mcp)
        if not oauth_provider:
            return []

        mapping = self._providers.get(oauth_provider)
        if not mapping:
            return []

        return mapping.mcp_scopes.get(klavis_mcp, [])

    def get_reusable_klavis_mcps(self, oauth_provider: str) -> list[str]:
        """Get Klavis MCPs that can reuse tokens from an OAuth provider.

        Args:
            oauth_provider: OAuth provider name (e.g., "google")

        Returns:
            List of Klavis MCP names that can reuse the token
        """
        mapping = self._providers.get(oauth_provider)
        if not mapping:
            return []
        return mapping.klavis_mcps

    def can_reuse_token(self, klavis_mcp: str, token_scopes: list[str]) -> bool:
        """Check if a token with given scopes can be reused for a Klavis MCP.

        Args:
            klavis_mcp: Klavis MCP server name
            token_scopes: Scopes the token has

        Returns:
            True if the token has all required scopes
        """
        required_scopes = self.get_required_scopes(klavis_mcp)
        if not required_scopes:
            # No specific scopes required, assume compatible
            return True

        # Check if all required scopes are present
        token_scopes_set = set(token_scopes)
        return all(scope in token_scopes_set for scope in required_scopes)

    def list_oauth_providers(self) -> list[str]:
        """List all OAuth providers.

        Returns:
            List of OAuth provider names
        """
        return list(self._providers.keys())

    def get_mapping(self, oauth_provider: str) -> OAuthProviderMapping | None:
        """Get mapping for an OAuth provider.

        Args:
            oauth_provider: OAuth provider name

        Returns:
            OAuthProviderMapping or None
        """
        return self._providers.get(oauth_provider)
