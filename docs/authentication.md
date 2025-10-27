# Authentication System

Nexus server supports flexible authentication for self-hosted and SaaS deployments.

## Overview

The authentication system provides:
- **User identity mapping**: Map tokens to user IDs for access control
- **Multiple providers**: API keys, database keys, JWT/OIDC tokens
- **Extensibility**: Easy migration from self-hosted to SaaS with SSO/OIDC

## Two Ways to Use Authentication

### 1. CLI Mode (Quick Start)

The `nexus serve` CLI command supports both simple API key and database authentication:

```bash
# Simple API key (single key, no user mapping)
nexus serve --api-key sk-mysecret

# Database authentication (multi-user, expiry, revocation)
export NEXUS_DATABASE_URL="postgresql://user:pass@localhost/nexus"
nexus serve --auth-type database
```

**Best for**: Development, testing, single-user deployments (simple key), or production multi-user deployments (database auth)

**Simple API Key Limitations**:
- Single API key only (no user identity mapping)
- No expiry, revocation, or audit trails
- Simple token comparison only

**Database Authentication** (requires `NEXUS_DATABASE_URL`):
- Multiple users with identity mapping
- API key expiry and revocation
- Usage tracking and audit trails

**Note**: Other authentication types (`static`, `local`, `oidc`, `multi-oidc`) are only available in programmatic mode (see below).

### 2. Programmatic Mode (Production)

For production deployments, use the programmatic API with full authentication providers:

```python
from nexus import NexusFilesystem
from nexus.server.rpc_server import NexusRPCServer
from nexus.server.auth import create_auth_provider

# Create auth provider
auth_provider = create_auth_provider("static", auth_config)

# Start server with auth
server = NexusRPCServer(
    nexus_fs=nx,
    host="0.0.0.0",
    port=8080,
    auth_provider=auth_provider  # Full auth provider support
)
server.serve_forever()
```

**Best for**: Production, multi-user, enterprise deployments

**Benefits**:
- Multiple authentication types
- User identity mapping
- Advanced features (expiry, revocation, SSO)

## Authentication Providers

### 1. Static API Key Authentication

**Best for**: Small self-hosted deployments, development, testing

Simple configuration file-based authentication. API keys are defined in YAML and loaded at startup.

**Configuration** (`auth.yaml`):

```yaml
api_keys:
  sk-alice-secret-key:
    user_id: "alice"
    tenant_id: null  # Self-hosted, no multi-tenancy
    is_admin: true
    metadata:
      email: "alice@example.com"

  sk-bob-secret-key:
    user_id: "bob"
    tenant_id: null
    is_admin: false
    metadata:
      email: "bob@example.com"
```

**Usage** (Programmatic only):

```python
from nexus.server.auth import create_auth_provider

auth_config = {
    "api_keys": {
        "sk-alice-secret-key": {
            "user_id": "alice",
            "tenant_id": None,
            "is_admin": True,
            "metadata": {"email": "alice@example.com"}
        },
        "sk-bob-secret-key": {
            "user_id": "bob",
            "tenant_id": None,
            "is_admin": False,
            "metadata": {"email": "bob@example.com"}
        }
    }
}

auth_provider = create_auth_provider("static", auth_config)
```

**Security considerations**:
- Store `auth.yaml` securely (not in version control)
- Use long, random keys (e.g., `sk-<name>-<32-hex-chars>`)
- Rotate keys periodically
- For production, consider database authentication

### 2. Database API Key Authentication

**Best for**: Production self-hosted deployments, multi-user environments

Database-backed authentication with key expiry, revocation, and audit trails.

**Features**:
- HMAC-SHA256 hashed keys with salt (only hash stored in database)
- Optional expiry dates
- Revocation support
- Usage tracking (last_used_at)
- Audit trail

**Usage** (CLI and Programmatic):

CLI usage:
```bash
# Set database URL
export NEXUS_DATABASE_URL="postgresql://user:pass@localhost/nexus"

# Start server with database authentication
nexus serve --host 0.0.0.0 --port 8080 --auth-type database
```

Programmatic usage:

