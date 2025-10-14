# Configuration

Nexus v0.1.0 supports embedded mode with simple configuration options.

## Configuration Methods

### 1. Configuration File (nexus.yaml)

Create `nexus.yaml` in your project directory:

```yaml
# Deployment mode (only 'embedded' is implemented in v0.1.0)
mode: embedded

# Data directory for storing files and metadata
data_dir: ./nexus-data

# Optional: Custom database path (auto-generated in data_dir if not specified)
# db_path: ./custom-path/metadata.db
```

### 2. Environment Variables

Override configuration with environment variables:

```bash
export NEXUS_MODE=embedded
export NEXUS_DATA_DIR=./nexus-data
export NEXUS_DB_PATH=./custom-metadata.db
```

### 3. Programmatic Configuration

Configure directly in Python:

```python
import nexus

# Using dict
config = {
    "mode": "embedded",
    "data_dir": "./nexus-data",
}
nx = nexus.connect(config=config)

# Using file path
nx = nexus.connect("./config.yaml")
```

## Configuration Reference

### Available Options (v0.1.0)

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `mode` | string | `embedded` | Deployment mode (only `embedded` supported in v0.1.0) |
| `data_dir` | string | `./nexus-data` | Directory for storing files and database |
| `db_path` | string | `{data_dir}/metadata.db` | Path to SQLite metadata database |

### Planned Options (Future Versions)

These config options exist in the code but are not yet implemented:

- `cache_size_mb` - In-memory cache (planned for v0.2.0)
- `enable_vector_search` - Vector search features (planned for v0.2.0+)
- `enable_llm_cache` - LLM response caching (planned for v0.3.0+)
- `url` - Server URL for monolithic/distributed modes (v0.5.0+)
- `api_key` - Authentication for remote modes (v0.5.0+)

## Example Configurations

### Development

```yaml
mode: embedded
data_dir: ./nexus-dev
```

### Production (Embedded)

```yaml
mode: embedded
data_dir: /var/lib/nexus
db_path: /var/lib/nexus/metadata.db
```

### Using Environment Variables

```bash
# .env file
NEXUS_MODE=embedded
NEXUS_DATA_DIR=/data/nexus
```

## Configuration Discovery

Nexus searches for configuration in this order:

1. Explicit config passed to `nexus.connect()`
2. Environment variables (`NEXUS_*`)
3. `./nexus.yaml` in current directory
4. `./nexus.yml` in current directory
5. `~/.nexus/config.yaml` in home directory
6. Default values (embedded mode with `./nexus-data`)

## Next Steps

- [Deployment Modes](deployment-modes.md) - Learn about deployment options (v0.1.0 only supports embedded)
- [Quick Start](quickstart.md) - Get started with embedded mode
