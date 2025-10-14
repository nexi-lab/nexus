# Deployment Modes

Nexus supports three deployment modes from a single codebase, allowing you to start small and scale as needed.

## Comparison

| Mode | Users | Data | Use Case | Setup Time |
|------|-------|------|----------|------------|
| **Embedded** | 1 | ~10GB | Individual developers, CLI tools | 60 seconds |
| **Monolithic** | 1-20 | ~100GB | Small teams, staging | 10 minutes |
| **Distributed** | 100+ | Petabyte+ | Enterprise, production | Hours |

## Embedded Mode

### Overview

Embedded mode runs entirely in-process with no external dependencies. Perfect for:

- Individual developers
- CLI tools
- Testing and development
- Desktop applications
- Edge devices

### Architecture

```
┌─────────────────┐
│  Your Application │
│                 │
│  ┌───────────┐ │
│  │   Nexus   │ │  SQLite
│  │  Embedded │ │  Local FS
│  └───────────┘ │  In-memory cache
└─────────────────┘
```

### Setup

```python
import nexus

# Zero configuration - just works
nx = nexus.connect()

# Or with config file
nx = nexus.connect(config_file="nexus.yaml")
```

### Features

- ✅ File operations (read, write, delete, list)
- ✅ SQLite metadata store
- ✅ Local filesystem backend
- ✅ In-memory caching
- ✅ Basic file search (glob, grep)
- ✅ Batch operations
- ❌ Multi-user support
- ❌ Remote access
- ❌ High availability

## Monolithic Mode

### Overview

Monolithic mode runs as a single server with external databases. Perfect for:

- Small to medium teams (1-20 users)
- Staging environments
- Internal tools
- Department-level deployments

### Architecture

```
┌────────────┐     ┌──────────────┐
│  Clients   │────▶│  Nexus Server │
│ (REST API) │     │  (Monolithic) │
└────────────┘     └───────┬───────┘
                          │
         ┌────────────────┼────────────────┐
         │                │                │
    ┌────▼────┐    ┌─────▼──────┐   ┌────▼────┐
    │PostgreSQL│    │   Redis    │   │ Qdrant  │
    └──────────┘    └────────────┘   └─────────┘
```

### Setup

#### Using Docker Compose

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
      - QDRANT_URL=http://qdrant:6333
    volumes:
      - ./config.yaml:/app/config.yaml
      - nexus-data:/data

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

  qdrant:
    image: qdrant/qdrant:latest
    ports:
      - "6333:6333"
    volumes:
      - qdrant-data:/qdrant/storage

volumes:
  nexus-data:
  postgres-data:
  redis-data:
  qdrant-data:
```

Start with:

```bash
docker-compose up -d
```

### Features

- ✅ All embedded mode features
- ✅ Multi-user support
- ✅ REST API
- ✅ API key authentication
- ✅ PostgreSQL metadata store
- ✅ Redis caching
- ✅ Vector search (Qdrant)
- ✅ LLM integration
- ✅ Job scheduling
- ❌ High availability
- ❌ Horizontal scaling

## Distributed Mode

### Overview

Distributed mode is a fully distributed architecture with high availability. Perfect for:

- Enterprise deployments
- Production environments
- High-scale applications (100+ users)
- Mission-critical systems

### Architecture

```
          ┌─────────────┐
          │Load Balancer│
          └──────┬──────┘
                 │
     ┌───────────┴───────────┐
     │                       │
┌────▼─────┐          ┌─────▼────┐
│ Nexus    │   ...    │  Nexus   │
│ Worker 1 │          │ Worker N │
└────┬─────┘          └─────┬────┘
     │                      │
     └──────────┬───────────┘
                │
     ┌──────────┴──────────────────┐
     │                             │
┌────▼────────┐         ┌─────────▼──────┐
│ PostgreSQL  │         │ Redis Cluster  │
│   HA/RR     │         │  (Sentinel)    │
└─────────────┘         └────────────────┘
     │                             │
     │          ┌─────────▼────────┤
     │          │                  │
┌────▼──────┐  │  ┌──────────┐   │
│  Qdrant   │  │  │   S3     │   │
│  Cluster  │  │  └──────────┘   │
└───────────┘  │                  │
               └──────────────────┘
```

### Setup

#### Using Kubernetes with Helm

```bash
# Add the Nexus Helm repository
helm repo add nexus https://charts.nexus.io
helm repo update

# Install with production configuration
helm install nexus nexus/nexus-distributed \
  --set replicas=5 \
  --set postgres.enabled=true \
  --set postgres.ha=true \
  --set redis.enabled=true \
  --set redis.cluster=true \
  --set qdrant.enabled=true \
  --set qdrant.replicas=3 \
  --set autoscaling.enabled=true \
  --set autoscaling.minReplicas=3 \
  --set autoscaling.maxReplicas=20 \
  --namespace nexus \
  --create-namespace \
  --values production-values.yaml
```

Example `production-values.yaml`:

```yaml
replicaCount: 5

image:
  repository: nexus/nexus
  tag: latest
  pullPolicy: Always

resources:
  limits:
    cpu: 4000m
    memory: 8Gi
  requests:
    cpu: 2000m
    memory: 4Gi

autoscaling:
  enabled: true
  minReplicas: 3
  maxReplicas: 20
  targetCPUUtilizationPercentage: 70
  targetMemoryUtilizationPercentage: 80

postgres:
  enabled: true
  ha: true
  replicaCount: 3
  resources:
    limits:
      cpu: 2000m
      memory: 4Gi

redis:
  enabled: true
  cluster: true
  nodes: 6

qdrant:
  enabled: true
  replicas: 3

monitoring:
  enabled: true
  prometheus: true
  grafana: true
  jaeger: true
  loki: true

ingress:
  enabled: true
  className: nginx
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
  hosts:
    - host: nexus.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: nexus-tls
      hosts:
        - nexus.example.com
```

### Features

- ✅ All monolithic mode features
- ✅ High availability
- ✅ Horizontal scaling
- ✅ Load balancing
- ✅ Automatic failover
- ✅ Distributed caching
- ✅ Multi-region support
- ✅ Advanced monitoring
- ✅ Distributed tracing

## Choosing a Mode

### Start with Embedded if:

- You're a single developer
- Building a CLI tool or desktop app
- Need quick prototyping
- Running on edge devices
- Don't need multi-user support

### Upgrade to Monolithic when:

- You have a small team (2-20 users)
- Need multi-user support
- Require API access
- Want vector search
- Need LLM integration

### Move to Distributed when:

- Serving 100+ users
- Need high availability
- Require horizontal scaling
- Have compliance requirements
- Operating at enterprise scale

## Migration Path

Nexus makes it easy to migrate between modes:

1. **Embedded → Monolithic**
   ```bash
   # Export metadata
   nexus export --format jsonl > metadata.jsonl

   # Import to monolithic
   nexus import --url http://server:8080 < metadata.jsonl
   ```

2. **Monolithic → Distributed**
   ```bash
   # Database migration handled automatically
   # Just update configuration and redeploy
   ```

## Next Steps

- [Embedded Deployment](../deployment/embedded.md)
- [Monolithic Deployment](../deployment/monolithic.md)
- [Distributed Deployment](../deployment/distributed.md)
- [Configuration Guide](configuration.md)
