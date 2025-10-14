# Quick Start

Get started with Nexus in minutes with our three deployment modes.

## Embedded Mode (60 seconds)

Perfect for individual developers and CLI tools.

### 1. Install Nexus

```bash
uv pip install nexus-ai-fs
```

### 2. Create a Configuration File

Create `nexus.yaml`:

```yaml
mode: embedded
data_dir: ./nexus-data
cache_size_mb: 100
enable_vector_search: true
```

### 3. Start Using Nexus

```python
import nexus

# Auto-discovers nexus.yaml
nx = nexus.connect()

async with nx:
    # Write and read files
    await nx.write("/workspace/data.txt", b"Hello World")
    content = await nx.read("/workspace/data.txt")
    print(content)  # b"Hello World"

    # List files
    files = await nx.list("/workspace/")
    for file in files:
        print(f"{file.path} - {file.size} bytes")
```

## Monolithic Mode (10 minutes)

For small teams and staging environments.

### Using Docker Compose

Create `docker-compose.yml`:

```yaml
version: '3.8'

services:
  nexus:
    image: nexus/nexus:latest
    ports:
      - "8080:8080"
    environment:
      - NEXUS_MODE=monolithic
      - DATABASE_URL=postgresql://nexus:password@postgres:5432/nexus
      - REDIS_URL=redis://redis:6379
    volumes:
      - ./nexus-data:/data
    depends_on:
      - postgres
      - redis

  postgres:
    image: postgres:15
    environment:
      - POSTGRES_DB=nexus
      - POSTGRES_USER=nexus
      - POSTGRES_PASSWORD=password
    volumes:
      - postgres-data:/var/lib/postgresql/data

  redis:
    image: redis:7-alpine
    volumes:
      - redis-data:/data

volumes:
  postgres-data:
  redis-data:
```

Start the services:

```bash
docker-compose up -d
```

### Connect to the Server

```python
import nexus

# Connect to remote server
nx = nexus.connect(
    url="http://localhost:8080",
    api_key="your-api-key"
)

async with nx:
    await nx.write("/workspace/data.txt", b"Hello from server!")
    content = await nx.read("/workspace/data.txt")
```

## Distributed Mode (Hours)

For enterprise production deployments.

### Using Helm

```bash
# Add the Nexus Helm repository
helm repo add nexus https://charts.nexus.io
helm repo update

# Install with custom values
helm install nexus nexus/nexus-distributed \
  --set replicas=5 \
  --set postgres.enabled=true \
  --set redis.enabled=true \
  --set qdrant.enabled=true \
  --namespace nexus \
  --create-namespace
```

## What's Next?

- [Configuration Guide](configuration.md) - Learn about all configuration options
- [Core Concepts](../guide/concepts.md) - Understand Nexus architecture
- [API Reference](../api.md) - Explore the full API
