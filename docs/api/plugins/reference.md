# Plugins API

← [API Documentation](README.md)

**Note:** This page has been reorganized into multiple focused documents for better navigation.

## Documentation

- **[Overview](plugins/index.md)** - Plugin system overview and quick start
- **[Creating Plugins](plugins/creating-plugins.md)** - Step-by-step development guide
- **[Lifecycle Hooks](plugins/hooks.md)** - React to filesystem events
- **[Plugin Registry](plugins/registry.md)** - Manage and configure plugins
- **[Examples](plugins/examples.md)** - Real-world plugin implementations

## Quick Start

Plugins extend Nexus functionality through:
- **Custom CLI commands**: Add new commands to the `nexus` CLI
- **Lifecycle hooks**: React to filesystem events (write, read, delete, etc.)
- **Configuration**: Manage plugin-specific settings
- **NexusFS access**: Full access to the Nexus filesystem API

## Plugin Structure

All plugins inherit from the `NexusPlugin` base class and implement required methods:

```python
from nexus.plugins import NexusPlugin, PluginMetadata
from typing import Callable

class MyPlugin(NexusPlugin):
    """Example plugin implementation."""

    def metadata(self) -> PluginMetadata:
        """Return plugin metadata (required)."""
        return PluginMetadata(
            name="my-plugin",
            version="1.0.0",
            description="My custom plugin",
            author="Your Name",
            homepage="https://github.com/yourname/nexus-plugin-my",
            requires=["other-plugin>=1.0.0"]  # Optional dependencies
        )

    def commands(self) -> dict[str, Callable]:
        """Return dict of command names to async functions."""
        return {
            "hello": self.hello_command,
            "process": self.process_command,
        }

    def hooks(self) -> dict[str, Callable]:
        """Return dict of hook names to async functions."""
        return {
            "before_write": self.validate_content,
            "after_write": self.log_write,
        }

    async def initialize(self, config: dict[str, Any]) -> None:
        """Initialize plugin with configuration."""
        self._config = config
        # Perform async initialization here

    async def shutdown(self) -> None:
        """Cleanup when plugin is disabled."""
        # Cleanup resources here
        pass

    # Command implementations
    async def hello_command(self, name: str = "World"):
        """Say hello to someone."""
        print(f"Hello, {name}!")

    async def process_command(self, path: str):
        """Process a file using NexusFS."""
        if self.nx:
            content = self.nx.read(path)
            print(f"Processing {path}: {len(content)} bytes")

    # Hook implementations
    async def validate_content(self, context: dict) -> dict:
        """Validate content before writing."""
        path = context.get("path")
        content = context.get("content")

        # Perform validation
        if b"forbidden" in content:
            print(f"Warning: Forbidden content in {path}")
            # Return None to stop the write operation
            # return None

        # Return modified context (or original)
        return context

    async def log_write(self, context: dict) -> dict:
        """Log after writing a file."""
        path = context.get("path")
        print(f"File written: {path}")
        return context
```

## Plugin Metadata

The `PluginMetadata` class defines plugin information:

```python
@dataclass
class PluginMetadata:
    name: str                      # Plugin name (used in CLI)
    version: str                   # Semantic version (e.g., "1.0.0")
    description: str               # Short description
    author: str                    # Author name
    homepage: str | None          # Homepage URL (optional)
    requires: list[str] | None    # Plugin dependencies (optional)
```

## Creating Commands

Commands are async functions that become CLI commands under `nexus <plugin-name> <command>`.

### Basic Command

```python
async def my_command(self, arg1: str, arg2: int = 10):
    """Command with arguments.

    Usage:
        nexus my-plugin my-command <arg1> [--arg2 10]
    """
    print(f"arg1={arg1}, arg2={arg2}")
```

### Command with NexusFS

```python
async def list_files(self, path: str = "/"):
    """List files in Nexus.

    Usage:
        nexus my-plugin list-files [path]
    """
    if not self.nx:
        print("Error: NexusFS not available")
        return

    files = self.nx.list(path, recursive=True)
    for file in files:
        print(file)
```

### Command with Rich Output

```python
from rich.console import Console
from rich.table import Table

async def show_stats(self):
    """Show statistics with rich formatting."""
    console = Console()

    table = Table(title="Statistics")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Files", "100")
    table.add_row("Size", "1.5 MB")

    console.print(table)
```

