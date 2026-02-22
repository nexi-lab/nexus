"""Klavis API client for hosted MCP server integration.

This module provides a client for interacting with Klavis AI's hosted MCP servers,
enabling OAuth flows and MCP server instance management.

Klavis provides 100+ pre-configured OAuth integrations (GitHub, Slack, Notion, etc.)
and hosts MCP servers so you don't need to run them locally.

Example:
    >>> from nexus.mcp.klavis_client import KlavisClient
    >>>
    >>> client = KlavisClient(api_key="your-klavis-api-key")
    >>>
    >>> # Get OAuth URL for GitHub
    >>> auth_url = await client.get_oauth_url(
    ...     provider="github",
    ...     user_id="alice",
    ...     redirect_url="http://localhost:3000/callback"
    ... )
    >>>
    >>> # After OAuth, create MCP server instance
    >>> mcp_url = await client.create_mcp_instance(
    ...     provider="github",
    ...     user_id="alice"
    ... )
    >>> # mcp_url can be used with SSE transport

References:
    - https://www.klavis.ai/docs
    - https://github.com/Klavis-AI/klavis
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class KlavisError(Exception):
    """Error from Klavis API."""

    def __init__(self, message: str, status_code: int | None = None, response: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


@dataclass
class KlavisOAuthResult:
    """Result from Klavis OAuth URL request."""

    authorization_url: str
    state: str | None = None
    provider: str | None = None
    instance_id: str | None = None


@dataclass
class KlavisMCPInstance:
    """Result from creating an MCP server instance."""

    url: str
    instance_id: str | None = None
    provider: str | None = None
    user_id: str | None = None
    transport: str = "sse"
    oauth_url: str | None = None  # OAuth URL if OAuth is required


class KlavisClient:
    """Client for Klavis AI MCP hosting platform.

    Klavis provides:
    - Pre-configured OAuth apps for 100+ services
    - Hosted MCP servers (no local processes needed)
    - Automatic token refresh and management

    Attributes:
        api_key: Klavis API key
        base_url: Klavis API base URL
    """

    DEFAULT_BASE_URL = "https://api.klavis.ai"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        timeout: float = 30.0,
    ):
        """Initialize Klavis client.

        Args:
            api_key: Klavis API key (from https://www.klavis.ai)
            base_url: Optional custom API base URL
            timeout: HTTP request timeout in seconds
        """
        self.api_key = api_key
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout

    def _get_headers(self) -> dict[str, str]:
        """Get HTTP headers for API requests."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def create_mcp_instance(
        self,
        provider: str,
        user_id: str,
        connection_type: str = "SSE",  # "SSE" or "StreamableHttp"
    ) -> KlavisMCPInstance:
        """Create an MCP server instance for a user.

        This is the main entry point - it creates the instance AND returns
        the OAuth URL if OAuth is required for this provider.

        Args:
            provider: Provider name (e.g., "github", "slack")
            user_id: User identifier
            connection_type: Connection type ("sse" or "streamable_http")

        Returns:
            KlavisMCPInstance with URL and optional OAuth URL

        Raises:
            KlavisError: If API request fails
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/mcp-server/instance/create",
                    json={
                        "serverName": provider,  # camelCase per Klavis API
                        "userId": user_id,
                        "connectionType": connection_type,
                    },
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                data = response.json()

                return KlavisMCPInstance(
                    url=data.get("url", data.get("serverUrl", "")),
                    instance_id=data.get("instanceId"),
                    provider=provider,
                    user_id=user_id,
                    transport="sse",
                    oauth_url=data.get("oauthUrl", data.get("authorizationUrl")),
                )

            except httpx.HTTPStatusError as e:
                error_detail = e.response.text
                raise KlavisError(
                    f"Failed to create MCP instance for {provider}: {error_detail}",
                    status_code=e.response.status_code,
                ) from e
            except Exception as e:
                raise KlavisError(f"Failed to create MCP instance for {provider}: {e}") from e

    async def get_oauth_url(
        self,
        provider: str,
        user_id: str,
        _redirect_url: str | None = None,
        _scopes: list[str] | None = None,
    ) -> KlavisOAuthResult:
        """Get OAuth authorization URL from Klavis.

        Note: In Klavis, the OAuth URL is typically returned from create_mcp_instance.
        This method creates an instance first to get the OAuth URL.

        Args:
            provider: Provider name (e.g., "github", "slack", "notion")
            user_id: Unique user identifier for this connection
            redirect_url: Optional redirect URL (Klavis may have its own)
            scopes: Optional OAuth scopes (uses Klavis defaults if not provided)

        Returns:
            KlavisOAuthResult with authorization_url

        Raises:
            KlavisError: If API request fails
        """
        # Create instance first - it will return OAuth URL if needed
        instance = await self.create_mcp_instance(
            provider=provider,
            user_id=user_id,
        )

        if instance.oauth_url:
            return KlavisOAuthResult(
                authorization_url=instance.oauth_url,
                state=None,
                provider=provider,
                instance_id=instance.instance_id,
            )
        else:
            # If no OAuth URL, the instance is ready to use
            # Return a result indicating OAuth is not needed
            return KlavisOAuthResult(
                authorization_url="",  # Empty = no OAuth needed
                state=None,
                provider=provider,
                instance_id=instance.instance_id,
            )

    async def list_available_providers(self) -> list[dict[str, Any]]:
        """List available MCP providers from Klavis.

        Returns:
            List of provider information dicts

        Raises:
            KlavisError: If API request fails
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(
                    f"{self.base_url}/v1/mcp-server/providers",
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                data = response.json()

                providers: list[dict[str, Any]] = data.get("providers", [])
                return providers

            except httpx.HTTPStatusError as e:
                # This endpoint might not exist, return empty list
                logger.warning(f"Failed to list Klavis providers: {e}")
                return []
            except Exception as e:
                logger.warning(f"Failed to list Klavis providers: {e}")
                return []

    async def get_connection_status(
        self,
        provider: str,
        user_id: str,
    ) -> dict[str, Any]:
        """Get connection status for a user's provider.

        Args:
            provider: Provider name
            user_id: User identifier

        Returns:
            Connection status dict

        Raises:
            KlavisError: If API request fails
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(
                    f"{self.base_url}/v1/mcp-server/instance/{provider}/{user_id}/status",
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                result: dict[str, Any] = response.json()
                return result

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return {"connected": False, "provider": provider, "user_id": user_id}
                error_detail = e.response.text
                raise KlavisError(
                    f"Failed to get connection status: {error_detail}",
                    status_code=e.response.status_code,
                ) from e
            except Exception as e:
                raise KlavisError(f"Failed to get connection status: {e}") from e

    async def disconnect(
        self,
        provider: str,
        user_id: str,
    ) -> bool:
        """Disconnect a user from a provider.

        Args:
            provider: Provider name
            user_id: User identifier

        Returns:
            True if disconnected successfully

        Raises:
            KlavisError: If API request fails
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.delete(
                    f"{self.base_url}/v1/mcp-server/instance/{provider}/{user_id}",
                    headers=self._get_headers(),
                )
                response.raise_for_status()
                return True

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return True  # Already disconnected
                error_detail = e.response.text
                raise KlavisError(
                    f"Failed to disconnect: {error_detail}",
                    status_code=e.response.status_code,
                ) from e
            except Exception as e:
                raise KlavisError(f"Failed to disconnect: {e}") from e
