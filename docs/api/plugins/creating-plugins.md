# Creating Plugins

← [Plugins API](index.md)

This guide walks you through creating a custom Nexus plugin from scratch.

## Plugin Development Workflow

### 1. Create Plugin Package

```bash
# Create plugin directory structure
mkdir nexus-plugin-my
cd nexus-plugin-my

# Create package structure
mkdir -p src/nexus_my_plugin
touch src/nexus_my_plugin/__init__.py
touch src/nexus_my_plugin/plugin.py
touch pyproject.toml
touch README.md
```

### 2. Implement Plugin Class

Create `src/nexus_my_plugin/plugin.py`:

```python
from nexus.plugins import NexusPlugin, PluginMetadata
from typing import Callable, Any

class MyPlugin(NexusPlugin):
    """My custom Nexus plugin."""

    def metadata(self) -> PluginMetadata:
        """Return plugin metadata."""
        return PluginMetadata(
            name="my-plugin",
            version="1.0.0",
            description="My custom plugin for Nexus",
            author="Your Name",
            homepage="https://github.com/yourname/nexus-plugin-my",
            requires=[]  # Optional plugin dependencies
        )

    def commands(self) -> dict[str, Callable]:
        """Register CLI commands."""
        return {
            "hello": self.hello_command,
            "list-files": self.list_files_command,
        }

    def hooks(self) -> dict[str, Callable]:
        """Register lifecycle hooks."""
        return {
            "before_write": self.validate_content,
            "after_write": self.log_write,
        }

    async def initialize(self, config: dict[str, Any]) -> None:
        """Initialize plugin with configuration."""
        self._config = config
        # Perform initialization here
        print(f"Initializing {self.metadata().name}...")

    async def shutdown(self) -> None:
        """Cleanup when plugin is disabled."""
        # Cleanup resources here
        print(f"Shutting down {self.metadata().name}...")

    # ===== Command Implementations =====

    async def hello_command(self, name: str = "World"):
        """Say hello to someone.

        Usage:
            nexus my-plugin hello
            nexus my-plugin hello --name Alice
        """
        print(f"Hello, {name}!")

    async def list_files_command(self, path: str = "/"):
        """List files in Nexus.

        Usage:
            nexus my-plugin list-files
            nexus my-plugin list-files --path /workspace
        """
        if not self.nx:
            print("Error: NexusFS not available")
            return

        files = self.nx.list(path, recursive=True)
        print(f"Files in {path}:")
        for file in files:
            print(f"  {file}")

    # ===== Hook Implementations =====

    async def validate_content(self, context: dict) -> dict | None:
        """Validate content before writing."""
        path = context.get("path", "")
        content = context.get("content", b"")

        # Example validation: warn about large files
        if len(content) > 10 * 1024 * 1024:  # 10MB
            print(f"Warning: Large file write: {path} ({len(content)} bytes)")

        return context  # Continue with write

    async def log_write(self, context: dict) -> dict:
        """Log after writing a file."""
        path = context.get("path", "")
        print(f"[my-plugin] File written: {path}")
        return context
```

Export the plugin class in `src/nexus_my_plugin/__init__.py`:

```python
from nexus_my_plugin.plugin import MyPlugin

__all__ = ["MyPlugin"]
```

### 3. Configure Package

Create `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=65.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "nexus-plugin-my"
version = "1.0.0"
description = "My custom plugin for Nexus"
authors = [{name = "Your Name", email = "your.email@example.com"}]
readme = "README.md"
requires-python = ">=3.9"
dependencies = [
    "nexus-ai-fs>=0.3.0",
]

[project.urls]
Homepage = "https://github.com/yourname/nexus-plugin-my"
Repository = "https://github.com/yourname/nexus-plugin-my"

[project.entry-points."nexus.plugins"]
my-plugin = "nexus_my_plugin:MyPlugin"
```

The entry point format is:
```
<plugin-cli-name> = "<package>:<PluginClass>"
```

### 4. Install and Test

```bash
# Install in development mode
pip install -e .

# Test plugin discovery
python -c "from nexus.plugins import PluginRegistry; r = PluginRegistry(); print(r.discover())"

# Test plugin commands
nexus my-plugin hello
nexus my-plugin hello --name Alice
nexus my-plugin list-files --path /
```

## Creating Commands

