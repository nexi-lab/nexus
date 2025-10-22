"""Unit tests for plugin system."""

import contextlib
import tempfile
from pathlib import Path

import pytest
import yaml

from nexus.plugins import NexusPlugin, PluginMetadata, PluginRegistry
from nexus.plugins.hooks import HookType, PluginHooks


class TestPlugin(NexusPlugin):
    """Test plugin for testing."""

    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="test-plugin",
            version="0.1.0",
            description="Test plugin",
            author="Test Author",
        )

    def commands(self) -> dict:
        return {
            "hello": self.hello_command,
        }

    async def hello_command(self, name: str = "World"):
        return f"Hello, {name}!"

    def hooks(self) -> dict:
        return {
            "before_write": self.before_write_hook,
        }

    async def before_write_hook(self, context: dict):
        context["modified"] = True
        return context


class TestPluginBase:
    """Test NexusPlugin base class."""

    def test_plugin_metadata(self):
        """Test plugin metadata."""
        plugin = TestPlugin()
        metadata = plugin.metadata()

        assert metadata.name == "test-plugin"
        assert metadata.version == "0.1.0"
        assert metadata.description == "Test plugin"
        assert metadata.author == "Test Author"

    def test_plugin_commands(self):
        """Test plugin commands."""
        plugin = TestPlugin()
        commands = plugin.commands()

        assert "hello" in commands
        assert callable(commands["hello"])

    @pytest.mark.asyncio
    async def test_plugin_command_execution(self):
        """Test executing a plugin command."""
        plugin = TestPlugin()
        result = await plugin.hello_command("Test")

        assert result == "Hello, Test!"

    def test_plugin_hooks(self):
        """Test plugin hooks."""
        plugin = TestPlugin()
        hooks = plugin.hooks()

        assert "before_write" in hooks
        assert callable(hooks["before_write"])

    @pytest.mark.asyncio
    async def test_plugin_hook_execution(self):
        """Test executing a plugin hook."""
        plugin = TestPlugin()
        context = {"path": "/test"}
        result = await plugin.before_write_hook(context)

        assert result["modified"] is True
        assert result["path"] == "/test"

    def test_plugin_enable_disable(self):
        """Test enabling/disabling plugins."""
        plugin = TestPlugin()

        assert plugin.is_enabled() is True

        plugin.disable()
        assert plugin.is_enabled() is False

        plugin.enable()
        assert plugin.is_enabled() is True

    @pytest.mark.asyncio
    async def test_plugin_initialization(self):
        """Test plugin initialization with config."""
        plugin = TestPlugin()
        config = {"api_key": "test-key", "enabled": True}

        await plugin.initialize(config)

        assert plugin.get_config("api_key") == "test-key"
        assert plugin.get_config("enabled") is True
        assert plugin.get_config("missing", "default") == "default"