```python
from sqlalchemy.orm import sessionmaker
from nexus.server.auth import create_auth_provider

# Create auth provider
session_factory = sessionmaker(bind=engine)
auth_provider = create_auth_provider(
    "database",
    session_factory=session_factory
)
```

**Creating API keys**:

```python
from sqlalchemy.orm import sessionmaker
from nexus.server.auth import DatabaseAPIKeyAuth
from nexus.storage.models import Base
from sqlalchemy import create_engine

# Connect to your database
engine = create_engine("postgresql://user:pass@localhost/nexus")
Base.metadata.create_all(engine)  # Create tables if needed
SessionFactory = sessionmaker(bind=engine)

# Create a new API key
with SessionFactory() as session:
    key_id, raw_key = DatabaseAPIKeyAuth.create_key(
        session,
        user_id="alice",
        name="Alice's Production Key",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(days=90)  # 90-day expiry
    )
    session.commit()

    # IMPORTANT: Save raw_key securely, it's only shown once!
    print(f"API Key: {raw_key}")
    print(f"Key ID: {key_id}")
```

**Revoking keys**:

```python
with SessionFactory() as session:
    DatabaseAPIKeyAuth.revoke_key(session, key_id)
    session.commit()
```

## Client Authentication

### Python Client

```python
from nexus.remote import RemoteNexusFS

# Connect with API key
nx = RemoteNexusFS(
    server_url="http://localhost:8080",
    api_key="sk-alice-secret-key"
)

# Use as normal
nx.write("/workspace/file.txt", b"Hello, World!")
```

### HTTP API

Include API key in Authorization header:

```bash
curl -X POST http://localhost:8080/api/nfs/read \
  -H "Authorization: Bearer sk-alice-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"id": 1, "params": {"path": "/workspace/file.txt"}}'
```

### 3. Local Authentication (Username/Password with JWT)

**Best for**: Self-hosted deployments with username/password login

Username/password authentication with JWT token generation.

**Features**:
- Bcrypt password hashing
- JWT token generation and validation
- Token expiry (default: 1 hour)
- Subject-based identity (user, agent, service, session)

**Usage** (Programmatic only):

```python
from nexus.server.auth import create_auth_provider

auth_config = {
    "jwt_secret": "your-secret-key",
    "users": {
        "alice@example.com": {
            "password_hash": "$2b$12$...",  # bcrypt hash
            "subject_type": "user",
            "subject_id": "alice",
            "tenant_id": "org_acme",
            "is_admin": False
        }
    }
}

auth_provider = create_auth_provider("local", auth_config)
```

### 4. OIDC/OAuth Authentication

**Best for**: Enterprise deployments with SSO (Google, GitHub, Azure AD, Okta, Auth0)

OAuth/OIDC authentication for SSO integration.

**Features**:
- JWT token validation from external identity providers
- JWKS-based signature verification
- Support for multiple OIDC providers
- No OAuth flow (handled by frontend/web UI)

**Usage** (Programmatic only):

**Single OIDC Provider**:

```python
from nexus.server.auth import create_auth_provider

auth_config = {
    "issuer": "https://accounts.google.com",
    "audience": "your-client-id",
    "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs"
}

auth_provider = create_auth_provider("oidc", auth_config)
```

**Multiple OIDC Providers**:

```python
auth_config = {
    "providers": {
        "google": {
            "issuer": "https://accounts.google.com",
            "audience": "your-google-client-id"
        },
        "github": {
            "issuer": "https://token.actions.githubusercontent.com",
            "audience": "your-github-client-id"
        }
    }
}

auth_provider = create_auth_provider("multi-oidc", auth_config)
```

## Migration Path: Self-Hosted → SaaS

The authentication system is designed for easy migration:

**Self-hosted** (v0.4):
- `tenant_id = None` (single tenant)
- Simple API keys or database keys

**SaaS** (v0.5+):
- `tenant_id = "acme"` (multi-tenant isolation)
- SSO/OIDC integration
- Organization-level permissions

Example database key for future SaaS:

