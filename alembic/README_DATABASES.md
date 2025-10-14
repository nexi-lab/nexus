# Using Alembic with Different Databases

This guide explains how to use Alembic migrations with different database backends.

## Quick Start

### SQLite (Default)

```bash
# Edit alembic.ini (already configured)
sqlalchemy.url = sqlite:///nexus.db

# Run migrations
alembic upgrade head
```

### PostgreSQL

```bash
# Option 1: Edit alembic.ini
sqlalchemy.url = postgresql://user:password@localhost:5432/nexus

# Option 2: Use environment variable
export DATABASE_URL="postgresql://user:password@localhost:5432/nexus"
# Then modify alembic/env.py to read from environment

# Run migrations
alembic upgrade head
```

## Environment-Based Configuration

To support multiple databases without editing `alembic.ini`, modify `alembic/env.py`:

```python
import os
from alembic import context

# Get URL from environment or use default
config.set_main_option(
    'sqlalchemy.url',
    os.environ.get('DATABASE_URL', 'sqlite:///nexus.db')
)
```

Then use:

```bash
# SQLite
alembic upgrade head

# PostgreSQL
DATABASE_URL="postgresql://user:pass@localhost/nexus" alembic upgrade head

# MySQL
DATABASE_URL="mysql://user:pass@localhost/nexus" alembic upgrade head
```

## Creating Database-Specific Migrations

If you need different migrations for different databases:

```bash
# Create a branch for each database
alembic revision --head=base --branch=sqlite -m "SQLite specific"
alembic revision --head=base --branch=postgres -m "PostgreSQL specific"

# Upgrade to specific branch
alembic upgrade sqlite@head
alembic upgrade postgres@head
```

## Common Commands

```bash
# Show current version
alembic current

# Show migration history
alembic history

# Upgrade to latest
alembic upgrade head

# Upgrade by 1 version
alembic upgrade +1

# Downgrade by 1 version
alembic downgrade -1

# Downgrade to base
alembic downgrade base

# Create new migration
alembic revision --autogenerate -m "description"

# Show SQL without applying
alembic upgrade head --sql
```

## Testing Migrations

```bash
# Test upgrade
alembic upgrade head

# Test downgrade
alembic downgrade base

# Test re-upgrade
alembic upgrade head

# Verify database state
sqlite3 nexus.db ".schema"  # SQLite
psql nexus -c "\dt"          # PostgreSQL
```

## Database-Specific Notes

### SQLite
- ✅ No server required
- ✅ Single file
- ⚠️ Limited ALTER TABLE support
- ⚠️ No concurrent writes

### PostgreSQL
- ✅ Full ALTER TABLE support
- ✅ Transactional DDL
- ✅ Concurrent access
- ⚠️ Requires server

### MySQL
- ⚠️ No transactional DDL
- ⚠️ ALTER TABLE can be slow
- ✅ Good for read-heavy workloads

## Troubleshooting

### "No such table: alembic_version"
```bash
alembic stamp head
```

### "Database is locked" (SQLite)
- Close all connections
- Check for WAL files
- Use `PRAGMA journal_mode=WAL`

### "Permission denied" (PostgreSQL)
```bash
# Grant permissions
GRANT ALL PRIVILEGES ON DATABASE nexus TO user;
```

## References

- [Alembic Tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html)
- [SQLAlchemy Dialects](https://docs.sqlalchemy.org/en/20/dialects/)
- [Migration Best Practices](https://alembic.sqlalchemy.org/en/latest/cookbook.html)
