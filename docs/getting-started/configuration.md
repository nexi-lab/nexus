# Configuration

Nexus supports three deployment modes: **standalone**, **remote**, and **federation**.

## Configuration Methods

### 1. Configuration File (nexus.yaml)

Create `nexus.yaml` in your project directory:

```yaml
# Deployment mode: standalone | remote | federation
mode: standalone

# Data directory for storing files and metadata
data_dir: ./nexus-data
```

### 2. Environment Variables

Override configuration with environment variables:

```bash
export NEXUS_MODE=standalone
export NEXUS_DATA_DIR=./nexus-data
```

### 3. Programmatic Configuration

Configure directly in Python:

```python
import nexus

# Using dict
config = {
    "mode": "standalone",
    "data_dir": "./nexus-data",
}
nx = nexus.connect(config=config)

# Using file path
nx = nexus.connect("./config.yaml")
```

## Configuration Reference

### Core Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `mode` | string | `standalone` | Deployment mode (`standalone`, `remote`, `federation`) |
| `data_dir` | string | `./nexus-data` | Directory for storing files and metadata |
| `url` | string | — | Server URL (required for `remote` mode) |
| `api_key` | string | — | Authentication key for remote/server access |

### Additional Options

- `cache_size_mb` — In-memory cache size
- `enable_vector_search` — Vector search features
- `enable_llm_cache` — LLM response caching

## Example Configurations

### Development (Standalone)

```yaml
mode: standalone
data_dir: ./nexus-dev
```

### Production (Standalone Server)

```yaml
mode: standalone
data_dir: /var/lib/nexus
```

### Remote Client

```yaml
mode: remote
url: https://nexus.example.com:2026
api_key: sk-your-api-key
```

### Federation (Multi-Node)

```bash
# Environment variables for federation nodes
NEXUS_MODE=federation
NEXUS_NODE_ID=1
NEXUS_BIND_ADDR=0.0.0.0:2126
NEXUS_PEERS=2@peer2:2126,3@peer3:2126
```

### Using Environment Variables

```bash
# .env file
NEXUS_MODE=standalone
NEXUS_DATA_DIR=/data/nexus
```

## Configuration Discovery

Nexus searches for configuration in this order:

1. Explicit config passed to `nexus.connect()`
2. Environment variables (`NEXUS_*`)
3. `./nexus.yaml` in current directory
4. `./nexus.yml` in current directory
5. `~/.nexus/config.yaml` in home directory
6. Default values (standalone mode with `./nexus-data`)

## Next Steps

- [Deployment Modes](deployment-modes.md) - Learn about standalone, remote, and federation modes
- [Quick Start](quickstart.md) - Get started with standalone mode
