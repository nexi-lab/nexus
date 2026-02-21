"""Plugin service protocol (ops-scenario-matrix S27: Plugins).

Defines the contract for plugin lifecycle management — discovery,
installation, configuration, hooks, and teardown.

Storage Affinity: **CacheStore** (plugin registry, hook registrations)
                  + **ObjectStore** (plugin config YAML files).

References:
    - docs/architecture/ops-scenario-matrix.md  (S27)
    - docs/architecture/data-storage-matrix.md  (Four Pillars)
    - Issue #1287: Extract NexusFS domain services from god object
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PluginProtocol(Protocol):
    """Service contract for plugin lifecycle and hook management.

    Captures the public API of ``plugins/registry.PluginRegistry``.
    """

    # ── Discovery & Lifecycle ─────────────────────────────────────────

    async def discover(self) -> list[str]:
        """Discover available plugins via entry points.

        Returns:
            List of discovered plugin names.
        """
        ...

    async def initialize_all(self) -> list[str]:
        """Load and initialize all discovered plugins.

        Returns:
            List of successfully initialized plugin names.
        """
        ...

    async def shutdown_all(self) -> None:
        """Shutdown all loaded plugins gracefully."""
        ...

    # ── Registration ──────────────────────────────────────────────────

    def register_plugin(self, plugin: Any, name: str | None = None) -> None:
        """Manually register a plugin instance.

        Args:
            plugin: NexusPlugin instance.
            name: Optional override name (defaults to plugin metadata name).
        """
        ...

    async def unregister_plugin(self, plugin_name: str) -> None:
        """Unregister a plugin, calling its shutdown hook.

        Args:
            plugin_name: Name of plugin to unregister.
        """
        ...

    # ── Query ─────────────────────────────────────────────────────────

    async def get_plugin(self, name: str) -> Any | None:
        """Get a plugin by name, lazily loading if needed.

        Returns:
            NexusPlugin instance or None.
        """
        ...

    def list_plugins(self) -> list[Any]:
        """List metadata for all registered/discovered plugins.

        Returns:
            List of PluginMetadata records.
        """
        ...

    # ── Enable / Disable ──────────────────────────────────────────────

    def enable_plugin(self, name: str) -> None:
        """Enable a plugin by name."""
        ...

    def disable_plugin(self, name: str) -> None:
        """Disable a plugin by name."""
        ...

    # ── Hooks ─────────────────────────────────────────────────────────

    async def execute_hook(
        self,
        hook_type: Any,
        context: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Execute a hook with context.

        Args:
            hook_type: HookType enum value.
            context: Context data passed through the hook chain.

        Returns:
            Modified context or None if execution should stop.
        """
        ...

    # ── Configuration ─────────────────────────────────────────────────

    def save_plugin_config(self, plugin_name: str, config: dict[str, Any]) -> None:
        """Persist configuration for a plugin.

        Args:
            plugin_name: Name of the plugin.
            config: Configuration dictionary to save.
        """
        ...
