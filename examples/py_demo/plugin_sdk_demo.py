"""Nexus Plugin System SDK Demo - Comprehensive programmatic usage.

This demo shows how to work with the Nexus plugin system programmatically:
- Discover and load plugins
- Access plugin metadata
- Use plugin registry
- Execute plugin commands
- Work with plugin configuration
- Test plugin functionality

This is useful for:
- Building tools that manage plugins
- Testing plugin implementations
- Creating plugin management UIs
- Integrating plugins into applications
"""

import asyncio
import tempfile
from pathlib import Path

import nexus


def main() -> None:
    """Run the plugin system SDK demo."""
    print("=" * 70)
    print("Nexus Plugin System - Python SDK Demo")
    print("=" * 70)
    print()

    # Run async demo
    asyncio.run(plugin_demo())


async def plugin_demo() -> None:
    """Run the async plugin demo."""

    # ============================================================
    # Part 1: Plugin Discovery and Registry
    # ============================================================
    print("=" * 70)
    print("PART 1: Plugin Discovery and Registry")
    print("=" * 70)
    print()

    print("1. Creating plugin registry...")
    from nexus.plugins.registry import PluginRegistry

    # Create registry (without NexusFS for now)
    registry = PluginRegistry(nexus_fs=None)
    print("   ✓ Registry created")
    print()

    print("2. Discovering installed plugins via entry points...")
    discovered = registry.discover()
    print(f"   ✓ Discovered {len(discovered)} plugin(s)")
    if discovered:
        for plugin_name in discovered:
            print(f"     - {plugin_name}")
    else:
        print("   Note: No plugins installed")
        print("   Install with: pip install nexus-ai-fs[plugins]")
    print()

    print("3. Listing plugin metadata...")
    plugins = registry.list_plugins()
    if plugins:
        for metadata in plugins:
            print(f"   • {metadata.name} v{metadata.version}")
            print(f"     {metadata.description}")
            print(f"     Author: {metadata.author}")
            if metadata.homepage:
                print(f"     Homepage: {metadata.homepage}")
            print()
    else:
        print("   No plugins available")
    print()

    # ============================================================
    # Part 2: Plugin Metadata and Information
    # ============================================================
    print("=" * 70)
    print("PART 2: Plugin Metadata and Information")
    print("=" * 70)
    print()

    if discovered:
        first_plugin_name = discovered[0]
        print(f"4. Getting plugin instance: {first_plugin_name}")
        plugin = registry.get_plugin(first_plugin_name)

        if plugin:
            print("   ✓ Plugin loaded")
            print()

            print("5. Accessing plugin metadata...")
            metadata = plugin.metadata()
            print(f"   Name: {metadata.name}")
            print(f"   Version: {metadata.version}")
            print(f"   Description: {metadata.description}")
            print(f"   Author: {metadata.author}")
            if metadata.homepage:
                print(f"   Homepage: {metadata.homepage}")
            if metadata.requires:
                print(f"   Dependencies: {', '.join(metadata.requires)}")
            print()

            print("6. Listing plugin commands...")
            commands = plugin.commands()
            if commands:
                print(f"   Found {len(commands)} command(s):")
                for cmd_name, cmd_func in commands.items():
                    # Get command docstring if available
                    doc = cmd_func.__doc__ or "No description"
                    # Get first line of docstring
                    first_line = doc.strip().split("\n")[0]
                    print(f"   • {cmd_name}: {first_line}")
                print()
            else:
                print("   No commands registered")
                print()

            print("7. Checking plugin hooks...")
            hooks = plugin.hooks()
            if hooks:
                print(f"   Found {len(hooks)} hook(s):")
                for hook_name in hooks:
                    print(f"   • {hook_name}")
                print()
            else:
                print("   No hooks registered")
                print()

            print("8. Plugin state...")
            print(f"   Enabled: {plugin.is_enabled()}")
            print(f"   Has NexusFS: {plugin.nx is not None}")
            print()
    else:
        print("4-8. Skipping (no plugins installed)")
        print()

    # ============================================================
    # Part 3: Plugin Configuration
    # ============================================================
    print("=" * 70)
    print("PART 3: Plugin Configuration")
    print("=" * 70)
    print()

    if discovered and plugin:
        print("9. Accessing plugin configuration...")
        # Configuration is loaded from ~/.nexus/plugins/<name>/config.yaml
        api_key = plugin.get_config("api_key", default="not-set")
        endpoint = plugin.get_config("endpoint", default="https://default.example.com")

        print(f"   api_key: {api_key}")
        print(f"   endpoint: {endpoint}")
        print()

        print("10. Configuration file location...")
        config_dir = Path.home() / ".nexus" / "plugins" / plugin.metadata().name
        config_file = config_dir / "config.yaml"
        print(f"   Config path: {config_file}")
        print(f"   Exists: {config_file.exists()}")
        print()

        if config_file.exists():
            print("   Config contents:")
            with open(config_file) as f:
                for line in f:
                    print(f"     {line.rstrip()}")
        else:
            print("   Config file not found")
            print("   Create with:")
            print(f"     mkdir -p {config_dir}")
            print(f"     cat > {config_file} << EOF")
            print("     api_key: your-key-here")
            print("     endpoint: https://api.example.com")
            print("     EOF")
        print()
    else:
        print("9-10. Skipping (no plugins available)")
        print()

    # ============================================================
    # Part 4: Plugin Enable/Disable
    # ============================================================
    print("=" * 70)
    print("PART 4: Plugin Enable/Disable")
    print("=" * 70)
    print()

    if discovered:
        plugin_name = discovered[0]
        print(f"11. Testing enable/disable for: {plugin_name}")

        # Check initial state
        plugin = registry.get_plugin(plugin_name)
        if plugin:
            initial_state = plugin.is_enabled()
            print(f"   Initial state: {'Enabled' if initial_state else 'Disabled'}")

            # Disable plugin
            print("   Disabling plugin...")
            registry.disable_plugin(plugin_name)
            print(f"   State: {'Enabled' if plugin.is_enabled() else 'Disabled'}")

            # Enable plugin
            print("   Enabling plugin...")
            registry.enable_plugin(plugin_name)
            print(f"   State: {'Enabled' if plugin.is_enabled() else 'Disabled'}")
            print()
    else:
        print("11. Skipping (no plugins available)")
        print()

    # ============================================================
    # Part 5: Working with NexusFS
    # ============================================================
    print("=" * 70)
    print("PART 5: Working with NexusFS")
    print("=" * 70)
    print()

    print("12. Creating NexusFS instance for plugins...")
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "nexus-data"

        # Connect to Nexus
        nx = nexus.connect(config={"data_dir": str(data_dir)})
        print(f"   ✓ Connected to Nexus (data_dir: {data_dir})")
        print()

        print("13. Creating registry with NexusFS...")
        # Create new registry with NexusFS
        fs_registry = PluginRegistry(nexus_fs=nx)
        discovered_with_fs = fs_registry.discover()
        print(f"   ✓ Discovered {len(discovered_with_fs)} plugin(s) with FS access")
        print()

        if discovered_with_fs:
            print("14. Accessing plugin with NexusFS...")
            fs_plugin = fs_registry.get_plugin(discovered_with_fs[0])

            if fs_plugin and fs_plugin.nx:
                print("   ✓ Plugin has NexusFS access")
                print()

                # Create some test files
                print("15. Creating test files for plugin to process...")
                nx.write("/workspace/test.txt", b"Hello from Nexus!")
                nx.write("/workspace/data.json", b'{"message": "test"}')
                print("   ✓ Created test files")
                print()

                print("16. Plugin can now access files:")
                print(f"   • Can read: {fs_plugin.nx.exists('/workspace/test.txt')}")
                print(f"   • Can list: {len(fs_plugin.nx.list('/workspace'))} files")
                print()

                # Example: If plugin has a command that processes files
                commands = fs_plugin.commands()
                if commands:
                    print("17. Plugin commands have file access:")
                    print(f"   Available commands: {', '.join(commands.keys())}")
                    print("   Commands can call self.nx.read(), self.nx.write(), etc.")
                    print()
            else:
                print("   Note: Plugin does not have NexusFS access")
                print()
        else:
            print("14-17. Skipping (no plugins available)")
            print()

        # Close Nexus connection
        nx.close()

    # ============================================================
    # Part 6: Plugin Registry Management
    # ============================================================
    print("=" * 70)
    print("PART 6: Plugin Registry Management")
    print("=" * 70)
    print()

    print("18. Manual plugin registration (for testing)...")
    from nexus.plugins import NexusPlugin, PluginMetadata

    class TestPlugin(NexusPlugin):
        """Test plugin for demonstration."""

        def metadata(self) -> PluginMetadata:
            return PluginMetadata(
                name="test-plugin",
                version="0.1.0",
                description="Test plugin for SDK demo",
                author="Demo",
            )

        def commands(self) -> dict:
            return {
                "hello": self.hello_command,
            }

        async def hello_command(self, name: str = "World") -> None:
            """Say hello."""
            print(f"Hello, {name}!")

    # Create and register test plugin
    test_plugin = TestPlugin()
    test_registry = PluginRegistry()
    test_registry.register(test_plugin, "test-plugin")

    print("   ✓ Registered test plugin")
    print(f"   Name: {test_plugin.metadata().name}")
    print(f"   Commands: {list(test_plugin.commands().keys())}")
    print()

    print("19. Calling plugin command programmatically...")
    hello_cmd = test_plugin.commands()["hello"]
    await hello_cmd("Nexus SDK Demo")
    print()

    print("20. Unregistering plugin...")
    test_registry.unregister("test-plugin")
    remaining = test_registry.list_plugins()
    print("   ✓ Unregistered test plugin")
    print(f"   Remaining plugins: {len(remaining)}")
    print()

    # ============================================================
    # Part 7: Advanced - Hooks System
    # ============================================================
    print("=" * 70)
    print("PART 7: Advanced - Hooks System")
    print("=" * 70)
    print()

    print("21. Demonstrating lifecycle hooks...")

    class HookDemoPlugin(NexusPlugin):
        """Plugin demonstrating hook usage."""

        def metadata(self) -> PluginMetadata:
            return PluginMetadata(
                name="hook-demo",
                version="0.1.0",
                description="Hook demonstration plugin",
                author="Demo",
            )

        def hooks(self) -> dict:
            return {
                "before_write": self.before_write_hook,
                "after_read": self.after_read_hook,
            }

        async def before_write_hook(self, context: dict) -> dict:
            """Called before writing a file."""
            print(f"   [HOOK] before_write: {context.get('path', 'unknown')}")
            return context

        async def after_read_hook(self, context: dict) -> dict:
            """Called after reading a file."""
            print(f"   [HOOK] after_read: {context.get('path', 'unknown')}")
            return context

    hook_plugin = HookDemoPlugin()
    print("   ✓ Created plugin with hooks")
    print(f"   Hooks: {list(hook_plugin.hooks().keys())}")
    print()

    print("22. Simulating hook execution...")
    # Normally hooks are executed by PluginRegistry
    before_write_hook = hook_plugin.hooks()["before_write"]
    after_read_hook = hook_plugin.hooks()["after_read"]

    # Simulate before write
    write_context = {"path": "/workspace/new-file.txt", "content": b"data"}
    await before_write_hook(write_context)

    # Simulate after read
    read_context = {"path": "/workspace/existing-file.txt", "content": b"result"}
    await after_read_hook(read_context)
    print()

    # ============================================================
    # Final Summary
    # ============================================================
    print("=" * 70)
    print("Demo Complete!")
    print("=" * 70)
    print()

    print("✨ Key Concepts Demonstrated:")
    print("   • Plugin discovery via entry points")
    print("   • Accessing plugin metadata and commands")
    print("   • Plugin configuration management")
    print("   • Enable/disable plugins programmatically")
    print("   • Providing NexusFS access to plugins")
    print("   • Manual plugin registration for testing")
    print("   • Lifecycle hooks implementation")
    print()

    print("📚 Plugin Registry API:")
    print("   • PluginRegistry.discover() - Find installed plugins")
    print("   • PluginRegistry.get_plugin(name) - Get plugin instance")
    print("   • PluginRegistry.list_plugins() - Get all plugin metadata")
    print("   • PluginRegistry.register/unregister() - Manual management")
    print("   • PluginRegistry.enable_plugin/disable_plugin() - Toggle plugins")
    print()

    print("🔧 Plugin Development:")
    print("   • Inherit from NexusPlugin base class")
    print("   • Implement metadata() method (required)")
    print("   • Optionally implement commands() and hooks()")
    print("   • Access NexusFS via self.nx property")
    print("   • Use get_config() for configuration values")
    print()

    print("📖 Next Steps:")
    print("   • Install first-party plugins: pip install nexus-ai-fs[plugins]")
    print("   • Read plugin development guide: docs/PLUGIN_DEVELOPMENT.md")
    print("   • Study example plugins: nexus-plugin-anthropic/")
    print("   • Create your own plugin following the guide")
    print()

    print("🔗 Resources:")
    print("   • Plugin Development Guide: docs/PLUGIN_DEVELOPMENT.md")
    print("   • Plugin System Overview: docs/PLUGIN_SYSTEM.md")
    print("   • API Reference: In-code documentation")
    print()


if __name__ == "__main__":
    main()