### Pipeline Commands

Commands can participate in pipelines using stdin/stdout:

```python
async def transform(self):
    """Transform data from pipeline.

    Usage:
        nexus plugin-a output | nexus my-plugin transform | nexus plugin-b input
    """
    # Check if piped
    if self.is_piped_input():
        # Read JSON from stdin
        data = self.read_json_input()

        # Transform data
        result = {"transformed": data}

        # Write JSON to stdout
        self.write_json_output(result)
    else:
        print("Error: This command requires piped input")
```

## Lifecycle Hooks

Hooks allow plugins to react to filesystem events. Available hook types:

### Hook Types

```python
from nexus.plugins.hooks import HookType

# Available hooks:
HookType.BEFORE_WRITE    # Before writing a file
HookType.AFTER_WRITE     # After writing a file
HookType.BEFORE_READ     # Before reading a file
HookType.AFTER_READ      # After reading a file
HookType.BEFORE_DELETE   # Before deleting a file
HookType.AFTER_DELETE    # After deleting a file
HookType.BEFORE_MKDIR    # Before creating directory
HookType.AFTER_MKDIR     # After creating directory
HookType.BEFORE_COPY     # Before copying file
HookType.AFTER_COPY      # After copying file
```

### Hook Implementation

Hooks receive a context dictionary and can:
- Inspect the operation context
- Modify the context (for `before_*` hooks)
- Cancel the operation by returning `None`
- Log or perform side effects

```python
def hooks(self) -> dict[str, Callable]:
    """Register hooks."""
    return {
        "before_write": self.validate_write,
        "after_write": self.index_content,
        "before_delete": self.check_dependencies,
    }

async def validate_write(self, context: dict) -> dict | None:
    """Validate before writing.

    Context contains:
        - path: File path
        - content: File content (bytes)
        - if_match: Optional etag for OCC
        - if_none_match: Create-only flag

    Returns:
        - Modified context dict to continue
        - None to cancel the operation
    """
    path = context["path"]
    content = context["content"]

    # Check file size
    if len(content) > 10 * 1024 * 1024:  # 10MB
        print(f"Warning: Large file write: {path} ({len(content)} bytes)")

    # Validate content
    if path.endswith(".json"):
        import json
        try:
            json.loads(content)
        except json.JSONDecodeError:
            print(f"Error: Invalid JSON in {path}")
            return None  # Cancel write

    return context  # Continue with operation

async def index_content(self, context: dict) -> dict:
    """Index content after writing."""
    path = context["path"]
    # Index for search...
    print(f"Indexed: {path}")
    return context

async def check_dependencies(self, context: dict) -> dict | None:
    """Check dependencies before deleting."""
    path = context["path"]

    # Check if file is referenced elsewhere
    if self._has_dependencies(path):
        print(f"Error: Cannot delete {path} - has dependencies")
        return None  # Cancel delete

    return context
```

### Hook Priority

Hooks can have priorities (higher = executed first):

```python
# In plugin configuration (~/.nexus/plugins/my-plugin/config.yaml)
hook_priority:
  before_write: 10
  after_write: 5
```

## Plugin Configuration

### Configuration Location

Plugins are configured in `~/.nexus/plugins/<plugin-name>/config.yaml`:

```yaml
# ~/.nexus/plugins/my-plugin/config.yaml
api_key: "sk-..."
enabled: true
cache_dir: "/tmp/my-plugin"
hook_priority:
  before_write: 10
  after_write: 5
custom_setting: "value"
```

### Accessing Configuration

```python
async def initialize(self, config: dict[str, Any]) -> None:
    """Initialize with configuration."""
    self._config = config

    # Access config values
    api_key = self.get_config("api_key")
    cache_dir = self.get_config("cache_dir", "/tmp/default")

    # Use configuration
    self.setup_api(api_key)

def get_api_key(self) -> str:
    """Get API key from config or environment."""
    return (
        self.get_config("api_key") or
        os.getenv("MY_PLUGIN_API_KEY") or
        ""
    )
```

## Plugin Registry

The plugin registry manages plugin discovery, registration, and lifecycle.

### Auto-Discovery

Plugins are discovered via Python entry points. In your plugin's `pyproject.toml`:

