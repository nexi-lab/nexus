# PostgreSQL Setup Guide for Nexus

This guide explains how to configure Nexus to use PostgreSQL instead of SQLite for metadata storage.

## Overview

Nexus supports multiple database backends:
- **SQLite** (default): Local file-based database, perfect for development and single-user deployments
- **PostgreSQL**: Production-ready relational database with better concurrency and remote access

## When to Use PostgreSQL

Choose PostgreSQL when you need:
- **Multiple concurrent writers**: PostgreSQL handles concurrent writes much better than SQLite
- **Remote database access**: Access your metadata from multiple machines
- **Production workloads**: Better suited for high-traffic production environments
- **Data replication**: Built-in replication and backup features
- **Advanced features**: Better query optimization, full-text search, JSON operations

## Installation

### 1. Install PostgreSQL Driver

```bash
# Install Nexus with PostgreSQL support
pip install nexus-ai-fs[postgres]

# Or install the driver separately
pip install psycopg2-binary
```

### 2. Set Up PostgreSQL Database

#### Option A: Using Docker (Recommended for Development)

```bash
# Start PostgreSQL container
docker run --name nexus-postgres \
  -e POSTGRES_PASSWORD=nexus \
  -e POSTGRES_DB=nexus \
  -e POSTGRES_USER=nexus \
  -p 5432:5432 \
  -d postgres:15

# Verify it's running
docker ps | grep nexus-postgres

# View logs
docker logs nexus-postgres
```

#### Option B: Using Hosted PostgreSQL

You can use any PostgreSQL hosting service:
- **AWS RDS**: Managed PostgreSQL on AWS
- **Google Cloud SQL**: Managed PostgreSQL on GCP
- **Azure Database**: Managed PostgreSQL on Azure
- **Heroku Postgres**: Free tier available
- **Supabase**: Free PostgreSQL with REST API
- **Neon**: Serverless PostgreSQL

#### Option C: Local PostgreSQL Installation

**Ubuntu/Debian:**
```bash
sudo apt update
sudo apt install postgresql postgresql-contrib
sudo systemctl start postgresql
sudo systemctl enable postgresql
```

**macOS (Homebrew):**
```bash
brew install postgresql@15
brew services start postgresql@15
```

**Create Database:**
```bash
# Connect to PostgreSQL
sudo -u postgres psql

# Create database and user
CREATE DATABASE nexus;
CREATE USER nexus WITH PASSWORD 'your_secure_password';
GRANT ALL PRIVILEGES ON DATABASE nexus TO nexus;
\q
```

## Configuration

### Environment Variables

Nexus checks environment variables in this priority order:
1. `NEXUS_DATABASE_URL` - Full PostgreSQL connection URL
2. `POSTGRES_URL` - Alternative to NEXUS_DATABASE_URL
3. Falls back to SQLite if neither is set

### PostgreSQL Connection URL Format

```
postgresql://[user[:password]@][host][:port][/dbname][?param1=value1&...]
```

**Examples:**

```bash
# Local PostgreSQL (default port 5432)
export NEXUS_DATABASE_URL="postgresql://nexus:password@localhost/nexus"

# Remote PostgreSQL with custom port
export NEXUS_DATABASE_URL="postgresql://user:pass@db.example.com:5433/nexus"

# PostgreSQL with SSL
export NEXUS_DATABASE_URL="postgresql://user:pass@host/db?sslmode=require"

# Using connection parameters
export NEXUS_DATABASE_URL="postgresql://user:pass@host/db?connect_timeout=10&application_name=nexus"
```

### Using .env File

Create a `.env` file in your project root:

```bash
# .env
NEXUS_DATABASE_URL=postgresql://nexus:nexus@localhost/nexus
```

Nexus will automatically load this file if you have `python-dotenv` installed:

```bash
pip install python-dotenv
```

## Code Usage

### Python API

```python
from nexus.storage.metadata_store import SQLAlchemyMetadataStore

# Method 1: Use environment variable (recommended)
# export NEXUS_DATABASE_URL="postgresql://nexus:nexus@localhost/nexus"
store = SQLAlchemyMetadataStore()

# Method 2: Pass URL directly
store = SQLAlchemyMetadataStore(
    db_url="postgresql://nexus:nexus@localhost/nexus"
)

# Method 3: For backward compatibility with SQLite
store = SQLAlchemyMetadataStore(
    db_path="/path/to/local.db"  # Will use SQLite
)
```

### Nexus Filesystem

