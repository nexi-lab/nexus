"""Plugin registry and discovery system."""

import importlib.metadata
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from nexus.core.filesystem import NexusFilesystem
from nexus.core.registry import BaseRegistry
from nexus.plugins.base import NexusPlugin, PluginMetadata
from nexus.plugins.hooks import HookType, PluginHooks

logger = logging.getLogger(__name__)


@dataclass
class PluginInfo:
    """Lightweight plugin descriptor for lazy loading.

    Stores entry point metadata without importing the plugin module.
    The actual plugin class is loaded on first access via get_plugin().
    """

    name: str
    entry_point_name: str
    module_path: str
    loaded: bool = False
    metadata: PluginMetadata | None = None
    _entry_point: Any = field(default=None, repr=False)


class PluginRegistry(BaseRegistry[NexusPlugin]):
    """Registry for discovering and managing Nexus plugins.

    Inherits generic register/get/list/clear from ``BaseRegistry`` and adds
    hook management, entry-point discovery, lazy loading, and per-plugin
    configuration.
    """

    def __init__(self, nexus_fs: NexusFilesystem | None = None, config_dir: Path | None = None):
        """Initialize plugin registry.

        Args:
            nexus_fs: NexusFilesystem instance to pass to plugins
            config_dir: Directory for plugin configurations (default: ~/.nexus/plugins)
        """
        super().__init__(name="plugins")
        self._nexus_fs = nexus_fs
        self._plugin_info: dict[str, PluginInfo] = {}
        self._hooks = PluginHooks()
        self._config_dir = config_dir or Path.home() / ".nexus" / "plugins"
        self._config_dir.mkdir(parents=True, exist_ok=True)

    async def discover(self) -> list[str]:
        """Discover plugins using entry points.

        Scans for plugins registered under the 'nexus.plugins' entry point.
        Uses lazy loading: only records entry point metadata without importing
        plugin modules. Actual loading happens on first get_plugin() call.

        Returns:
            List of discovered plugin names
        """
        discovered: list[str] = []

        try:
            entry_points = importlib.metadata.entry_points()

            if hasattr(entry_points, "select"):
                nexus_plugins = entry_points.select(group="nexus.plugins")
            else:
                result = entry_points.get("nexus.plugins")
                nexus_plugins = cast(Any, result if result else [])

            for entry_point in nexus_plugins:
                try:
                    plugin_name = entry_point.name
                    logger.info("Discovering plugin: %s", plugin_name)

                    info = PluginInfo(
                        name=plugin_name,
                        entry_point_name=entry_point.name,
                        module_path=str(entry_point.value),
                        _entry_point=entry_point,
                    )
                    self._plugin_info[plugin_name] = info
                    discovered.append(plugin_name)

                except Exception as e:
                    logger.error("Failed to discover plugin %s: %s", entry_point.name, e)
                    continue

        except Exception as e:
            logger.error("Plugin discovery failed: %s", e)

        return discovered

    async def _load_plugin(self, info: PluginInfo) -> NexusPlugin | None:
        """Load and initialize a plugin from its entry point.

        Args:
            info: Plugin info with entry point reference

        Returns:
            Initialized NexusPlugin instance, or None on failure
        """
        if info._entry_point is None:
            logger.error("No entry point for plugin %s", info.name)
            return None

        try:
            plugin_class = info._entry_point.load()
            plugin = plugin_class(self._nexus_fs)

            config = self._load_plugin_config(info.name)
            await plugin.initialize(config)

            loaded_plugin: NexusPlugin = plugin
            super().register(info.name, loaded_plugin, allow_overwrite=True)
            self._register_plugin_hooks(info.name, loaded_plugin)

            info.loaded = True
            info.metadata = loaded_plugin.metadata()
            logger.info("Loaded plugin: %s v%s", info.name, info.metadata.version)
            return loaded_plugin

        except Exception as e:
            logger.error("Failed to load plugin %s: %s", info.name, e)
            return None

    async def initialize_all(self) -> list[str]:
        """Load and initialize all discovered plugins.

        Returns:
            List of successfully initialized plugin names
        """
        initialized: list[str] = []
        for name, info in self._plugin_info.items():
            if not info.loaded:
                plugin = await self._load_plugin(info)
                if plugin is not None:
                    initialized.append(name)
        return initialized

    async def shutdown_all(self) -> None:
        """Shutdown all loaded plugins gracefully."""
        for name in list(self.list_names()):
            plugin = self.get(name)
            if plugin is None:
                continue
            try:
                await plugin.shutdown()
                logger.info("Shut down plugin: %s", name)
            except Exception:
                logger.exception("Failed to shut down plugin %s", name)

    def register_plugin(self, plugin: NexusPlugin, name: str | None = None) -> None:
        """Manually register a plugin (synchronous, for testing).

        Args:
            plugin: Plugin instance to register
            name: Plugin name (defaults to plugin.metadata().name)
        """
        plugin_name = name or plugin.metadata().name
        super().register(plugin_name, plugin, allow_overwrite=True)
        self._register_plugin_hooks(plugin_name, plugin)
        logger.info("Registered plugin: %s", plugin_name)

    async def unregister_plugin(self, plugin_name: str) -> None:
        """Unregister a plugin, calling its shutdown hook.

        Args:
            plugin_name: Name of plugin to unregister
        """
        plugin = self.get(plugin_name)
        if plugin is None:
            return

        # Unregister hooks
        for hook_type in HookType:
            for hook_name, handler in plugin.hooks().items():
                if hook_name == hook_type.value:
                    self._hooks.unregister(hook_type, handler)

        # Shutdown plugin
        try:
            await plugin.shutdown()
        except Exception:
            logger.exception("Error during shutdown of plugin %s", plugin_name)

        super().unregister(plugin_name)
        self._plugin_info.pop(plugin_name, None)
        logger.info("Unregistered plugin: %s", plugin_name)

    async def get_plugin(self, name: str) -> NexusPlugin | None:
        """Get a registered plugin by name, loading it lazily if needed.

        Args:
            name: Plugin name

        Returns:
            Plugin instance or None if not found
        """
        # Already loaded (via BaseRegistry)
        plugin = self.get(name)
        if plugin is not None:
            return plugin

        # Lazy load from discovered info
        info = self._plugin_info.get(name)
        if info and not info.loaded:
            return await self._load_plugin(info)

        return None

    def get_plugin_sync(self, name: str) -> NexusPlugin | None:
        """Get a loaded plugin synchronously (no lazy loading).

        Args:
            name: Plugin name

        Returns:
            Plugin instance or None if not loaded
        """
        return self.get(name)

    def list_plugins(self) -> list[PluginMetadata]:
        """List all registered plugins.

        Returns metadata for loaded plugins and basic info for discovered-but-unloaded plugins.

        Returns:
            List of plugin metadata
        """
        result: list[PluginMetadata] = []

        # Loaded plugins — full metadata
        for plugin in self.list_all():
            result.append(plugin.metadata())

        # Discovered but unloaded — basic info from PluginInfo
        for name, info in self._plugin_info.items():
            if not info.loaded and self.get(name) is None:
                if info.metadata:
                    result.append(info.metadata)
                else:
                    result.append(PluginMetadata(
                        name=info.name,
                        version="(not loaded)",
                        description=f"Discovered from {info.module_path}",
                        author="",
                    ))

        return result

    def enable_plugin(self, name: str) -> None:
        """Enable a plugin.

        Args:
            name: Plugin name
        """
        plugin = self.get(name)
        if plugin:
            plugin.enable()
            logger.info("Enabled plugin: %s", name)

    def disable_plugin(self, name: str) -> None:
        """Disable a plugin.

        Args:
            name: Plugin name
        """
        plugin = self.get(name)
        if plugin:
            plugin.disable()
            logger.info("Disabled plugin: %s", name)

    def get_hooks(self) -> PluginHooks:
        """Get the hooks registry.

        Returns:
            PluginHooks instance
        """
        return self._hooks

    async def execute_hook(
        self, hook_type: HookType, context: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Execute a hook with context.

        Args:
            hook_type: Type of hook to execute
            context: Context data

        Returns:
            Modified context or None if execution should stop
        """
        return await self._hooks.execute(hook_type, context)

    def _register_plugin_hooks(self, plugin_name: str, plugin: NexusPlugin) -> None:
        """Register all hooks from a plugin.

        Args:
            plugin_name: Name of the plugin
            plugin: Plugin instance
        """
        for hook_name, handler in plugin.hooks().items():
            try:
                hook_type = HookType(hook_name)
                priority = plugin.get_config(f"hook_priority.{hook_name}", 0)
                self._hooks.register(hook_type, handler, priority)
                logger.debug("Registered hook %s for plugin %s", hook_name, plugin_name)
            except ValueError:
                logger.warning("Unknown hook type %s from plugin %s", hook_name, plugin_name)

    def _load_plugin_config(self, plugin_name: str) -> dict[str, Any]:
        """Load configuration for a plugin.

        Args:
            plugin_name: Name of the plugin

        Returns:
            Configuration dictionary
        """
        config_file = self._config_dir / plugin_name / "config.yaml"

        if not config_file.exists():
            return {}

        try:
            with open(config_file) as f:
                config = yaml.safe_load(f) or {}
                logger.debug("Loaded config for %s: %s", plugin_name, config_file)
                return config
        except Exception as e:
            logger.error("Failed to load config for %s: %s", plugin_name, e)
            return {}

    def save_plugin_config(self, plugin_name: str, config: dict[str, Any]) -> None:
        """Save configuration for a plugin.

        Args:
            plugin_name: Name of the plugin
            config: Configuration dictionary
        """
        plugin_dir = self._config_dir / plugin_name
        plugin_dir.mkdir(parents=True, exist_ok=True)

        config_file = plugin_dir / "config.yaml"

        try:
            with open(config_file, "w") as f:
                yaml.dump(config, f, default_flow_style=False)
                logger.info("Saved config for %s: %s", plugin_name, config_file)
        except Exception as e:
            logger.error("Failed to save config for %s: %s", plugin_name, e)
