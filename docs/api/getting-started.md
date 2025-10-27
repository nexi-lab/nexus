# Getting Started

← [API Documentation](README.md)

This guide will help you get up and running with Nexus quickly.

## Installation

```bash
# Install from PyPI
pip install nexus-ai-fs

# Install with all optional dependencies
pip install nexus-ai-fs[all]

# Install specific features
pip install nexus-ai-fs[postgres]  # PostgreSQL support
pip install nexus-ai-fs[semantic-search]  # Vector search
```

## Quick Start

```python
import nexus

# Connect to Nexus (auto-creates database)
nx = nexus.connect(config={"data_dir": "./nexus-data"})

# Write a file
nx.write("/documents/hello.txt", b"Hello, Nexus!")

# Read a file
content = nx.read("/documents/hello.txt")
print(content.decode())  # "Hello, Nexus!"

# List files
files = nx.list()
print(files)  # ['/documents/hello.txt']

# Delete a file
nx.delete("/documents/hello.txt")

# Close connection
nx.close()
```

## Context Manager

```python
import nexus

with nexus.connect(config={"data_dir": "./nexus-data"}) as nx:
    nx.write("/file.txt", b"content")
    content = nx.read("/file.txt")
# Automatically closed
```

## See Also

- [Core API](core-api.md) - Connection and configuration details
- [File Operations](file-operations.md) - Complete file operation reference
- [Configuration](configuration.md) - Advanced configuration options
- [CLI Reference](cli-reference.md) - Command-line interface

## Next Steps

1. Learn about [core connection options](core-api.md)
2. Explore [file operations](file-operations.md)
3. Set up [permissions and access control](permissions.md)
4. Try [workspace snapshots](versioning.md) for version control