```toml
[project.entry-points."nexus.plugins"]
my-plugin = "nexus_my_plugin:MyPlugin"
```

### Manual Registration

```python
from nexus.plugins import PluginRegistry
from nexus import connect

nx = connect()
registry = PluginRegistry(nx)

# Discover plugins
discovered = registry.discover()
print(f"Discovered {len(discovered)} plugins")

# Get a plugin
plugin = registry.get_plugin("my-plugin")

# List all plugins
plugins = registry.list_plugins()
for metadata in plugins:
    print(f"{metadata.name} v{metadata.version}: {metadata.description}")

# Enable/disable plugins
registry.enable_plugin("my-plugin")
registry.disable_plugin("my-plugin")

# Execute a hook
from nexus.plugins.hooks import HookType
context = {"path": "/file.txt", "content": b"data"}
result = await registry.execute_hook(HookType.BEFORE_WRITE, context)
```

## Accessing NexusFS

Plugins have full access to the NexusFS API via `self.nx`:

```python
async def process_files(self):
    """Process files using NexusFS."""
    if not self.nx:
        print("Error: NexusFS not available")
        return

    # Read files
    content = self.nx.read("/data/file.txt")

    # Write files
    self.nx.write("/output/result.txt", b"processed data")

    # List files
    files = self.nx.list("/data", recursive=True)

    # Search files
    matches = self.nx.glob("**/*.py")

    # Search content
    results = self.nx.grep("TODO", path="/src")

    # Version operations
    versions = self.nx.list_versions("/file.txt")
    old_content = self.nx.get_version("/file.txt", version=1)
```

## Example Plugins

### 1. File Validator Plugin

```python
from nexus.plugins import NexusPlugin, PluginMetadata

class ValidatorPlugin(NexusPlugin):
    """Validates files before writing."""

    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="validator",
            version="1.0.0",
            description="Validates file content before writing",
            author="Nexus Team"
        )

    def hooks(self) -> dict[str, Callable]:
        return {
            "before_write": self.validate_content
        }

    async def validate_content(self, context: dict) -> dict | None:
        """Validate file content."""
        path = context["path"]
        content = context["content"]

        # Validate JSON files
        if path.endswith(".json"):
            import json
            try:
                json.loads(content)
            except json.JSONDecodeError as e:
                print(f"Error: Invalid JSON in {path}: {e}")
                return None  # Cancel write

        # Validate Python files
        if path.endswith(".py"):
            import ast
            try:
                ast.parse(content.decode("utf-8"))
            except SyntaxError as e:
                print(f"Error: Invalid Python in {path}: {e}")
                return None  # Cancel write

        return context
```

### 2. Anthropic Skills Plugin

The official Anthropic plugin demonstrates advanced plugin features:

```python
from nexus.plugins import NexusPlugin, PluginMetadata
import anthropic

class AnthropicPlugin(NexusPlugin):
    """Anthropic Claude Skills API integration."""

    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="anthropic",
            version="0.2.0",
            description="Anthropic Claude Skills API integration",
            author="Nexus Team",
            homepage="https://github.com/nexi-lab/nexus-plugin-anthropic"
        )

    def commands(self) -> dict[str, Callable]:
        return {
            "upload-skill": self.upload_skill,
            "list-skills": self.list_skills,
            "import-github": self.import_github_skill,
        }

    async def upload_skill(self, skill_name: str, api_key: str | None = None):
        """Upload a skill to Claude Skills API."""
        from nexus.skills import SkillRegistry, SkillExporter

        # Get API key
        api_key = api_key or self.get_config("api_key") or os.getenv("ANTHROPIC_API_KEY")
        client = anthropic.Anthropic(api_key=api_key)

        # Export skill
        registry = SkillRegistry(self.nx)
        await registry.discover()

        exporter = SkillExporter(registry)
        export_path = f"/tmp/{skill_name}.zip"
        await exporter.export_skill(skill_name, export_path)

        # Upload to Claude API
        with open(export_path, "rb") as f:
            response = client.beta.skills.create(
                display_title=skill_name,
                files=[("skill.zip", f.read())]
            )

        print(f"Uploaded: {response.id}")
```

## CLI Integration

### Using Plugin Commands

```bash
# List available plugins
nexus plugins list

# Use plugin commands
nexus <plugin-name> <command> [arguments]

# Examples:
nexus anthropic upload-skill my-skill
nexus validator check /data
nexus my-plugin hello --name Alice
```

