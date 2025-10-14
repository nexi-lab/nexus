# Configuration

Nexus supports multiple configuration methods across all deployment modes.

## Configuration Methods

### 1. Configuration File (nexus.yaml)

The recommended method for persistent configuration.

```yaml
# Deployment mode: embedded, monolithic, or distributed
mode: embedded

# Data directory for embedded mode
data_dir: ./nexus-data

# Cache configuration
cache_size_mb: 100
cache_ttl_seconds: 3600

# Feature flags
enable_vector_search: true
enable_llm_cache: true

# Logging
log_level: INFO
log_format: json
```

### 2. Environment Variables

Override any configuration with environment variables:

```bash
export NEXUS_MODE=embedded
export NEXUS_DATA_DIR=./nexus-data
export NEXUS_CACHE_SIZE_MB=100
export NEXUS_LOG_LEVEL=DEBUG
```

### 3. Programmatic Configuration

Configure directly in Python:

```python
import nexus

config = {
    "mode": "embedded",
    "data_dir": "./nexus-data",
    "cache_size_mb": 100,
    "enable_vector_search": True
}

nx = nexus.connect(config=config)
```

## Configuration Reference

### General Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `mode` | string | `embedded` | Deployment mode: embedded, monolithic, distributed |
| `data_dir` | string | `./nexus-data` | Data directory for embedded mode |
| `log_level` | string | `INFO` | Logging level: DEBUG, INFO, WARNING, ERROR |
| `log_format` | string | `text` | Log format: text or json |

### Cache Configuration

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `cache_size_mb` | integer | `100` | Maximum cache size in megabytes |
| `cache_ttl_seconds` | integer | `3600` | Cache entry time-to-live |
| `cache_type` | string | `memory` | Cache backend: memory or redis |

### Feature Flags

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `enable_vector_search` | boolean | `false` | Enable semantic search features |
| `enable_llm_cache` | boolean | `false` | Enable LLM response caching |
| `enable_metrics` | boolean | `false` | Enable metrics collection |

### Database Configuration (Monolithic/Distributed)

```yaml
database:
  url: postgresql://user:pass@localhost/nexus
  pool_size: 20
  max_overflow: 10
  echo: false
```

### Redis Configuration (Monolithic/Distributed)

```yaml
redis:
  url: redis://localhost:6379
  db: 0
  password: null
  ssl: false
```

### Vector Database Configuration

```yaml
vector_db:
  type: qdrant
  url: http://localhost:6333
  collection_name: nexus
  embedding_dim: 1536
```

## Example Configurations

### Development

```yaml
mode: embedded
data_dir: ./nexus-dev
cache_size_mb: 50
log_level: DEBUG
enable_vector_search: true
enable_llm_cache: true
```

### Production (Monolithic)

```yaml
mode: monolithic
log_level: INFO
log_format: json
enable_metrics: true

database:
  url: postgresql://nexus:${DB_PASSWORD}@postgres:5432/nexus
  pool_size: 20

redis:
  url: redis://:${REDIS_PASSWORD}@redis:6379
  db: 0
  ssl: true

vector_db:
  type: qdrant
  url: http://qdrant:6333
  collection_name: nexus_prod
```

### Production (Distributed)

```yaml
mode: distributed
log_level: INFO
log_format: json
enable_metrics: true
enable_tracing: true

database:
  url: postgresql://nexus:${DB_PASSWORD}@postgres-ha:5432/nexus
  pool_size: 50
  max_overflow: 20

redis:
  url: redis-cluster://:${REDIS_PASSWORD}@redis-cluster:6379
  db: 0
  ssl: true

vector_db:
  type: qdrant
  url: http://qdrant-cluster:6333
  collection_name: nexus_prod

observability:
  prometheus_port: 9090
  jaeger_endpoint: http://jaeger:14268/api/traces
  loki_endpoint: http://loki:3100/loki/api/v1/push
```

## Next Steps

- [Deployment Modes](deployment-modes.md) - Learn about different deployment options
- [Security](../reference/security.md) - Security configuration
- [Monitoring](../reference/monitoring.md) - Observability setup
