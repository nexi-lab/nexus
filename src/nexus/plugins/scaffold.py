"""Plugin scaffold generator for creating new Nexus plugins.

Generates a complete plugin project structure with pyproject.toml,
entry points, plugin class, tests, and README.
"""

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Valid plugin types and their descriptions
PLUGIN_TYPES = {
    "generic": "General-purpose plugin with commands and hooks",
    "storage": "Storage backend plugin (implements Backend protocol)",
    "parser": "Content parser plugin (implements ParserProvider protocol)",
}


def scaffold_plugin(
    name: str,
    output_dir: Path,
    *,
    plugin_type: str = "generic",
    author: str = "Nexus Team",
    description: str = "",
) -> dict[str, Any]:
    """Generate a new plugin project from scaffold template.

    Args:
        name: Plugin name (e.g., "my-backend"). Used for directory and package names.
        output_dir: Parent directory where plugin project will be created.
        plugin_type: One of "generic", "storage", "parser".
        author: Plugin author name.
        description: Plugin description.

    Returns:
        Dict with created file paths and metadata.

    Raises:
        ValueError: If name is invalid or plugin_type is unknown.
        FileExistsError: If the target directory already exists.
    """
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        raise ValueError(
            f"Invalid plugin name: {name!r}. Use alphanumeric characters, hyphens, or underscores."
        )

    if plugin_type not in PLUGIN_TYPES:
        raise ValueError(
            f"Unknown plugin type: {plugin_type!r}. Choose from: {', '.join(PLUGIN_TYPES)}"
        )

    # Normalize names
    package_name = f"nexus-plugin-{name}"
    module_name = f"nexus_{name.replace('-', '_')}"
    class_name = "".join(word.capitalize() for word in name.split("-")) + "Plugin"
    description = description or f"Nexus {name} plugin"

    # Target directory
    project_dir = output_dir / package_name
    if project_dir.exists():
        raise FileExistsError(f"Directory already exists: {project_dir}")

    # Create directory structure
    src_dir = project_dir / "src" / module_name
    tests_dir = project_dir / "tests"

    src_dir.mkdir(parents=True)
    tests_dir.mkdir(parents=True)

    files_created: list[str] = []

    # pyproject.toml
    pyproject = _generate_pyproject(package_name, module_name, class_name, name, author, description)
    _write(project_dir / "pyproject.toml", pyproject, files_created)

    # __init__.py
    init_content = f'"""Nexus {name} plugin."""\n\nfrom {module_name}.plugin import {class_name}\n\n__all__ = ["{class_name}"]\n'
    _write(src_dir / "__init__.py", init_content, files_created)

    # plugin.py
    plugin_content = _generate_plugin_class(
        module_name, class_name, name, author, description, plugin_type
    )
    _write(src_dir / "plugin.py", plugin_content, files_created)

    # tests/__init__.py
    _write(tests_dir / "__init__.py", "", files_created)

    # tests/test_plugin.py
    test_content = _generate_test_file(module_name, class_name, name)
    _write(tests_dir / "test_plugin.py", test_content, files_created)

    # README.md
    readme = _generate_readme(package_name, name, description, plugin_type)
    _write(project_dir / "README.md", readme, files_created)

    logger.info("Created plugin scaffold at %s (%d files)", project_dir, len(files_created))

    return {
        "project_dir": str(project_dir),
        "package_name": package_name,
        "module_name": module_name,
        "class_name": class_name,
        "files_created": files_created,
        "plugin_type": plugin_type,
    }


def _write(path: Path, content: str, tracker: list[str]) -> None:
    """Write content to file and track it."""
    path.write_text(content)
    tracker.append(str(path))


def _generate_pyproject(
    package_name: str,
    module_name: str,
    class_name: str,
    name: str,
    author: str,
    description: str,
) -> str:
    return f'''[build-system]
requires = ["setuptools>=61.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "{package_name}"
version = "0.1.0"
description = "{description}"
readme = "README.md"
requires-python = ">=3.11"
license = {{text = "Apache-2.0"}}
authors = [
    {{name = "{author}"}}
]
keywords = ["nexus", "plugin", "{name}"]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: Apache Software License",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.12",
]

dependencies = [
    "click>=8.1.0",
    "rich>=13.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.4.0",
    "pytest-asyncio>=0.21.0",
    "ruff>=0.0.287",
]

[project.entry-points."nexus.plugins"]
{name} = "{module_name}.plugin:{class_name}"

[tool.setuptools.packages.find]
where = ["src"]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "W"]
'''


