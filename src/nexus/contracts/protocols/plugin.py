"""Plugin registry protocol (ops-scenario-matrix S27: Plugins).

Defines the contract for plugin lifecycle management — discovery,
loading, unloading, and configuration of extension plugins.

References:
    - docs/architecture/ops-scenario-matrix.md  (S27)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.plugins.base import NexusPlugin, PluginMetadata


@runtime_checkable
class PluginRegistryProtocol(Protocol):
    """Service contract for plugin lifecycle management."""

    async def discover(self) -> list[str]: ...

    async def initialize_all(self) -> list[str]: ...

    async def shutdown_all(self) -> None: ...

    def register_plugin(
        self,
        plugin: NexusPlugin,
        name: str | None = None,
    ) -> None: ...

    async def unregister_plugin(self, plugin_name: str) -> None: ...

    async def get_plugin(self, name: str) -> NexusPlugin | None: ...

    def get_plugin_sync(self, name: str) -> NexusPlugin | None: ...

    def list_plugins(self) -> list[PluginMetadata]: ...

    def enable_plugin(self, name: str) -> None: ...

    def disable_plugin(self, name: str) -> None: ...

    def save_plugin_config(
        self,
        plugin_name: str,
        config: dict[str, Any],
    ) -> None: ...
