# Nexus API Documentation

[![Version](https://img.shields.io/badge/version-0.7.0-blue.svg)](https://github.com/nexus-ai/nexus)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](https://github.com/nexus-ai/nexus/blob/main/LICENSE)

Complete API reference for Nexus - the AI-native distributed filesystem architecture combining unified storage, self-evolving agent memory, and intelligent document processing.

## 📚 Documentation Structure

This documentation is organized into focused, maintainable sections. Choose your starting point based on your needs:

### Quick Start

- **[Getting Started](getting-started.md)** - Installation, quick start guide, and first steps
- **[Core API](core-api.md)** - Understanding `nexus.connect()` and basic concepts

### Core Operations

- **[File Operations](file-operations.md)** - Read, write, delete, rename, and batch operations
- **[File Discovery](file-discovery.md)** - List, glob, and grep for finding files
- **[Directory Operations](directory-operations.md)** - Create and manage directories

### Advanced Features

- **[Workflow Automation](workflows.md)** - Event-driven workflows that trigger automatically on file operations
- **[Versioning](versioning.md)** - Version tracking and workspace snapshots
- **[Workspace Management](workspace-management.md)** - Workspace registry and configuration
- **[Memory Management](memory-management.md)** - Memory registry for agent memories
- **[Semantic Search](semantic-search.md)** - Vector search and semantic operations
- **[Metadata](metadata.md)** - Export, import, and batch metadata operations

### Access Control & Configuration

- **[Permissions](permissions.md)** - Operation contexts, ReBAC, namespace management, and access control
- **[Mounts](mounts.md)** - Mount management and multi-source access
- **[Configuration](configuration.md)** - Config files, environment variables, and backends

### CLI Reference

- **[CLI Overview](cli/index.md)** - Command-line interface documentation hub
  - [File Operations](cli/file-operations.md) - write, cat, rm, cp, move
  - [Directory Operations](cli/directory-operations.md) - mkdir, rmdir, ls, tree
  - [Search](cli/search.md) - glob, grep
  - [Versioning](cli/versioning.md) - versions history/get/diff/rollback
  - [Workspace](cli/workspace.md) - workspace register/snapshot/restore/log
  - [Memory](cli/memory.md) - memory register/store/search
  - [Semantic Search](cli/semantic-search.md) - search init/index/query
  - [Permissions](cli/permissions.md) - rebac commands
  - [Mounts](cli/mounts.md) - mounts list/add/remove
  - [Server](cli/server.md) - serve, mount, unmount
  - [Advanced](cli/advanced.md) - ops log, undo, size, sync

### Reference

- **[SDK Parity Matrix](sdk-parity-matrix.md)** - ✅ Feature parity between local and remote modes (100%)
- **[RPC/Server API](rpc-api.md)** - Remote access and server setup
- **[Error Handling](error-handling.md)** - Exception types and error patterns
- **[Advanced Usage](advanced-usage.md)** - Complex patterns and optimization
- **[Migration & Compatibility](migration-compatibility.md)** - Version migration guides

## 🚀 Quick Reference

### Installation

```bash
# Install from PyPI
pip install nexus-ai-fs

# Install with all features
pip install nexus-ai-fs[all]
```

### Basic Usage

```python
import nexus

# Connect to Nexus
nx = nexus.connect(config={"data_dir": "./nexus-data"})

# Write a file
nx.write("/documents/hello.txt", b"Hello, Nexus!")

# Read a file
content = nx.read("/documents/hello.txt")
print(content.decode())  # "Hello, Nexus!"

# List files
files = nx.list()
print(files)  # ['/documents/hello.txt']

# Close connection
nx.close()
```

### Context Manager (Recommended)

```python
import nexus

with nexus.connect() as nx:
    nx.write("/file.txt", b"content")
    content = nx.read("/file.txt")
# Automatically closed
```

## 🎯 Common Use Cases

### For Application Developers
Start with [Getting Started](getting-started.md) → [File Operations](file-operations.md) → [Error Handling](error-handling.md)

### For AI Agent Developers
Start with [Getting Started](getting-started.md) → [Memory Management](memory-management.md) → [Semantic Search](semantic-search.md)

### For System Administrators
Start with [Configuration](configuration.md) → [CLI Reference](cli/index.md) → [RPC/Server API](rpc-api.md)

### For Security Engineers
Start with [Permissions](permissions.md) → [Configuration](configuration.md) → [Error Handling](error-handling.md)

## 📖 API Overview

### Core Interface

| Category | Methods | Documentation |
|----------|---------|---------------|
| **Connection** | `connect()` | [Core API](core-api.md) |
| **File Ops** | `write()`, `read()`, `delete()`, `rename()`, `write_batch()` | [File Operations](file-operations.md) |
| **Discovery** | `list()`, `glob()`, `grep()` | [File Discovery](file-discovery.md) |
| **Directories** | `mkdir()`, `rmdir()`, `is_directory()` | [Directory Operations](directory-operations.md) |
| **Versions** | `get_version()`, `list_versions()`, `rollback()`, `diff_versions()` | [Versioning](versioning.md) |
| **Snapshots** | `workspace_snapshot()`, `workspace_restore()`, `workspace_log()` | [Versioning](versioning.md) |
| **Workspaces** | `register_workspace()`, `unregister_workspace()`, `list_workspaces()` | [Workspace Management](workspace-management.md) |
| **Memories** | `register_memory()`, `unregister_memory()`, `list_memories()` | [Memory Management](memory-management.md) |
| **Search** | `semantic_search()`, `initialize_semantic_search()` | [Semantic Search](semantic-search.md) |
| **Metadata** | `export_metadata()`, `import_metadata()`, `batch_get_content_ids()` | [Metadata](metadata.md) |

### CLI Interface

Complete command reference: [CLI Reference](cli/index.md)

```bash
# File operations (local mode)
nexus write /path/to/file.txt "content"
nexus read /path/to/file.txt
nexus delete /path/to/file.txt

# Discovery
nexus ls /documents
nexus glob "**/*.py"
nexus grep "TODO" --file-pattern "**/*.py"

# Remote mode - connect to server
export NEXUS_URL=http://localhost:8765
export NEXUS_API_KEY=your-api-key
nexus write /file.txt "remote data"  # All commands work remotely

# Server mode
nexus serve --host 0.0.0.0 --port 8080
```

## ✅ SDK/RPC/CLI Parity (v0.6.0+)

**Status: 100% Complete**

As of v0.6.0, Nexus provides complete feature parity between local (embedded) and remote (client-server) modes across **both SDK and CLI**:

- ✅ All 52 SDK methods available in both local and remote modes
- ✅ All CLI commands support remote mode via `--remote-url` or `NEXUS_URL` env var
- ✅ Mount management directly exposed on SDK and CLI
- ✅ ReBAC methods directly available as `nx.rebac_*` (SDK) and `nexus rebac` (CLI)
- ✅ Namespace and privacy methods added to SDK and CLI
- ✅ Memory commands (`store`, `query`, `search`, `list`) work remotely via `NEXUS_URL`
- ✅ Legacy ACL/UNIX methods properly deprecated with migration guidance

See [SDK Parity Matrix](sdk-parity-matrix.md) for complete comparison.

## 🔧 Configuration

Nexus supports multiple configuration methods:

```python
# Auto-detect (uses defaults)
nx = nexus.connect()

# Inline config
nx = nexus.connect(config={"data_dir": "./my-data"})

# Config file
nx = nexus.connect(config="./nexus.yaml")

# Environment variables
# Set NEXUS_DATA_DIR=/path/to/data
nx = nexus.connect()
```

See [Configuration](configuration.md) for complete details.

## 🌐 Deployment Modes

Nexus supports three deployment modes with one codebase:

- **Embedded** - Zero-deployment, library mode - Currently available
- **Monolithic** - Single server for teams - Coming soon
- **Distributed** - Kubernetes-ready for enterprise scale - Coming soon

See [Core API](core-api.md) for mode details.

## 🔒 Security & Permissions

Nexus provides enterprise-grade access control:

- **Operation Contexts** - User/group-based permissions
- **ReBAC (Relationship-Based Access Control)** - Fine-grained authorization
- **Tenant Isolation** - Multi-tenant security
- **Directory Inheritance** - Automatic permission propagation

See [Permissions](permissions.md) for complete security documentation.

## 🎓 Learning Path

### Beginner
1. [Getting Started](getting-started.md) - Install and run first example
2. [File Operations](file-operations.md) - Learn basic CRUD operations
3. [File Discovery](file-discovery.md) - Find and search files

### Intermediate
4. [Versioning](versioning.md) - Track changes and snapshots
5. [Configuration](configuration.md) - Customize your setup
6. [Permissions](permissions.md) - Secure your data

### Advanced
7. [Semantic Search](semantic-search.md) - AI-powered search
8. [Advanced Usage](advanced-usage.md) - Optimization and patterns
9. [RPC/Server API](rpc-api.md) - Build distributed systems

## 📦 Version Information

- **API Stability:** Beta
- **Python Required:** 3.11+
- **Breaking Changes:** See [Migration & Compatibility](migration-compatibility.md)

## 🤝 Support

- **Issues:** [GitHub Issues](https://github.com/nexus-ai/nexus/issues)
- **Discussions:** [GitHub Discussions](https://github.com/nexus-ai/nexus/discussions)
- **Documentation:** This API reference + [Main Docs](../../README.md)

## 📄 Related Documentation

- [Main Documentation](../../README.md)
- [Architecture Overview](../../ARCHITECTURE.md)
- [Examples](../../examples/)
- [API Verification Report](../API_VERIFICATION_REPORT.md)

---

**Generated from:** [api.md reorganization](API_BREAKDOWN_PLAN.md)
