# Plugin System

## What is the Plugin System?

Nexus's **plugin system** enables extending functionality without modifying core code. Plugins can add custom backends, CLI commands, workflow actions, triggers, lifecycle hooks, and integrations with external services.

### Why Plugins?

| Without Plugins | With Plugins |
|-----------------|--------------|
| ❌ Modify core code for custom features | ✅ Drop-in extensions |
| ❌ Fork repository for custom storage | ✅ Custom backend plugins |
| ❌ Hardcode integrations | ✅ Reusable, shareable plugins |
| ❌ Complex merges on updates | ✅ Upgrade Nexus independently |

**Key Innovation:** Extend Nexus without touching core code.

---

## Plugin Types

### 1. Backend Plugins (Storage)

Add custom storage backends:

```python
from nexus.backends import Backend

class S3Backend(Backend):
    @property
    def name(self) -> str:
        return "s3"

    def write_content(self, content: bytes, context) -> str:
        # Upload to S3, return hash
        pass

    def read_content(self, content_hash: str, context) -> bytes:
        # Download from S3
        pass
```

**Use cases:**
- Cloud storage (S3, Azure, Backblaze)
- Custom encryption layers
- Database-backed storage
- OAuth backends (Google Drive, OneDrive)

---

### 2. CLI Command Plugins

Add custom commands to `nexus` CLI:

```python
from nexus.plugins import NexusPlugin

class MyPlugin(NexusPlugin):
    def commands(self) -> dict:
        return {
            "scrape": self.scrape_url,
            "analyze": self.analyze_file,
        }

    async def scrape_url(self, url: str):
        """Scrape a URL and save to Nexus."""
        content = await scrape(url)
        self.nx.write(f"/scraped/{url}.md", content)
```

```bash
# Use plugin commands
nexus my-plugin scrape https://example.com
nexus my-plugin analyze /path/to/file.pdf
```

---

### 3. Workflow Action Plugins

Extend workflows with custom actions:

```python
from nexus.workflows.actions import BaseAction

class CustomAction(BaseAction):
    async def execute(self, context: WorkflowContext) -> ActionResult:
        # Custom processing logic
        return ActionResult(success=True, output={"result": "..."})
```

```yaml
# Use in workflows
actions:
  - name: custom_step
    type: custom
    config:
      param1: value1
```

---

### 4. Workflow Trigger Plugins

Add custom workflow triggers:

```python
from nexus.workflows.triggers import BaseTrigger

class WebhookTrigger(BaseTrigger):
    def matches(self, event_context: dict) -> bool:
        return event_context.get("source") == "webhook"
```

---

### 5. Lifecycle Hook Plugins

Intercept file operations:

```python
class AuditPlugin(NexusPlugin):
    def hooks(self) -> dict:
        return {
            "before_write": self.audit_write,
            "after_read": self.audit_read,
        }

    async def audit_write(self, context: dict) -> dict:
        # Log file write
        log.info(f"Writing {context['path']}")
        return context

    async def audit_read(self, context: dict) -> dict:
        # Log file read
        log.info(f"Reading {context['path']}")
        return context
```

---

## Creating a Plugin

### Minimal Plugin

```python
from nexus.plugins import NexusPlugin, PluginMetadata

class HelloPlugin(NexusPlugin):
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="hello-plugin",
            version="0.1.0",
            description="A simple hello world plugin",
            author="Your Name",
        )

    def commands(self) -> dict:
        return {"greet": self.greet}

    async def greet(self, name: str = "World"):
        """Say hello to someone."""
        print(f"Hello, {name}!")
```

---

### Project Structure

```
my-nexus-plugin/
├── pyproject.toml
├── README.md
└── src/
    └── nexus_myplugin/
        ├── __init__.py
        └── plugin.py
```

---

### pyproject.toml

```toml
[project]
name = "nexus-plugin-myplugin"
version = "0.1.0"
description = "My Nexus plugin"
requires-python = ">=3.11"
dependencies = [
    "nexus-ai-fs>=0.7.0",
]

[project.entry-points."nexus.plugins"]
myplugin = "nexus_myplugin.plugin:MyPlugin"

[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"
```

