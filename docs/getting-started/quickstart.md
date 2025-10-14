# Quick Start

Get started with Nexus v0.1.0 in embedded mode (60 seconds).

## Installation

```bash
# Clone the repository
git clone https://github.com/nexi-lab/nexus.git
cd nexus

# Install with uv (recommended)
uv venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
uv pip install -e ".[dev]"

# Or with pip
pip install -e ".[dev]"
```

## Basic Usage

### 1. Create Configuration (Optional)

Create `nexus.yaml`:

```yaml
mode: embedded
data_dir: ./nexus-data
```

### 2. Use Nexus in Your Code

```python
import nexus

# Connect to Nexus (auto-discovers nexus.yaml)
nx = nexus.connect()

# Write a file
nx.write("/workspace/hello.txt", b"Hello World")

# Read a file
content = nx.read("/workspace/hello.txt")
print(content)  # b'Hello World'

# Check if file exists
if nx.exists("/workspace/hello.txt"):
    print("File exists!")

# List files
files = nx.list("/workspace/")
print(files)  # ['/workspace/hello.txt']

# Delete a file
nx.delete("/workspace/hello.txt")

# Clean up
nx.close()
```

### 3. Using Context Manager

```python
import nexus

with nexus.connect() as nx:
    nx.write("/data/test.txt", b"Test content")
    content = nx.read("/data/test.txt")
    print(content)
# Automatically closes on exit
```

## What's Implemented

**Available in v0.1.0:**
- ✅ Embedded mode
- ✅ Basic file operations: `read()`, `write()`, `delete()`, `exists()`, `list()`
- ✅ SQLite metadata store
- ✅ Local filesystem backend
- ✅ Configuration from file/env/dict
- ✅ Virtual path management

**Not Yet Implemented:**
- ❌ Async operations
- ❌ Caching
- ❌ Vector/semantic search
- ❌ LLM integration
- ❌ Monolithic/distributed modes
- ❌ REST API
- ❌ S3/GDrive backends
- ❌ Docker deployment

See the [Roadmap](../index.md#roadmap) for planned features.

## Example: Simple File Storage

```python
import nexus

# Initialize
config = {
    "mode": "embedded",
    "data_dir": "./my-data"
}
nx = nexus.connect(config)

# Store some data
nx.write("/documents/report.txt", b"Q4 Revenue Report...")
nx.write("/documents/notes.txt", b"Meeting notes...")
nx.write("/images/logo.png", logo_bytes)

# Retrieve data
report = nx.read("/documents/report.txt")

# List all documents
docs = nx.list("/documents/")
print(f"Found {len(docs)} documents")

# Clean up
nx.close()
```

## Error Handling

```python
import nexus
from nexus import NexusFileNotFoundError, InvalidPathError

nx = nexus.connect()

try:
    content = nx.read("/nonexistent.txt")
except NexusFileNotFoundError as e:
    print(f"File not found: {e}")

try:
    nx.write("../invalid/../path.txt", b"data")
except InvalidPathError as e:
    print(f"Invalid path: {e}")
```

## Next Steps

- [Configuration Guide](configuration.md) - Learn about configuration options
- [API Reference](../api/api.md) - Explore the full API
- [Development Guide](../development/development.md) - Contributing to Nexus
