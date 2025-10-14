# Nexus Documentation

Welcome to the Nexus documentation! This directory contains comprehensive guides for using and developing Nexus.

## Documentation Index

### 📘 User Documentation

#### [API Reference](./api.md)
Complete Python API documentation for Nexus embedded mode.

**Contents:**
- Getting started guide
- Core API reference (`nexus.connect()`, file operations)
- Configuration options
- Error handling
- Advanced usage patterns
- Code examples

**For:** Application developers using Nexus

---

#### [Database Compatibility Guide](./DATABASE_COMPATIBILITY.md)
Understanding database backends and compatibility.

**Contents:**
- SQLite vs PostgreSQL comparison
- Configuration examples
- Type compatibility matrix
- Performance considerations
- Migration path (SQLite → PostgreSQL)
- Testing with different databases

**For:** Deployment engineers, DBAs

---

### 🔧 Developer Documentation

#### [Development Guide](./development.md)
Complete guide for contributing to Nexus.

**Contents:**
- Development environment setup
- Project structure overview
- Development workflow
- Testing guidelines
- Database migration workflow
- Code style and conventions
- Contributing guidelines

**For:** Contributors, maintainers

---

## Quick Links

### Getting Started
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Examples](../examples/)

### API Reference
- [nexus.connect()](./api.md#nexusconnect)
- [File Operations](./api.md#file-operations)
- [Configuration](./api.md#configuration)
- [Error Handling](./api.md#error-handling)

### Development
- [Setup](./development.md#getting-started)
- [Running Tests](./development.md#testing)
- [Code Style](./development.md#code-style)
- [Contributing](./development.md#contributing)

---

## Installation

```bash
# Install from source
git clone https://github.com/nexi-lab/nexus.git
cd nexus
pip install -e .

# Or just install dependencies
pip install sqlalchemy alembic
```

---

## Quick Start

```python
import nexus

# Connect to Nexus (auto-creates database)
nx = nexus.connect(config={"data_dir": "./nexus-data"})

# Write a file
nx.write("/hello.txt", b"Hello, Nexus!")

# Read a file
content = nx.read("/hello.txt")
print(content.decode())

# List files
files = nx.list()

# Close
nx.close()
```

**→ See [API Reference](./api.md) for complete documentation**

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                   USER APPLICATION                      │
│                 (your Python code)                      │
└────────────────────┬────────────────────────────────────┘
                     │
                     │ import nexus
                     │ nx = nexus.connect()
                     ▼
┌─────────────────────────────────────────────────────────┐
│              nexus.connect()                            │
│              (auto-detects mode)                        │
└────────────────────┬────────────────────────────────────┘
                     │ Returns Embedded instance
                     ▼
┌─────────────────────────────────────────────────────────┐
│              Embedded Filesystem                        │
│              (nexus.core.embedded)                      │
├─────────────────────────────────────────────────────────┤
│  • Path validation                                      │
│  • ETag computation                                     │
│  • Automatic metadata tracking                          │
└─────┬──────────────────────────────────┬────────────────┘
      │                                  │
      │ Metadata                         │ Storage
      ▼                                  ▼
┌──────────────────────┐      ┌──────────────────────────┐
│ SQLAlchemy Metadata  │      │   Storage Backend        │
│      Store           │      │   (LocalBackend)         │
├──────────────────────┤      ├──────────────────────────┤
│ • FilePathModel      │      │ • Physical file I/O      │
│ • FileMetadataModel  │      │ • Local filesystem       │
│ • ContentChunkModel  │      │   operations             │
└──────┬───────────────┘      └──────────────────────────┘
       │
       ▼
┌──────────────────────┐
│   SQLite Database    │
│   (metadata.db)      │
└──────────────────────┘
```

---

## Project Structure

```
nexus/
├── src/nexus/              # Source code
│   ├── __init__.py         # Main entry (nexus.connect)
│   ├── core/               # Core functionality
│   │   ├── embedded.py     # Embedded mode
│   │   └── backends/       # Storage backends
│   └── storage/            # SQLAlchemy storage layer
│       ├── models.py       # Database models
│       └── metadata_store.py  # Metadata store
│
├── tests/                  # Test suite
│   ├── unit/               # Unit tests
│   └── integration/        # Integration tests
│
├── alembic/                # Database migrations
│   └── versions/           # Migration files
│
├── examples/               # Example code
│   └── integrated_demo.py  # Main demo
│
├── docs/                   # Documentation (you are here!)
│   ├── README.md           # This file
│   ├── api.md              # API reference
│   ├── development.md      # Development guide
│   └── DATABASE_COMPATIBILITY.md  # Database guide
│
└── pyproject.toml          # Project config
```

---

## Features by Version

### ✅ v0.1.0 (Current)
- Embedded mode
- SQLite metadata store
- SQLAlchemy ORM models
- Alembic migrations
- Soft delete support
- Custom metadata
- Local file backend
- Automatic metadata tracking

### 🚧 v0.2.0+ (Planned)
- PostgreSQL support
- Multi-tenancy
- Multiple backends (S3, GCS, Azure)
- Content deduplication
- Version tracking
- Distributed locking

---

## Learning Path

### For Application Developers

1. **Start here:** [Quick Start](#quick-start)
2. **Read:** [API Reference](./api.md)
3. **Try:** [Examples](../examples/integrated_demo.py)
4. **Deep dive:** [Database Guide](./DATABASE_COMPATIBILITY.md)

### For Contributors

1. **Setup:** [Development Guide - Getting Started](./development.md#getting-started)
2. **Understand:** [Project Structure](./development.md#project-structure)
3. **Practice:** [Development Workflow](./development.md#development-workflow)
4. **Contribute:** [Pull Request Process](./development.md#contributing)

---

## Additional Resources

- **Contributing Guidelines**: `../CONTRIBUTING.md`
- **Example Code**: `../examples/`
- **Issue Tracker**: https://github.com/nexi-lab/nexus/issues

---

## Support

### Documentation Issues
If you find errors or have suggestions for documentation improvements:
1. Open an issue: https://github.com/nexi-lab/nexus/issues
2. Tag it with `documentation`
3. Describe the problem or suggestion

### Getting Help
- **Questions**: GitHub Discussions
- **Bugs**: GitHub Issues
- **Feature Requests**: GitHub Issues

---

## License

Apache License 2.0 - See `../LICENSE` for details.