**Key parts:**
- `project.entry-points."nexus.plugins"` - Registers plugin for auto-discovery
- Entry point name (`myplugin`) - Used in CLI: `nexus myplugin command`
- Entry point value - Points to plugin class

---

### Plugin Class

```python
# src/nexus_myplugin/plugin.py
from nexus.plugins import NexusPlugin, PluginMetadata

class MyPlugin(NexusPlugin):
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="myplugin",
            version="0.1.0",
            description="My custom plugin",
            author="Me",
            homepage="https://github.com/me/nexus-plugin-myplugin",
        )

    def commands(self) -> dict:
        """Define CLI commands."""
        return {
            "hello": self.hello,
            "process": self.process_file,
        }

    async def hello(self, name: str = "World"):
        """Say hello."""
        print(f"Hello, {name}!")

    async def process_file(self, path: str):
        """Process a file from Nexus."""
        # Access Nexus filesystem
        content = self.nx.read(path)

        # Process content
        processed = content.decode().upper()

        # Write back
        self.nx.write(f"{path}.processed", processed.encode())
        print(f"✓ Processed {path}")
```

---

### Install and Use

```bash
# Install plugin (editable mode for development)
pip install -e .

# Use commands
nexus myplugin hello Alice
nexus myplugin process /workspace/file.txt
```

---

## Plugin Configuration

### Configuration File

Plugins can read configuration from `~/.nexus/plugins/<plugin-name>/config.yaml`:

```yaml
# ~/.nexus/plugins/myplugin/config.yaml
api_key: "your-api-key"
model: "gpt-4"
enabled: true
max_retries: 3
```

### Accessing Configuration

```python
class MyPlugin(NexusPlugin):
    async def initialize(self, config: dict):
        """Called when plugin loads."""
        self.api_key = config.get("api_key")
        self.model = config.get("model", "gpt-4")  # Default value

        if not self.api_key:
            raise ValueError("api_key required in config")

    async def my_command(self):
        # Use configuration
        api_key = self.get_config("api_key")
        model = self.get_config("model", "gpt-4")
```

---

### Environment Variables

Override config with environment variables:

```python
import os

class MyPlugin(NexusPlugin):
    async def initialize(self, config: dict):
        # Prefer env var over config file
        self.api_key = os.getenv("MYPLUGIN_API_KEY") or config.get("api_key")
```

```bash
# Use environment variable
export MYPLUGIN_API_KEY="your-key"
nexus myplugin command
```

---

## Lifecycle Hooks

### Available Hooks

```python
class HookType:
    BEFORE_WRITE = "before_write"
    AFTER_WRITE = "after_write"
    BEFORE_READ = "before_read"
    AFTER_READ = "after_read"
    BEFORE_DELETE = "before_delete"
    AFTER_DELETE = "after_delete"
    BEFORE_MKDIR = "before_mkdir"
    AFTER_MKDIR = "after_mkdir"
    BEFORE_COPY = "before_copy"
    AFTER_COPY = "after_copy"
```

---

### Implementing Hooks

```python
class ValidationPlugin(NexusPlugin):
    def hooks(self) -> dict:
        return {
            "before_write": self.validate_file,
            "after_read": self.log_access,
        }

    async def validate_file(self, context: dict) -> dict | None:
        """
        Validate before writing.

        Args:
            context: {
                "path": "/workspace/file.txt",
                "content": b"file content",
                "metadata": {...},
            }

        Returns:
            dict: Modified context (continue execution)
            None: Stop execution (block operation)
        """
        path = context["path"]
        content = context["content"]

        # Check file size
        if len(content) > 10 * 1024 * 1024:  # 10MB
            print(f"❌ File too large: {path}")
            return None  # Block write

        # Add metadata
        context["metadata"]["validated"] = True
        return context  # Allow write

    async def log_access(self, context: dict) -> dict:
        """Log file reads."""
        path = context["path"]
        user = context.get("user", "unknown")
        print(f"📖 {user} read {path}")
        return context
```

---

### Hook Priority

Hooks execute in **priority order** (higher priority = first):

```python
# ~/.nexus/plugins/myplugin/config.yaml
hook_priority:
  before_write: 10  # Run first
  after_read: 5     # Run before priority 0
```

