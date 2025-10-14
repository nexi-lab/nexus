# Database Compatibility Guide

This document explains how the Nexus metadata store works with different database backends.

## Overview

Nexus uses SQLAlchemy for database operations, which provides database-agnostic ORM capabilities. The metadata store can work with both **SQLite** (embedded mode) and **PostgreSQL** (monolithic/distributed modes).

## SQLite (Embedded Mode - v0.1.0)

**Default configuration for embedded mode.**

### Features
- Single file database (no server required)
- Perfect for local/embedded deployments
- WAL (Write-Ahead Logging) mode for better concurrency
- Foreign key constraints enabled
- Connection pooling with `NullPool`

### Configuration

```python
from nexus.storage.metadata_store import SQLAlchemyMetadataStore

# Default: SQLite
store = SQLAlchemyMetadataStore("./nexus.db")
```

### Alembic Configuration (alembic.ini)

```ini
sqlalchemy.url = sqlite:///nexus.db
```

### Limitations
- UUID stored as String (TEXT) type
- JSONB stored as Text with JSON serialization
- Single file can become a bottleneck at very large scale

## PostgreSQL (Monolithic/Distributed Mode - v0.2.0+)

**Recommended for production deployments.**

### Features
- True UUID type support
- Native JSONB support
- Better concurrency and performance at scale
- Advanced indexing (GIN indexes on JSONB)
- Distributed transaction support

### Configuration

```python
from nexus.storage.metadata_store import SQLAlchemyMetadataStore

# PostgreSQL
store = SQLAlchemyMetadataStore(
    "postgresql://user:password@localhost:5432/nexus"
)
```

### Alembic Configuration (alembic.ini)

```ini
sqlalchemy.url = postgresql://user:password@localhost:5432/nexus
```

### Model Adaptations for PostgreSQL

The current models are designed to work with both databases, but for optimal PostgreSQL usage, you may want to:

1. **Use native UUID type:**
   ```python
   from sqlalchemy.dialects.postgresql import UUID

   path_id: Mapped[uuid.UUID] = mapped_column(
       UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
   )
   ```

2. **Use JSONB for metadata:**
   ```python
   from sqlalchemy.dialects.postgresql import JSONB

   value: Mapped[dict] = mapped_column(JSONB, nullable=True)
   ```

3. **Add GIN indexes:**
   ```python
   Index('idx_file_metadata_value_gin', 'value', postgresql_using='gin')
   ```

## Running Migrations

### SQLite

```bash
# Upgrade to latest
alembic upgrade head

# Downgrade to base
alembic downgrade base

# Create new migration
alembic revision --autogenerate -m "Description"
```

### PostgreSQL

```bash
# Set database URL
export DATABASE_URL="postgresql://user:password@localhost:5432/nexus"

# Update alembic.ini or use command-line override
alembic -x dbUrl=$DATABASE_URL upgrade head
```

Or modify `alembic/env.py` to read from environment:

```python
from os import environ

config.set_main_option(
    'sqlalchemy.url',
    environ.get('DATABASE_URL', 'sqlite:///nexus.db')
)
```

## Type Compatibility Matrix

| SQLAlchemy Type | SQLite Type | PostgreSQL Type | Notes |
|----------------|-------------|-----------------|-------|
| `String(36)` | `TEXT` | `VARCHAR(36)` | Used for UUID strings |
| `Text` | `TEXT` | `TEXT` | Unlimited text |
| `BigInteger` | `BIGINT` | `BIGINT` | Large integers |
| `Integer` | `INTEGER` | `INTEGER` | Standard integers |
| `DateTime` | `DATETIME` | `TIMESTAMP` | Timestamps |
| `Boolean` | `INTEGER` | `BOOLEAN` | True/False |

For PostgreSQL-specific types:
- `UUID` → Native UUID type (requires `sqlalchemy.dialects.postgresql`)
- `JSONB` → Binary JSON storage with indexing

## Performance Considerations

### SQLite
- ✅ Fast for reads
- ✅ No network overhead
- ✅ Simple deployment
- ⚠️ Single writer at a time (mitigated by WAL mode)
- ⚠️ Not ideal for >1GB databases

### PostgreSQL
- ✅ Excellent multi-user concurrency
- ✅ Scales to TB+ datasets
- ✅ Advanced query optimization
- ✅ Replication and high availability
- ⚠️ Requires server infrastructure
- ⚠️ Network latency

## Migration Path: SQLite → PostgreSQL

When growing from embedded to distributed mode:

1. **Export data from SQLite:**
   ```bash
   alembic downgrade base
   pg_dump sqlite_data > export.sql
   ```

2. **Create PostgreSQL database:**
   ```bash
   createdb nexus
   ```

3. **Update configuration:**
   ```python
   # Change from
   store = SQLAlchemyMetadataStore("nexus.db")
   # To
   store = SQLAlchemyMetadataStore("postgresql://user:pass@localhost/nexus")
   ```

4. **Run migrations:**
   ```bash
   alembic upgrade head
   ```

5. **Import data:**
   ```bash
   psql nexus < export.sql
   ```

## Testing with Different Databases

### SQLite Tests (default)
```bash
PYTHONPATH=src python -m pytest tests/unit/storage/
```

### PostgreSQL Tests
```bash
# Start PostgreSQL (Docker)
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:15

# Set test database URL
export TEST_DATABASE_URL="postgresql://postgres:postgres@localhost:5432/nexus_test"

# Run tests
PYTHONPATH=src python -m pytest tests/unit/storage/
```

## Best Practices

1. **Use SQLite for:**
   - Desktop applications
   - Mobile apps
   - Single-user deployments
   - Development/testing
   - Embedded systems

2. **Use PostgreSQL for:**
   - Multi-tenant SaaS
   - High-concurrency applications
   - Large-scale data (>10GB)
   - Production distributed systems
   - When you need replication/HA

3. **Connection pooling:**
   - SQLite: Use `NullPool` (default in our implementation)
   - PostgreSQL: Use `QueuePool` with appropriate pool size

4. **Migrations:**
   - Always test migrations on a copy of production data
   - Use transactions where supported
   - Keep migrations reversible (implement `downgrade()`)

## Future Enhancements (v0.2.0+)

- [ ] Automatic database type detection
- [ ] Connection pool configuration via config file
- [ ] Read replica support for PostgreSQL
- [ ] Partition support for large tables
- [ ] Multi-database sharding
- [ ] CockroachDB support (PostgreSQL-compatible)

## References

- [SQLAlchemy Documentation](https://docs.sqlalchemy.org/)
- [Alembic Documentation](https://alembic.sqlalchemy.org/)
- [SQLite Performance Tuning](https://www.sqlite.org/wal.html)
- [PostgreSQL Best Practices](https://wiki.postgresql.org/wiki/Don't_Do_This)
