# Plugin Examples

← [Plugins API](index.md)

Real-world examples of Nexus plugins to help you get started.

## 1. File Validator Plugin

Validates file content before writing:

```python
from nexus.plugins import NexusPlugin, PluginMetadata
import json
import ast

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
            try:
                json.loads(content)
            except json.JSONDecodeError as e:
                print(f"❌ Invalid JSON in {path}: {e}")
                return None  # Cancel write

        # Validate Python files
        if path.endswith(".py"):
            try:
                ast.parse(content.decode("utf-8"))
            except SyntaxError as e:
                print(f"❌ Invalid Python in {path}: {e}")
                return None  # Cancel write

        return context
```

## 2. Anthropic Plugin

Official plugin for Claude Skills API integration:

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

    def _get_client(self, api_key: str | None = None):
        """Get Anthropic client."""
        api_key = api_key or self.get_config("api_key") or os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("Anthropic API key not found")
        return anthropic.Anthropic(api_key=api_key)

    async def upload_skill(self, skill_name: str, api_key: str | None = None):
        """Upload a skill to Claude Skills API."""
        from nexus.skills import SkillRegistry, SkillExporter

        client = self._get_client(api_key)

        # Export skill
        registry = SkillRegistry(self.nx)
        await registry.discover()

        exporter = SkillExporter(registry)
        export_path = f"/tmp/{skill_name}.zip"
        await exporter.export_skill(skill_name, export_path)

        # Upload to Claude
        with open(export_path, "rb") as f:
            response = client.beta.skills.create(
                display_title=skill_name,
                files=[("skill.zip", f.read())]
            )

        print(f"✅ Uploaded: {response.id}")
```

[View full source](https://github.com/nexi-lab/nexus-plugin-anthropic)

## 3. Auto-Format Plugin

Automatically formats code files:

```python
from nexus.plugins import NexusPlugin, PluginMetadata
import black
import autopep8

class FormatterPlugin(NexusPlugin):
    """Auto-formats code files."""

    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="formatter",
            version="1.0.0",
            description="Auto-formats code files on write",
            author="Dev Team"
        )

    def hooks(self) -> dict[str, Callable]:
        return {
            "before_write": self.auto_format
        }

    async def auto_format(self, context: dict) -> dict:
        """Auto-format code files."""
        path = context["path"]
        content = context["content"]

        # Format Python with Black
        if path.endswith(".py"):
            try:
                formatted = black.format_str(
                    content.decode("utf-8"),
                    mode=black.Mode()
                )
                context["content"] = formatted.encode("utf-8")
                print(f"✨ Formatted: {path}")
            except Exception as e:
                print(f"Warning: Could not format {path}: {e}")

        return context
```

## 4. Backup Plugin

Creates backups before overwriting files:

```python
from nexus.plugins import NexusPlugin, PluginMetadata
from datetime import datetime

class BackupPlugin(NexusPlugin):
    """Creates backups before overwriting."""

    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="backup",
            version="1.0.0",
            description="Creates backups before overwriting files",
            author="Safety Team"
        )

    def hooks(self) -> dict[str, Callable]:
        return {
            "before_write": self.create_backup
        }

    async def create_backup(self, context: dict) -> dict:
        """Create backup before overwriting."""
        path = context["path"]

        # Skip if file doesn't exist
        if not self.nx or not self.nx.exists(path):
            return context

        # Read current version
        old_content = self.nx.read(path)

        # Create backup with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{path}.backup_{timestamp}"
        self.nx.write(backup_path, old_content)

        print(f"💾 Backup created: {backup_path}")
        return context
```

## 5. Statistics Plugin

Tracks file operations and provides statistics:

```python
from nexus.plugins import NexusPlugin, PluginMetadata
from collections import defaultdict
from rich.console import Console
from rich.table import Table

