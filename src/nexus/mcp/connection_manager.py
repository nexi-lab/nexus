"""Unified MCP Connection Manager.

This module provides a unified interface for connecting to MCP providers,
whether they are Klavis-hosted or local (with your own OAuth apps).

The same command works for both:
    - nexus mcp connect github --user alice        # Klavis
    - nexus mcp connect gdrive --user alice@gmail  # Local

Example:
    >>> from nexus.mcp import MCPConnectionManager
    >>>
    >>> manager = MCPConnectionManager(filesystem=nx)
    >>> await manager.connect("github", user_id="alice")
    >>>
    >>> # List connections
    >>> connections = await manager.list_connections()
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import webbrowser
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.mcp.klavis_client import KlavisClient, KlavisError
from nexus.mcp.provider_registry import MCPProviderRegistry, ProviderConfig, ProviderType
from nexus.skills.mcp_models import MCPMount
from nexus.skills.mcp_mount import MCPMountManager

if TYPE_CHECKING:
    from nexus.skills.protocols import NexusFilesystem

logger = logging.getLogger(__name__)


class MCPConnectionError(Exception):
    """Error during MCP connection."""

    pass


@dataclass
class MCPConnection:
    """Represents a connection to an MCP provider."""

    provider: str
    user_id: str
    provider_type: ProviderType
    connected_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    # For Klavis providers
    mcp_url: str | None = None
    klavis_instance_id: str | None = None

    # For local providers
    oauth_credential_id: str | None = None
    backend_type: str | None = None
    backend_config: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "provider": self.provider,
            "user_id": self.user_id,
            "provider_type": self.provider_type.value,
            "connected_at": self.connected_at.isoformat(),
            "mcp_url": self.mcp_url,
            "klavis_instance_id": self.klavis_instance_id,
            "oauth_credential_id": self.oauth_credential_id,
            "backend_type": self.backend_type,
            "backend_config": self.backend_config,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MCPConnection:
        """Create from dictionary."""
        return cls(
            provider=data["provider"],
            user_id=data["user_id"],
            provider_type=ProviderType(data["provider_type"]),
            connected_at=datetime.fromisoformat(data["connected_at"]),
            mcp_url=data.get("mcp_url"),
            klavis_instance_id=data.get("klavis_instance_id"),
            oauth_credential_id=data.get("oauth_credential_id"),
            backend_type=data.get("backend_type"),
            backend_config=data.get("backend_config"),
        )


class MCPConnectionManager:
    """Unified manager for MCP connections (Klavis + local).

    This class provides a single interface for connecting to any MCP provider,
    regardless of whether it's hosted by Klavis or requires local OAuth.

    Attributes:
        filesystem: Nexus filesystem for storing connection info
        registry: Provider configuration registry
        klavis: Klavis client for hosted providers
        mount_manager: MCP mount manager for tool discovery
    """

    # Path for storing connection info
    CONNECTIONS_PATH = "/skills/system/mcp-connections/"

    def __init__(
        self,
        filesystem: NexusFilesystem | None = None,
        registry: MCPProviderRegistry | None = None,
        klavis_api_key: str | None = None,
    ):
        """Initialize connection manager.

        Args:
            filesystem: Nexus filesystem instance
            registry: Provider registry (loads default if not provided)
            klavis_api_key: Klavis API key (from env KLAVIS_API_KEY if not provided)
        """
        self.filesystem = filesystem
        self.registry = registry or MCPProviderRegistry.load_default()

        # Get Klavis API key from env if not provided
        klavis_key = klavis_api_key or os.getenv("KLAVIS_API_KEY")
        self.klavis = KlavisClient(klavis_key) if klavis_key else None

        # Create mount manager for tool discovery/storage
        self.mount_manager = MCPMountManager(filesystem)

        # Cache of active connections
        self._connections: dict[str, MCPConnection] = {}

        # Load existing connections
        self._load_connections()

    def _load_connections(self) -> None:
        """Load existing connections from storage."""
        try:
            if self.filesystem and self.filesystem.exists(self.CONNECTIONS_PATH):
                items = self.filesystem.list(self.CONNECTIONS_PATH)
                for item in items:
                    # Item might be full path, just filename, or dict
                    if isinstance(item, dict):
                        item_str = str(item.get("name", item.get("path", "")))
                    else:
                        item_str = str(item)
                    item_name = item_str.split("/")[-1] if "/" in item_str else item_str
                    if item_name.endswith(".json"):
                        path = f"{self.CONNECTIONS_PATH}{item_name}"
                        try:
                            raw = self.filesystem.read(path)
                            data = json.loads(
                                raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                            )
                            conn = MCPConnection.from_dict(data)
                            key = f"{conn.provider}:{conn.user_id}"
                            self._connections[key] = conn
                        except Exception as e:
                            logger.warning(f"Failed to load connection from {path}: {e}")
        except Exception as e:
            logger.warning(f"Failed to load connections: {e}")

    def _save_connection(self, conn: MCPConnection) -> None:
        """Save a connection to storage."""
        try:
            if self.filesystem:
                # Ensure directory exists
                with contextlib.suppress(Exception):
                    self.filesystem.mkdir(self.CONNECTIONS_PATH, parents=True)

                # Use provider_user as filename
                filename = f"{conn.provider}_{conn.user_id.replace('@', '_at_')}.json"
                path = f"{self.CONNECTIONS_PATH}{filename}"
                content = json.dumps(conn.to_dict(), indent=2)
                self.filesystem.write(path, content.encode("utf-8"))

        except Exception as e:
            logger.error(f"Failed to save connection: {e}")

    def _delete_connection(self, provider: str, user_id: str) -> None:
        """Delete a connection from storage."""
        try:
            if self.filesystem:
                filename = f"{provider}_{user_id.replace('@', '_at_')}.json"
                path = f"{self.CONNECTIONS_PATH}{filename}"
                if self.filesystem.exists(path):
                    self.filesystem.delete(path)
        except Exception as e:
            logger.warning(f"Failed to delete connection file: {e}")

    async def connect(
        self,
        provider: str,
        user_id: str,
        scopes: list[str] | None = None,
        callback_port: int = 3000,
        open_browser: bool = True,
    ) -> MCPConnection:
        """Connect to an MCP provider.

        This is the unified entry point - it automatically handles
        Klavis-hosted or local OAuth based on provider configuration.

        Args:
            provider: Provider name (e.g., "github", "gdrive")
            user_id: User identifier for this connection
            scopes: Optional OAuth scopes (uses defaults if not provided)
            callback_port: Port for local OAuth callback server
            open_browser: Whether to open browser for OAuth

        Returns:
            MCPConnection with connection details

        Raises:
            MCPConnectionError: If connection fails
        """
        config = self.registry.get(provider)
        if not config:
            available = [name for name, _ in self.registry.list_providers()]
            raise MCPConnectionError(
                f"Unknown provider: {provider}. Available: {', '.join(available)}"
            )

        if config.type == ProviderType.KLAVIS:
            return await self._connect_klavis(config, user_id, scopes, callback_port, open_browser)
        else:
            return await self._connect_local(config, user_id, scopes, callback_port, open_browser)

    async def _connect_klavis(
        self,
        config: ProviderConfig,
        user_id: str,
        _scopes: list[str] | None,
        callback_port: int,
        open_browser: bool,
    ) -> MCPConnection:
        """Connect via Klavis (hosted OAuth + hosted MCP)."""
        if not self.klavis:
            raise MCPConnectionError(
                "Klavis API key not configured. Set KLAVIS_API_KEY environment variable."
            )

        klavis_name = config.klavis_name or config.name

        try:
            # 1. Create MCP instance - this returns both the MCP URL and OAuth URL if needed
            logger.info(f"Creating MCP instance for {config.name}...")
            mcp_instance = await self.klavis.create_mcp_instance(
                provider=klavis_name,
                user_id=user_id,
                connection_type="StreamableHttp",
            )

            # 2. If OAuth is required, do the OAuth flow
            if mcp_instance.oauth_url:
                logger.info(f"OAuth required for {config.name}")

                if open_browser:
                    logger.info(f"Opening browser for {config.name} authorization...")
                    webbrowser.open(mcp_instance.oauth_url)

                    # Wait for OAuth callback
                    logger.info("Waiting for OAuth callback...")
                    await self._wait_for_oauth_callback(callback_port)
                else:
                    # Don't wait for callback when browser is not opened
                    # User will manually complete OAuth
                    logger.info(f"OAuth URL (complete manually): {mcp_instance.oauth_url}")
                    logger.info("Skipping callback wait (--no-browser mode)")
            else:
                logger.info(f"No OAuth required for {config.name}, instance ready")

            # 3. Mount the MCP server in Nexus
            logger.info(f"Mounting MCP server: {mcp_instance.url}")
            mount = MCPMount(
                name=config.name,
                description=config.description or f"{config.display_name} via Klavis",
                transport="klavis_rest",
                url=mcp_instance.url,
            )
            await self.mount_manager.mount(mount)

            # 4. Create and store connection
            connection = MCPConnection(
                provider=config.name,
                user_id=user_id,
                provider_type=ProviderType.KLAVIS,
                mcp_url=mcp_instance.url,
                klavis_instance_id=mcp_instance.instance_id,
            )

            key = f"{config.name}:{user_id}"
            self._connections[key] = connection
            self._save_connection(connection)

            logger.info(f"Connected to {config.name} via Klavis")
            return connection

        except KlavisError as e:
            raise MCPConnectionError(f"Klavis error: {e}") from e
        except Exception as e:
            raise MCPConnectionError(f"Failed to connect to {config.name}: {e}") from e

    async def _connect_local(
        self,
        config: ProviderConfig,
        user_id: str,
        _scopes: list[str] | None,
        _callback_port: int,
        _open_browser: bool,
    ) -> MCPConnection:
        """Connect via local OAuth + local/stdio MCP."""
        if not config.oauth:
            raise MCPConnectionError(f"Provider {config.name} has no OAuth configuration")

        # For now, just create the mount config without full OAuth flow
        # The full OAuth flow would require the TokenManager integration

        logger.warning(
            f"Local OAuth flow for {config.name} requires manual setup. "
            f"Use 'nexus oauth setup-{config.name}' to configure credentials first."
        )

        # Create connection record
        connection = MCPConnection(
            provider=config.name,
            user_id=user_id,
            provider_type=ProviderType.LOCAL,
            backend_type=config.backend.type if config.backend else None,
            backend_config=config.backend.config_template if config.backend else None,
        )

        key = f"{config.name}:{user_id}"
        self._connections[key] = connection
        self._save_connection(connection)

        return connection

    async def _wait_for_oauth_callback(self, port: int, timeout: int = 300) -> dict[str, Any]:
        """Run local HTTP server to receive OAuth callback.

        Args:
            port: Port to listen on
            timeout: Timeout in seconds

        Returns:
            Callback parameters (code, state, etc.)
        """
        import asyncio

        try:
            from aiohttp import web
        except ImportError:
            # Fallback: just wait and assume success
            logger.warning("aiohttp not installed, skipping callback server")
            await asyncio.sleep(5)
            return {"success": True}

        result: dict[str, Any] = {}
        event = asyncio.Event()

        async def handle_callback(request: web.Request) -> web.Response:
            result["code"] = request.query.get("code")
            result["state"] = request.query.get("state")
            result["error"] = request.query.get("error")
            event.set()

            return web.Response(
                text="""
                <html>
                <head><title>Authorization Successful</title></head>
                <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                    <h1>✓ Authorization Successful</h1>
                    <p>You can close this window and return to the terminal.</p>
                </body>
                </html>
                """,
                content_type="text/html",
            )

        app = web.Application()
        app.router.add_get("/oauth/callback", handle_callback)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "localhost", port)

        try:
            await site.start()
            logger.debug(f"OAuth callback server listening on port {port}")

            # Wait for callback or timeout
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            except TimeoutError as e:
                raise MCPConnectionError("OAuth callback timeout") from e

        finally:
            await runner.cleanup()

        if result.get("error"):
            raise MCPConnectionError(f"OAuth error: {result['error']}")

        return result

    async def disconnect(self, provider: str, user_id: str) -> bool:
        """Disconnect from a provider.

        Args:
            provider: Provider name
            user_id: User identifier

        Returns:
            True if disconnected
        """
        key = f"{provider}:{user_id}"
        connection = self._connections.get(key)

        if not connection:
            return False

        # Unmount MCP server
        try:
            await self.mount_manager.unmount(provider)
        except Exception as e:
            logger.warning(f"Failed to unmount {provider}: {e}")

        # Disconnect from Klavis if applicable
        if connection.provider_type == ProviderType.KLAVIS and self.klavis:
            try:
                config = self.registry.get(provider)
                klavis_name = config.klavis_name if config and config.klavis_name else provider
                await self.klavis.disconnect(klavis_name, user_id)
            except Exception as e:
                logger.warning(f"Failed to disconnect from Klavis: {e}")

        # Remove from storage
        del self._connections[key]
        self._delete_connection(provider, user_id)

        logger.info(f"Disconnected from {provider}")
        return True

    def list_connections(self, user_id: str | None = None) -> list[MCPConnection]:
        """List all connections.

        Args:
            user_id: Optional filter by user

        Returns:
            List of connections
        """
        connections = list(self._connections.values())

        if user_id:
            connections = [c for c in connections if c.user_id == user_id]

        return connections

    def get_connection(self, provider: str, user_id: str) -> MCPConnection | None:
        """Get a specific connection.

        Args:
            provider: Provider name
            user_id: User identifier

        Returns:
            MCPConnection or None
        """
        key = f"{provider}:{user_id}"
        return self._connections.get(key)

    def list_available_providers(self) -> list[tuple[str, ProviderConfig]]:
        """List all available providers.

        Returns:
            List of (name, config) tuples
        """
        return self.registry.list_providers()
