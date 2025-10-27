<div align="center">
  <img src="assets/logo.png" alt="Nexus Logo" width="180"/>

  # Nexus Documentation

  Welcome to the Nexus documentation! This directory contains comprehensive guides for using and developing Nexus.
</div>

---

## 📖 [Complete Documentation Index](DOCUMENTATION_INDEX.md)

For a comprehensive, organized view of all documentation, see **[DOCUMENTATION_INDEX.md](DOCUMENTATION_INDEX.md)**.

The index includes:
- Quick start guides
- API reference
- Deployment guides
- Development guides
- Use case-driven navigation
- Examples and demos

---

## Quick Links

### Getting Started
- [Quick Start](getting-started/quickstart.md)
- [Installation](getting-started/installation.md)
- [Configuration](getting-started/configuration.md)

### API Reference
- [API Documentation](api/api.md)
- [Core API](api/core-api.md)
- [File Operations](api/file-operations.md)
- [CLI Reference](api/cli-reference.md)

### Deployment
- [Deployment Guide](deployment/DEPLOYMENT.md)
- [Docker Deployment](deployment/DOCKER_DEPLOYMENT.md)
- [GCP Deployment](deployment/GCP_DEPLOYMENT.md)

### Development
- [Development Guide](development/development.md)
- [Architecture](architecture/ARCHITECTURE.md)
- [Core Tenets](CORE_TENETS.md)

### Examples
- [Authentication Demos](../examples/auth_demo/)
- [Parity Demos](../examples/parity_demo/)
- [Python SDK Demos](../examples/py_demo/)

---

## Quick Start

### Installation

```bash
pip install nexus-ai-fs
```

### Basic Usage

```python
import nexus

# Connect to Nexus
nx = nexus.connect(config={"data_dir": "./nexus-data"})

# Write a file
nx.write("/hello.txt", b"Hello, Nexus!")

# Read a file
content = nx.read("/hello.txt")
print(content.decode())  # "Hello, Nexus!"

# List files
files = nx.list("/")

# Close
nx.close()
```

**→ For complete working examples and setup instructions, see [Getting Started Guide](getting-started/quickstart.md)**

**→ For production deployment with authentication, see [Authentication Guide](authentication.md)**

**→ See [Complete Documentation Index](DOCUMENTATION_INDEX.md) for all guides and references**

---

## Documentation Structure

```
docs/
├── DOCUMENTATION_INDEX.md   # Complete index (start here!)
├── api/                     # API reference documentation
├── architecture/            # Architecture documents
├── deployment/              # Deployment guides
├── design/                  # Design documents
├── development/             # Development guides
├── getting-started/         # Getting started guides
└── *.md                     # Major feature documentation
```

---

## Getting Help

- **GitHub Issues**: https://github.com/nexi-lab/nexus/issues
- **Examples**: See `../examples/` directory
- **Integration Tests**: See `../tests/integration/` for working examples
- **Full Documentation**: [DOCUMENTATION_INDEX.md](DOCUMENTATION_INDEX.md)

---

## License

Apache License 2.0 - See `../LICENSE` for details.
