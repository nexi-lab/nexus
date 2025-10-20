# Plugin Development Guide

This guide explains how to create plugins for Nexus to extend its functionality while maintaining vendor neutrality.

## Table of Contents

- [Overview](#overview)
- [Quick Start](#quick-start)
- [Plugin Architecture](#plugin-architecture)
- [Creating Your First Plugin](#creating-your-first-plugin)
- [Plugin API Reference](#plugin-api-reference)
- [Testing Your Plugin](#testing-your-plugin)
- [Publishing Your Plugin](#publishing-your-plugin)
- [Examples](#examples)
- [Best Practices](#best-practices)

## Overview

Nexus plugins allow you to:

- **Add custom CLI commands** - Extend `nexus` CLI with plugin-specific commands
- **Hook into lifecycle events** - React to file operations (before_write, after_read, etc.)
- **Integrate external services** - Connect to APIs, databases, or third-party tools
- **Extend functionality** - Add new features without modifying core Nexus

### Plugin Discovery

Nexus uses **Python entry points** for automatic plugin discovery. When you install a plugin package, Nexus automatically finds and loads it using:

```python
import importlib.metadata

entry_points = importlib.metadata.entry_points()
nexus_plugins = entry_points.select(group='nexus.plugins')
```

No manual registration needed - just install the package!

## Quick Start

### 1. Create Plugin Package Structure

```bash
my-plugin/
├── pyproject.toml           # Package configuration
├── README.md                # Plugin documentation
├── src/
│   └── nexus_my_plugin/     # Plugin source code
│       ├── __init__.py
│       └── plugin.py        # Main plugin class
├── tests/                   # Plugin tests
│   └── test_plugin.py
└── examples/                # Usage examples
    ├── cli_demo.sh
    └── sdk_demo.py
```

### 2. Define Plugin Metadata

Create `pyproject.toml`:

```toml
[project]
name = "nexus-plugin-my-plugin"
version = "0.1.0"
description = "My awesome Nexus plugin"
requires-python = ">=3.11"

dependencies = [
    "click>=8.1.0",
    "rich>=13.0.0",
]

# Register plugin entry point
[project.entry-points."nexus.plugins"]
my-plugin = "nexus_my_plugin.plugin:MyPlugin"
```

### 3. Implement Plugin Class

Create `src/nexus_my_plugin/plugin.py`:

```python
"""My awesome plugin for Nexus."""

from typing import Any, Callable, Optional
from rich.console import Console

from nexus.plugins import NexusPlugin, PluginMetadata
from nexus.core.nexus_fs import NexusFS

console = Console()


class MyPlugin(NexusPlugin):
    """My custom plugin implementation."""

    def metadata(self) -> PluginMetadata:
        """Return plugin metadata."""
        return PluginMetadata(
            name="my-plugin",
            version="0.1.0",
            description="My awesome Nexus plugin",
            author="Your Name",
            homepage="https://github.com/yourusername/nexus-plugin-my-plugin",
        )

    def commands(self) -> dict[str, Callable]:
        """Return CLI commands provided by this plugin."""
        return {
            "hello": self.hello_command,
            "process": self.process_command,
        }

    async def hello_command(self, name: str = "World") -> None:
        """Say hello!

        Args:
            name: Name to greet (default: World)
        """
        console.print(f"[green]Hello, {name}![/green]")
        console.print(f"Plugin version: {self.metadata().version}")

    async def process_command(self, path: str) -> None:
        """Process a file from Nexus.

        Args:
            path: Virtual path in Nexus filesystem
        """
        if not self.nx:
            console.print("[red]Error: NexusFS not available[/red]")
            return

        try:
            content = self.nx.read(path)
            console.print(f"[cyan]Processing:[/cyan] {path}")
            console.print(f"[green]Size:[/green] {len(content)} bytes")

            # Do something with the content
            # ...

            console.print("[green]✓ Processing complete[/green]")
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
```

### 4. Install and Test

```bash
# Install in development mode
cd my-plugin
pip install -e .

# Verify plugin is loaded
nexus plugins list

# Test plugin commands
nexus my-plugin hello
nexus my-plugin hello Alice
nexus my-plugin process /workspace/data.txt
```

## Plugin Architecture

### Base Plugin Class

All plugins inherit from `NexusPlugin`:

```python
from nexus.plugins import NexusPlugin, PluginMetadata

class MyPlugin(NexusPlugin):
    """Your plugin implementation."""

    def __init__(self, nexus_fs: Optional[NexusFS] = None):
        """Initialize plugin.

        Args:
            nexus_fs: NexusFS instance (may be None)
        """
        super().__init__(nexus_fs)
        # Your initialization code

    def metadata(self) -> PluginMetadata:
        """Return plugin metadata (required)."""
        return PluginMetadata(...)

    def commands(self) -> dict[str, Callable]:
        """Return CLI commands (optional)."""
        return {}

    def hooks(self) -> dict[str, Callable]:
        """Return lifecycle hooks (optional)."""
        return {}
```

### Plugin Lifecycle

1. **Discovery** - Nexus scans entry points when CLI starts
2. **Instantiation** - Plugin class is instantiated with `NexusFS` instance
3. **Configuration** - Plugin config loaded from `~/.nexus/plugins/<name>/config.yaml`
4. **Registration** - Commands and hooks registered with Nexus
5. **Execution** - Commands/hooks called as needed
6. **Shutdown** - Plugin cleanup (if needed)

### Accessing NexusFS

Plugins have access to the Nexus filesystem via `self.nx`:

```python
class MyPlugin(NexusPlugin):
    async def my_command(self):
        if not self.nx:
            console.print("[red]NexusFS not available[/red]")
            return

        # Read files
        content = self.nx.read("/workspace/data.txt")

        # Write files
        self.nx.write("/workspace/output.txt", b"result")

        # List files
        files = self.nx.ls("/workspace")

        # Search files
        matches = self.nx.grep("pattern", file_pattern="*.py")
```

## Creating Your First Plugin

Let's create a complete plugin that generates documentation summaries.

### Step 1: Setup Package

```bash
mkdir nexus-plugin-doc-summarizer
cd nexus-plugin-doc-summarizer
mkdir -p src/nexus_doc_summarizer
mkdir -p tests
mkdir -p examples
```

### Step 2: Create pyproject.toml

```toml
[project]
name = "nexus-plugin-doc-summarizer"
version = "0.1.0"
description = "Generate summaries of documentation files"
requires-python = ">=3.11"
authors = [
    {name = "Your Name", email = "you@example.com"}
]

dependencies = [
    "click>=8.1.0",
    "rich>=13.0.0",
    "anthropic>=0.8.0",  # For LLM integration
]

[project.entry-points."nexus.plugins"]
doc-summarizer = "nexus_doc_summarizer.plugin:DocSummarizerPlugin"

[build-system]
requires = ["setuptools>=61.0", "wheel"]
build-backend = "setuptools.build_meta"
```

### Step 3: Implement Plugin

Create `src/nexus_doc_summarizer/plugin.py`:

```python
"""Documentation summarizer plugin for Nexus."""

import os
from pathlib import Path
from typing import Any, Callable, Optional

from anthropic import Anthropic
from rich.console import Console
from rich.progress import track

from nexus.plugins import NexusPlugin, PluginMetadata

console = Console()


class DocSummarizerPlugin(NexusPlugin):
    """Plugin for generating documentation summaries."""

    def metadata(self) -> PluginMetadata:
        """Return plugin metadata."""
        return PluginMetadata(
            name="doc-summarizer",
            version="0.1.0",
            description="Generate summaries of documentation files",
            author="Your Name",
            homepage="https://github.com/yourusername/nexus-plugin-doc-summarizer",
        )

    def commands(self) -> dict[str, Callable]:
        """Return plugin commands."""
        return {
            "summarize": self.summarize_file,
            "batch": self.batch_summarize,
        }

    async def summarize_file(
        self,
        path: str,
        output: Optional[str] = None,
        model: str = "claude-3-5-sonnet-20241022",
    ) -> None:
        """Summarize a single documentation file.

        Args:
            path: Path to file in Nexus
            output: Output path for summary (optional)
            model: Claude model to use
        """
        if not self.nx:
            console.print("[red]Error: NexusFS not available[/red]")
            return

        # Get API key from config or environment
        api_key = self.get_config("api_key") or os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            console.print("[red]Error: ANTHROPIC_API_KEY not set[/red]")
            return

        try:
            # Read file from Nexus
            console.print(f"[cyan]Reading:[/cyan] {path}")
            content = self.nx.read(path).decode('utf-8')

            # Generate summary
            console.print(f"[cyan]Generating summary...[/cyan]")
            client = Anthropic(api_key=api_key)

            response = client.messages.create(
                model=model,
                max_tokens=1024,
                messages=[{
                    "role": "user",
                    "content": f"Summarize this documentation in 2-3 paragraphs:\n\n{content}"
                }]
            )

            summary = response.content[0].text

            # Save summary
            if output:
                output_path = output
            else:
                output_path = str(Path(path).with_suffix('.summary.md'))

            self.nx.write(output_path, summary.encode('utf-8'))
            console.print(f"[green]✓ Summary saved to:[/green] {output_path}")
            console.print(f"\n{summary}")

        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")

    async def batch_summarize(
        self,
        pattern: str = "**/*.md",
        output_dir: str = "/workspace/summaries/",
    ) -> None:
        """Summarize multiple documentation files.

        Args:
            pattern: Glob pattern to match files
            output_dir: Directory for summaries
        """
        if not self.nx:
            console.print("[red]Error: NexusFS not available[/red]")
            return

        try:
            # Find all matching files
            files = self.nx.glob(pattern)
            console.print(f"[cyan]Found {len(files)} files matching pattern[/cyan]")

            # Create output directory
            self.nx.mkdir(output_dir, parents=True, exist_ok=True)

            # Process each file
            for file_path in track(files, description="Summarizing..."):
                output_name = Path(file_path).name.replace('.md', '.summary.md')
                output_path = f"{output_dir}/{output_name}"

                await self.summarize_file(file_path, output=output_path)

            console.print(f"[green]✓ Processed {len(files)} files[/green]")

        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
```

### Step 4: Create README

Create `README.md`:

```markdown
# Nexus Plugin: Doc Summarizer

Generate AI-powered summaries of documentation files.

## Installation

```bash
pip install nexus-plugin-doc-summarizer
```

## Configuration

Set your Anthropic API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-api03-...
```

Or create `~/.nexus/plugins/doc-summarizer/config.yaml`:

```yaml
api_key: sk-ant-api03-...
model: claude-3-5-sonnet-20241022
```

## Usage

```bash
# Summarize a single file
nexus doc-summarizer summarize /workspace/README.md

# Summarize with custom output path
nexus doc-summarizer summarize /workspace/README.md --output /summaries/readme-summary.md

# Batch summarize all markdown files
nexus doc-summarizer batch --pattern "**/*.md" --output-dir /summaries/
```
```

### Step 5: Install and Test

```bash
# Install in development mode
pip install -e .

# Test commands
nexus plugins list
nexus doc-summarizer summarize /workspace/README.md
```

## Plugin API Reference

### NexusPlugin Base Class

```python
class NexusPlugin:
    """Base class for Nexus plugins."""

    def __init__(self, nexus_fs: Optional[NexusFS] = None):
        """Initialize plugin with NexusFS instance."""
        self._nexus_fs = nexus_fs
        self._config = {}
        self._enabled = True

    @property
    def nx(self) -> Optional[NexusFS]:
        """Access to NexusFS instance."""
        return self._nexus_fs

    def metadata(self) -> PluginMetadata:
        """Return plugin metadata (REQUIRED)."""
        raise NotImplementedError

    def commands(self) -> dict[str, Callable]:
        """Return CLI commands (OPTIONAL)."""
        return {}

    def hooks(self) -> dict[str, Callable]:
        """Return lifecycle hooks (OPTIONAL)."""
        return {}

    def get_config(self, key: str, default: Any = None) -> Any:
        """Get configuration value."""
        return self._config.get(key, default)

    def is_enabled(self) -> bool:
        """Check if plugin is enabled."""
        return self._enabled

    def enable(self) -> None:
        """Enable the plugin."""
        self._enabled = True

    def disable(self) -> None:
        """Disable the plugin."""
        self._enabled = False
```

### PluginMetadata

```python
@dataclass
class PluginMetadata:
    """Plugin metadata."""

    name: str                          # Plugin identifier
    version: str                       # Semantic version
    description: str                   # Short description
    author: str                        # Author name
    homepage: Optional[str] = None     # Plugin homepage URL
    requires: Optional[list[str]] = None  # Required dependencies
```

### Lifecycle Hooks

Plugins can hook into file operations:

```python
from nexus.plugins import HookType

class MyPlugin(NexusPlugin):
    def hooks(self) -> dict[str, Callable]:
        """Register lifecycle hooks."""
        return {
            HookType.BEFORE_WRITE.value: self.before_write,
            HookType.AFTER_READ.value: self.after_read,
            HookType.BEFORE_DELETE.value: self.before_delete,
        }

    async def before_write(self, context: dict) -> Optional[dict]:
        """Called before writing a file.

        Args:
            context: {
                'path': str,           # Virtual path
                'content': bytes,      # File content
                'metadata': dict,      # File metadata
            }

        Returns:
            Modified context or None to cancel operation
        """
        # Validate or transform content
        if context['path'].endswith('.json'):
            # Validate JSON
            import json
            try:
                json.loads(context['content'])
            except json.JSONDecodeError:
                console.print("[red]Invalid JSON[/red]")
                return None  # Cancel write

        return context  # Allow write

    async def after_read(self, context: dict) -> Optional[dict]:
        """Called after reading a file.

        Args:
            context: {
                'path': str,           # Virtual path
                'content': bytes,      # File content
            }

        Returns:
            Modified context with updated content
        """
        # Transform content on read
        if context['path'].endswith('.env'):
            # Decrypt environment file
            context['content'] = self._decrypt(context['content'])

        return context
```

### Available Hook Types

```python
class HookType(Enum):
    """Available lifecycle hooks."""

    BEFORE_WRITE = "before_write"      # Before writing file
    AFTER_WRITE = "after_write"        # After writing file
    BEFORE_READ = "before_read"        # Before reading file
    AFTER_READ = "after_read"          # After reading file
    BEFORE_DELETE = "before_delete"    # Before deleting file
    AFTER_DELETE = "after_delete"      # After deleting file
    BEFORE_LIST = "before_list"        # Before listing directory
    AFTER_LIST = "after_list"          # After listing directory
```

## Testing Your Plugin

### Unit Tests

Create `tests/test_plugin.py`:

```python
"""Tests for doc-summarizer plugin."""

import pytest
from unittest.mock import Mock, patch

from nexus_doc_summarizer.plugin import DocSummarizerPlugin


def test_plugin_metadata():
    """Test plugin metadata."""
    plugin = DocSummarizerPlugin()
    metadata = plugin.metadata()

    assert metadata.name == "doc-summarizer"
    assert metadata.version == "0.1.0"
    assert "summarize" in metadata.description.lower()


def test_plugin_commands():
    """Test plugin provides expected commands."""
    plugin = DocSummarizerPlugin()
    commands = plugin.commands()

    assert "summarize" in commands
    assert "batch" in commands


@pytest.mark.asyncio
async def test_summarize_file():
    """Test file summarization."""
    # Create mock NexusFS
    mock_nx = Mock()
    mock_nx.read.return_value = b"# Test Doc\n\nSome content here."
    mock_nx.write.return_value = None

    # Create plugin with mock
    plugin = DocSummarizerPlugin(nexus_fs=mock_nx)

    # Mock Anthropic API
    with patch('nexus_doc_summarizer.plugin.Anthropic') as mock_anthropic:
        mock_client = Mock()
        mock_response = Mock()
        mock_response.content = [Mock(text="Test summary")]
        mock_client.messages.create.return_value = mock_response
        mock_anthropic.return_value = mock_client

        # Set API key in config
        plugin._config = {"api_key": "test-key"}

        # Test summarize
        await plugin.summarize_file("/test.md", output="/summary.md")

        # Verify calls
        mock_nx.read.assert_called_once_with("/test.md")
        mock_nx.write.assert_called_once()
```

### Integration Tests

Create `tests/test_integration.py`:

```python
"""Integration tests with real NexusFS."""

import pytest
import tempfile
from pathlib import Path

import nexus
from nexus_doc_summarizer.plugin import DocSummarizerPlugin


@pytest.fixture
def nx():
    """Create temporary Nexus filesystem."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "nexus-data"
        nx = nexus.connect(config={"data_dir": str(data_dir)})
        yield nx
        nx.close()


@pytest.mark.asyncio
async def test_plugin_with_nexus(nx):
    """Test plugin with real NexusFS."""
    # Create test file
    nx.write("/workspace/test.md", b"# Test\n\nSome content.")

    # Create plugin
    plugin = DocSummarizerPlugin(nexus_fs=nx)
    plugin._config = {"api_key": "test-key"}

    # Test (will fail without real API key, but tests integration)
    # await plugin.summarize_file("/workspace/test.md")
```

### Run Tests

```bash
# Install test dependencies
pip install -e ".[test]"

# Run tests
pytest

# Run with coverage
pytest --cov=nexus_doc_summarizer --cov-report=html
```

## Publishing Your Plugin

### 1. Prepare Package

```bash
# Update version in pyproject.toml
# Update README.md with usage examples
# Ensure tests pass
pytest

# Build package
python -m build
```

### 2. Publish to PyPI

```bash
# Install twine
pip install twine

# Upload to PyPI
twine upload dist/*
```

### 3. Document Installation

Update README with installation instructions:

```markdown
## Installation

```bash
# From PyPI
pip install nexus-plugin-doc-summarizer

# Or via Nexus
nexus plugins install doc-summarizer
```
```

### 4. Submit to Plugin Registry

(Future) Submit your plugin to the official Nexus plugin registry for discoverability.

## Examples

See real-world plugin implementations:

### First-Party Plugins

1. **[nexus-plugin-anthropic](../nexus-plugin-anthropic/)** - Claude Skills API integration
   - Upload/download skills to Claude
   - Browse and import GitHub skills
   - Comprehensive API integration example

2. **[nexus-plugin-skill-seekers](../nexus-plugin-skill-seekers/)** - Documentation scraper
   - Scrape documentation websites
   - Generate SKILL.md files
   - Batch processing workflows

### Plugin Examples Directory

Each plugin includes comprehensive examples:

```bash
# Anthropic plugin examples
cd nexus-plugin-anthropic/examples
./cli_examples.sh
python python_sdk_examples.py

# Skill Seekers plugin examples
cd nexus-plugin-skill-seekers/examples
./skill_seekers_cli_demo.sh
python skill_seekers_sdk_demo.py
```

## Best Practices

### 1. Follow Naming Convention

- Package name: `nexus-plugin-{name}`
- Module name: `nexus_{name}`
- Entry point name: `{name}` (hyphenated)

### 2. Provide Clear Commands

```python
async def my_command(self, arg: str, option: bool = False) -> None:
    """Clear docstring explaining what the command does.

    Args:
        arg: Description of required argument
        option: Description of optional flag (default: False)
    """
```

### 3. Handle Errors Gracefully

```python
try:
    content = self.nx.read(path)
except FileNotFoundError:
    console.print(f"[red]File not found: {path}[/red]")
    return
except Exception as e:
    console.print(f"[red]Error: {e}[/red]")
    import traceback
    traceback.print_exc()
    return
```

### 4. Use Configuration

Support both config files and environment variables:

```python
def get_api_key(self) -> Optional[str]:
    """Get API key from config or environment."""
    return (
        self.get_config("api_key") or
        os.getenv("MY_PLUGIN_API_KEY") or
        os.getenv("API_KEY")
    )
```

### 5. Provide Rich Output

Use Rich for beautiful CLI output:

```python
from rich.console import Console
from rich.table import Table
from rich.progress import track

console = Console()

# Tables
table = Table(title="Results")
table.add_column("Name")
table.add_column("Status")
console.print(table)

# Progress bars
for item in track(items, description="Processing..."):
    process(item)
```

### 6. Write Tests

Aim for high test coverage:

```bash
# Unit tests
tests/test_plugin.py          # Plugin class tests
tests/test_commands.py        # Command tests
tests/test_utils.py           # Utility function tests

# Integration tests
tests/test_integration.py     # End-to-end tests

# Run with coverage
pytest --cov=your_plugin --cov-report=html
```

### 7. Document Thoroughly

- Clear README with installation and usage
- Docstrings for all public methods
- Examples directory with working demos
- Configuration options documented

### 8. Version Semantically

Follow semantic versioning (MAJOR.MINOR.PATCH):

- MAJOR: Breaking API changes
- MINOR: New features, backward compatible
- PATCH: Bug fixes, backward compatible

### 9. Maintain Compatibility

- Support Python 3.11+
- Test with multiple Nexus versions
- Document version requirements

### 10. Be a Good Citizen

- Keep plugins lightweight
- Minimize dependencies
- Don't pollute global namespace
- Clean up resources on shutdown

## Troubleshooting

### Plugin Not Discovered

```bash
# Check entry point registration
pip show nexus-plugin-my-plugin

# Verify entry points
python -c "import importlib.metadata; print(list(importlib.metadata.entry_points(group='nexus.plugins')))"

# Reinstall in development mode
pip uninstall nexus-plugin-my-plugin
pip install -e .
```

### Commands Not Showing

```bash
# Verify plugin is loaded
nexus plugins list

# Check plugin is enabled
nexus plugins info my-plugin

# Enable if disabled
nexus plugins enable my-plugin
```

### NexusFS Not Available

Some commands run without NexusFS context:

```python
async def my_command(self):
    if not self.nx:
        console.print("[yellow]Warning: NexusFS not available[/yellow]")
        # Provide alternative behavior
        return
```

## Contributing

We welcome plugin contributions! To contribute:

1. Follow this development guide
2. Add comprehensive tests
3. Document your plugin thoroughly
4. Submit to plugin registry (future)

## Support

- **Documentation**: [Nexus Documentation](https://github.com/nexi-lab/nexus)
- **Issues**: [GitHub Issues](https://github.com/nexi-lab/nexus/issues)
- **Discussions**: [GitHub Discussions](https://github.com/nexi-lab/nexus/discussions)

---

**Ready to build your plugin?** Start with the [Quick Start](#quick-start) and check out our [Examples](#examples)!