```python
from nexus import NexusFS

# NexusFS automatically picks up NEXUS_DATABASE_URL
fs = NexusFS()

# Or specify explicitly
fs = NexusFS(
    metadata_store_config={
        "db_url": "postgresql://nexus:nexus@localhost/nexus"
    }
)
```

## Database Migrations

Nexus uses Alembic for database schema management.

### Run Migrations

```bash
# Set database URL
export NEXUS_DATABASE_URL="postgresql://nexus:nexus@localhost/nexus"

# Run migrations
alembic upgrade head

# Check current version
alembic current

# View migration history
alembic history
```

### Auto-run Migrations (Python API)

```python
from nexus.storage.metadata_store import SQLAlchemyMetadataStore

# Auto-run migrations on startup
store = SQLAlchemyMetadataStore(
    db_url="postgresql://nexus:nexus@localhost/nexus",
    run_migrations=True  # This will run `alembic upgrade head`
)
```

## Performance Tuning

### Connection Pool Settings

Nexus automatically configures connection pooling for PostgreSQL:

```python
# Default settings (configured automatically)
{
    "pool_size": 5,           # Base pool size
    "max_overflow": 10,       # Max additional connections
    "pool_timeout": 30,       # Seconds to wait for connection
    "pool_recycle": 3600,     # Recycle connections after 1 hour
    "pool_pre_ping": True,    # Test connections before use
}
```

### PostgreSQL Configuration

For production workloads, tune these PostgreSQL settings in `postgresql.conf`:

```ini
# Memory Settings
shared_buffers = 256MB          # 25% of RAM for small instances
effective_cache_size = 1GB      # 50-75% of RAM
work_mem = 4MB                  # Per-operation memory
maintenance_work_mem = 64MB     # For VACUUM, CREATE INDEX, etc.

# Connection Settings
max_connections = 100           # Adjust based on your needs

# Write Performance
wal_buffers = 16MB
checkpoint_completion_target = 0.9

# Query Performance
random_page_cost = 1.1          # For SSD storage
effective_io_concurrency = 200  # For SSD storage

# Logging (for debugging)
log_min_duration_statement = 1000  # Log queries slower than 1 second
```

## Security Best Practices

### 1. Use Strong Passwords

```bash
# Generate a secure password
openssl rand -base64 32
```

### 2. Use SSL/TLS Connections

```bash
export NEXUS_DATABASE_URL="postgresql://user:pass@host/db?sslmode=require"
```

SSL Modes:
- `disable`: No SSL (not recommended for production)
- `require`: SSL required but don't verify certificate
- `verify-ca`: SSL required, verify CA certificate
- `verify-full`: SSL required, verify CA and hostname

### 3. Restrict Network Access

Configure PostgreSQL's `pg_hba.conf`:

```
# TYPE  DATABASE  USER    ADDRESS         METHOD
local   nexus     nexus                   scram-sha-256
host    nexus     nexus   127.0.0.1/32    scram-sha-256
host    nexus     nexus   10.0.0.0/8      scram-sha-256  # Internal network
```

### 4. Use Environment Variables

Never hardcode credentials in your code:

```python
# ❌ Bad - hardcoded credentials
store = SQLAlchemyMetadataStore(
    db_url="postgresql://admin:password123@localhost/nexus"
)

# ✅ Good - use environment variables
store = SQLAlchemyMetadataStore()  # Reads from NEXUS_DATABASE_URL
```

## Monitoring

### Check Connection Pool Status

```python
from nexus.storage.metadata_store import SQLAlchemyMetadataStore

store = SQLAlchemyMetadataStore()

# Get pool statistics
pool = store.engine.pool
print(f"Pool size: {pool.size()}")
print(f"Checked out: {pool.checkedout()}")
print(f"Overflow: {pool.overflow()}")
print(f"Total connections: {pool.size() + pool.overflow()}")
```

### PostgreSQL Monitoring Queries

```sql
-- Active connections
SELECT count(*) FROM pg_stat_activity WHERE datname = 'nexus';

-- Long-running queries
SELECT pid, now() - query_start as duration, query
FROM pg_stat_activity
WHERE state = 'active' AND now() - query_start > interval '1 minute'
ORDER BY duration DESC;

-- Database size
SELECT pg_size_pretty(pg_database_size('nexus'));

-- Table sizes
SELECT
  schemaname,
  tablename,
  pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS size
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;
```

## Troubleshooting

### Connection Refused

```bash
# Check if PostgreSQL is running
docker ps | grep postgres  # For Docker
sudo systemctl status postgresql  # For system installation

# Check if port is open
netstat -an | grep 5432
telnet localhost 5432
```

