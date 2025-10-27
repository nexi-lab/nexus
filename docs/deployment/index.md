# Deployment Guide

Deploy Nexus in any environment - from local development to global production.

---

## Deployment Modes

<div class="features-grid" markdown>

<div class="feature-card" markdown>
### :material-laptop: Embedded Mode
SQLite-based deployment for development and single-server production.

**Best for:** Development, single-server apps, edge deployments

[Learn More →](server-setup.md#embedded-mode){ .md-button }
</div>

<div class="feature-card" markdown>
### :material-server: Server Mode
Centralized server with client connections.

**Best for:** Team collaboration, distributed systems

[Learn More →](server-setup.md){ .md-button }
</div>

<div class="feature-card" markdown>
### :material-database: PostgreSQL Mode
Production-grade deployment with PostgreSQL backend.

**Best for:** Production, high-availability, compliance

[Learn More →](postgresql.md){ .md-button }
</div>

<div class="feature-card" markdown>
### :material-shield-key: Authenticated Mode
Secure deployment with API key authentication.

**Best for:** Public-facing services, multi-tenant SaaS

[Learn More →](../authentication.md){ .md-button }
</div>

</div>

---

## Quick Start Deployment

=== "Embedded (Development)"

    ```python
    import nexus

    # Embedded mode with SQLite
    nx = nexus.connect(config={
        "data_dir": "./nexus-data",
        "backend": "sqlite"
    })
    ```

=== "Server (Production)"

    ```bash
    # Start server
    nexus serve \
      --host 0.0.0.0 \
      --port 8080 \
      --data-dir /var/lib/nexus \
      --backend postgresql

    # Connect clients
    export NEXUS_REMOTE_URL=http://nexus.example.com:8080
    export NEXUS_API_KEY=your-api-key
    ```

=== "PostgreSQL (Enterprise)"

    ```python
    import nexus

    nx = nexus.connect(config={
        "backend": "postgresql",
        "db_url": "postgresql://user:pass@localhost/nexus",
        "data_dir": "/var/lib/nexus"
    })
    ```

=== "Docker (Quick)"

    ```bash
    # Run Nexus in Docker
    docker run -d \
      -p 8080:8080 \
      -v nexus-data:/data \
      -e NEXUS_BACKEND=postgresql \
      -e NEXUS_DB_URL=postgresql://... \
      nexus/nexus:latest
    ```

---

## Architecture Patterns

### Single Server

```mermaid
graph LR
    A[Client 1] --> S[Nexus Server]
    B[Client 2] --> S
    C[Client 3] --> S
    S --> D[(SQLite/PostgreSQL)]
```

Best for small to medium deployments.

### High Availability

```mermaid
graph LR
    A[Load Balancer] --> S1[Nexus Server 1]
    A --> S2[Nexus Server 2]
    A --> S3[Nexus Server 3]
    S1 --> D[(PostgreSQL HA)]
    S2 --> D
    S3 --> D
```

Best for production with high availability requirements.

### Multi-Region

```mermaid
graph TB
    A[Region 1] --> P1[PostgreSQL Primary]
    B[Region 2] --> P2[PostgreSQL Replica]
    C[Region 3] --> P3[PostgreSQL Replica]
```

Best for global deployments with low latency requirements.

---

## Deployment Checklist

!!! success "Production Readiness"
    Before deploying to production, ensure you have:

    - [ ] **Backend**: PostgreSQL configured and tested
    - [ ] **Authentication**: API keys enabled and rotated
    - [ ] **Permissions**: ReBAC policies configured
    - [ ] **Monitoring**: Logs and metrics collection setup
    - [ ] **Backups**: Automated backup strategy in place
    - [ ] **Testing**: Load testing completed
    - [ ] **Security**: Network policies and firewalls configured
    - [ ] **Documentation**: Runbooks and incident response plans ready

---

## Performance Tuning

### Database Optimization

```sql
-- PostgreSQL performance settings
ALTER SYSTEM SET shared_buffers = '2GB';
ALTER SYSTEM SET effective_cache_size = '6GB';
ALTER SYSTEM SET work_mem = '16MB';
ALTER SYSTEM SET maintenance_work_mem = '512MB';
```

### Connection Pooling

```python
nx = nexus.connect(config={
    "backend": "postgresql",
    "db_url": "postgresql://...",
    "pool_size": 20,
    "max_overflow": 10,
    "pool_timeout": 30
})
```

### Caching

```python
nx = nexus.connect(config={
    "enable_metadata_cache": True,
    "cache_ttl": 300,  # 5 minutes
    "cache_size": 1000
})
```

---

## Monitoring

### Health Checks

```bash
# Health endpoint
curl http://localhost:8080/health

# Detailed metrics
curl http://localhost:8080/metrics
```

### Logging

```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
```

---

## Troubleshooting

<div class="value-prop-grid" markdown>

<div class="value-prop" markdown>
#### Connection Issues
- Check firewall rules
- Verify server is running: `nexus health`
- Test network connectivity: `curl http://server:8080/health`
</div>

<div class="value-prop" markdown>
#### Performance Issues
- Monitor database connections
- Check disk I/O and space
- Review slow query logs
- Enable connection pooling
</div>

<div class="value-prop" markdown>
#### Authentication Errors
- Verify API key is correct
- Check key hasn't expired
- Ensure proper headers are set
- Review permission policies
</div>

</div>

---

## Next Steps

- [Server Setup Guide](server-setup.md)
- [PostgreSQL Configuration](postgresql.md)
- [Authentication Setup](../authentication.md)
- [Performance Optimization](../advanced/sql-patterns.md)
