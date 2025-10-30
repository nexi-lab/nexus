# Nexus SDK Usage Guide

> **Clean programmatic interface for building custom tools and integrations**

The Nexus SDK provides a stable, semantic-versioned API for building third-party tools, GUIs, TUIs, web interfaces, IDE plugins, and custom automation on top of Nexus.

## Why Use the SDK?

The SDK (`nexus.sdk`) is designed for **programmatic access** with:

- ✅ Clean, minimal interface without CLI dependencies
- ✅ Semantic versioning separate from CLI changes
- ✅ Stable API for long-term integrations
- ✅ No Rich/Click dependencies (pure Python)
- ✅ Well-documented with type hints

**Use the SDK if you are:**
- Building a custom GUI or TUI
- Creating a web interface
- Writing an IDE plugin
- Developing language bindings
- Building automation tools
- Integrating Nexus into your application

**Use the CLI if you are:**
- Working interactively in a terminal
- Writing shell scripts
- Exploring Nexus features manually

## Quick Start

### Installation

```bash
pip install nexus-ai-fs
```

### Basic Usage

```python
from nexus.sdk import connect

# Connect to Nexus (auto-discovers configuration)
nx = connect()

# File operations
nx.write("/workspace/file.txt", b"Hello World")
content = nx.read("/workspace/file.txt")
print(content)  # b"Hello World"

# List files
files = nx.list("/workspace", recursive=True)
for file in files:
    print(f"{file.path} ({file.size} bytes)")

# Delete file
nx.delete("/workspace/file.txt")
```

### Configuration

The SDK supports multiple ways to configure Nexus:

**Auto-discovery (recommended):**
```python
from nexus.sdk import connect

# Auto-discovers from nexus.yaml or environment variables
nx = connect()
```

**Explicit configuration:**
```python
from nexus.sdk import connect

# Pass configuration dictionary
nx = connect(config={
    "backend": "local",
    "data_dir": "./my-nexus-data",
    "enable_permissions": True,
    "tenant_id": "my-team",
    "agent_id": "my-agent",
})
```

**From config file:**
```python
from nexus.sdk import connect

# Load from specific config file
nx = connect(config="/path/to/nexus.yaml")
```

## Core Operations

### File Operations

```python
from nexus.sdk import connect

nx = connect()

# Write file
nx.write("/workspace/data.txt", b"content")

# Read file
content = nx.read("/workspace/data.txt")

# Copy file
nx.cp("/workspace/src.txt", "/workspace/dest.txt")

# Move/rename file
nx.mv("/workspace/old.txt", "/workspace/new.txt")

# Delete file
nx.delete("/workspace/file.txt")

# Check if file exists
if nx.exists("/workspace/file.txt"):
    print("File exists")
```

### Directory Operations

```python
from nexus.sdk import connect

nx = connect()

# Create directory
nx.mkdir("/workspace/mydir")

# Create nested directories
nx.mkdir("/workspace/deep/nested/dir", parents=True)

# Remove empty directory
nx.rmdir("/workspace/mydir")

# Remove directory recursively
nx.rmdir("/workspace/mydir", recursive=True)

# Check if path is directory
if nx.is_directory("/workspace/mydir"):
    print("Is a directory")
```

### File Discovery

```python
from nexus.sdk import connect

nx = connect()

# List files
files = nx.list("/workspace")
for file in files:
    print(f"{file.path}: {file.size} bytes")

# List recursively
files = nx.list("/workspace", recursive=True)

# Glob patterns
python_files = nx.glob("**/*.py")
for file in python_files:
    print(file.path)

# Search file contents (grep)
matches = nx.grep("TODO", file_pattern="**/*.py")
for match in matches:
    print(f"{match.path}:{match.line_number}: {match.line}")
```

### File Metadata

```python
from nexus.sdk import connect

nx = connect()

# Get file metadata
metadata = nx.get_metadata("/workspace/file.txt")
print(f"Size: {metadata.size}")
print(f"Created: {metadata.created_at}")
print(f"Modified: {metadata.modified_at}")
print(f"Hash: {metadata.content_hash}")
```

## Advanced Features

### Permissions (ReBAC)

**Note:** Embedded mode (SDK) doesn't enforce permissions by default - it's single-user with zero deployment. For production with permissions, use server mode with authentication.

To test permission logic in embedded mode, explicitly enable permissions:

```python
from nexus.sdk import connect

# Opt-in to permissions for testing (disabled by default in embedded mode)
nx = connect(config={"enforce_permissions": True})

# Grant permissions using ReBAC
nx.rebac_create(
    subject=("user", "alice"),
    relation="direct_editor",
    object=("file", "/workspace/file.txt")
)

# Check permissions
has_access = nx.rebac_check(
    subject=("user", "alice"),
    permission="write",
    object=("file", "/workspace/file.txt")
)

# Who can access this file?
subjects = nx.rebac_expand(
    permission="read",
    object=("file", "/workspace/file.txt")
)
```

**Production setup:** For real permission enforcement, use server mode:
```bash
# Server with authentication (permissions enabled automatically)
nexus serve --auth-type database
```

See [Permission System Guide](PERMISSION_SYSTEM.md) and [ReBAC API Patterns](REBAC_API_PATTERNS.md) for complete documentation.

### Skills Management