```python
# Self-hosted (now)
key_id, raw_key = DatabaseAPIKeyAuth.create_key(
    session,
    user_id="alice",
    tenant_id=None,  # No multi-tenancy
    is_admin=True
)

# SaaS (future)
key_id, raw_key = DatabaseAPIKeyAuth.create_key(
    session,
    user_id="alice@acme.com",
    tenant_id="acme",  # Multi-tenant isolation
    is_admin=False  # Tenant-scoped admin
)
```

## Security Best Practices

1. **Key Generation**:
   - Use cryptographically secure random sources
   - Format: `sk-<identifier>-<32-hex-chars>`
   - Never commit keys to version control

2. **Key Storage**:
   - Database: Store HMAC-SHA256 hash only (with salt)
   - Config files: Protect with filesystem permissions
   - Environment variables: Use secure secret management

3. **Key Rotation**:
   - Rotate keys every 90 days
   - Use expiry dates to enforce rotation
   - Revoke old keys after rotation

4. **Monitoring**:
   - Track `last_used_at` for inactive keys
   - Monitor failed authentication attempts
   - Alert on suspicious patterns

5. **Access Control**:
   - Use `is_admin` flag for privileged operations
   - Map users to appropriate permissions
   - Implement principle of least privilege

## API Reference

### AuthProvider (Base Class)

```python
class AuthProvider(ABC):
    async def authenticate(self, token: str) -> AuthResult:
        """Authenticate a request token."""
        pass

    async def validate_token(self, token: str) -> bool:
        """Quick validation without full authentication."""
        pass

    def close(self) -> None:
        """Cleanup resources."""
        pass
```

### AuthResult

```python
@dataclass
class AuthResult:
    authenticated: bool
    user_id: str | None = None
    tenant_id: str | None = None
    is_admin: bool = False
    metadata: dict[str, Any] | None = None
```

### Factory Function

```python
def create_auth_provider(
    auth_type: str | None,
    auth_config: dict[str, Any] | None = None,
    **kwargs: Any
) -> AuthProvider | None:
    """Create authentication provider from configuration."""
    pass
```

## Supported Authentication Types

| Type | Status | Use Case | Features |
|------|--------|----------|----------|
| Simple API Key | ✅ CLI & Programmatic | Development, testing | Single key, no user mapping |
| Database Keys | ✅ CLI & Programmatic | Production | Expiry, revocation, audit, HMAC-SHA256 |
| Static API Keys | ✅ Programmatic only | Small teams | Multiple keys, user mapping |
| Local JWT | ✅ Programmatic only | Self-hosted | Username/password login |
| OIDC Single | ✅ Programmatic only | Enterprise SSO | Google, GitHub, Azure AD |
| OIDC Multi | ✅ Programmatic only | Multi-provider SSO | Multiple identity providers |

## Future Extensions

Planned for future versions:

- **SAML**: Legacy enterprise authentication
- **JWT refresh tokens**: Long-lived sessions
- **RBAC**: Role-based access control
- **MFA**: Multi-factor authentication
- **API key scopes**: Fine-grained permissions

## Troubleshooting

### Authentication Failed

```
Authentication failed: invalid or expired token
```

**Solutions**:
1. Check API key is correct
2. Verify key hasn't expired (`expires_at`)
3. Ensure key hasn't been revoked
4. Check Authorization header format: `Bearer <token>`

### Using Advanced Auth with CLI

The CLI `nexus serve` command supports:
- **Simple API key**: `--api-key <key>` (single key, no user mapping)
- **Database authentication**: `--auth-type database` (multi-user, requires `NEXUS_DATABASE_URL`)

For other auth providers (static config, local JWT, OIDC), use the programmatic API:

```python
from nexus.server.auth import create_auth_provider
from nexus.server.rpc_server import NexusRPCServer

auth_provider = create_auth_provider("static", auth_config)
server = NexusRPCServer(nexus_fs=nx, auth_provider=auth_provider)
server.serve_forever()
```

### Database Authentication Not Available

```
Error: session_factory is required for database authentication
```

**Solutions**:
1. Ensure you're using PostgreSQL or SQLite backend
2. Check `NEXUS_DATABASE_URL` is set
3. Create session factory and pass to `create_auth_provider()`
4. Verify database connection is working

## Examples

### Example 1a: CLI Simple Authentication (Development)