Commands are async functions that become CLI commands.

### Basic Command

```python
async def my_command(self, arg1: str, arg2: int = 10):
    """Command with arguments.

    Args:
        arg1: Required string argument
        arg2: Optional integer argument (default: 10)

    Usage:
        nexus my-plugin my-command <arg1>
        nexus my-plugin my-command <arg1> --arg2 20
    """
    print(f"arg1={arg1}, arg2={arg2}")
```

### Command with NexusFS Access

```python
async def process_file(self, path: str):
    """Process a file using NexusFS.

    Usage:
        nexus my-plugin process-file /data/file.txt
    """
    if not self.nx:
        print("Error: NexusFS not available")
        return

    try:
        # Read file
        content = self.nx.read(path)
        print(f"Processing {path}: {len(content)} bytes")

        # Process content
        processed = content.upper()

        # Write result
        output_path = f"{path}.processed"
        self.nx.write(output_path, processed)
        print(f"Wrote result to {output_path}")

    except Exception as e:
        print(f"Error: {e}")
```

### Command with Rich Output

```python
from rich.console import Console
from rich.table import Table

async def show_stats(self):
    """Show statistics with rich formatting."""
    console = Console()

    if not self.nx:
        console.print("[red]Error: NexusFS not available[/red]")
        return

    # Collect statistics
    files = self.nx.list("/", recursive=True)
    total_size = sum(len(self.nx.read(f)) for f in files)

    # Create table
    table = Table(title="Nexus Statistics")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Total Files", str(len(files)))
    table.add_row("Total Size", f"{total_size / 1024 / 1024:.2f} MB")

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
    if self.is_piped_input():
        # Read JSON from stdin
        try:
            data = self.read_json_input()

            # Transform data
            result = {
                "transformed": True,
                "original": data,
                "timestamp": datetime.now().isoformat()
            }

            # Write JSON to stdout
            self.write_json_output(result)

        except json.JSONDecodeError:
            print("Error: Invalid JSON input", file=sys.stderr)
    else:
        print("Error: This command requires piped input")
```

## Configuration

### Reading Configuration

```python
async def initialize(self, config: dict[str, Any]) -> None:
    """Initialize with configuration."""
    self._config = config

    # Get configuration values
    api_key = self.get_config("api_key")
    cache_dir = self.get_config("cache_dir", "/tmp/my-plugin")
    enabled_features = self.get_config("features", [])

    # Validate configuration
    if not api_key:
        raise ValueError("api_key is required in configuration")

    # Use configuration
    self._setup_api(api_key)
    self._cache_dir = Path(cache_dir)
    self._cache_dir.mkdir(parents=True, exist_ok=True)
```

### Configuration File

Users configure your plugin in `~/.nexus/plugins/my-plugin/config.yaml`:

```yaml
# ~/.nexus/plugins/my-plugin/config.yaml
api_key: "sk-..."
cache_dir: "/tmp/my-plugin"
features:
  - feature1
  - feature2
hook_priority:
  before_write: 10
  after_write: 5
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

    except FileNotFoundError:
        print(f"Error: File not found: {path}")
    except PermissionError:
        print(f"Error: Permission denied: {path}")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
```

### 2. Configuration Validation

```python
async def initialize(self, config: dict[str, Any]) -> None:
    """Initialize with configuration validation."""
    self._config = config

    # Validate required config
    required_keys = ["api_key", "endpoint"]
    for key in required_keys:
        if not self.get_config(key):
            raise ValueError(f"{key} is required in plugin configuration")

    # Validate config values
    cache_dir = self.get_config("cache_dir", "/tmp/default")
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir, exist_ok=True)
```

### 3. Resource Cleanup

```python
async def initialize(self, config: dict[str, Any]) -> None:
    """Initialize resources."""
    self._config = config
    self._client = SomeClient(api_key=config["api_key"])
    self._cache = {}
    print("Initialized my-plugin")

async def shutdown(self) -> None:
    """Cleanup resources."""
    if hasattr(self, "_client"):
        await self._client.close()

    if hasattr(self, "_cache"):
        self._cache.clear()

    print("Shut down my-plugin")
```

### 4. Type Hints

```python
from typing import Callable, Any, Optional

async def my_command(
    self,
    path: str,
    limit: int = 10,
    verbose: bool = False
) -> None:
    """Command with type hints."""
    ...
```

