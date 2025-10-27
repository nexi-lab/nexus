# Plugins API Overview

← [API Documentation](../README.md)

Nexus provides a powerful plugin system that allows you to extend functionality through custom commands, lifecycle hooks, and configuration.

## What are Plugins?

Plugins extend Nexus functionality through:

- **Custom CLI commands**: Add new commands to the `nexus` CLI
- **Lifecycle hooks**: React to filesystem events (write, read, delete, etc.)
- **Configuration**: Manage plugin-specific settings
- **NexusFS access**: Full access to the Nexus filesystem API

## Quick Start

### Installing a Plugin

```bash
# Install from PyPI
pip install nexus-plugin-anthropic

# Plugin is automatically discovered
nexus plugins list
```

### Using a Plugin

```bash
# Use plugin commands
nexus anthropic upload-skill my-skill
nexus anthropic list-skills
```

### Creating a Plugin

```python
from nexus.plugins import NexusPlugin, PluginMetadata

class MyPlugin(NexusPlugin):
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="my-plugin",
            version="1.0.0",
            description="My custom plugin",
            author="Your Name"
        )

    def commands(self) -> dict[str, Callable]:
        return {
            "hello": self.hello_command
        }

    async def hello_command(self, name: str = "World"):
        print(f"Hello, {name}!")
```

## Plugin Structure

All plugins inherit from `NexusPlugin` and implement:

```python
class NexusPlugin(ABC):
    """Base class for all Nexus plugins."""

    @abstractmethod
    def metadata(self) -> PluginMetadata:
        """Return plugin metadata (required)."""
        pass

    def commands(self) -> dict[str, Callable]:
        """Return CLI commands (optional)."""
        return {}

    def hooks(self) -> dict[str, Callable]:
        """Return lifecycle hooks (optional)."""
        return {}

    async def initialize(self, config: dict[str, Any]) -> None:
        """Initialize plugin with configuration."""
        self._config = config

    async def shutdown(self) -> None:
        """Cleanup when plugin is disabled."""
        pass
```

## Plugin Metadata

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

## Documentation

<div class="grid cards" markdown>

- :material-rocket: **[Creating Plugins](creating-plugins.md)**

    Learn how to create custom plugins from scratch

- :material-hook: **[Lifecycle Hooks](hooks.md)**

    React to filesystem events with hooks

- :material-database: **[Plugin Registry](registry.md)**

    Discover and manage plugins

- :material-code-braces: **[Examples](examples.md)**

    Real-world plugin examples

</div>

## Available Hooks

Plugins can react to these filesystem events:

- `before_write` / `after_write` - File write operations
- `before_read` / `after_read` - File read operations
- `before_delete` / `after_delete` - File delete operations
- `before_mkdir` / `after_mkdir` - Directory creation
- `before_copy` / `after_copy` - File copy operations

[Learn more about hooks →](hooks.md)

## Plugin Configuration

Plugins can be configured in `~/.nexus/plugins/<plugin-name>/config.yaml`:

```yaml
# ~/.nexus/plugins/my-plugin/config.yaml
api_key: "sk-..."
enabled: true
cache_dir: "/tmp/my-plugin"
hook_priority:
  before_write: 10
custom_setting: "value"
```

Access configuration in your plugin:

```python
async def initialize(self, config: dict[str, Any]) -> None:
    api_key = self.get_config("api_key")
    cache_dir = self.get_config("cache_dir", "/tmp/default")
```

## CLI Integration

### List Plugins

```bash
nexus plugins list
```

### Use Plugin Commands

```bash
nexus <plugin-name> <command> [arguments]

# Examples:
nexus anthropic upload-skill my-skill
nexus my-plugin hello --name Alice
```

### Get Command Help

```bash
nexus <plugin-name> <command> --help
```

## Official Plugins

### Anthropic Plugin

Integration with Claude Skills API - upload, download, and manage skills.

```bash
pip install nexus-plugin-anthropic

# Upload skill to Claude
nexus anthropic upload-skill my-skill

# Import from GitHub
nexus anthropic import-github project-manager
```

**[View Documentation →](anthropic.md)**

### Firecrawl Plugin

Production-grade web scraping with JS rendering and anti-bot detection.

```bash
pip install nexus-plugin-firecrawl

# Scrape a page
nexus firecrawl scrape https://docs.example.com/api

# Crawl entire site
nexus firecrawl crawl https://docs.example.com --max-pages 100
```

**[View Documentation →](firecrawl.md)**

### Skill Seekers Plugin

Generate Nexus skills from documentation URLs automatically.

```bash
pip install nexus-plugin-skill-seekers

# Generate skill from URL
nexus skill-seekers generate https://docs.stripe.com/api

# Batch generate from URLs file
nexus skill-seekers batch urls.txt
```

**[View Documentation →](skill-seekers.md)**

## Next Steps

1. **[Create your first plugin](creating-plugins.md)** - Step-by-step guide
2. **[Learn about hooks](hooks.md)** - React to filesystem events
3. **[Explore examples](examples.md)** - See real-world plugins
4. **[Understand the registry](registry.md)** - Manage plugins

## See Also

- [Skills API](../skills/index.md) - AI skills management
- [CLI Reference](../cli-reference.md) - Command-line interface
- [Core API](../core-api.md) - Core filesystem API
