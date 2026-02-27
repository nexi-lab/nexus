# Database Sharing Across Connectors

All OAuth-enabled connectors (Gmail, Slack, GDrive, X, etc.) share the same TokenManager database for credential storage.

## Unified Database Pattern

### Environment Variable (Recommended)

Set `TOKEN_MANAGER_DB` once, and all connectors use it:

```bash
# In .env or environment
export TOKEN_MANAGER_DB="${HOME}/.nexus/nexus.db"

# Or use PostgreSQL for production
export TOKEN_MANAGER_DB="postgresql://user/pass@localhost/nexus"
```

### How It Works

All connectors use `Backend.resolve_database_url()`:

```python
# From backend.py
@staticmethod
def resolve_database_url(db_param: str) -> str:
    """Resolve database URL with TOKEN_MANAGER_DB priority."""
    import os
    return os.getenv("TOKEN_MANAGER_DB") or db_param
```

**Priority:**
1. `TOKEN_MANAGER_DB` environment variable (if set)
2. `token_manager_db` parameter passed to constructor

## Example: Multiple Connectors

```python
import os
from nexus.backends.gmail_connector import GmailConnectorBackend
from nexus.backends.slack_connector import SlackConnectorBackend
from nexus.backends.gdrive_connector import GoogleDriveConnectorBackend

# Set once (or in environment)
DB_PATH = os.getenv("TOKEN_MANAGER_DB", "~/.nexus/nexus.db")

# All connectors share the same database
gmail = GmailConnectorBackend(
    token_manager_db=DB_PATH,
    user_email="user@example.com"
)

slack = SlackConnectorBackend(
    token_manager_db=DB_PATH,
    user_email="user@example.com"
)

gdrive = GoogleDriveConnectorBackend(
    token_manager_db=DB_PATH,
    user_email="user@example.com"
)

# All OAuth tokens stored in the same database!
```

## Benefits

### 1. Centralized Credential Management
- Single source of truth for all OAuth tokens
- Easy to backup and restore
- Simplified key rotation

### 2. Consistent User Experience
- One database to configure
- Same authentication flow for all services
- Shared token expiration handling

### 3. Multi-User Support
- All connectors respect the same user_email
- Tokens isolated by user and provider
- Single database supports multiple workspaces

## Database Schemas

### Supported Databases

| Database | Connection String | Use Case |
|----------|------------------|----------|
| SQLite | `sqlite:///path/to/nexus.db` | Local development |
| SQLite (relative) | `~/.nexus/nexus.db` | Default local setup |
| PostgreSQL | `postgresql://user/pass@host/db` | Production |
| MySQL | `mysql://user/pass@host/db` | Production |

### Example Configurations

#### Local Development (.env)
```bash
TOKEN_MANAGER_DB="${HOME}/.nexus/nexus.db"
```

#### Docker Compose
```yaml
services:
  nexus:
    environment:
      - TOKEN_MANAGER_DB=postgresql://nexus:password@postgres:5432/nexus
```

#### Google Cloud Run
```bash
gcloud run deploy nexus \
  --set-env-vars TOKEN_MANAGER_DB="${DATABASE_URL}"
```

## Tables Created

The shared database contains:

```sql
-- OAuth tokens (encrypted)
CREATE TABLE oauth_tokens (
    id SERIAL PRIMARY KEY,
    provider VARCHAR(50) NOT NULL,      -- 'gmail', 'slack', 'gdrive', etc.
    user_email VARCHAR(255) NOT NULL,
    zone_id VARCHAR(255) DEFAULT 'default',
    access_token TEXT NOT NULL,         -- Encrypted
    refresh_token TEXT,                 -- Encrypted
    expires_at TIMESTAMP,
    scopes TEXT[],
    metadata JSONB,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(provider, user_email, zone_id)
);

-- Encryption keys
CREATE TABLE encryption_keys (
    id SERIAL PRIMARY KEY,
    key_data TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);
```

## Security

### Encryption
- All tokens encrypted at rest using Fernet (symmetric encryption)
- Encryption keys stored in `encryption_keys` table
- Key rotation supported

### Access Control
- Tokens scoped by (provider, user_email, zone_id)
- No cross-user token access
- Tenant isolation for multi-tenancy

### Best Practices

1. **Use PostgreSQL for production**
   ```bash
   export TOKEN_MANAGER_DB="postgresql://..."
   ```

2. **Enable SSL for remote databases**
   ```bash
   export TOKEN_MANAGER_DB="postgresql://...?sslmode=require"
   ```

3. **Backup regularly**
   ```bash
   # SQLite
   cp ~/.nexus/nexus.db ~/.nexus/nexus.db.backup

   # PostgreSQL
   pg_dump nexus > nexus_backup.sql
   ```

4. **Rotate encryption keys**
   ```python
   from nexus.server.auth.token_manager import TokenManager

   tm = TokenManager(db_url=os.getenv("TOKEN_MANAGER_DB"))
   tm.rotate_encryption_key()
   ```

## Troubleshooting

### Issue: "Database locked" (SQLite)

**Solution:** Use WAL mode
```python
import sqlite3
conn = sqlite3.connect("~/.nexus/nexus.db")
conn.execute("PRAGMA journal_mode=WAL")
```

### Issue: "Connection refused" (PostgreSQL)

**Solution:** Check connection string
```bash
# Test connection
psql "${TOKEN_MANAGER_DB}"
```

### Issue: "Different databases for different connectors"

**Solution:** Set TOKEN_MANAGER_DB globally
```bash
# In .env (loads for all connectors)
export TOKEN_MANAGER_DB="postgresql://..."

# Or set once in environment
export TOKEN_MANAGER_DB="${HOME}/.nexus/nexus.db"
```

## Migration

### From Separate Databases to Shared

If you have tokens in different databases:

```python
from nexus.server.auth.token_manager import TokenManager

# Old databases
gmail_tm = TokenManager(db_path="~/.nexus/gmail.db")
slack_tm = TokenManager(db_path="~/.nexus/slack.db")

# New shared database
shared_tm = TokenManager(db_path="~/.nexus/nexus.db")

# Export and import tokens
for provider in ['gmail', 'slack']:
    tokens = old_tm.list_tokens(provider=provider)
    for token in tokens:
        shared_tm.store_token(token)
```

## See Also

- [Slack Secrets Management](slack-secrets-management.md)
- [OAuth Configuration](../configs/oauth.yaml)
- [Backend Base Class](../src/nexus/backends/backend.py)