class StatsPlugin(NexusPlugin):
    """Tracks file operation statistics."""

    def __init__(self, nexus_fs=None):
        super().__init__(nexus_fs)
        self._stats = defaultdict(int)

    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="stats",
            version="1.0.0",
            description="Tracks file operation statistics",
            author="Analytics Team"
        )

    def commands(self) -> dict[str, Callable]:
        return {
            "show": self.show_stats,
            "reset": self.reset_stats,
        }

    def hooks(self) -> dict[str, Callable]:
        return {
            "after_write": self.track_write,
            "after_read": self.track_read,
            "after_delete": self.track_delete,
        }

    async def track_write(self, context: dict) -> dict:
        """Track write operations."""
        self._stats["writes"] += 1
        return context

    async def track_read(self, context: dict) -> dict:
        """Track read operations."""
        self._stats["reads"] += 1
        return context

    async def track_delete(self, context: dict) -> dict:
        """Track delete operations."""
        self._stats["deletes"] += 1
        return context

    async def show_stats(self):
        """Show statistics."""
        console = Console()

        table = Table(title="File Operation Statistics")
        table.add_column("Operation", style="cyan")
        table.add_column("Count", style="green")

        table.add_row("Reads", str(self._stats["reads"]))
        table.add_row("Writes", str(self._stats["writes"]))
        table.add_row("Deletes", str(self._stats["deletes"]))

        console.print(table)

    async def reset_stats(self):
        """Reset statistics."""
        self._stats.clear()
        print("✅ Statistics reset")
```

Usage:
```bash
nexus stats show
nexus stats reset
```

## 6. Quota Enforcement Plugin

Enforces storage quotas:

```python
from nexus.plugins import NexusPlugin, PluginMetadata

class QuotaPlugin(NexusPlugin):
    """Enforces storage quotas."""

    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="quota",
            version="1.0.0",
            description="Enforces storage quotas per user",
            author="Admin Team"
        )

    def hooks(self) -> dict[str, Callable]:
        return {
            "before_write": self.check_quota
        }

    async def check_quota(self, context: dict) -> dict | None:
        """Check storage quota before write."""
        path = context["path"]
        content = context["content"]

        # Get user from path (e.g., /users/alice/file.txt -> alice)
        parts = path.split("/")
        if len(parts) < 3 or parts[1] != "users":
            return context  # No quota for non-user paths

        user = parts[2]
        quota = self.get_config(f"quotas.{user}", 100 * 1024 * 1024)  # 100MB default

        # Calculate current usage
        if self.nx:
            user_files = self.nx.glob(f"/users/{user}/**/*")
            usage = sum(len(self.nx.read(f)) for f in user_files)

            # Check quota
            if usage + len(content) > quota:
                print(f"❌ Quota exceeded for user {user}")
                print(f"   Usage: {usage / 1024 / 1024:.2f} MB")
                print(f"   Quota: {quota / 1024 / 1024:.2f} MB")
                return None  # Cancel write

        return context
```

Configuration:
```yaml
# ~/.nexus/plugins/quota/config.yaml
quotas:
  alice: 104857600   # 100MB
  bob: 52428800      # 50MB
```

## 7. Search Indexer Plugin

Indexes files for search:

```python
from nexus.plugins import NexusPlugin, PluginMetadata
from whoosh.index import create_in, open_dir
from whoosh.fields import Schema, TEXT, ID

class SearchPlugin(NexusPlugin):
    """Indexes files for full-text search."""

    def __init__(self, nexus_fs=None):
        super().__init__(nexus_fs)
        self._index = None

    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="search",
            version="1.0.0",
            description="Full-text search indexing",
            author="Search Team",
            requires=["whoosh>=2.7.4"]
        )

    def commands(self) -> dict[str, Callable]:
        return {
            "index": self.index_all,
            "search": self.search_files,
        }

    def hooks(self) -> dict[str, Callable]:
        return {
            "after_write": self.index_file,
            "after_delete": self.remove_from_index,
        }

    async def initialize(self, config: dict[str, Any]) -> None:
        """Initialize search index."""
        index_dir = self.get_config("index_dir", "/tmp/nexus-search")
        # Setup Whoosh index...

    async def index_file(self, context: dict) -> dict:
        """Index file after write."""
        path = context["path"]
        # Add to search index...
        print(f"🔍 Indexed: {path}")
        return context

    async def search_files(self, query: str):
        """Search indexed files."""
        # Search using Whoosh...
        print(f"Searching for: {query}")
```

## See Also

- [Creating Plugins](creating-plugins.md) - Build your own plugin
- [Lifecycle Hooks](hooks.md) - Hook documentation
- [Plugin Registry](registry.md) - Plugin management