### 5. Documentation

```python
async def my_command(self, path: str, recursive: bool = False):
    """Process files in a directory.

    This command processes all files in the specified directory
    and generates a report.

    Args:
        path: Directory path to process
        recursive: Whether to process subdirectories (default: False)

    Usage:
        nexus my-plugin my-command /data
        nexus my-plugin my-command /data --recursive

    Examples:
        # Process files in /data directory
        nexus my-plugin my-command /data

        # Process files recursively
        nexus my-plugin my-command /data --recursive
    """
    ...
```

## Testing Your Plugin

### Unit Tests

Create `tests/test_plugin.py`:

```python
import pytest
from nexus_my_plugin import MyPlugin
from nexus.plugins import PluginMetadata

@pytest.mark.asyncio
async def test_plugin_metadata():
    """Test plugin metadata."""
    plugin = MyPlugin()
    metadata = plugin.metadata()

    assert isinstance(metadata, PluginMetadata)
    assert metadata.name == "my-plugin"
    assert metadata.version == "1.0.0"

@pytest.mark.asyncio
async def test_hello_command():
    """Test hello command."""
    plugin = MyPlugin()
    # Test command (capture output)
    await plugin.hello_command("Alice")
    # Assert expected behavior

@pytest.mark.asyncio
async def test_with_nexus_fs(tmp_path):
    """Test plugin with NexusFS."""
    from nexus import connect

    # Create test Nexus instance
    nx = connect(config={"data_dir": str(tmp_path)})

    # Create plugin with NexusFS
    plugin = MyPlugin(nx)
    await plugin.initialize({})

    # Test commands
    await plugin.list_files_command("/")

    # Cleanup
    nx.close()
```

### Integration Tests

```python
@pytest.mark.asyncio
async def test_plugin_integration(tmp_path):
    """Test plugin with full Nexus integration."""
    from nexus import connect
    from nexus.plugins import PluginRegistry

    # Setup
    nx = connect(config={"data_dir": str(tmp_path)})
    registry = PluginRegistry(nx)

    # Register plugin
    plugin = MyPlugin(nx)
    registry.register(plugin)

    # Test plugin is registered
    assert registry.get_plugin("my-plugin") is not None

    # Test commands
    await plugin.hello_command("Test")

    # Cleanup
    nx.close()
```

## Publishing Your Plugin

### 1. Prepare for Release

```bash
# Update version in pyproject.toml
# Update README.md with usage instructions
# Add LICENSE file
# Add CHANGELOG.md
```

### 2. Build Package

```bash
# Install build tools
pip install build twine

# Build distribution
python -m build

# Check distribution
twine check dist/*
```

### 3. Publish to PyPI

```bash
# Test on TestPyPI first
twine upload --repository testpypi dist/*

# Publish to PyPI
twine upload dist/*
```

### 4. Document Usage

Update README.md:

```markdown
# nexus-plugin-my

My custom plugin for Nexus.

## Installation

\`\`\`bash
pip install nexus-plugin-my
\`\`\`

## Usage

\`\`\`bash
# Hello command
nexus my-plugin hello --name Alice

# List files
nexus my-plugin list-files --path /workspace
\`\`\`

## Configuration

Create `~/.nexus/plugins/my-plugin/config.yaml`:

\`\`\`yaml
api_key: "your-api-key"
cache_dir: "/tmp/my-plugin"
\`\`\`
```

## Troubleshooting

### Plugin Not Discovered

```bash
# Check entry points
python -c "import importlib.metadata; print(list(importlib.metadata.entry_points(group='nexus.plugins')))"

# Reinstall plugin
pip uninstall nexus-plugin-my
pip install -e .

# Clear Python cache
find . -type d -name __pycache__ -exec rm -rf {} +
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

### Command Not Working

```bash
# Check plugin registration
nexus plugins list

# Get command help
nexus my-plugin <command> --help

# Check for errors
nexus my-plugin <command> --verbose
```

## Next Steps

- **[Learn about hooks](hooks.md)** - React to filesystem events
- **[Explore examples](examples.md)** - See real-world plugins
- **[Understand the registry](registry.md)** - Plugin management

## See Also

- [Hooks Documentation](hooks.md)
- [Plugin Registry](registry.md)
- [Plugin Examples](examples.md)