### Authentication Failed

```bash
# Verify credentials
psql -U nexus -d nexus -h localhost
# Enter password when prompted

# If using Docker, connect directly to container
docker exec -it nexus-postgres psql -U nexus -d nexus
```

### Database Does Not Exist

```sql
-- List databases
\l

-- Create database if needed
CREATE DATABASE nexus;
GRANT ALL PRIVILEGES ON DATABASE nexus TO nexus;
```

### Migration Errors

```bash
# Check current migration version
alembic current

# View migration history
alembic history

# Rollback one migration
alembic downgrade -1

# Reset to base
alembic downgrade base

# Apply all migrations
alembic upgrade head
```

### Connection Pool Exhausted

If you see "QueuePool limit exceeded" errors:

```python
# Increase pool size for high-concurrency applications
from sqlalchemy import create_engine, pool

engine = create_engine(
    "postgresql://user:pass@host/db",
    poolclass=pool.QueuePool,
    pool_size=20,        # Increase from default 5
    max_overflow=40,     # Increase from default 10
)
```

## Backup and Restore

### Backup

```bash
# Backup to file
pg_dump -U nexus -d nexus -f nexus_backup.sql

# Backup with compression
pg_dump -U nexus -d nexus | gzip > nexus_backup.sql.gz

# Backup using Docker
docker exec nexus-postgres pg_dump -U nexus nexus > nexus_backup.sql
```

### Restore

```bash
# Restore from file
psql -U nexus -d nexus -f nexus_backup.sql

# Restore from compressed file
gunzip < nexus_backup.sql.gz | psql -U nexus -d nexus

# Restore using Docker
docker exec -i nexus-postgres psql -U nexus nexus < nexus_backup.sql
```

## Migration from SQLite to PostgreSQL

### 1. Export Data from SQLite

```python
from nexus.storage.metadata_store import SQLAlchemyMetadataStore

# Connect to SQLite
sqlite_store = SQLAlchemyMetadataStore(db_path="nexus-data/metadata.db")

# Export all file metadata
all_files = sqlite_store.list("")

print(f"Exporting {len(all_files)} files...")
```

### 2. Import Data to PostgreSQL

```python
# Connect to PostgreSQL
pg_store = SQLAlchemyMetadataStore(
    db_url="postgresql://nexus:nexus@localhost/nexus"
)

# Batch import (efficient)
pg_store.put_batch(all_files)

print(f"Imported {len(all_files)} files to PostgreSQL")
```

### 3. Update Configuration

```bash
# Update environment variable
export NEXUS_DATABASE_URL="postgresql://nexus:nexus@localhost/nexus"

# Or update .env file
echo "NEXUS_DATABASE_URL=postgresql://nexus:nexus@localhost/nexus" >> .env
```

## Example: Complete Setup Script

```bash
#!/bin/bash
set -e

echo "Setting up Nexus with PostgreSQL..."

# 1. Install dependencies
echo "Installing dependencies..."
pip install nexus-ai-fs[postgres]

# 2. Start PostgreSQL (Docker)
echo "Starting PostgreSQL..."
docker run --name nexus-postgres \
  -e POSTGRES_PASSWORD=nexus \
  -e POSTGRES_DB=nexus \
  -e POSTGRES_USER=nexus \
  -p 5432:5432 \
  -d postgres:15

# Wait for PostgreSQL to be ready
echo "Waiting for PostgreSQL to be ready..."
sleep 5

# 3. Set environment variable
echo "Setting environment variable..."
export NEXUS_DATABASE_URL="postgresql://nexus:nexus@localhost/nexus"

# 4. Run migrations
echo "Running database migrations..."
alembic upgrade head

# 5. Test connection
echo "Testing connection..."
python examples/postgres_demo.py

echo "✅ Setup complete!"
echo "Set NEXUS_DATABASE_URL in your environment:"
echo "  export NEXUS_DATABASE_URL=\"postgresql://nexus:nexus@localhost/nexus\""
```

## Resources

- [PostgreSQL Documentation](https://www.postgresql.org/docs/)
- [SQLAlchemy PostgreSQL Dialect](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html)
- [Alembic Documentation](https://alembic.sqlalchemy.org/)
- [psycopg2 Documentation](https://www.psycopg.org/docs/)

## Support

For issues or questions:
- GitHub Issues: https://github.com/nexi-lab/nexus/issues
- Documentation: https://github.com/nexi-lab/nexus
