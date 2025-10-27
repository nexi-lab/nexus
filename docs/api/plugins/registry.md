# Plugin Registry

← [Plugins API](index.md)

The Plugin Registry manages plugin discovery, registration, and lifecycle.

## Overview

```python
from nexus.plugins import PluginRegistry
from nexus import connect

nx = connect()
registry = PluginRegistry(nx)
```

## Discovery

Plugins are automatically discovered via Python entry points:

```python
# Discover all plugins
discovered = registry.discover()
print(f"Discovered {len(discovered)} plugins: {discovered}")

# Example output:
# Discovered 2 plugins: ['anthropic', 'my-plugin']
```

### Entry Point Configuration

In your plugin's `pyproject.toml`:

```toml
[project.entry-points."nexus.plugins"]
my-plugin = "nexus_my_plugin:MyPlugin"
```

## Registration

### Auto-Registration

Plugins with entry points are automatically registered on discovery:

```python
registry = PluginRegistry(nx)
registry.discover()  # Auto-registers all discovered plugins
```

### Manual Registration

Register a plugin manually without entry points:

```python
from nexus_my_plugin import MyPlugin

plugin = MyPlugin(nx)
registry.register(plugin, name="my-plugin")
```

## Managing Plugins

### List Plugins

```python
# Get all plugin metadata
plugins = registry.list_plugins()
for metadata in plugins:
    print(f"{metadata.name} v{metadata.version}")
    print(f"  {metadata.description}")
    print(f"  Author: {metadata.author}")
```

### Get Plugin

```python
# Get a specific plugin
plugin = registry.get_plugin("my-plugin")
if plugin:
    print(f"Found: {plugin.metadata().name}")
else:
    print("Plugin not found")
```

### Enable/Disable Plugins

```python
# Disable a plugin
registry.disable_plugin("my-plugin")

# Enable a plugin
registry.enable_plugin("my-plugin")

# Check if enabled
plugin = registry.get_plugin("my-plugin")
if plugin and plugin.is_enabled():
    print("Plugin is enabled")
```

### Unregister Plugin

```python
# Unregister a plugin
registry.unregister("my-plugin")
```

## Hook Management

### Get Hooks Registry

```python
hooks = registry.get_hooks()
```

### Execute Hooks

```python
from nexus.plugins.hooks import HookType

# Execute a hook
context = {
    "path": "/file.txt",
    "content": b"data"
}

result = await registry.execute_hook(HookType.BEFORE_WRITE, context)

if result is None:
    print("Operation canceled by hook")
else:
    print("Operation allowed, context:", result)
```

### Get Hook Handlers

```python
from nexus.plugins.hooks import HookType

hooks = registry.get_hooks()
handlers = hooks.get_handlers(HookType.BEFORE_WRITE)

print(f"Registered before_write handlers: {len(handlers)}")
```

## Configuration

### Plugin Configuration Directory

Plugins are configured in `~/.nexus/plugins/<plugin-name>/`:

```
~/.nexus/plugins/
├── my-plugin/
│   └── config.yaml
└── anthropic/
    └── config.yaml
```

### Loading Configuration

Configuration is loaded automatically during discovery:

```python
# Configuration is loaded and passed to plugin.initialize()
registry.discover()

# Access config in your plugin:
class MyPlugin(NexusPlugin):
    async def initialize(self, config: dict[str, Any]) -> None:
        api_key = self.get_config("api_key")
        # Use configuration...
```

### Saving Configuration

```python
# Save plugin configuration
config = {
    "api_key": "sk-...",
    "enabled": True
}
registry.save_plugin_config("my-plugin", config)
```

## CLI Integration

### List Plugins

```bash
nexus plugins list
```

Output:
```
Available Plugins:
  - anthropic v0.2.0: Anthropic Claude Skills API integration
  - my-plugin v1.0.0: My custom plugin
```

### Plugin Commands

```bash
# Use plugin commands
nexus <plugin-name> <command> [args]

# Examples:
nexus anthropic list-skills
nexus my-plugin hello --name Alice
```

## Complete Example

```python
from nexus import connect
from nexus.plugins import PluginRegistry, NexusPlugin, PluginMetadata
from nexus.plugins.hooks import HookType

# Connect to Nexus
nx = connect()

# Create registry
registry = PluginRegistry(nx)

# Discover plugins
discovered = registry.discover()
print(f"Discovered plugins: {discovered}")

# List all plugins
plugins = registry.list_plugins()
for metadata in plugins:
    print(f"\nPlugin: {metadata.name}")
    print(f"  Version: {metadata.version}")
    print(f"  Description: {metadata.description}")

# Get a specific plugin
plugin = registry.get_plugin("anthropic")
if plugin:
    print(f"\nFound plugin: {plugin.metadata().name}")
    print(f"  Commands: {list(plugin.commands().keys())}")
    print(f"  Hooks: {list(plugin.hooks().keys())}")

# Execute a hook
context = {"path": "/test.txt", "content": b"test data"}
result = await registry.execute_hook(HookType.BEFORE_WRITE, context)

if result:
    print("\nHook allowed operation")
else:
    print("\nHook canceled operation")

# Get hook statistics
hooks = registry.get_hooks()
for hook_type in HookType:
    handlers = hooks.get_handlers(hook_type)
    if handlers:
        print(f"{hook_type.value}: {len(handlers)} handlers")
```

## Troubleshooting

### Plugin Not Discovered

```python
# Check entry points
import importlib.metadata
entry_points = importlib.metadata.entry_points(group='nexus.plugins')
print(list(entry_points))

# Enable debug logging
import logging
logging.basicConfig(level=logging.DEBUG)
registry.discover()
```

### Plugin Not Loading

```python
# Check for errors during discovery
import logging
logging.basicConfig(level=logging.INFO)

registry = PluginRegistry(nx)
discovered = registry.discover()

# Check if plugin was discovered
if "my-plugin" in discovered:
    plugin = registry.get_plugin("my-plugin")
    if plugin:
        print("Plugin loaded successfully")
    else:
        print("Plugin discovered but not loaded")
else:
    print("Plugin not discovered")
```

### Hook Not Executing

```python
# List registered hooks
from nexus.plugins.hooks import HookType

hooks = registry.get_hooks()
handlers = hooks.get_handlers(HookType.BEFORE_WRITE)

print(f"before_write handlers: {len(handlers)}")
for handler in handlers:
    print(f"  {handler}")
```

## See Also

- [Creating Plugins](creating-plugins.md)
- [Lifecycle Hooks](hooks.md)
- [Plugin Examples](examples.md)