```python
from nexus.sdk import connect, SkillRegistry, SkillManager

nx = connect()

# Create skill registry
registry = SkillRegistry(nx)

# Discover skills from filesystem
await registry.discover()

# List available skills
skills = registry.list_skills()
print(f"Available skills: {skills}")

# Get skill metadata
metadata = registry.get_metadata("analyze-code")
print(f"{metadata.name}: {metadata.description}")

# Load full skill content
skill = await registry.get_skill("analyze-code")
print(skill.content)

# Manage skills
manager = SkillManager(nx, registry)

# Create new skill
await manager.create_skill(
    "my-skill",
    description="My custom skill",
    template="basic",
    author="Alice"
)

# Fork existing skill
await manager.fork_skill(
    "analyze-code",
    "my-analyzer",
    tier="agent",
    author="Bob"
)

# Publish skill to team
await manager.publish_skill(
    "my-skill",
    source_tier="agent",
    target_tier="tenant"
)
```

### Version Tracking

```python
from nexus.sdk import connect

nx = connect()

# Versions are created automatically on writes
nx.write("/workspace/doc.txt", b"Version 1")
nx.write("/workspace/doc.txt", b"Version 2")
nx.write("/workspace/doc.txt", b"Version 3")

# List version history
versions = nx.list_versions("/workspace/doc.txt")
for v in versions:
    print(f"Version {v.version}: {v.created_at}")

# Get specific version
v1_content = nx.get_version("/workspace/doc.txt", version=1)
print(v1_content)  # b"Version 1"

# Compare versions
diff = nx.diff_versions("/workspace/doc.txt", v1=1, v2=3)
print(diff)  # Unified diff output

# Rollback to previous version
nx.rollback("/workspace/doc.txt", version=2)
```

### Remote Filesystem

```python
from nexus.sdk import RemoteNexusFS

# Connect to remote Nexus server
nx = RemoteNexusFS(
    server_url="http://your-server:8080",
    api_key="your-api-key"
)

# Same API as local filesystem!
nx.write("/workspace/file.txt", b"remote data")
content = nx.read("/workspace/file.txt")
files = nx.list("/workspace", recursive=True)
```

## Building Custom Tools

### Example: Custom File Manager GUI

```python
from nexus.sdk import connect, FileNotFoundError
import tkinter as tk
from tkinter import ttk

class NexusFileManager:
    def __init__(self):
        self.nx = connect()
        self.root = tk.Tk()
        self.root.title("Nexus File Manager")

        # Create file tree
        self.tree = ttk.Treeview(self.root)
        self.tree.pack(fill="both", expand=True)

        # Load files
        self.load_files("/")

    def load_files(self, path):
        try:
            files = self.nx.list(path, recursive=False)
            for file in files:
                self.tree.insert("", "end", text=file.path,
                               values=(file.size, file.modified_at))
        except FileNotFoundError:
            print(f"Path not found: {path}")

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    app = NexusFileManager()
    app.run()
```

### Example: Custom TUI with Rich

```python
from nexus.sdk import connect
from rich.console import Console
from rich.table import Table

console = Console()

def display_files(path="/"):
    nx = connect()
    files = nx.list(path, recursive=True)

    table = Table(title=f"Files in {path}")
    table.add_column("Path")
    table.add_column("Size", justify="right")
    table.add_column("Modified")

    for file in files:
        table.add_row(file.path, f"{file.size:,}", str(file.modified_at))

    console.print(table)

display_files("/workspace")
```

### Example: Web API with FastAPI

```python
from fastapi import FastAPI, HTTPException
from nexus.sdk import connect, FileNotFoundError
from pydantic import BaseModel

app = FastAPI()
nx = connect()

class FileWrite(BaseModel):
    content: bytes

@app.get("/files/{path:path}")
def read_file(path: str):
    try:
        content = nx.read(f"/{path}")
        return {"path": path, "content": content}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")

@app.post("/files/{path:path}")
def write_file(path: str, data: FileWrite):
    nx.write(f"/{path}", data.content)
    return {"path": path, "status": "written"}

@app.get("/list/{path:path}")
def list_files(path: str, recursive: bool = False):
    files = nx.list(f"/{path}", recursive=recursive)
    return {"files": [{"path": f.path, "size": f.size} for f in files]}
```

## SDK Reference

For complete SDK reference, see:
- [Core API Reference](./api/core-api.md)
- [Permissions API Reference](./api/permissions.md)
- [Complete API Documentation](./api/api.md)
- [Configuration Guide](./api/configuration.md)

## Migration from CLI to SDK

If you're currently using the CLI programmatically (e.g., via `subprocess`), migrate to the SDK:

**Before (using CLI via subprocess):**
```python
import subprocess
import json

# List files using CLI
result = subprocess.run(
    ["nexus", "ls", "/workspace", "--json"],
    capture_output=True,
    text=True
)
files = json.loads(result.stdout)
```

**After (using SDK):**
```python
from nexus.sdk import connect

nx = connect()
files = nx.list("/workspace")
```

Benefits:
- ✅ No subprocess overhead
- ✅ Native Python objects (no JSON parsing)
- ✅ Better error handling
- ✅ Type hints and IDE autocomplete
- ✅ Direct access to all features

## Contributing

We welcome SDK improvements! Please see [CONTRIBUTING.md](../CONTRIBUTING.md) for details.

## License

Apache 2.0 License - see [LICENSE](../LICENSE) for details.