```python
class PluginHooks:
    def register(
        self,
        hook_type: str,
        handler: Callable,
        priority: int = 0
    ):
        """Higher priority = executed first."""
        pass
```

---

## Real-World Plugin Examples

### Example 1: Anthropic Plugin

**Package:** `nexus-plugin-anthropic`

**Features:**
- Upload skills to Claude Skills API
- Download/list/delete skills
- Import skills from GitHub (anthropics/skills repo)

**Commands:**
```bash
nexus anthropic upload-skill my-skill
nexus anthropic list-skills
nexus anthropic download-skill skill-123
nexus anthropic import-github text-analysis
```

**Configuration:**
```yaml
# ~/.nexus/plugins/anthropic/config.yaml
api_key: "your-claude-api-key"
```

**Implementation:**
```python
class AnthropicPlugin(NexusPlugin):
    def commands(self) -> dict:
        return {
            "upload-skill": self.upload_skill,
            "list-skills": self.list_skills,
            "download-skill": self.download_skill,
            "import-github": self.import_github_skill,
        }

    async def upload_skill(self, skill_name: str):
        # Read skill from Nexus
        skill_content = self.nx.read(f"/skills/{skill_name}.md")

        # Upload to Claude
        response = await claude_api.upload_skill(
            name=skill_name,
            content=skill_content.decode()
        )

        print(f"✓ Uploaded skill: {response['skill_id']}")
```

---

### Example 2: Firecrawl Plugin

**Package:** `nexus-plugin-firecrawl`

**Features:**
- Production-grade web scraping
- JavaScript rendering
- Anti-bot detection
- LLM-ready markdown output

**Commands:**
```bash
# Scrape single URL
nexus firecrawl scrape https://example.com

# Crawl entire website
nexus firecrawl crawl https://example.com --limit 100

# Extract structured data
nexus firecrawl extract https://example.com --schema schema.json

# Web search
nexus firecrawl search "AI agents" --limit 10
```

**Piping Support:**
```bash
# Pipe to other tools
nexus firecrawl scrape https://example.com | jq '.markdown'

# Save to file
nexus firecrawl scrape https://example.com > page.md
```

**Implementation:**
```python
class FirecrawlPlugin(NexusPlugin):
    async def scrape(self, url: str):
        # Scrape with Firecrawl
        result = await firecrawl.scrape(url)

        # Save to Nexus
        filename = url.replace("https://", "").replace("/", "_")
        self.nx.write(f"/scraped/{filename}.md", result.encode())

        # JSON output for piping
        if self.is_piped_output():
            self.write_json_output({
                "url": url,
                "markdown": result,
                "metadata": {...}
            })
            return

        # Normal console output
        print(result)
```

---

### Example 3: Skill Seekers Plugin

**Package:** `nexus-plugin-skill-seekers`

**Features:**
- Generate skills from documentation
- AI-powered skill creation (Claude)
- llms.txt detection for fast scraping
- Multi-page crawling with Firecrawl

**Commands:**
```bash
# Generate skill from docs
nexus skill-seekers generate https://docs.example.com --name example-skill

# Import existing skill
nexus skill-seekers import /path/to/skill.md

# Batch generate from sitemap
nexus skill-seekers batch sitemap.xml
```

**Implementation:**
```python
class SkillSeekersPlugin(NexusPlugin):
    async def generate_skill(self, url: str, name: str):
        # Scrape documentation
        docs = await self.scrape_docs(url)

        # Generate skill with AI
        skill_content = await claude.generate_skill(
            docs=docs,
            name=name
        )

        # Save to Nexus skills directory
        self.nx.write(f"/skills/{name}.md", skill_content.encode())

        print(f"✓ Generated skill: {name}")
```

---

## MCP (Model Context Protocol) Integration

### What is MCP?

**Model Context Protocol** exposes Nexus as a server that AI agents (Claude, etc.) can use as a tool.

### Running MCP Server

```bash
# Local mode
nexus mcp serve --transport stdio

# Remote mode with auth
NEXUS_URL=http://localhost:8080 \
NEXUS_API_KEY=your-key \
nexus mcp serve --transport stdio

# HTTP transport
nexus mcp serve --transport http --port 8081
```

