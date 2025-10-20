"""Base plugin interface for Nexus plugins."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nexus.core.nexus_fs import NexusFS


@dataclass
class PluginMetadata:
    """Metadata for a Nexus plugin."""

    name: str
    version: str
    description: str
    author: str
    homepage: str | None = None
    requires: list[str] | None = None


class NexusPlugin(ABC):
    """Base class for all Nexus plugins.

    Plugins extend Nexus functionality through:
    - Custom CLI commands
    - Lifecycle hooks
    - Configuration
    - Access to NexusFS

    Example:
        class MyPlugin(NexusPlugin):
            def metadata(self) -> PluginMetadata:
                return PluginMetadata(
                    name="my-plugin",
                    version="0.1.0",
                    description="My custom plugin",
                    author="Me"
                )

            def commands(self) -> dict[str, Callable]:
                return {
                    "hello": self.hello_command,
                }

            async def hello_command(self, name: str = "World"):
                print(f"Hello, {name}!")
    """

    def __init__(self, nexus_fs: NexusFS | None = None):
        """Initialize plugin with optional NexusFS instance."""
        self._nexus_fs = nexus_fs
        self._config: dict[str, Any] = {}
        self._enabled = True

    @property
    def nx(self) -> NexusFS | None:
        """Access to NexusFS instance."""
        return self._nexus_fs

    @abstractmethod
    def metadata(self) -> PluginMetadata:
        """Return plugin metadata."""
        pass

    def commands(self) -> dict[str, Callable]:
        """Return dict of command names to callable functions.

        Returns:
            Dictionary mapping command names to async callable functions.
            Each command will be accessible as: nexus <plugin-name> <command>
        """
        return {}

    def hooks(self) -> dict[str, Callable]:
        """Return dict of hook names to callable functions.

        Returns:
            Dictionary mapping hook names to async callable functions.
            Available hooks:
            - before_write: Called before writing a file
            - after_write: Called after writing a file
            - before_read: Called before reading a file
            - after_read: Called after reading a file
            - before_delete: Called before deleting a file
            - after_delete: Called after deleting a file
        """
        return {}

    async def initialize(self, config: dict[str, Any]) -> None:
        """Initialize plugin with configuration.

        Args:
            config: Plugin-specific configuration dictionary
        """
        self._config = config

    async def shutdown(self) -> None:
        """Cleanup when plugin is disabled or Nexus is shutting down.

        This is an optional hook that plugins can override for cleanup tasks.
        Default implementation does nothing.
        """
        # Default: no cleanup required
        return

    def is_enabled(self) -> bool:
        """Check if plugin is enabled."""
        return self._enabled

    def enable(self) -> None:
        """Enable the plugin."""
        self._enabled = True

    def disable(self) -> None:
        """Disable the plugin."""
        self._enabled = False

    def get_config(self, key: str, default: Any = None) -> Any:
        """Get plugin configuration value.

        Args:
            key: Configuration key
            default: Default value if key not found

        Returns:
            Configuration value or default
        """
        return self._config.get(key, default)
