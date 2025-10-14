# Nexus: AI-Native Distributed Filesystem

[![Test](https://github.com/nexi-lab/nexus/actions/workflows/test.yml/badge.svg)](https://github.com/nexi-lab/nexus/actions/workflows/test.yml)
[![Lint](https://github.com/nexi-lab/nexus/actions/workflows/lint.yml/badge.svg)](https://github.com/nexi-lab/nexus/actions/workflows/lint.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

!!! note "Version 0.1.0 - Foundation Release"
    Currently implements **embedded mode only**. Monolithic and distributed modes are planned for future releases.

## What is Nexus?

Nexus is an AI-native distributed filesystem designed for AI agent infrastructure. **v0.1.0 is the foundation release** providing core embedded filesystem functionality with SQLite-backed metadata storage.

## Current Status (v0.1.0)

### ✅ What's Implemented

- **Embedded Mode** - In-process filesystem with SQLite metadata
- **Core Operations** - `read()`, `write()`, `delete()`, `exists()`, `list()`
- **Local Storage** - Files stored on local filesystem
- **Metadata Tracking** - Size, etag, timestamps via SQLite
- **Virtual Paths** - Unified path namespace
- **Configuration** - File/environment/dict based config

### ❌ Not Yet Implemented

!!! warning "Planned Features"
    Everything else in the vision is planned but not implemented:

- Async operations
- Caching layer
- Vector/semantic search
- LLM integration
- Agent memory
- Monolithic/distributed modes
- REST API
- S3/GDrive backends
- Docker deployment
- Multi-user support

## Quick Start

### Installation

```bash
git clone https://github.com/nexi-lab/nexus.git
cd nexus
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
```

### Basic Usage

```python
import nexus

# Connect to embedded filesystem
nx = nexus.connect()

# Write a file
nx.write("/workspace/hello.txt", b"Hello World")

# Read it back
content = nx.read("/workspace/hello.txt")
print(content)  # b'Hello World'

# Check existence
if nx.exists("/workspace/hello.txt"):
    print("File exists!")

# List files
files = nx.list("/workspace/")
print(files)  # ['/workspace/hello.txt']

# Delete
nx.delete("/workspace/hello.txt")

# Clean up
nx.close()
```

### With Context Manager

```python
import nexus

with nexus.connect() as nx:
    nx.write("/data/test.txt", b"Test content")
    content = nx.read("/data/test.txt")
# Automatically closed
```

## Configuration

Create `nexus.yaml`:

```yaml
mode: embedded
data_dir: ./nexus-data
```

Or use environment variables:

```bash
export NEXUS_MODE=embedded
export NEXUS_DATA_DIR=./nexus-data
```

Or configure programmatically:

```python
import nexus

nx = nexus.connect({
    "mode": "embedded",
    "data_dir": "./my-data"
})
```

## Architecture (v0.1.0)

```
┌─────────────────────┐
│  Your Application   │
│                     │
│  ┌──────────────┐  │
│  │    Nexus     │  │
│  │   Embedded   │  │
│  └──────┬───────┘  │
│         │          │
│    ┌────┴────┐     │
│    │ SQLite  │     │  (metadata)
│    └─────────┘     │
│         │          │
│    ┌────┴────┐     │
│    │Local FS │     │  (file content)
│    └─────────┘     │
└─────────────────────┘
```

## API Reference (v0.1.0)

### `nexus.connect(config=None)`

Connect to Nexus filesystem.

**Arguments:**
- `config` - Config dict, file path, or None (auto-discover)

**Returns:** `Embedded` instance

### `Embedded` Class

#### `read(path: str) -> bytes`

Read file content.

**Raises:** `NexusFileNotFoundError` if file doesn't exist

#### `write(path: str, content: bytes) -> None`

Write file content. Creates parent directories automatically.

#### `delete(path: str) -> None`

Delete a file.

**Raises:** `NexusFileNotFoundError` if file doesn't exist

#### `exists(path: str) -> bool`

Check if file exists.

#### `list(prefix: str = "") -> list[str]`

List files with given path prefix.

#### `close() -> None`

Close the filesystem and release resources.

## Exceptions

- `NexusError` - Base exception
- `NexusFileNotFoundError` - File not found
- `NexusPermissionError` - Permission denied
- `InvalidPathError` - Invalid path (e.g., contains `..`)
- `BackendError` - Storage backend error
- `MetadataError` - Metadata store error

## Development

```bash
# Run tests
pytest

# Type checking
mypy src/nexus

# Linting
ruff check .

# Formatting
ruff format .
```

## Roadmap

### v0.1.0 - Embedded Mode Foundation ✅ (Current)
- ✅ Core filesystem operations
- ✅ SQLite metadata store
- ✅ Local filesystem backend
- ✅ Configuration system
- ✅ Basic CLI stub

### v0.2.0 - Document Processing (Planned)
- PDF/Excel/CSV parsers
- Document type detection
- Text extraction
- Qdrant embedded integration

### v0.3.0 - AI Integration (Planned)
- LLM provider abstraction
- Claude/OpenAI integration
- Semantic search
- KV cache for prompts

### v0.4.0 - Agent Workspaces (Planned)
- Agent workspace structure
- Custom command system
- Agent memory storage

### v0.5.0 - Monolithic Server (Planned)
- FastAPI REST API
- Multi-tenancy
- PostgreSQL/Redis
- Docker deployment

### v0.9.0 - Distributed Mode (Planned)
- Kubernetes deployment
- High availability
- Horizontal scaling

### v1.0.0 - Production Release (Planned)
- Complete feature set
- Production-tested
- Full documentation

## Contributing

See [Development Guide](development/development.md) for contribution guidelines.

## License

Apache 2.0 - see [LICENSE](https://github.com/nexi-lab/nexus/blob/main/LICENSE)

## Links

- [GitHub Repository](https://github.com/nexi-lab/nexus)
- [Issue Tracker](https://github.com/nexi-lab/nexus/issues)
- [Getting Started Guide](getting-started/quickstart.md)
- [Configuration Reference](getting-started/configuration.md)