### Command Help

```bash
# Get help for plugin commands
nexus <plugin-name> <command> --help
```

## Plugin Development Workflow

### 1. Create Plugin Package

```bash
# Create plugin directory structure
mkdir nexus-plugin-my
cd nexus-plugin-my

# Create package
mkdir -p src/nexus_my_plugin
touch src/nexus_my_plugin/__init__.py
touch src/nexus_my_plugin/plugin.py
```

### 2. Implement Plugin

```python
# src/nexus_my_plugin/plugin.py
from nexus.plugins import NexusPlugin, PluginMetadata

class MyPlugin(NexusPlugin):
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="my-plugin",
            version="1.0.0",
            description="My plugin",
            author="Me"
        )

    def commands(self) -> dict[str, Callable]:
        return {
            "hello": self.hello
        }

    async def hello(self):
        print("Hello from my plugin!")

# Export plugin class
__all__ = ["MyPlugin"]
```

### 3. Configure Entry Point

```toml
# pyproject.toml
[project]
name = "nexus-plugin-my"
version = "1.0.0"

[project.entry-points."nexus.plugins"]
my-plugin = "nexus_my_plugin.plugin:MyPlugin"
```

### 4. Install and Test

```bash
# Install in development mode
pip install -e .

# Test plugin discovery
python -c "from nexus.plugins import PluginRegistry; r = PluginRegistry(); print(r.discover())"

# Use plugin
nexus my-plugin hello
```

## Best Practices

### 1. Error Handling

```python
async def my_command(self, path: str):
    """Command with proper error handling."""
    try:
        if not self.nx:
            raise ValueError("NexusFS not available")

        content = self.nx.read(path)
        # Process content...

    except Exception as e:
        print(f"Error: {e}")
        return
```

### 2. Configuration Validation

```python
async def initialize(self, config: dict[str, Any]) -> None:
    """Initialize with configuration validation."""
    self._config = config

    # Validate required config
    api_key = self.get_config("api_key")
    if not api_key:
        raise ValueError("api_key is required in plugin configuration")

    # Validate config values
    cache_dir = self.get_config("cache_dir", "/tmp/default")
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir)
```

### 3. Resource Cleanup

```python
async def initialize(self, config: dict[str, Any]) -> None:
    """Initialize resources."""
    self._client = SomeClient(api_key=config["api_key"])
    self._cache = {}

async def shutdown(self) -> None:
    """Cleanup resources."""
    if hasattr(self, "_client"):
        await self._client.close()

    if hasattr(self, "_cache"):
        self._cache.clear()
```

### 4. Hook Safety

```python
async def safe_hook(self, context: dict) -> dict:
    """Hook with error handling."""
    try:
        # Process context...
        return context
    except Exception as e:
        # Log error but don't break the chain
        print(f"Hook error: {e}")
        return context  # Return original context
```

## Troubleshooting

### Plugin Not Discovered

```bash
# Check entry points
python -c "import importlib.metadata; print(list(importlib.metadata.entry_points(group='nexus.plugins')))"

# Reinstall plugin
pip uninstall nexus-plugin-my
pip install -e .
```

### Plugin Not Loading

```python
# Enable debug logging
import logging
logging.basicConfig(level=logging.DEBUG)

from nexus.plugins import PluginRegistry
registry = PluginRegistry()
discovered = registry.discover()  # Check logs for errors
```

### Hook Not Executing

```python
# Check hook registration
registry = PluginRegistry()
registry.discover()

from nexus.plugins.hooks import HookType
handlers = registry.get_hooks().get_handlers(HookType.BEFORE_WRITE)
print(f"Registered handlers: {handlers}")
```

## See Also

- [Skills API](skills.md) - AI skills system
- [CLI Reference](cli-reference.md) - Command-line interface
- [Core API](core-api.md) - Core filesystem API
- [Configuration](configuration.md) - Configuration system

## Next Steps

1. Create your first plugin following the [development workflow](#plugin-development-workflow)
2. Study the [Anthropic plugin](https://github.com/nexi-lab/nexus-plugin-anthropic) example
3. Explore available [hook types](#hook-types) for your use case
4. Configure your plugin using [configuration files](#plugin-configuration)