class TestPluginHooks:
    """Test plugin hooks system."""

    @pytest.mark.asyncio
    async def test_hook_registration(self):
        """Test registering hooks."""
        hooks = PluginHooks()

        async def test_hook(context):
            return context

        hooks.register(HookType.BEFORE_WRITE, test_hook)
        handlers = hooks.get_handlers(HookType.BEFORE_WRITE)

        assert len(handlers) == 1
        assert handlers[0] == test_hook

    @pytest.mark.asyncio
    async def test_hook_execution(self):
        """Test executing hooks."""
        hooks = PluginHooks()

        async def test_hook(context):
            context["executed"] = True
            return context

        hooks.register(HookType.BEFORE_WRITE, test_hook)
        result = await hooks.execute(HookType.BEFORE_WRITE, {"path": "/test"})

        assert result["executed"] is True
        assert result["path"] == "/test"

    @pytest.mark.asyncio
    async def test_hook_priority(self):
        """Test hook execution order by priority."""
        hooks = PluginHooks()

        execution_order = []

        async def hook_low(context):
            execution_order.append("low")
            return context

        async def hook_high(context):
            execution_order.append("high")
            return context

        # Register with different priorities (higher = executed first)
        hooks.register(HookType.BEFORE_WRITE, hook_low, priority=0)
        hooks.register(HookType.BEFORE_WRITE, hook_high, priority=10)

        await hooks.execute(HookType.BEFORE_WRITE, {})

        assert execution_order == ["high", "low"]

    @pytest.mark.asyncio
    async def test_hook_stop_execution(self):
        """Test stopping hook execution by returning None."""
        hooks = PluginHooks()

        async def hook_stop(context):
            return None  # Stop execution

        async def hook_after(context):
            context["should_not_execute"] = True
            return context

        hooks.register(HookType.BEFORE_WRITE, hook_stop, priority=10)
        hooks.register(HookType.BEFORE_WRITE, hook_after, priority=0)

        result = await hooks.execute(HookType.BEFORE_WRITE, {})

        assert result is None

    def test_hook_unregister(self):
        """Test unregistering hooks."""
        hooks = PluginHooks()

        async def test_hook(context):
            return context

        hooks.register(HookType.BEFORE_WRITE, test_hook)
        assert len(hooks.get_handlers(HookType.BEFORE_WRITE)) == 1

        hooks.unregister(HookType.BEFORE_WRITE, test_hook)
        assert len(hooks.get_handlers(HookType.BEFORE_WRITE)) == 0

    def test_hook_clear(self):
        """Test clearing hooks."""
        hooks = PluginHooks()

        async def test_hook(context):
            return context

        hooks.register(HookType.BEFORE_WRITE, test_hook)
        hooks.register(HookType.AFTER_WRITE, test_hook)

        hooks.clear(HookType.BEFORE_WRITE)
        assert len(hooks.get_handlers(HookType.BEFORE_WRITE)) == 0
        assert len(hooks.get_handlers(HookType.AFTER_WRITE)) == 1

        hooks.clear()  # Clear all
        assert len(hooks.get_handlers(HookType.AFTER_WRITE)) == 0


class TestPluginRegistry:
    """Test plugin registry."""

    def test_plugin_registration(self):
        """Test manually registering a plugin."""
        registry = PluginRegistry()
        plugin = TestPlugin()

        registry.register(plugin)

        assert registry.get_plugin("test-plugin") is not None
        assert registry.get_plugin("test-plugin") == plugin

    def test_plugin_unregistration(self):
        """Test unregistering a plugin."""
        registry = PluginRegistry()
        plugin = TestPlugin()

        registry.register(plugin)
        assert registry.get_plugin("test-plugin") is not None

        registry.unregister("test-plugin")
        assert registry.get_plugin("test-plugin") is None

    def test_list_plugins(self):
        """Test listing plugins."""
        registry = PluginRegistry()
        plugin = TestPlugin()

        registry.register(plugin)
        plugins = registry.list_plugins()

        assert len(plugins) == 1
        assert plugins[0].name == "test-plugin"

    def test_enable_disable_plugin(self):
        """Test enabling/disabling plugins via registry."""
        registry = PluginRegistry()
        plugin = TestPlugin()

        registry.register(plugin)

        registry.disable_plugin("test-plugin")
        assert not plugin.is_enabled()

        registry.enable_plugin("test-plugin")
        assert plugin.is_enabled()

    @pytest.mark.asyncio
    async def test_plugin_hooks_registration(self):
        """Test that plugin hooks are registered in registry."""
        registry = PluginRegistry()
        plugin = TestPlugin()

        registry.register(plugin)

        # Execute hook
        context = {"path": "/test"}
        result = await registry.execute_hook(HookType.BEFORE_WRITE, context)

        assert result["modified"] is True

    def test_get_hooks(self):
        """Test getting hooks from registry."""
        registry = PluginRegistry()
        hooks = registry.get_hooks()

        assert isinstance(hooks, PluginHooks)

    def test_unregister_nonexistent_plugin(self):
        """Test unregistering a plugin that doesn't exist (should not raise)."""
        registry = PluginRegistry()

        # Should not raise an error
        registry.unregister("nonexistent-plugin")

    def test_enable_disable_nonexistent_plugin(self):
        """Test enabling/disabling nonexistent plugin (should not raise)."""
        registry = PluginRegistry()

        # Should not raise errors
        registry.enable_plugin("nonexistent-plugin")
        registry.disable_plugin("nonexistent-plugin")

    def test_get_nonexistent_plugin(self):
        """Test getting a plugin that doesn't exist."""
        registry = PluginRegistry()

        result = registry.get_plugin("nonexistent-plugin")
        assert result is None

    def test_discover_no_plugins(self):
        """Test discovery when no plugins are installed."""
        registry = PluginRegistry()

        # Should return empty list when no plugins found
        discovered = registry.discover()

        # Can't assert exact value since system may have plugins
        # but at least verify it returns a list
        assert isinstance(discovered, list)


