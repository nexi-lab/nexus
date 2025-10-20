# Nexus Plugin Examples

Complete examples for working with the Nexus plugin system.

## Quick Start

### Prerequisites

**Install plugins from local source:**
```bash
# From the main nexus directory
pip install -e ./nexus-plugin-anthropic
pip install -e ./nexus-plugin-skill-seekers

# Verify
nexus plugins list
```

See [PLUGIN_INSTALLATION.md](../PLUGIN_INSTALLATION.md) for detailed setup.

### Run Examples

```bash
# CLI demo
./examples/plugin_cli_demo.sh

# SDK demo
python examples/plugin_sdk_demo.py
```

## Available Examples

### 1. Plugin CLI Demo (`plugin_cli_demo.sh`)

Demonstrates plugin management via CLI commands.

**Run it:**
```bash
./plugin_cli_demo.sh
```

### 2. Plugin SDK Demo (`plugin_sdk_demo.py`)

Shows programmatic plugin usage with Python API.

**Run it:**
```bash
python plugin_sdk_demo.py
```

## Documentation

- [Plugin Installation Guide](../PLUGIN_INSTALLATION.md)
- [Plugin Development Guide](../docs/PLUGIN_DEVELOPMENT.md)
- [Plugin System Overview](../docs/PLUGIN_SYSTEM.md)
