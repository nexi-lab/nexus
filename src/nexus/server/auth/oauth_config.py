"""OAuth provider configuration system.

This module provides configuration models and a factory for OAuth providers,
allowing OAuth providers to be configured via YAML config files instead of
hardcoding provider information.
"""

from typing import Any

from pydantic import BaseModel, Field


class OAuthProviderConfig(BaseModel):
    """Configuration for a single OAuth provider.

    This defines how to instantiate and configure an OAuth provider,
    including its class, scopes, and environment variable names
    for credentials.
    """

    name: str = Field(
        description="OAuth provider name/identifier (e.g., 'google', 'microsoft', 'x')"
    )
    display_name: str = Field(
        description="Human-readable display name (e.g., 'Google', 'Microsoft', 'X (Twitter)')"
    )
    provider_class: str = Field(
        description="Fully qualified class path (e.g., 'nexus.server.auth.google_oauth.GoogleOAuthProvider')"
    )
    scopes: list[str] = Field(
        default_factory=list,
        description="OAuth scopes for this provider",
    )
    client_id_env: str = Field(
        description="Environment variable name for OAuth client ID (e.g., 'NEXUS_OAUTH_GOOGLE_CLIENT_ID')"
    )
    client_secret_env: str = Field(
        description="Environment variable name for OAuth client secret (e.g., 'NEXUS_OAUTH_GOOGLE_CLIENT_SECRET')"
    )
    requires_pkce: bool = Field(
        default=False,
        description="Whether this provider requires PKCE (Proof Key for Code Exchange)",
    )
    icon_url: str | None = Field(
        default=None,
        description="URL to provider icon/logo for display in UI",
    )
    redirect_uri: str | None = Field(
        default=None,
        description="Default OAuth redirect URI (can be overridden via RPC parameter)",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional provider-specific metadata",
    )

    model_config = {"frozen": False}


class OAuthConfig(BaseModel):
    """OAuth configuration containing all provider configurations."""

    redirect_uri: str | None = Field(
        default=None,
        description="Global default OAuth redirect URI (used if provider doesn't specify redirect_uri)",
    )
    providers: list[OAuthProviderConfig] = Field(
        default_factory=list,
        description="List of OAuth provider configurations",
    )

    def get_provider_config(self, name: str) -> OAuthProviderConfig | None:
        """Get provider configuration by name.

        Args:
            name: Provider name/identifier

        Returns:
            OAuthProviderConfig if found, None otherwise
        """
        for provider in self.providers:
            if provider.name == name:
                return provider
        return None

    def get_all_provider_names(self) -> list[str]:
        """Get list of all configured provider names.

        Returns:
            List of provider names
        """
        return [provider.name for provider in self.providers]