class TestPluginRegistryConfig:
    """Test plugin registry configuration management."""

    def test_load_plugin_config_nonexistent(self):
        """Test loading config when file doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = PluginRegistry(config_dir=Path(tmpdir))
            config = registry._load_plugin_config("test-plugin")

            assert config == {}

    def test_load_plugin_config_valid(self):
        """Test loading valid plugin config."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            plugin_dir = config_dir / "test-plugin"
            plugin_dir.mkdir(parents=True)

            config_file = plugin_dir / "config.yaml"
            test_config = {"api_key": "test-key", "enabled": True}

            with open(config_file, "w") as f:
                yaml.dump(test_config, f)

            registry = PluginRegistry(config_dir=config_dir)
            config = registry._load_plugin_config("test-plugin")

            assert config == test_config

    def test_load_plugin_config_invalid_yaml(self):
        """Test loading config with invalid YAML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            plugin_dir = config_dir / "test-plugin"
            plugin_dir.mkdir(parents=True)

            config_file = plugin_dir / "config.yaml"

            # Write invalid YAML
            with open(config_file, "w") as f:
                f.write("invalid: yaml: content: [")

            registry = PluginRegistry(config_dir=config_dir)
            config = registry._load_plugin_config("test-plugin")

            # Should return empty dict on error
            assert config == {}

    def test_load_plugin_config_empty_file(self):
        """Test loading config from empty file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            plugin_dir = config_dir / "test-plugin"
            plugin_dir.mkdir(parents=True)

            config_file = plugin_dir / "config.yaml"

            # Write empty file
            config_file.touch()

            registry = PluginRegistry(config_dir=config_dir)
            config = registry._load_plugin_config("test-plugin")

            assert config == {}

    def test_save_plugin_config(self):
        """Test saving plugin config."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            registry = PluginRegistry(config_dir=config_dir)

            test_config = {"api_key": "test-key", "enabled": True}
            registry.save_plugin_config("test-plugin", test_config)

            # Verify file was created
            config_file = config_dir / "test-plugin" / "config.yaml"
            assert config_file.exists()

            # Verify content
            with open(config_file) as f:
                loaded_config = yaml.safe_load(f)

            assert loaded_config == test_config

    def test_save_plugin_config_creates_dir(self):
        """Test that saving config creates plugin directory if needed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            registry = PluginRegistry(config_dir=config_dir)

            plugin_dir = config_dir / "new-plugin"
            assert not plugin_dir.exists()

            registry.save_plugin_config("new-plugin", {"key": "value"})

            assert plugin_dir.exists()
            assert (plugin_dir / "config.yaml").exists()

    def test_save_plugin_config_write_error(self):
        """Test saving config when write fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            registry = PluginRegistry(config_dir=config_dir)

            # Create plugin dir and make config file read-only
            plugin_dir = config_dir / "test-plugin"
            plugin_dir.mkdir(parents=True)
            config_file = plugin_dir / "config.yaml"
            config_file.touch()
            config_file.chmod(0o444)  # Read-only

            # Try to save - should handle error gracefully
            with contextlib.suppress(Exception):
                registry.save_plugin_config("test-plugin", {"key": "value"})

            # Cleanup
            config_file.chmod(0o644)


class TestPluginHooksWithInvalidHookType:
    """Test plugin hooks with invalid hook types."""

    def test_register_plugin_with_invalid_hook_type(self):
        """Test registering a plugin with an invalid hook type."""

        class PluginWithInvalidHook(NexusPlugin):
            """Plugin with invalid hook type."""

            def metadata(self) -> PluginMetadata:
                return PluginMetadata(
                    name="invalid-hook-plugin",
                    version="0.1.0",
                    description="Test plugin with invalid hook",
                    author="Test",
                )

            def hooks(self) -> dict:
                return {
                    "invalid_hook_type": self.invalid_hook,
                }

            async def invalid_hook(self, context: dict) -> dict:
                return context

        registry = PluginRegistry()
        plugin = PluginWithInvalidHook()

        # Should register without error, but skip invalid hook
        registry.register(plugin)

        assert registry.get_plugin("invalid-hook-plugin") is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