---

### Available MCP Tools (14 total)

**File Operations (7):**
- `nexus_read_file(path)` - Read file content
- `nexus_write_file(path, content)` - Write file
- `nexus_delete_file(path)` - Delete file
- `nexus_list_files(path, recursive)` - List directory
- `nexus_file_info(path)` - Get metadata
- `nexus_mkdir(path)` - Create directory
- `nexus_rmdir(path, recursive)` - Remove directory

**Search (3):**
- `nexus_glob(pattern, path)` - Pattern matching
- `nexus_grep(pattern, path)` - Content search
- `nexus_semantic_search(query, limit)` - Natural language search

**Memory (2):**
- `nexus_store_memory(content, type, importance)` - Store memories
- `nexus_query_memory(query, type, limit)` - Query memories

**Workflows (2):**
- `nexus_list_workflows()` - List workflows
- `nexus_execute_workflow(name, inputs)` - Run workflow

---

### Claude Desktop Configuration

```json
{
  "mcpServers": {
    "nexus": {
      "command": "nexus",
      "args": ["mcp", "serve", "--transport", "stdio"],
      "env": {
        "NEXUS_URL": "http://localhost:8080",
        "NEXUS_API_KEY": "your-api-key"
      }
    }
  }
}
```

**Location:** `~/.claude/config.json` (macOS) or `%APPDATA%\Claude\config.json` (Windows)

---

### Using Nexus in Claude

```
User: Can you read the file /workspace/report.txt from Nexus?

Claude: I'll use the nexus_read_file tool to read that file.

[Uses nexus_read_file("/workspace/report.txt")]

The report contains...
```

```
User: Search for Python files in the project

Claude: I'll search using glob pattern.

[Uses nexus_glob("**/*.py", "/workspace")]

Found 42 Python files:
- /workspace/main.py
- /workspace/utils.py
- ...
```

---

## Plugin CLI Commands

### List Plugins

```bash
nexus plugins list

# Output:
# anthropic (v0.2.0) - Claude Skills API integration
# firecrawl (v0.1.5) - Production web scraping
# skill-seekers (v0.1.0) - AI-powered skill generation
```

---

### Plugin Info

```bash
nexus plugins info anthropic

# Output:
# Name: anthropic
# Version: 0.2.0
# Author: Nexus Team
# Homepage: https://github.com/nexi-lab/nexus-plugin-anthropic
# Description: Claude Skills API integration
# Commands:
#   - upload-skill
#   - download-skill
#   - list-skills
#   - delete-skill
#   - import-github
```

---

### Enable/Disable Plugins

```bash
# Disable plugin
nexus plugins disable anthropic

# Enable plugin
nexus plugins enable anthropic
```

---

### Install Plugins

```bash
# Install from PyPI
pip install nexus-plugin-firecrawl

# Install from Git
pip install git+https://github.com/nexi-lab/nexus-plugin-anthropic

# Install local plugin (development)
cd my-plugin
pip install -e .
```

---

## Best Practices

### 1. Naming Conventions

```python
# ✅ Good
Package: nexus-plugin-myplugin
Module: nexus_myplugin
Entry point: myplugin  # Short name

# ❌ Bad
Package: myplugin  # Confusing
Module: my_plugin  # Inconsistent
Entry point: nexus-plugin-myplugin  # Too long
```

---

### 2. Error Handling

```python
# ✅ Good
async def command(self, url: str):
    try:
        result = await scrape(url)
        console.print("[green]✓ Success[/green]")
    except ValueError as e:
        console.print(f"[red]Error: {e}[/red]")
        return
    except Exception as e:
        console.print(f"[red]Unexpected error: {e}[/red]")
        traceback.print_exc()

# ❌ Bad
async def command(self, url: str):
    result = await scrape(url)  # No error handling
```

---

### 3. Configuration Security

```python
# ✅ Good
api_key = os.getenv("PLUGIN_API_KEY") or self.get_config("api_key")

if not api_key:
    raise ValueError("API key required. Set PLUGIN_API_KEY env var or config file.")

# ❌ Bad
api_key = "hardcoded-key-123"  # Never hardcode secrets
```

---

