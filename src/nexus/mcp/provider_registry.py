"""MCP Provider Registry for unified provider configuration.

This module provides a registry for MCP providers, supporting both
Klavis-hosted and local providers with a unified configuration format.

Example:
    >>> from nexus.mcp.provider_registry import MCPProviderRegistry
    >>>
    >>> registry = MCPProviderRegistry.load_default()
    >>>
    >>> # Get provider config
    >>> github = registry.get("github")
    >>> print(github.type)  # ProviderType.KLAVIS
    >>>
    >>> # List all providers
    >>> for name, config in registry.list_providers():
    ...     print(f"{name}: {config.type.value}")
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class ProviderType(Enum):
    """Type of MCP provider."""

    KLAVIS = "klavis"  # Hosted by Klavis
    LOCAL = "local"  # Local OAuth + local/stdio MCP


@dataclass
class OAuthConfig:
    """OAuth configuration for local providers."""

    provider_class: str  # e.g., "nexus.server.auth.google_oauth.GoogleOAuthProvider"
    client_id_env: str  # Environment variable for client ID
    client_secret_env: str  # Environment variable for client secret
    scopes: list[str] = field(default_factory=list)
    requires_pkce: bool = False
    # For generic OAuth providers
    authorization_url: str | None = None
    token_url: str | None = None


@dataclass
class MCPConfig:
    """MCP server configuration for local providers."""

    transport: str = "stdio"  # "stdio" or "sse"
    command: str | None = None  # For stdio
    args: list[str] = field(default_factory=list)
    env_mapping: dict[str, str] = field(
        default_factory=dict
    )  # e.g., {"GITHUB_TOKEN": "{access_token}"}
    url: str | None = None  # For sse/http


@dataclass
class BackendConfig:
    """Backend connector configuration (alternative to MCP)."""

    type: str  # e.g., "gdrive_connector"
    config_template: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderConfig:
    """Configuration for an MCP provider."""

    name: str
    type: ProviderType
    display_name: str
    description: str = ""

    # For Klavis providers
    klavis_name: str | None = None
    default_scopes: list[str] = field(default_factory=list)

    # For local providers
    oauth: OAuthConfig | None = None
    mcp: MCPConfig | None = None
    backend: BackendConfig | None = None

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> ProviderConfig:
        """Create ProviderConfig from dictionary."""
        provider_type = ProviderType(data.get("type", "local"))

        oauth = None
        if "oauth" in data:
            oauth_data = data["oauth"]
            oauth = OAuthConfig(
                provider_class=oauth_data.get("provider_class", ""),
                client_id_env=oauth_data.get("client_id_env", ""),
                client_secret_env=oauth_data.get("client_secret_env", ""),
                scopes=oauth_data.get("scopes", []),
                requires_pkce=oauth_data.get("requires_pkce", False),
                authorization_url=oauth_data.get("authorization_url"),
                token_url=oauth_data.get("token_url"),
            )

        mcp = None
        if "mcp" in data:
            mcp_data = data["mcp"]
            mcp = MCPConfig(
                transport=mcp_data.get("transport", "stdio"),
                command=mcp_data.get("command"),
                args=mcp_data.get("args", []),
                env_mapping=mcp_data.get("env_mapping", {}),
                url=mcp_data.get("url"),
            )

        backend = None
        if "backend" in data:
            backend_data = data["backend"]
            backend = BackendConfig(
                type=backend_data.get("type", ""),
                config_template=backend_data.get("config_template", {}),
            )

        return cls(
            name=name,
            type=provider_type,
            display_name=data.get("display_name", name),
            description=data.get("description", ""),
            klavis_name=data.get("klavis_name"),
            default_scopes=data.get("default_scopes", []),
            oauth=oauth,
            mcp=mcp,
            backend=backend,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        result: dict[str, Any] = {
            "type": self.type.value,
            "display_name": self.display_name,
            "description": self.description,
        }

        if self.klavis_name:
            result["klavis_name"] = self.klavis_name
        if self.default_scopes:
            result["default_scopes"] = self.default_scopes

        if self.oauth:
            result["oauth"] = {
                "provider_class": self.oauth.provider_class,
                "client_id_env": self.oauth.client_id_env,
                "client_secret_env": self.oauth.client_secret_env,
                "scopes": self.oauth.scopes,
                "requires_pkce": self.oauth.requires_pkce,
            }
            if self.oauth.authorization_url:
                result["oauth"]["authorization_url"] = self.oauth.authorization_url
            if self.oauth.token_url:
                result["oauth"]["token_url"] = self.oauth.token_url

        if self.mcp:
            result["mcp"] = {
                "transport": self.mcp.transport,
            }
            if self.mcp.command:
                result["mcp"]["command"] = self.mcp.command
            if self.mcp.args:
                result["mcp"]["args"] = self.mcp.args
            if self.mcp.env_mapping:
                result["mcp"]["env_mapping"] = self.mcp.env_mapping
            if self.mcp.url:
                result["mcp"]["url"] = self.mcp.url

        if self.backend:
            result["backend"] = {
                "type": self.backend.type,
                "config_template": self.backend.config_template,
            }

        return result


class MCPProviderRegistry:
    """Registry for MCP providers.

    Manages provider configurations from YAML config files,
    supporting both Klavis-hosted and local providers.
    """

    def __init__(self, providers: dict[str, ProviderConfig] | None = None):
        """Initialize registry.

        Args:
            providers: Optional dict of provider configs
        """
        self._providers: dict[str, ProviderConfig] = providers or {}

    @classmethod
    def from_yaml(cls, path: str | Path) -> MCPProviderRegistry:
        """Load registry from YAML file.

        Args:
            path: Path to YAML config file

        Returns:
            MCPProviderRegistry instance
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Provider config not found: {path}")

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        providers = {}
        for name, config in data.get("providers", {}).items():
            providers[name] = ProviderConfig.from_dict(name, config)

        return cls(providers=providers)

    @classmethod
    def load_default(cls) -> MCPProviderRegistry:
        """Load default provider registry.

        Searches for mcp-providers.yaml in:
        1. NEXUS_MCP_PROVIDERS_PATH environment variable
        2. /app/configs/mcp-providers.yaml (Docker)
        3. configs/mcp-providers.yaml (relative to package)

        Returns:
            MCPProviderRegistry with default providers
        """
        tried_paths = []

        # Try environment variable
        env_path = os.getenv("NEXUS_MCP_PROVIDERS_PATH")
        if env_path:
            tried_paths.append(env_path)
            if Path(env_path).exists():
                return cls.from_yaml(env_path)

        # Try Docker path
        docker_path = Path("/app/configs/mcp-providers.yaml")
        tried_paths.append(str(docker_path))
        if docker_path.exists():
            return cls.from_yaml(docker_path)

        # Try relative to package
        package_dir = Path(__file__).parent.parent.parent.parent
        config_path = package_dir / "configs" / "mcp-providers.yaml"
        tried_paths.append(str(config_path))
        if config_path.exists():
            return cls.from_yaml(config_path)

        # Return empty registry with built-in defaults
        logger.warning(
            f"No mcp-providers.yaml found, using built-in defaults. Tried: {tried_paths}"
        )
        return cls.with_builtin_defaults()

    @classmethod
    def with_builtin_defaults(cls) -> MCPProviderRegistry:
        """Create registry with built-in default providers.

        Returns:
            MCPProviderRegistry with common providers pre-configured
        """
        providers = {
            # Klavis-hosted providers
            "github": ProviderConfig(
                name="github",
                type=ProviderType.KLAVIS,
                display_name="GitHub",
                description="GitHub repositories, issues, pull requests",
                klavis_name="github",
                default_scopes=["repo", "read:user"],
            ),
            "slack": ProviderConfig(
                name="slack",
                type=ProviderType.KLAVIS,
                display_name="Slack",
                description="Slack messages, channels, users",
                klavis_name="slack",
            ),
            "notion": ProviderConfig(
                name="notion",
                type=ProviderType.KLAVIS,
                display_name="Notion",
                description="Notion pages, databases, blocks",
                klavis_name="notion",
            ),
            "linear": ProviderConfig(
                name="linear",
                type=ProviderType.KLAVIS,
                display_name="Linear",
                description="Linear issues, projects, teams",
                klavis_name="linear",
            ),
            # Connector backends (not MCP yet, but could add MCP support later)
            # These use Nexus OAuth + connector backends for now.
            # To use: 'nexus oauth setup-gdrive' + 'nexus mounts add'
            #
            # "gdrive": ProviderConfig(
            #     name="gdrive",
            #     type=ProviderType.LOCAL,
            #     display_name="Google Drive",
            #     description="Google Drive files and folders",
            #     oauth=OAuthConfig(...),
            #     backend=BackendConfig(type="gdrive_connector", ...),
            #     # TODO: Add mcp=MCPConfig(...) when MCP server available
            # ),
            # "x": ProviderConfig(
            #     name="x",
            #     type=ProviderType.LOCAL,
            #     display_name="X (Twitter)",
            #     description="X timeline, posts, users",
            #     oauth=OAuthConfig(...),
            #     backend=BackendConfig(type="x_connector", ...),
            #     # TODO: Add mcp=MCPConfig(...) when MCP server available
            # ),
        }
        return cls(providers=providers)

    def get(self, name: str) -> ProviderConfig | None:
        """Get provider configuration by name.

        Args:
            name: Provider name

        Returns:
            ProviderConfig or None if not found
        """
        return self._providers.get(name)

    def list_providers(self) -> list[tuple[str, ProviderConfig]]:
        """List all providers.

        Returns:
            List of (name, config) tuples
        """
        return list(self._providers.items())

    def list_klavis_providers(self) -> list[tuple[str, ProviderConfig]]:
        """List Klavis-hosted providers.

        Returns:
            List of (name, config) tuples for Klavis providers
        """
        return [
            (name, config)
            for name, config in self._providers.items()
            if config.type == ProviderType.KLAVIS
        ]

    def list_local_providers(self) -> list[tuple[str, ProviderConfig]]:
        """List local providers.

        Returns:
            List of (name, config) tuples for local providers
        """
        return [
            (name, config)
            for name, config in self._providers.items()
            if config.type == ProviderType.LOCAL
        ]

    def add_provider(self, config: ProviderConfig) -> None:
        """Add a provider to the registry.

        Args:
            config: Provider configuration
        """
        self._providers[config.name] = config

    def remove_provider(self, name: str) -> bool:
        """Remove a provider from the registry.

        Args:
            name: Provider name

        Returns:
            True if removed, False if not found
        """
        if name in self._providers:
            del self._providers[name]
            return True
        return False

    def to_yaml(self, path: str | Path) -> None:
        """Save registry to YAML file.

        Args:
            path: Path to save to
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        data = {"providers": {name: config.to_dict() for name, config in self._providers.items()}}

        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
