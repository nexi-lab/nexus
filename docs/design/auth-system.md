# Nexus Authentication & Authorization System

**Version:** 2.0 (Reflects Actual Implementation)

---

## Table of Contents

1. [Overview](#overview)
2. [Implemented Features](#implemented-features)
3. [Authentication Providers](#authentication-providers)
4. [Authorization System (ReBAC)](#authorization-system-rebac)
5. [Usage Examples](#usage-examples)
6. [Configuration](#configuration)
7. [Database Schema](#database-schema)
8. [Security Features](#security-features)
9. [What's NOT Implemented](#whats-not-implemented)
10. [Migration Notes](#migration-notes)

---

## Overview

Nexus provides a flexible authentication and authorization system that supports:

- **Multiple authentication methods**: Static API keys, database API keys, local username/password (JWT), and OIDC/OAuth
- **Relationship-based access control (ReBAC)**: Zanzibar-style permission system
- **Multi-tenant isolation**: Secure tenant-scoped operations
- **Subject-based identity**: Support for users, agents, services, and sessions

### ⚠️ Important: Authentication is Server-Only

**Authentication only applies when running Nexus in server mode.**

- **Embedded Mode** (library/SDK usage): No authentication required
  ```python
  from nexus.sdk import connect
  nx = connect()  # Direct access, no auth needed
  nx.write("/workspace/file.txt", b"Hello!")
  ```

- **Server Mode** (client-server): Authentication required
  ```python
  from nexus.remote import RemoteNexusFS
  nx = RemoteNexusFS(
      server_url="http://localhost:8080",
      api_key="sk-alice-xxx"  # Authentication required
  )
  nx.write("/workspace/file.txt", b"Hello!")
  ```

Two modes of operation:
- **Embedded mode** = direct file access, single process, no auth
- **Server mode** = client-server, multi-user, requires auth

**Quick Reference:**

| Mode | Usage | Authentication | Use Case |
|------|-------|----------------|----------|
| **Embedded** | `from nexus.sdk import connect`<br>`nx = connect()` | ❌ None | Single-user apps, scripts, notebooks |
| **Server** | `from nexus.remote import RemoteNexusFS`<br>`nx = RemoteNexusFS(url, api_key)` | ✅ Required | Multi-user apps, production deployments |

### Architecture

**Embedded Mode (No Auth):**
```
┌─────────────────────────────────────────────────────┐
│              Python SDK                              │
│  from nexus.sdk import connect                      │
│  nx = connect()                                     │
└──────────────────┬──────────────────────────────────┘
                   │ Direct function calls
                   ▼
┌─────────────────────────────────────────────────────┐
│              NexusFS Core                           │
│  - Direct filesystem operations                     │
│  - No authentication required                       │
│  - Single process, single user                      │
└──────────────────┬──────────────────────────────────┘
                   │
                   ▼
         File Operations
```

**Server Mode (Auth Required):**
```
┌─────────────────────────────────────────────────────┐
│              Client Layer                           │
│  (RemoteNexusFS, CLI, HTTP API)                     │
└──────────────────┬──────────────────────────────────┘
                   │ Authorization: Bearer <token>
                   ▼
┌─────────────────────────────────────────────────────┐
│        Authentication Provider (Factory)            │
│  ┌──────────────┬──────────┬──────────┬──────────┐ │
│  │ Static Key   │ Database │ LocalAuth│   OIDC   │ │
│  │    Auth      │  Auth    │   JWT    │  (SSO)   │ │
│  └──────────────┴──────────┴──────────┴──────────┘ │
│                      ↓                              │
│           Returns: AuthResult                       │
│        (subject_type, subject_id, tenant_id,        │
│         is_admin, metadata)                         │
└─────────────────┬──────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────┐
│      Authorization: ReBAC Engine                    │
│  - Relationship tuple storage (tenant-scoped)       │
│  - Graph traversal for permission checking          │
│  - Namespace configuration                          │
│  - Permission enforcement                           │
└──────────────────┬─────────────────────────────────┘
                   │
                   ▼
         File Operations
    (read, write, list, etc.)
```

---

## Implemented Features

### ✅ Authentication (v0.5.0+)

| Feature | Status | Notes |
|---------|--------|-------|
| **Static API Keys** | ✅ Implemented | Config file-based, simple self-hosted deployments |
| **Database API Keys** | ✅ Implemented | SHA-256 hashed, expiry, revocation, usage tracking |
| **Local Auth (JWT)** | ✅ Implemented | Username/password + bcrypt + JWT tokens |
| **OIDC/OAuth** | ✅ Implemented | Token validation for Google, GitHub, Microsoft, Auth0, Okta |
| **Multi-OIDC** | ✅ Implemented | Support multiple providers simultaneously |
| **Auth Factory** | ✅ Implemented | Configuration-based provider creation |

### ✅ Authorization (v0.6.0+)

| Feature | Status | Notes |
|---------|--------|-------|
| **ReBAC System** | ✅ Implemented | Zanzibar-style relationship-based access control |
| **Tenant Isolation** | ✅ Implemented | Complete tenant-scoped queries and caching |
| **Subject Types** | ✅ Implemented | user, agent, service, session |
| **Permission Mapping** | ✅ Implemented | Read/write/execute → ReBAC relations |
| **Admin Bypass** | ✅ Implemented | Kill-switch for admin operations |

### ✅ Database Models (v0.5.0+)

| Model | Status | Purpose |
|-------|--------|---------|
| `APIKeyModel` | ✅ Implemented | Database-backed API key storage |
| `UserModel` | ✅ Implemented | Local user accounts (for LocalAuth) |
| `RefreshTokenModel` | ✅ Implemented | Long-lived refresh tokens (schema ready, API pending) |
| `ReBACTupleModel` | ✅ Implemented | Relationship tuples with tenant isolation |
| `ReBACCheckCacheModel` | ✅ Implemented | Tenant-scoped permission check cache |

---

## Authentication Providers

### 1. Static API Key Authentication

**Best for:** Development, self-hosted deployments, small teams

**Features:**
- API keys defined in configuration file or YAML
- No database required
- Subject type mapping (user, agent, service, session)
- Metadata support

**Configuration:**

```yaml
# auth-config.yaml
api_keys:
  sk-alice-xxx:
    subject_type: "user"
    subject_id: "alice"
    tenant_id: "org_acme"
    is_admin: true
    metadata:
      email: "alice@example.com"
      role: "administrator"

  sk-agent-xxx:
    subject_type: "agent"
    subject_id: "agent_claude_001"
    tenant_id: "org_acme"
    is_admin: false
```

**Python SDK Usage:**

```python
from nexus.remote import RemoteNexusFS

# Connect with API key
nx = RemoteNexusFS(
    server_url="http://localhost:8080",
    api_key="sk-alice-xxx"
)

# Use normally
nx.write("/workspace/file.txt", b"Hello, World!")
content = nx.read("/workspace/file.txt")
```

**CLI Usage:**

```bash
# Start server with static auth
nexus serve \
    --host 127.0.0.1 \
    --port 8080 \
    --auth-type static \
    --auth-config /path/to/auth-config.yaml

# Use CLI with remote server
nexus write /workspace/file.txt "Hello from CLI!" \
    --remote-url http://127.0.0.1:8080 \
    --remote-api-key sk-alice-xxx

nexus cat /workspace/file.txt \
    --remote-url http://127.0.0.1:8080 \
    --remote-api-key sk-alice-xxx
```

**HTTP API Usage:**

```bash
# All API calls include Authorization header
curl -X POST http://localhost:8080/api/nfs/write \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer sk-alice-xxx" \
    -d '{
        "id": 1,
        "params": {
            "path": "/workspace/file.txt",
            "content": "Hello, World!"
        }
    }'
```

---

### 2. Database API Key Authentication

**Best for:** Production deployments, key rotation, audit trails

**Features:**
- SHA-256 hashed keys stored in database
- Optional expiry dates
- Revocation support
- Usage tracking (last_used_at)
- Key metadata (names, descriptions)

**Python SDK Usage:**

```python
from datetime import UTC, datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.server.auth import DatabaseAPIKeyAuth
from nexus.storage.models import Base

# Setup database
engine = create_engine("sqlite:///nexus.db")
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

    print(f"Save this key: {raw_key}")
    # Output: Save this key: sk-alice-a1b2c3d4e5f6...

# Start server with database auth
from nexus.sdk import connect
from nexus.server.auth import create_auth_provider
from nexus.server.rpc_server import NexusRPCServer

nx = connect({"data_dir": "./nexus-data"})
auth_provider = create_auth_provider("database", session_factory=SessionFactory)

server = NexusRPCServer(
    nexus_fs=nx,
    host="0.0.0.0",
    port=8080,
    auth_provider=auth_provider
)
server.serve_forever()

# Use the key from client
from nexus.remote import RemoteNexusFS

client = RemoteNexusFS(
    server_url="http://localhost:8080",
    api_key=raw_key  # Use the generated key
)
client.write("/workspace/file.txt", b"Hello!")
```

**CLI Usage:**

```bash
# Start server with database auth
export NEXUS_DATABASE_URL="postgresql://user:pass@localhost/nexus"
nexus serve \
    --host 0.0.0.0 \
    --port 8080 \
    --auth-type database

# Create API key via Python
python -c "
from datetime import UTC, datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from nexus.server.auth import DatabaseAPIKeyAuth

engine = create_engine('postgresql://user:pass@localhost/nexus')
SessionFactory = sessionmaker(bind=engine)

with SessionFactory() as session:
    key_id, raw_key = DatabaseAPIKeyAuth.create_key(
        session,
        user_id='alice',
        name='Alice Production Key',
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(days=90)
    )
    session.commit()
    print(f'Key: {raw_key}')
"

# Use the key
nexus ls /workspace \
    --remote-url http://localhost:8080 \
    --remote-api-key sk-alice-xxx
```

**Key Management:**

```python
# Revoke a key
with SessionFactory() as session:
    DatabaseAPIKeyAuth.revoke_key(session, key_id)
    session.commit()

# Check key metadata
from sqlalchemy import select
from nexus.storage.models import APIKeyModel

with SessionFactory() as session:
    stmt = select(APIKeyModel).where(APIKeyModel.user_id == "alice")
    api_key = session.scalar(stmt)

    print(f"User: {api_key.user_id}")
    print(f"Name: {api_key.name}")
    print(f"Admin: {bool(api_key.is_admin)}")
    print(f"Created: {api_key.created_at}")
    print(f"Last used: {api_key.last_used_at}")
    print(f"Expires: {api_key.expires_at}")
    print(f"Revoked: {bool(api_key.revoked)}")
```

---

### 3. Local Authentication (JWT)

**Best for:** Self-hosted deployments with user accounts

**Features:**
- Username/password authentication
- Bcrypt password hashing (12 rounds)
- JWT token generation (HS256)
- Token expiration (default: 1 hour)
- Auto-generated secrets (or configure your own)
- Subject-based identity

**Python SDK Usage:**

```python
from nexus.server.auth import LocalAuth

# Create auth provider
auth = LocalAuth(
    jwt_secret="your-secret-key-here",  # Auto-generated if not provided
    token_expiry=3600  # 1 hour
)

# Create users
auth.create_user(
    email="alice@example.com",
    password="secure-password-123",
    subject_type="user",
    subject_id="alice",
    tenant_id="org_acme",
    is_admin=True
)

auth.create_user(
    email="bob@example.com",
    password="another-password-456",
    subject_type="user",
    subject_id="bob",
    tenant_id="org_acme",
    is_admin=False
)

# Verify credentials and get token
token = auth.verify_password_and_create_token(
    "alice@example.com",
    "secure-password-123"
)

print(f"JWT Token: {token}")

# Use token for authentication
from nexus.remote import RemoteNexusFS

client = RemoteNexusFS(
    server_url="http://localhost:8080",
    api_key=token  # JWT token acts as API key
)
client.write("/workspace/file.txt", b"Hello!")
```

**Configuration:**

```yaml
# nexus.yaml
auth:
  type: local
  jwt_secret: ${JWT_SECRET}  # Set via environment variable
  token_expiry: 3600  # 1 hour
  users:
    alice@example.com:
      password_hash: "$2b$12$..."  # Bcrypt hash
      subject_type: "user"
      subject_id: "alice"
      tenant_id: "org_acme"
      is_admin: true
```

**Security Notes:**
- Passwords are hashed with bcrypt (12 rounds, ~300ms to verify)
- JWT tokens are signed with HS256
- Tokens expire after configured duration
- Auto-generates secure secret if not provided (invalidates tokens on restart)
- For production: Set `NEXUS_JWT_SECRET` environment variable

---

### 4. OIDC/OAuth Authentication

**Best for:** SaaS deployments, enterprise SSO

**Features:**
- Validates JWT tokens from external identity providers
- Supports Google, GitHub, Microsoft, Auth0, Okta, etc.
- No OAuth flow implementation (handled by frontend/web UI)
- Subject ID prefixing for uniqueness
- Admin email lists
- Tenant ID extraction from claims

**Python SDK Usage:**

```python
from nexus.server.auth import OIDCAuth

# Create OIDC auth provider
auth = OIDCAuth(
    issuer="https://accounts.google.com",
    audience="your-client-id.apps.googleusercontent.com",
    admin_emails=["admin@example.com"]
)

# After user completes OAuth flow on frontend, they receive an ID token
# Use that token to authenticate with Nexus
from nexus.remote import RemoteNexusFS

client = RemoteNexusFS(
    server_url="http://localhost:8080",
    api_key=id_token  # OIDC ID token from OAuth flow
)
client.write("/workspace/file.txt", b"Hello!")
```

**Multi-Provider Configuration:**

```python
from nexus.server.auth import OIDCAuth, MultiOIDCAuth

# Support multiple providers
auth = MultiOIDCAuth(providers={
    "google": OIDCAuth(
        issuer="https://accounts.google.com",
        audience="google-client-id.apps.googleusercontent.com",
        admin_emails=["admin@example.com"]
    ),
    "github": OIDCAuth(
        issuer="https://github.com",
        audience="github-client-id",
        admin_emails=["admin@example.com"]
    ),
    "microsoft": OIDCAuth(
        issuer="https://login.microsoftonline.com/tenant-id/v2.0",
        audience="microsoft-client-id.apps.microsoft.com",
        admin_emails=["admin@example.com"]
    )
})

# Server will try each provider until one validates the token
server = NexusRPCServer(
    nexus_fs=nx,
    host="0.0.0.0",
    port=8080,
    auth_provider=auth
)
```

**Configuration:**

```yaml
# nexus.yaml
auth:
  type: multi-oidc
  providers:
    google:
      issuer: "https://accounts.google.com"
      audience: "your-client-id.apps.googleusercontent.com"
      admin_emails:
        - "admin@example.com"

    github:
      issuer: "https://github.com"
      audience: "your-github-client-id"
      admin_emails:
        - "admin@example.com"

    auth0:
      issuer: "https://your-tenant.auth0.com"
      audience: "your-auth0-client-id"
      tenant_id_claim: "org_id"  # Extract tenant from this claim
      admin_emails:
        - "admin@example.com"
```

**Important Notes:**
- Nexus only does **token validation** (server-side)
- OAuth **flow** (login buttons, redirects, callback handling) is handled by your frontend/web UI
- Frontend obtains ID token from provider, sends to Nexus for validation
- Nexus validates signature, issuer, audience, expiration

---

### 5. Authentication Factory

**Best for:** Configuration-driven deployments

The factory pattern allows you to create auth providers from configuration:

```python
from nexus.server.auth import create_auth_provider
from sqlalchemy.orm import sessionmaker

# Static keys
auth_provider = create_auth_provider(
    "static",
    auth_config={"api_keys": {...}}
)

# Database keys
auth_provider = create_auth_provider(
    "database",
    session_factory=SessionFactory
)

# Local auth
auth_provider = create_auth_provider(
    "local",
    auth_config={"jwt_secret": "...", "users": {...}}
)

# OIDC
auth_provider = create_auth_provider(
    "oidc",
    auth_config={"issuer": "...", "audience": "..."}
)

# Multi-OIDC
auth_provider = create_auth_provider(
    "multi-oidc",
    auth_config={"providers": {...}}
)

# No authentication
auth_provider = create_auth_provider(None)  # Returns None
```

---

## Authorization System (ReBAC)


Nexus uses a **Relationship-Based Access Control (ReBAC)** system inspired by Google's Zanzibar. Permissions are determined by relationships between subjects and objects.

### Core Concepts

```
Subject: (type, id)
  ├─ ("user", "alice")
  ├─ ("agent", "claude_001")
  ├─ ("service", "backup_service")
  └─ ("session", "session_abc")

Object: (type, id)
  ├─ ("file", "/workspace/file.txt")
  ├─ ("directory", "/workspace")
  └─ ("resource", "billing_data")

Relation: string
  ├─ "direct_owner"  # Direct ownership
  ├─ "owner"         # Computed via graph traversal
  ├─ "editor"        # Can read and write
  └─ "viewer"        # Can read only

Permission: string
  ├─ "read"
  ├─ "write"
  └─ "execute"
```

### How It Works

1. **Relationship Tuples** are stored in the database:
   ```
   (user:alice, direct_owner, file:/workspace/file.txt, tenant:org_acme)
   (user:bob, viewer, file:/workspace/file.txt, tenant:org_acme)
   ```

2. **Permission Checks** traverse the relationship graph:
   ```python
   # Check if Alice can read the file
   rebac_manager.rebac_check(
       subject=("user", "alice"),
       permission="read",
       object=("file", "/workspace/file.txt"),
       tenant_id="org_acme"
   )
   # → Returns True (owner can read)
   ```

3. **Namespace Configuration** defines permission rules:
   ```python
   namespace_config = {
       "file": {
           "relations": {
               "owner": {"can": ["read", "write", "execute"]},
               "editor": {"can": ["read", "write"]},
               "viewer": {"can": ["read"]}
           }
       }
   }
   ```

### Python SDK Usage

```python
from nexus.core.rebac_manager import ReBACManager

# Create ReBAC manager
rebac = ReBACManager(session_factory, namespace_config)

# Grant permission (create relationship tuple)
rebac.rebac_write(
    subject=("user", "alice"),
    relation="direct_owner",
    object=("file", "/workspace/file.txt"),
    tenant_id="org_acme"
)

# Check permission
can_read = rebac.rebac_check(
    subject=("user", "alice"),
    permission="read",
    object=("file", "/workspace/file.txt"),
    tenant_id="org_acme"
)

# List all subjects with permission
subjects = rebac.rebac_expand(
    permission="read",
    object=("file", "/workspace/file.txt"),
    tenant_id="org_acme"
)
# → [("user", "alice"), ("user", "bob"), ...]
```

### CLI Usage

```bash
# Create relationship (grant permission)
nexus rebac create \
    --subject user:alice \
    --relation direct_owner \
    --object file:/workspace/file.txt \
    --tenant org_acme

# Check permission
nexus rebac check \
    --subject user:alice \
    --permission read \
    --object file:/workspace/file.txt \
    --tenant org_acme

# List subjects with permission
nexus rebac expand \
    --permission read \
    --object file:/workspace/file.txt \
    --tenant org_acme
```

### Multi-Tenant Isolation

ReBAC enforces tenant isolation at multiple levels:

1. **Write-time validation**: Prevents cross-tenant relationships
2. **Query-time filtering**: All queries scoped to tenant_id
3. **Cache isolation**: Tenant-scoped caching prevents poisoning
4. **Graph traversal**: Relationships can only traverse within same tenant

```python
# All ReBAC operations require tenant_id
rebac.rebac_write(
    subject=("user", "alice"),
    relation="direct_owner",
    object=("file", "/workspace/file.txt"),
    tenant_id="org_acme"  # REQUIRED
)

# Attempts to create cross-tenant relationships will fail
rebac.rebac_write(
    subject=("user", "alice"),  # From tenant org_acme
    relation="viewer",
    object=("file", "/workspace/file.txt"),  # From tenant org_xyz
    tenant_id="org_acme"
)
# → Raises ValidationError: Cross-tenant relationship not allowed
```

---

## Usage Examples

### Complete Examples

See the following files for complete working examples:

#### Python SDK:
- `examples/auth_demo/static_auth_demo.py` - Static API key auth
- `examples/auth_demo/database_auth_demo.py` - Database API key auth with expiry/revocation

#### CLI:
- `examples/auth_demo/static_auth_demo.sh` - Static API key auth via CLI
- `examples/auth_demo/database_auth_demo.sh` - Database API key auth via CLI

#### Multi-Tenant:
- `examples/py_demo/multi_tenant_rebac_demo.py` - ReBAC with tenant isolation
- `examples/script_demo/multi_tenant_cli_demo.sh` - Multi-tenant via CLI

---

## Configuration

### Server Configuration (YAML)

```yaml
# nexus.yaml

# Server settings
server:
  host: 0.0.0.0
  port: 8080

# Database
database:
  url: postgresql://user:password@localhost:5432/nexus
  # or sqlite:///./nexus.db

# Authentication
auth:
  # Type: static, database, local, oidc, multi-oidc, or null (no auth)
  type: database

  # For static auth
  # api_keys:
  #   sk-alice-xxx:
  #     subject_id: "alice"
  #     is_admin: true

  # For local auth
  # jwt_secret: ${JWT_SECRET}
  # token_expiry: 3600

  # For OIDC auth
  # issuer: "https://accounts.google.com"
  # audience: "your-client-id"
  # admin_emails:
  #   - admin@example.com

# Authorization
rebac:
  enabled: true
  enforce_permissions: false  # Enable in production
  admin_bypass: true  # Admins skip permission checks

# Multi-tenant
tenant:
  isolation_enabled: true
  default_tenant_id: "default"
```

### Environment Variables

```bash
# Database
export NEXUS_DATABASE_URL="postgresql://user:password@localhost:5432/nexus"

# JWT Secret (for LocalAuth)
export NEXUS_JWT_SECRET="your-super-secret-key-here"

# Data directory
export NEXUS_DATA_DIR="./nexus-data"

# Server
export NEXUS_HOST="0.0.0.0"
export NEXUS_PORT="8080"
```

---

## Database Schema

### API Keys Table

```sql
CREATE TABLE api_keys (
    key_id VARCHAR(64) PRIMARY KEY,
    key_hash VARCHAR(64) NOT NULL,  -- SHA-256 hash
    user_id VARCHAR(255) NOT NULL,
    subject_type VARCHAR(50) DEFAULT 'user',
    subject_id VARCHAR(255),
    tenant_id VARCHAR(255),
    name VARCHAR(255),
    is_admin BOOLEAN DEFAULT FALSE,
    expires_at TIMESTAMP,
    revoked BOOLEAN DEFAULT FALSE,
    revoked_at TIMESTAMP,
    last_used_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    INDEX idx_key_hash (key_hash),
    INDEX idx_user_id (user_id),
    INDEX idx_tenant_id (tenant_id)
);
```

### Users Table (for LocalAuth)

```sql
CREATE TABLE users (
    id VARCHAR(64) PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,  -- Bcrypt hash
    subject_type VARCHAR(50) DEFAULT 'user',
    subject_id VARCHAR(255),
    tenant_id VARCHAR(255),
    name VARCHAR(255),
    is_admin BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW(),
    last_login TIMESTAMP,
    INDEX idx_email (email),
    INDEX idx_subject (subject_type, subject_id)
);
```

### Refresh Tokens Table

```sql
CREATE TABLE refresh_tokens (
    id SERIAL PRIMARY KEY,
    token VARCHAR(255) UNIQUE NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    revoked BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW(),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_token (token),
    INDEX idx_user_id (user_id)
);
```

### ReBAC Tuples Table

```sql
CREATE TABLE rebac_tuples (
    id SERIAL PRIMARY KEY,
    subject_type VARCHAR(50) NOT NULL,
    subject_id VARCHAR(255) NOT NULL,
    relation VARCHAR(100) NOT NULL,
    object_type VARCHAR(50) NOT NULL,
    object_id VARCHAR(255) NOT NULL,
    tenant_id VARCHAR(255),
    subject_tenant_id VARCHAR(255),
    object_tenant_id VARCHAR(255),
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE (subject_type, subject_id, relation, object_type, object_id, tenant_id),
    INDEX idx_subject (subject_type, subject_id, tenant_id),
    INDEX idx_object (object_type, object_id, tenant_id),
    INDEX idx_relation (relation, tenant_id)
);
```

### ReBAC Check Cache Table

```sql
CREATE TABLE rebac_check_cache (
    id SERIAL PRIMARY KEY,
    subject_type VARCHAR(50) NOT NULL,
    subject_id VARCHAR(255) NOT NULL,
    permission VARCHAR(100) NOT NULL,
    object_type VARCHAR(50) NOT NULL,
    object_id VARCHAR(255) NOT NULL,
    tenant_id VARCHAR(255),
    result BOOLEAN NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    UNIQUE (subject_type, subject_id, permission, object_type, object_id, tenant_id),
    INDEX idx_cache_lookup (subject_type, subject_id, permission, object_type, object_id, tenant_id),
    INDEX idx_expires_at (expires_at)
);
```

---

## Security Features

### Password Security (LocalAuth)

- ✅ **Bcrypt hashing** with 12 rounds (~300ms to verify)
- ✅ **Salt generation** per password (automatic with bcrypt)
- ✅ **Constant-time comparison** (bcrypt.checkpw)
- ✅ **No plaintext storage** ever

```python
import bcrypt

# Hash password
password_bytes = password.encode("utf-8")
salt = bcrypt.gensalt(rounds=12)
password_hash = bcrypt.hashpw(password_bytes, salt)

# Verify password (constant time)
is_valid = bcrypt.checkpw(password_bytes, stored_hash)
```

### JWT Token Security (LocalAuth)

- ✅ **HS256 signing** algorithm
- ✅ **Short expiration** (default: 1 hour)
- ✅ **Standard claims** (iat, exp, sub, email, role)
- ✅ **Signature verification** before trusting claims
- ✅ **Secure random secrets** (auto-generated or configured)

```python
from authlib.jose import jwt

# Create token
header = {"alg": "HS256"}
payload = {
    "sub": "alice",
    "email": "alice@example.com",
    "iat": int(time.time()),
    "exp": int(time.time()) + 3600
}
token = jwt.encode(header, payload, secret)

# Validate token
claims = jwt.decode(token, secret)
claims.validate()  # Checks exp, iat automatically
```

### API Key Security (DatabaseAuth)

- ✅ **SHA-256 hashing** before storage
- ✅ **High entropy** keys (32+ bytes)
- ✅ **Prefix format** (sk-<user>-<random>)
- ✅ **Full key shown only once** at creation
- ✅ **Optional expiry dates**
- ✅ **Revocation support**
- ✅ **Usage audit trail** (last_used_at)

```python
import secrets
import hashlib

# Generate key
raw_key = f"sk-{user_id[:8]}-{secrets.token_hex(16)}"
# → sk-alice-a1b2c3d4e5f6789...

# Hash for storage
key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

# Store hash only, return raw key once
return (key_id, raw_key)  # User must save raw_key!
```

### Multi-Tenant Security

- ✅ **Write-time validation** prevents cross-tenant relationships
- ✅ **Query-time filtering** enforces tenant scoping
- ✅ **Tenant-aware caching** prevents cache poisoning
- ✅ **Graph traversal limits** prevent DoS
- ✅ **Kill-switch** for admin bypass

### OIDC Security

- ✅ **JWT signature verification** using provider's public keys
- ✅ **Issuer validation** (iss claim)
- ✅ **Audience validation** (aud claim)
- ✅ **Expiration validation** (exp claim)
- ✅ **No password storage** (delegated to IdP)

---

## What's NOT Implemented

The following features from the original design document are **not yet implemented**:

### ❌ Missing Features

1. **Web Setup Wizard**
   - No HTML UI for first-run admin creation
   - Bootstrap must be done via code or config

2. **CLI Login Command**
   - No `nexus login` command
   - No credential storage in `~/.nexus/credentials`
   - Users must pass `--remote-api-key` for each CLI command

3. **Authentication API Endpoints**
   - No `POST /auth/login` endpoint
   - No `POST /auth/refresh` endpoint
   - No `POST /auth/logout` endpoint
   - No `GET/POST /api/user/me` endpoints
   - No `POST /api/user/change-password` endpoint

4. **API Key Management API**
   - No CRUD endpoints for API keys
   - Keys must be managed via Python code or database directly

5. **OAuth Flow Implementation**
   - Server only validates OIDC tokens
   - No OAuth redirect/callback endpoints
   - Frontend must implement OAuth flow

6. **SAML Support**
   - Enterprise SAML not implemented
   - Use OIDC providers instead

7. **MFA/2FA**
   - No TOTP support
   - No WebAuthn support
   - No backup codes

8. **Rate Limiting**
   - No built-in rate limiting on auth endpoints
   - Use reverse proxy (nginx, Caddy) for rate limiting

9. **Session Management UI**
   - No web UI for viewing active sessions
   - No session revocation UI

### Workarounds

**For CLI authentication:**
```bash
# Set environment variable to avoid repeating --remote-api-key
export NEXUS_REMOTE_URL="http://localhost:8080"
export NEXUS_REMOTE_API_KEY="sk-alice-xxx"

# Then use CLI normally
nexus ls /workspace
nexus cat /workspace/file.txt
```

**For key management:**
```python
# Use Python to manage keys
from nexus.server.auth import DatabaseAPIKeyAuth
from sqlalchemy.orm import sessionmaker

# Create key
with SessionFactory() as session:
    key_id, raw_key = DatabaseAPIKeyAuth.create_key(...)
    session.commit()

# Revoke key
with SessionFactory() as session:
    DatabaseAPIKeyAuth.revoke_key(session, key_id)
    session.commit()
```

**For OAuth flow:**
```javascript
// Frontend handles OAuth flow (example with Google)
const googleAuth = new GoogleAuth({
    clientId: "your-client-id.apps.googleusercontent.com"
});

// User clicks "Sign in with Google"
const idToken = await googleAuth.signIn();

// Send ID token to Nexus
const nexus = new NexusClient({
    serverUrl: "http://localhost:8080",
    apiKey: idToken  // OIDC ID token
});
```

---

## Migration Notes

### From v0.4.x to v0.5.0+

**Database Migrations:**

If you're upgrading from an earlier version, run database migrations:

```bash
# Using Alembic
alembic upgrade head

# Or manually apply:
# - Add subject_type, subject_id columns to api_keys table
# - Create users table
# - Create refresh_tokens table
```

**Configuration Changes:**

Old (v0.4.x):
```yaml
api_key: "sk-alice-xxx"  # Single API key
```

New (v0.5.0+):
```yaml
auth:
  type: static
  api_keys:
    sk-alice-xxx:
      subject_id: "alice"
      is_admin: true
```

**Code Changes:**

Old (v0.4.x):
```python
from nexus.remote import RemoteNexusFS

nx = RemoteNexusFS(
    server_url="http://localhost:8080",
    api_key="sk-alice-xxx"
)
```

New (v0.5.0+):
```python
# Same API - no changes required!
from nexus.remote import RemoteNexusFS

nx = RemoteNexusFS(
    server_url="http://localhost:8080",
    api_key="sk-alice-xxx"  # Works with all auth types
)
```

### From UNIX Permissions to ReBAC

If you were using UNIX-style permissions (owner/group/mode), these have been **removed in v0.6.0**.

**Migration:**

1. **Extract existing permissions:**
   ```python
   # Old system
   metadata = nx.metadata("/workspace/file.txt")
   owner = metadata["owner"]
   mode = metadata["mode"]
   ```

2. **Create ReBAC relationships:**
   ```python
   # New system
   rebac.rebac_write(
       subject=("user", owner),
       relation="direct_owner",
       object=("file", "/workspace/file.txt"),
       tenant_id="default"
   )

   # If mode allowed group read (040)
   rebac.rebac_write(
       subject=("group", group),
       relation="viewer",
       object=("file", "/workspace/file.txt"),
       tenant_id="default"
   )
   ```

3. **Update permission checks:**
   ```python
   # Old system
   if metadata["mode"] & 0o400:  # Owner can read
       ...

   # New system
   if rebac.rebac_check(
       subject=("user", owner),
       permission="read",
       object=("file", "/workspace/file.txt"),
       tenant_id="default"
   ):
       ...
   ```

---

## P0 Security Implementation Status

**Status:** ✅ **All P0 blockers resolved** (2025-10-25)

All critical security issues have been addressed before GA:

### ✅ 1. Token Type Discrimination

**Implementation:** `src/nexus/server/auth/factory.py`

- ✅ **Explicit prefix-based routing:**
  - `sk-*` → API key providers (static/database)
  - JWT structure → JWT/OIDC providers
- ✅ **Early rejection** of ambiguous tokens
- ✅ **Clear error codes** (UNAUTHORIZED)

**Example:**
```python
from nexus.server.auth.factory import DiscriminatingAuthProvider

auth = DiscriminatingAuthProvider(
    api_key_provider=DatabaseAPIKeyAuth(session_factory),
    jwt_provider=OIDCAuth.from_config(oidc_config)
)

# Routes sk-xxx to DatabaseAPIKeyAuth
# Routes JWT to OIDCAuth
result = await auth.authenticate(token)
```

### ✅ 2. Subject & Tenant Binding

**Implementation:** `src/nexus/server/auth/oidc.py`

- ✅ **Strict contract enforcement:**
  - `require_tenant=True` → Deny if tenant missing
  - `allow_default_tenant=False` → No fallback
- ✅ **Documented claims mapping:**
  - `subject_id_claim` (default: "sub")
  - `tenant_id_claim` (default: "org_id")
- ✅ **Explicit deny with clear errors**

**Example:**
```python
auth = OIDCAuth(
    issuer="https://accounts.google.com",
    audience="your-client-id",
    tenant_id_claim="org_id",
    require_tenant=True,  # Deny if org_id missing
    allow_default_tenant=False  # No fallback
)
```

### ✅ 3. OIDC Validation Guarantees

**Implementation:** `src/nexus/server/auth/oidc.py:157-239`

- ✅ **RS256/ES256 only** (no HS256) - Line 183
- ✅ **JWKS fetching & caching** (1h TTL) - Lines 125-155
- ✅ **Complete validation:** `iss`, `aud`, `exp`, `nbf`, `iat`
- ✅ **Clock skew handling** (±5 min) - Line 17
- ✅ **Key ID (kid) pinning** - Lines 192-198
- ✅ **Fail-closed on JWKS errors** - Line 155

**Security:**
```python
ALLOWED_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"]
CLOCK_SKEW_SECONDS = 300  # ±5 minutes
JWKS_CACHE_TTL = 3600  # 1 hour
```

### ✅ 4. Admin Bypass Control

**Implementation:** `src/nexus/core/permissions_enhanced.py`

- ✅ **Kill-switch DEFAULT OFF** - Line 316: `allow_admin_bypass=False`
- ✅ **Scoped bypass** with path allowlist - Line 319
- ✅ **Capability-based model** - Lines 392-398
- ✅ **Audit logging** - Every bypass logged
- ✅ **No deny-overrides** - Bypass limited to allowlist

**Example:**
```python
enforcer = EnhancedPermissionEnforcer(
    allow_admin_bypass=False,  # DEFAULT: Off
    admin_bypass_paths=["/admin/*", "/system/*"]  # Scoped
)
```

### ✅ 5. API Key Security

**Implementation:** `src/nexus/server/auth/database_key.py`

- ✅ **HMAC-SHA256 + salt** (not raw SHA-256) - Lines 175-192
- ✅ **Prefix validation** (`sk-` required) - Lines 76-78
- ✅ **Mandatory expiry option** - `require_expiry=True` for production
- ✅ **32+ bytes entropy** - Line 238
- ✅ **Tenant binding in key ID** - Line 241
- ✅ **Immediate revocation** - Line 232

**Format:**
```
sk-<tenant>_<user>_<id>_<random-hex>
sk-org_acme_alice_a3f2_9c8f7e6d5c4b3a2f1e0d9c8b7a6f5e4d3c2b1a0f
```

### ✅ 6. AuthZ Enforcement Defaults

**Implementation:** Multiple files

- ✅ **`enforce_permissions=True` by default:**
  - `src/nexus/config.py:96`
  - `src/nexus/core/nexus_fs.py:80`
- ✅ **Fail-closed on errors** - Deny on INDETERMINATE
- ✅ **Production-safe defaults**

**Breaking Change:**
```python
# OLD (v0.5.x): Permissions OFF by default
enforce_permissions: bool = False

# NEW (v0.6.0): Permissions ON by default
enforce_permissions: bool = True  # Secure by default
```

### Migration Guide for P0 Changes

**1. API Keys Must Have `sk-` Prefix**

```python
# OLD
api_keys = {
    "alice-secret-key": {"user_id": "alice", "is_admin": True}
}

# NEW (add sk- prefix)
api_keys = {
    "sk-alice-secret-key": {"user_id": "alice", "is_admin": True}
}
```

**2. Database API Keys Need Re-generation**

Old keys (SHA-256) are incompatible with new hashing (HMAC-SHA256):

```python
from nexus.server.auth.database_key import DatabaseAPIKeyAuth
from datetime import datetime, timedelta, UTC

with session_factory() as session:
    key_id, raw_key = DatabaseAPIKeyAuth.create_key(
        session,
        user_id="alice",
        name="Production API Key",
        tenant_id="org_acme",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(days=90)
    )
    print(f"New key: {raw_key}")
    session.commit()
```

**3. Permission Enforcement Now ON by Default**

To restore old behavior (not recommended):

```bash
# Environment variable
export NEXUS_ENFORCE_PERMISSIONS=false

# Or in code
nx = NexusFS(enforce_permissions=False)
```

**4. Admin Bypass Now OFF by Default**

To enable (with scoping):

```python
enforcer = EnhancedPermissionEnforcer(
    allow_admin_bypass=True,
    admin_bypass_paths=["/admin/*", "/workspace/*"]  # Scope it!
)
```

---

## Testing

The authentication system has comprehensive test coverage:

**Test Files:**
- `tests/test_auth_authlib.py` - All auth providers (32/32 tests passing)
- `tests/test_rebac.py` - ReBAC system tests
- `examples/auth_demo/` - Working demo scripts
- `examples/py_demo/p0_security_demo.py` - P0 security features demo
- `examples/script_demo/p0_security_demo.sh` - Shell script demo

**Run Tests:**

```bash
# All auth tests
pytest tests/test_auth_authlib.py -v

# P0 security tests
pytest tests/test_p0_fixes.py -v

# Specific provider
pytest tests/test_auth_authlib.py::test_static_api_key_auth -v

# With coverage
pytest tests/test_auth_authlib.py --cov=nexus.server.auth
```

---

## Further Reading

- **ReBAC System**: See `docs/PERMISSION_SYSTEM.md` for detailed ReBAC documentation
- **Multi-Tenant Architecture**: See `docs/multi-tenant-architecture.md`
- **Configuration Best Practices**: See `docs/config-best-practices.md`
- **API Reference**: See `docs/api/` directory

---

## Support

For questions, issues, or feature requests:
- GitHub Issues: https://github.com/nexi-lab/nexus/issues
- Documentation: https://nexi-lab.github.io/nexus/
- Examples: `examples/` directory in repository

---

**Version History:**
- v2.0 (2025-10-24): Rewritten to reflect actual implementation
- v1.0 (2025-01-24): Original design document (draft)