### 4. Async/Await

```python
# ✅ Good
async def command(self):
    result = await async_operation()

# ❌ Bad
def command(self):  # Not async
    result = await async_operation()  # Error!
```

---

### 5. NexusFS Integration

```python
# ✅ Good
if self.nx:
    self.nx.write("/output/result.txt", data)
else:
    # Fallback if not initialized
    with open("result.txt", "w") as f:
        f.write(data)

# ❌ Bad
self.nx.write(...)  # Might fail if nx is None
```

---

### 6. JSON Output for Piping

```python
# ✅ Good
if self.is_piped_output():
    self.write_json_output({"result": data})
    return

# Normal console output
console.print(data)

# ❌ Bad
print(data)  # Always print, breaks piping
```

---

## Advanced: Custom Backend Plugin

```python
from nexus.backends import Backend
from nexus.core.permissions import OperationContext

class S3Backend(Backend):
    def __init__(self, bucket: str, region: str = "us-east-1"):
        self.bucket = bucket
        self.region = region
        self.s3_client = boto3.client('s3', region_name=region)

    @property
    def name(self) -> str:
        return "s3"

    def write_content(self, content: bytes, context: OperationContext) -> str:
        # Compute hash
        content_hash = hashlib.sha256(content).hexdigest()

        # Upload to S3
        key = f"cas/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}"
        self.s3_client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content
        )

        return content_hash

    def read_content(self, content_hash: str, context: OperationContext) -> bytes:
        # Download from S3
        key = f"cas/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}"
        response = self.s3_client.get_object(
            Bucket=self.bucket,
            Key=key
        )
        return response['Body'].read()
```

**Usage:**
```python
from nexus import NexusFS

nx = NexusFS(backend=S3Backend(bucket="my-bucket"))
nx.write("/file.txt", b"content")
```

---

## FAQ

### Q: How do I debug my plugin?

**A**: Use print statements and traceback:

```python
async def command(self):
    try:
        print(f"DEBUG: Starting command")
        result = await operation()
        print(f"DEBUG: Result = {result}")
    except Exception as e:
        import traceback
        traceback.print_exc()
```

---

### Q: Can plugins access other plugins?

**A**: Not directly. Plugins should be independent. If you need shared functionality, create a shared library that both plugins depend on.

---

### Q: How do I publish my plugin?

**A**:
1. Publish to PyPI: `python -m build && twine upload dist/*`
2. Users install: `pip install nexus-plugin-yourname`
3. Auto-discovered via entry points

---

### Q: Can I disable a plugin without uninstalling?

**A**: Yes:
```bash
nexus plugins disable myplugin
```

Or set in config:
```yaml
# ~/.nexus/plugins/myplugin/config.yaml
enabled: false
```

---

### Q: How do I test my plugin?

**A**: Write pytest tests:

```python
# tests/test_plugin.py
import pytest
from nexus_myplugin import MyPlugin

@pytest.mark.asyncio
async def test_command():
    plugin = MyPlugin(nx=None)
    result = await plugin.hello("Test")
    assert result == "Hello, Test!"
```

---

## Next Steps

- **[Workflows & Triggers](workflows-vs-triggers.md)** - Extend workflows with custom actions
- **[Mounts & Backends](mounts-and-backends.md)** - Custom storage backends
- **[Skills System](skills-system.md)** - Create reusable AI capabilities
- **[API Reference: Plugin API](/api/plugin-api/)** - Complete API docs

---

## Related Files

- Base: `src/nexus/plugins/base.py:1`
- Registry: `src/nexus/plugins/registry.py:1`
- Hooks: `src/nexus/plugins/hooks.py:1`
- CLI: `src/nexus/cli/commands/plugins.py:1`
- MCP: `src/nexus/mcp/server.py:1`
- Backend: `src/nexus/backends/backend.py:1`
- Anthropic Plugin: `nexus-plugin-anthropic/src/nexus_anthropic/plugin.py:1`
- Firecrawl Plugin: `nexus-plugin-firecrawl/src/nexus_firecrawl/plugin.py:1`
- Skill Seekers Plugin: `nexus-plugin-skill-seekers/src/nexus_skill_seekers/plugin.py:1`
