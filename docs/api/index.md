# API Reference

Complete API documentation for Nexus.

---

## Quick Navigation

<div class="features-grid" markdown>

<div class="feature-card" markdown>
### :material-api: Core API
Essential operations for working with Nexus.

[View Core API →](core-api.md){ .md-button }
</div>

<div class="feature-card" markdown>
### :material-file: File Operations
Read, write, and manage files.

[File Operations →](file-operations.md){ .md-button }
</div>

<div class="feature-card" markdown>
### :material-folder: Directories
Create and manage directory structures.

[Directory Operations →](directory-operations.md){ .md-button }
</div>

<div class="feature-card" markdown>
### :material-brain: Memory
Agent memory management APIs.

[Memory API →](memory-management.md){ .md-button }
</div>

<div class="feature-card" markdown>
### :material-tag: Metadata
Rich metadata and tagging support.

[Metadata API →](metadata.md){ .md-button }
</div>

<div class="feature-card" markdown>
### :material-shield: Permissions
Fine-grained access control.

[Permissions API →](permissions.md){ .md-button }
</div>

<div class="feature-card" markdown>
### :material-history: Versioning
Time travel and version control.

[Versioning API →](versioning.md){ .md-button }
</div>

<div class="feature-card" markdown>
### :material-magnify: Search
Semantic search capabilities.

[Search API →](semantic-search.md){ .md-button }
</div>

<div class="feature-card" markdown>
### :material-robot: LLM Reading
AI-powered document Q&A.

[LLM Reading API →](llm-document-reading.md){ .md-button }
</div>

<div class="feature-card" markdown>
### :material-console: CLI
Command-line interface reference.

[CLI Reference →](cli-reference.md){ .md-button }
</div>

<div class="feature-card" markdown>
### :material-puzzle: Plugins
Extend Nexus with custom functionality.

[Plugins API →](plugins.md){ .md-button }
</div>

<div class="feature-card" markdown>
### :material-school: Skills
AI skills management and distribution.

[Skills API →](skills.md){ .md-button }
</div>

</div>

---

## API Overview

Nexus provides three main interfaces:

### Python SDK

```python
import nexus

# Connect to Nexus
nx = nexus.connect(config={"data_dir": "./nexus-data"})

# Use the API
nx.write("/file.txt", b"content")
content = nx.read("/file.txt")
```

### CLI

```bash
# File operations
nexus write /file.txt "content"
nexus read /file.txt

# Memory operations
nexus memory store "user_id" '{"preferences": "dark_mode"}'
nexus memory retrieve "user_id"
```

### RPC Server

```python
# Start server
nexus.serve(host="0.0.0.0", port=8080)

# Connect remotely
nx = nexus.connect(remote_url="http://localhost:8080")
```

### Plugins

```python
from nexus.plugins import NexusPlugin, PluginMetadata

class MyPlugin(NexusPlugin):
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="my-plugin",
            version="1.0.0",
            description="Custom plugin"
        )

    def commands(self) -> dict[str, Callable]:
        return {"hello": self.hello_command}

    async def hello_command(self):
        print("Hello from plugin!")
```

### Skills

```python
from nexus.skills import SkillRegistry, SkillManager

# Discover skills
registry = SkillRegistry(nx)
await registry.discover()

# Get a skill
skill = await registry.get_skill("analyze-code")

# Create new skill
manager = SkillManager(nx, registry)
await manager.create_skill(
    name="my-skill",
    description="Custom skill",
    template="basic"
)
```

---

## Common Patterns

### Context Management

```python
# Automatic cleanup with context manager
with nexus.connect(config={"data_dir": "./data"}) as nx:
    nx.write("/file.txt", b"content")
    content = nx.read("/file.txt")
# Connection automatically closed
```

### Error Handling

```python
from nexus.core.exceptions import FileNotFoundError, PermissionDeniedError

try:
    content = nx.read("/nonexistent.txt")
except FileNotFoundError:
    print("File not found")
except PermissionDeniedError:
    print("Access denied")
```

### Multi-Tenant Operations

```python
# Create tenant workspace
nx.workspace.create("/tenant/acme-corp", tenant_id="acme-123")

# All operations within workspace are isolated
nx.write(
    "/tenant/acme-corp/data.json",
    data,
    context={"tenant_id": "acme-123"}
)
```

---

## API Principles

!!! note "Design Philosophy"
    Nexus APIs follow these principles:

    - **Simple by default, powerful when needed**
    - **Consistent across SDK, CLI, and RPC**
    - **Type-safe with full IDE support**
    - **Defensive with clear error messages**
    - **Performance-optimized for production**

---

## Next Steps

<div class="value-prop-grid" markdown>

<div class="value-prop" markdown>
#### For Beginners
Start with the [Core API](core-api.md) to learn the basics, then explore [File Operations](file-operations.md).
</div>

<div class="value-prop" markdown>
#### For AI Developers
Check out [Memory Management](memory-management.md), [Semantic Search](semantic-search.md), and [Skills API](skills.md) for AI-specific features.
</div>

<div class="value-prop" markdown>
#### For Enterprise
Review [Permissions](permissions.md) and [Multi-Tenancy](../MULTI_TENANT.md) for production deployments.
</div>

</div>