def _generate_plugin_class(
    _module_name: str,  # reserved for future use (e.g., intra-package imports)
    class_name: str,
    name: str,
    author: str,
    description: str,
    plugin_type: str,
) -> str:
    base = f'''"""Nexus {name} plugin implementation."""

from typing import Callable

from nexus.plugins import NexusPlugin, PluginMetadata


class {class_name}(NexusPlugin):
    """{description}."""

    def metadata(self) -> PluginMetadata:
        """Return plugin metadata."""
        return PluginMetadata(
            name="{name}",
            version="0.1.0",
            description="{description}",
            author="{author}",
            requires=[],
        )

    def commands(self) -> dict[str, Callable]:
        """Return plugin commands."""
        return {{
            "hello": self.hello_command,
        }}

    def hooks(self) -> dict:
        """Return plugin hooks."""
        return {{}}

    async def hello_command(self, name: str = "World") -> None:
        """Example command."""
        from rich.console import Console

        console = Console()
        console.print(f"[green]Hello, {{name}}! From {name} plugin.[/green]")
'''

    if plugin_type == "storage":
        base += '''

    # Storage backend methods — implement these for a storage plugin:
    #
    # from nexus.backends.backend import Backend
    #
    # class MyBackend(Backend):
    #     async def write_content(self, hash_str: str, data: bytes) -> None: ...
    #     async def read_content(self, hash_str: str) -> bytes: ...
    #     async def delete_content(self, hash_str: str) -> None: ...
    #     async def content_exists(self, hash_str: str) -> bool: ...
'''
    elif plugin_type == "parser":
        base += '''

    # Parser methods — implement these for a parser plugin:
    #
    # from nexus.parsers.providers.base import ParseProvider
    #
    # class MyParser(ParseProvider):
    #     def supported_formats(self) -> list[str]: ...
    #     async def parse(self, content: bytes, format: str) -> str: ...
'''

    return base


def _generate_test_file(module_name: str, class_name: str, name: str) -> str:
    return f'''"""Tests for {name} plugin."""

import pytest

from {module_name}.plugin import {class_name}


class TestPluginMetadata:
    """Test plugin metadata."""

    def test_metadata_name(self) -> None:
        plugin = {class_name}()
        meta = plugin.metadata()
        assert meta.name == "{name}"

    def test_metadata_version(self) -> None:
        plugin = {class_name}()
        meta = plugin.metadata()
        assert meta.version == "0.1.0"


class TestPluginCommands:
    """Test plugin commands."""

    def test_has_commands(self) -> None:
        plugin = {class_name}()
        commands = plugin.commands()
        assert "hello" in commands

    @pytest.mark.asyncio
    async def test_hello_command(self) -> None:
        plugin = {class_name}()
        await plugin.hello_command("Test")


class TestPluginLifecycle:
    """Test plugin lifecycle."""

    @pytest.mark.asyncio
    async def test_initialize(self) -> None:
        plugin = {class_name}()
        await plugin.initialize({{}})
        assert plugin.is_enabled()

    @pytest.mark.asyncio
    async def test_shutdown(self) -> None:
        plugin = {class_name}()
        await plugin.initialize({{}})
        await plugin.shutdown()
'''


def _generate_readme(package_name: str, name: str, description: str, plugin_type: str) -> str:
    return f"""# {package_name}

{description}

## Installation

```bash
pip install {package_name}
```

Or install from source:

```bash
git clone <repository-url>
cd {package_name}
pip install -e ".[dev]"
```

## Usage

Once installed, the plugin is automatically discovered by Nexus:

```bash
# Verify installation
nexus plugins list

# Show plugin details
nexus plugins info {name}

# Run plugin commands
nexus {name} hello
nexus {name} hello --name "Alice"
```

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Lint
ruff check src/
```

## Plugin Type: {plugin_type}

{PLUGIN_TYPES.get(plugin_type, '')}

## License

Apache-2.0
"""
