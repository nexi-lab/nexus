"""Unified MCP Connection Manager.

This module provides a unified interface for connecting to MCP providers,
whether they are Klavis-hosted or local (with your own OAuth apps).

The same command works for both:
    - nexus mcp connect github --user alice        # Klavis
    - nexus mcp connect gdrive --user alice@gmail  # Local

Example:
    >>> from nexus.bricks.mcp import MCPConnectionManager
    >>>
    >>> manager = MCPConnectionManager(filesystem=nx)
    >>> await manager.connect("github", user_id="alice")
    >>>
    >>> # List connections
    >>> connections = await manager.list_connections()
"""

import json
import logging
import os
import webbrowser
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.bricks.approvals.policy_gate import PolicyGate
    from nexus.config import SSRFConfig
    from nexus.core.nexus_fs import NexusFS

from nexus.bricks.mcp.klavis_client import KlavisClient, KlavisError
from nexus.bricks.mcp.models import MCPMount
from nexus.bricks.mcp.mount import MCPMountManager
from nexus.bricks.mcp.provider_registry import MCPProviderRegistry, ProviderConfig, ProviderType

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
    def from_dict(cls, data: dict[str, Any]) -> "MCPConnection":
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
        filesystem: "NexusFS | None" = None,
        registry: MCPProviderRegistry | None = None,
        klavis_api_key: str | None = None,
        *,
        ssrf_config: "SSRFConfig | None" = None,
        policy_gate: "PolicyGate | None" = None,
        zone_id: str | None = None,
    ):
        """Initialize connection manager.

        Args:
            filesystem: Nexus filesystem instance
            registry: Provider registry (loads default if not provided)
            klavis_api_key: Klavis API key (from env KLAVIS_API_KEY if not provided)
            ssrf_config: Optional SSRFConfig override plumbed through to the
                underlying MCPMountManager. When None, a conservative default
                is used for SSE/HTTP URL validation (Issue #3792).
            policy_gate: Optional PolicyGate forwarded to the underlying
                MCPMountManager so SSRF-blocked egress can route through the
                approval queue (Issue #3790, Task 18). Often unavailable at
                construction time (the gate is built later in the FastAPI
                lifespan), so callers may attach it post-hoc via
                :meth:`set_policy_gate`. ``None`` preserves fail-closed.
            zone_id: Optional daemon zone forwarded to the underlying
                MCPMountManager so approval requests are scoped to the
                daemon's primary zone instead of ROOT_ZONE_ID (Issue
                #3790, F4). May be set later via :meth:`set_zone`. When
                ``None`` the gate fails closed at egress time.
        """
        self.filesystem = filesystem
        self.registry = registry or MCPProviderRegistry.load_default()

        # Get Klavis API key from env if not provided
        klavis_key = klavis_api_key or os.getenv("KLAVIS_API_KEY")
        self.klavis = KlavisClient(klavis_key) if klavis_key else None

        # Create mount manager for tool discovery/storage
        self._policy_gate = policy_gate
        self._zone_id = zone_id
        self.mount_manager = MCPMountManager(
            filesystem,
            ssrf_config=ssrf_config,
            policy_gate=policy_gate,
            zone_id=zone_id,
        )

        # Cache of active connections
        self._connections: dict[str, MCPConnection] = {}

        # Deferred loading flag -- _load_connections is async and cannot be
        # called from __init__.  The first async method that needs the cache
        # will call _ensure_connections_loaded().
        self._connections_loaded = False

    def set_policy_gate(self, gate: "PolicyGate | None") -> None:
        """Attach (or detach) the PolicyGate after construction.

        The gate is typically wired by the FastAPI approvals lifespan after
        this manager is already constructed, so callers need a way to inject
        it post-hoc. Updates both this manager and its embedded
        ``MCPMountManager`` so subsequent egress attempts route through the
        approval queue (Issue #3790).
        """
        self._policy_gate = gate
        # MCPMountManager exposes ``_policy_gate`` as the same private slot
        # consulted by ``_ssrf_blocked_via_gate``; updating it in place keeps
        # the existing instance valid (no reconstruction needed).
        self.mount_manager._policy_gate = gate

    def set_zone(self, zone_id: str | None) -> None:
        """Attach (or detach) the daemon zone after construction.

        F4 (Issue #3790): forwarded to the embedded ``MCPMountManager``
        so SSRF-blocked egress requests are scoped to the daemon's
        primary zone instead of ROOT_ZONE_ID. ``None`` triggers
        fail-closed routing at the gate hook.
        """
        self._zone_id = zone_id
        self.mount_manager.set_zone(zone_id)

    async def _ensure_connections_loaded(self) -> None:
        """Lazily load connections on first async access."""
        if not self._connections_loaded:
            await self._load_connections()
            self._connections_loaded = True

    async def _load_connections(self) -> None:
        """Load existing connections from storage."""
        try:
            if self.filesystem and self.filesystem.access(self.CONNECTIONS_PATH):
                items = self.filesystem.sys_readdir(self.CONNECTIONS_PATH)
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
                            raw = self.filesystem.sys_read(path)
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

    async def _save_connection(self, conn: "MCPConnection") -> None:
        """Save a connection to storage."""
        try:
            if self.filesystem:
                # Ensure directory exists
                try:
                    self.filesystem.mkdir(self.CONNECTIONS_PATH, parents=True)
                except FileExistsError:
                    pass
                except OSError as e:
                    logger.warning("Failed to create directory %s: %s", self.CONNECTIONS_PATH, e)

                # Use provider_user as filename
                filename = f"{conn.provider}_{conn.user_id.replace('@', '_at_')}.json"
                path = f"{self.CONNECTIONS_PATH}{filename}"
                content = json.dumps(conn.to_dict(), indent=2)
                self.filesystem.write(path, content.encode("utf-8"))

        except Exception as e:
            logger.error(f"Failed to save connection: {e}")

    async def _delete_connection(self, provider: str, user_id: str) -> None:
        """Delete a connection from storage."""
        try:
            if self.filesystem:
                filename = f"{provider}_{user_id.replace('@', '_at_')}.json"
                path = f"{self.CONNECTIONS_PATH}{filename}"
                if self.filesystem.access(path):
                    self.filesystem.sys_unlink(path)
        except Exception as e:
            logger.warning(f"Failed to delete connection file: {e}")

    async def connect(
        self,
        provider: str,
        user_id: str,
        scopes: list[str] | None = None,
        callback_port: int = 3000,
        open_browser: bool = True,
    ) -> "MCPConnection":
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
        await self._ensure_connections_loaded()

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
    ) -> "MCPConnection":
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
            await self._save_connection(connection)

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
    ) -> "MCPConnection":
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
        await self._save_connection(connection)

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
        await self._ensure_connections_loaded()

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
        await self._delete_connection(provider, user_id)

        logger.info(f"Disconnected from {provider}")
        return True

    async def list_connections(self, user_id: str | None = None) -> "list[MCPConnection]":
        """List all connections.

        Args:
            user_id: Optional filter by user

        Returns:
            List of connections
        """
        await self._ensure_connections_loaded()

        connections = list(self._connections.values())

        if user_id:
            connections = [c for c in connections if c.user_id == user_id]

        return connections

    async def get_connection(self, provider: str, user_id: str) -> "MCPConnection | None":
        """Get a specific connection.

        Args:
            provider: Provider name
            user_id: User identifier

        Returns:
            MCPConnection or None
        """
        await self._ensure_connections_loaded()

        key = f"{provider}:{user_id}"
        return self._connections.get(key)

    def list_available_providers(self) -> list[tuple[str, ProviderConfig]]:
        """List all available providers.

        Returns:
            List of (name, config) tuples
        """
        return self.registry.list_providers()