```bash
# Start server with simple API key
nexus serve --api-key sk-dev-secret-123

# Use from Python client
from nexus.remote import RemoteNexusFS
nx = RemoteNexusFS("http://localhost:8080", api_key="sk-dev-secret-123")
nx.write("/workspace/file.txt", b"Hello!")
```

### Example 1b: CLI Database Authentication (Production)

```bash
# Step 1: Create API keys in database
export NEXUS_DATABASE_URL="postgresql://user:pass@localhost/nexus"

python3 << 'EOF'
from datetime import UTC, datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from nexus.server.auth import DatabaseAPIKeyAuth
from nexus.storage.models import Base
import os

# Create database
engine = create_engine(os.getenv("NEXUS_DATABASE_URL"))
Base.metadata.create_all(engine)
SessionFactory = sessionmaker(bind=engine)

# Create API key
with SessionFactory() as session:
    key_id, raw_key = DatabaseAPIKeyAuth.create_key(
        session,
        user_id="alice",
        name="Alice's Production Key",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(days=90)
    )
    session.commit()
    print(f"API Key: {raw_key}")
    print(f"Key ID: {key_id}")
EOF

# Step 2: Start server with database authentication
nexus serve --host 0.0.0.0 --port 8080 --auth-type database

# Step 3: Use from Python client
from nexus.remote import RemoteNexusFS
nx = RemoteNexusFS("http://localhost:8080", api_key="sk-alice-...")
nx.write("/workspace/file.txt", b"Hello!")
```

### Example 2: Static API Keys (Small Teams)

```python
from nexus import NexusFilesystem
from nexus.server.rpc_server import NexusRPCServer
from nexus.server.auth import create_auth_provider

# Initialize filesystem
nx = NexusFilesystem(data_dir="./nexus-data")

# Create static auth provider
auth_config = {
    "api_keys": {
        "sk-alice-xxx": {"user_id": "alice", "is_admin": True},
        "sk-bob-xxx": {"user_id": "bob", "is_admin": False}
    }
}
auth_provider = create_auth_provider("static", auth_config)

# Start server
server = NexusRPCServer(
    nexus_fs=nx,
    host="0.0.0.0",
    port=8080,
    auth_provider=auth_provider
)
server.serve_forever()
```

### Example 3: Database Authentication (Production)

```python
from datetime import datetime, timedelta, UTC
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from nexus import NexusFilesystem
from nexus.server.rpc_server import NexusRPCServer
from nexus.server.auth import create_auth_provider, DatabaseAPIKeyAuth
from nexus.storage.models import Base

# Initialize database
engine = create_engine("postgresql://nexus:password@localhost/nexus")
Base.metadata.create_all(engine)
SessionFactory = sessionmaker(bind=engine)

# Create API keys
with SessionFactory() as session:
    key_id, raw_key = DatabaseAPIKeyAuth.create_key(
        session,
        user_id="alice",
        name="Alice's Production Key",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(days=90)
    )
    session.commit()
    print(f"API Key: {raw_key}")  # Save securely!

# Initialize filesystem
nx = NexusFilesystem(data_dir="./nexus-data")

# Create database auth provider
auth_provider = create_auth_provider("database", session_factory=SessionFactory)

# Start server
server = NexusRPCServer(
    nexus_fs=nx,
    host="0.0.0.0",
    port=8080,
    auth_provider=auth_provider
)
server.serve_forever()
```

### Example 4: OIDC Authentication (Enterprise SSO)

```python
from nexus import NexusFilesystem
from nexus.server.rpc_server import NexusRPCServer
from nexus.server.auth import create_auth_provider

# Initialize filesystem
nx = NexusFilesystem(data_dir="./nexus-data")

# Create OIDC auth provider (Google)
auth_config = {
    "issuer": "https://accounts.google.com",
    "audience": "your-client-id.apps.googleusercontent.com",
    "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs"
}
auth_provider = create_auth_provider("oidc", auth_config)

# Start server
server = NexusRPCServer(
    nexus_fs=nx,
    host="0.0.0.0",
    port=8080,
    auth_provider=auth_provider
)
server.serve_forever()
```
