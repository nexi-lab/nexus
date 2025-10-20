"""Plugin registry and discovery system."""

import importlib.metadata
import logging
from pathlib import Path
from typing import Any, cast

import yaml

from nexus.core.nexus_fs import NexusFS
from nexus.plugins.base import NexusPlugin, PluginMetadata
from nexus.plugins.hooks import HookType, PluginHooks

logger = logging.getLogger(__name__)


class PluginRegistry:
    """Registry for discovering and managing Nexus plugins."""

    def __init__(self, nexus_fs: NexusFS | None = None, config_dir: Path | None = None):
        """Initialize plugin registry.

        Args:
            nexus_fs: NexusFS instance to pass to plugins
            config_dir: Directory for plugin configurations (default: ~/.nexus/plugins)
        """
        self._nexus_fs = nexus_fs
        self._plugins: dict[str, NexusPlugin] = {}
        self._hooks = PluginHooks()
        self._config_dir = config_dir or Path.home() / ".nexus" / "plugins"
        self._config_dir.mkdir(parents=True, exist_ok=True)

    def discover(self) -> list[str]:
        """Discover plugins using entry points.

        Looks for plugins registered under the 'nexus.plugins' entry point.

        Returns:
            List of discovered plugin names
        """
        discovered = []

        try:
            # Use importlib.metadata to find entry points
            entry_points = importlib.metadata.entry_points()

            # Handle different versions of importlib.metadata
            if hasattr(entry_points, "select"):
                # Python 3.10+
                nexus_plugins = entry_points.select(group="nexus.plugins")
            else:
                # Python 3.9
                result = entry_points.get("nexus.plugins")
                nexus_plugins = cast(Any, result if result else [])

            for entry_point in nexus_plugins:
                try:
                    plugin_name = entry_point.name
                    logger.info(f"Discovering plugin: {plugin_name}")

                    # Load the plugin class
                    plugin_class = entry_point.load()

                    # Instantiate the plugin
                    plugin = plugin_class(self._nexus_fs)

                    # Load plugin configuration
                    config = self._load_plugin_config(plugin_name)
                    # Note: Plugin initialization is async, but we can't await in sync method
                    # Plugins should handle initialization in their commands if needed
                    # For now, just store the config
                    plugin._config = config

                    # Register the plugin
                    self._plugins[plugin_name] = plugin

                    # Register hooks
                    self._register_plugin_hooks(plugin_name, plugin)

                    discovered.append(plugin_name)
                    logger.info(f"Loaded plugin: {plugin_name} v{plugin.metadata().version}")

                except Exception as e:
                    logger.error(f"Failed to load plugin {entry_point.name}: {e}")
                    continue

        except Exception as e:
            logger.error(f"Plugin discovery failed: {e}")

        return discovered

    def register(self, plugin: NexusPlugin, name: str | None = None) -> None:
        """Manually register a plugin.

        Args:
            plugin: Plugin instance to register
            name: Plugin name (defaults to plugin.metadata().name)
        """
        plugin_name = name or plugin.metadata().name

        # Note: Configuration should be loaded during discovery
        # We can't await here in sync method, so assume plugin is already initialized

        self._plugins[plugin_name] = plugin
        self._register_plugin_hooks(plugin_name, plugin)

        logger.info(f"Registered plugin: {plugin_name}")

    def unregister(self, plugin_name: str) -> None:
        """Unregister a plugin.

        Args:
            plugin_name: Name of plugin to unregister
        """
        if plugin_name not in self._plugins:
            return

        plugin = self._plugins[plugin_name]

        # Unregister hooks
        for hook_type in HookType:
            for hook_name, handler in plugin.hooks().items():
                if hook_name == hook_type.value:
                    self._hooks.unregister(hook_type, handler)

        # Shutdown plugin
        # Note: Can't await in sync method
        # await plugin.shutdown()

        del self._plugins[plugin_name]
        logger.info(f"Unregistered plugin: {plugin_name}")

    def get_plugin(self, name: str) -> NexusPlugin | None:
        """Get a registered plugin by name.

        Args:
            name: Plugin name

        Returns:
            Plugin instance or None if not found
        """
        return self._plugins.get(name)

    def list_plugins(self) -> list[PluginMetadata]:
        """List all registered plugins.

        Returns:
            List of plugin metadata
        """
        return [plugin.metadata() for plugin in self._plugins.values()]

    def enable_plugin(self, name: str) -> None:
        """Enable a plugin.

        Args:
            name: Plugin name
        """
        plugin = self.get_plugin(name)
        if plugin:
            plugin.enable()
            logger.info(f"Enabled plugin: {name}")

    def disable_plugin(self, name: str) -> None:
        """Disable a plugin.

        Args:
            name: Plugin name
        """
        plugin = self.get_plugin(name)
        if plugin:
            plugin.disable()
            logger.info(f"Disabled plugin: {name}")

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
                # Default priority is 0, can be configured per plugin
                priority = plugin.get_config(f"hook_priority.{hook_name}", 0)
                self._hooks.register(hook_type, handler, priority)
                logger.debug(f"Registered hook {hook_name} for plugin {plugin_name}")
            except ValueError:
                logger.warning(f"Unknown hook type {hook_name} from plugin {plugin_name}")

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
                logger.debug(f"Loaded config for {plugin_name}: {config_file}")
                return config
        except Exception as e:
            logger.error(f"Failed to load config for {plugin_name}: {e}")
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
                logger.info(f"Saved config for {plugin_name}: {config_file}")
        except Exception as e:
            logger.error(f"Failed to save config for {plugin_name}: {e}")
