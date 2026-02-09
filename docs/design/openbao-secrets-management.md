# OpenBao Secrets Management Design

**Status**: Draft
**Author**: Claude (via user design session)
**Created**: 2026-02-09
**Branch**: `feat/openbao-secrets-service`
**Related Issues**: TBD

---

## Table of Contents

- [Overview](#overview)
- [Motivation](#motivation)
- [Nexus Secret Inventory](#nexus-secret-inventory)
- [OpenBao Architecture](#openbao-architecture)
- [Secret Organization Design](#secret-organization-design)
- [Access Control Strategy](#access-control-strategy)
- [Migration Strategy](#migration-strategy)
- [Implementation Plan](#implementation-plan)
- [Configuration](#configuration)
- [Security Considerations](#security-considerations)
- [Operational Guide](#operational-guide)
- [Testing Strategy](#testing-strategy)
- [Future Enhancements](#future-enhancements)
- [References](#references)

---

## Overview

This document describes the design for integrating **OpenBao** as the centralized secrets management system for Nexus. OpenBao will manage:

1. **Infrastructure secrets** - Database credentials, encryption keys, admin API keys
2. **OAuth provider credentials** - Google, Microsoft, Slack, X (Twitter) client secrets
3. **External service API keys** - LLM providers (Anthropic, OpenAI), tools (E2B, Tavily)
4. **User OAuth tokens** - Per-user access/refresh tokens (gradual migration)
5. **Cloud storage credentials** - GCS service accounts, S3 keys

### Goals

- ✅ **Centralized secret management** - Single source of truth for all secrets
- ✅ **Enhanced security** - Encryption at rest, audit logging, access policies
- ✅ **Multi-environment support** - Consistent secret handling across dev/staging/prod
- ✅ **Zero-downtime migration** - Gradual migration from database to OpenBao
- ✅ **Backward compatibility** - Fallback to database during migration

### Non-Goals

- ❌ Dynamic secret generation (future enhancement)
- ❌ Secret rotation automation (future enhancement)
- ❌ Hardware security module (HSM) integration (enterprise feature)

---

## Motivation

### Current State: Problems

1. **Scattered secrets** - Secrets stored across `.env` files, database, environment variables
2. **No centralized audit** - Difficult to track who accessed which secrets when
3. **Manual rotation** - No automated secret rotation capabilities
4. **Database coupling** - OAuth encryption key stored in database creates bootstrapping issues
5. **Limited access control** - All processes with database access can read all secrets

### Desired State: Benefits

1. **Centralized management** - All secrets in OpenBao with versioning and audit logs
2. **Fine-grained access control** - Different policies for bootstrap, runtime, admin
3. **Enhanced security** - Encryption at rest, TLS in transit, audit logging
4. **Easier rotation** - Centralized secret rotation with version tracking
5. **Multi-environment ready** - Same secret management across dev/staging/prod

---

## Nexus Secret Inventory

### 1. Infrastructure Secrets (System-Level)

| Secret | Current Location | Environment Variable | Purpose | Priority |
|--------|------------------|---------------------|---------|----------|
| **PostgreSQL Credentials** | `.env` file | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_DB` | Database connection | **P1** |
| **Nexus API Key** | `.env` or auto-generated | `NEXUS_API_KEY` | Server authentication | **P1** |
| **OAuth Encryption Key** | `system_settings` table or env | `NEXUS_OAUTH_ENCRYPTION_KEY` | Fernet key for token encryption | **P1** |
| **JWT Signing Key** | Not implemented | N/A | Session token signing (future) | **P3** |

### 2. OAuth Provider Credentials

| Provider | Environment Variables | Scopes | Priority |
|----------|----------------------|--------|----------|
| **Google** | `NEXUS_OAUTH_GOOGLE_CLIENT_ID`<br>`NEXUS_OAUTH_GOOGLE_CLIENT_SECRET` | Drive, Gmail, Calendar, Docs | **P1** |
| **Microsoft** | `NEXUS_OAUTH_MICROSOFT_CLIENT_ID`<br>`NEXUS_OAUTH_MICROSOFT_CLIENT_SECRET` | OneDrive, Outlook | **P1** |
| **Slack** | `NEXUS_OAUTH_SLACK_CLIENT_ID`<br>`NEXUS_OAUTH_SLACK_CLIENT_SECRET` | Workspace access | **P1** |
| **X (Twitter)** | `NEXUS_OAUTH_X_CLIENT_ID`<br>`NEXUS_OAUTH_X_CLIENT_SECRET` | Tweet access | **P1** |

### 3. External Service API Keys

#### LLM Providers (P2)
- **Anthropic**: `ANTHROPIC_API_KEY` - Claude API
- **OpenAI**: `OPENAI_API_KEY` - GPT models, embeddings
- **OpenRouter**: `OPENROUTER_API_KEY` - Multi-model access

#### Tools & Services (P3)
- **Tavily**: `TAVILY_API_KEY` - Search API
- **E2B**: `E2B_API_KEY` - Code execution sandboxes
- **Firecrawl**: `FIRECRAWL_API_KEY` - Web scraping
- **Unstructured**: `UNSTRUCTURED_API_KEY` - Document parsing
- **LlamaCloud**: `LLAMA_CLOUD_API_KEY` - LlamaParse
- **Klavis**: `KLAVIS_API_KEY` - MCP provider marketplace

#### Observability (P3)
- **LangSmith**: `LANGSMITH_API_KEY` - LLM observability

### 4. User OAuth Tokens (P2)

Currently stored in `oauth_credentials` table with Fernet encryption:
- User OAuth access tokens
- User OAuth refresh tokens
- Token metadata (expiry, scopes)

### 5. Cloud Storage Credentials (P2)

- **GCS**: Service account JSON (`GOOGLE_APPLICATION_CREDENTIALS`)
- **AWS S3**: Access key ID + Secret (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`)

---

## OpenBao Architecture

### Deployment Model

```
┌─────────────────────────────────────────────────────────────────┐
│                    Docker Compose Stack                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐      ┌──────────────┐      ┌──────────────┐ │
│  │   Nexus      │      │   OpenBao    │      │  PostgreSQL  │ │
│  │   Server     │─────▶│   Server     │      │              │ │
│  │   :2026      │      │   :8200      │      │   :5432      │ │
│  └──────────────┘      └──────────────┘      └──────────────┘ │
│         │                      │                      ▲         │
│         │                      │                      │         │
│         └──────────────────────┴──────────────────────┘         │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### OpenBao Configuration

**Development Mode:**
```bash
docker run -p 8200:8200 \
  -e OPENBAO_DEV_ROOT_TOKEN_ID=dev-root-token \
  -e OPENBAO_DEV_LISTEN_ADDRESS=0.0.0.0:8200 \
  ghcr.io/openbao/openbao:2.1.0 server -dev
```

**Production Mode:**
- Use AppRole authentication
- Enable TLS
- Use persistent storage (file backend or Consul)
- Enable audit logging

### OpenBao Images

**Recommended**: Use `ghcr.io/openbao/openbao:2.1.0` instead of `quay.io/openbao/openbao:2.1.0`
- **Reason**: GitHub Container Registry has faster CDN than quay.io
- **Note**: Same image, just different registry

---

## Secret Organization Design

### Path Hierarchy

```
secret/                                    # KV v2 mount point
├── nexus/
│   ├── infrastructure/                    # Server infrastructure secrets
│   │   ├── database/
│   │   │   ├── postgres-main             # Primary database
│   │   │   │   ├── user
│   │   │   │   ├── password
│   │   │   │   ├── host
│   │   │   │   ├── port
│   │   │   │   └── database
│   │   │   └── postgres-readonly         # Read-only credentials (future)
│   │   ├── encryption/
│   │   │   ├── oauth-key                 # Fernet encryption key
│   │   │   │   └── key
│   │   │   └── jwt-private-key           # JWT signing key (future)
│   │   └── api-keys/
│   │       └── nexus-admin               # Admin API key
│   │           └── key
│   │
│   ├── oauth-providers/                   # OAuth client credentials
│   │   ├── google/
│   │   │   └── credentials
│   │   │       ├── client_id
│   │   │       ├── client_secret
│   │   │       └── redirect_uri
│   │   ├── microsoft/
│   │   │   └── credentials
│   │   ├── slack/
│   │   │   └── credentials
│   │   └── x/
│   │       └── credentials
│   │
│   ├── external-services/                 # Third-party API keys
│   │   ├── llm/                          # LLM providers
│   │   │   ├── anthropic
│   │   │   │   └── api_key
│   │   │   ├── openai
│   │   │   │   ├── api_key
│   │   │   │   └── org_id
│   │   │   └── openrouter
│   │   │       └── api_key
│   │   ├── tools/                        # Tool integrations
│   │   │   ├── tavily
│   │   │   ├── e2b
│   │   │   └── firecrawl
│   │   ├── parsers/                      # Document parsing
│   │   │   ├── unstructured
│   │   │   └── llamacloud
│   │   └── observability/
│   │       └── langsmith
│   │
│   ├── cloud-storage/                     # Cloud storage backends
│   │   ├── gcs/
│   │   │   └── service-account           # Full JSON credentials
│   │   └── s3/
│   │       └── credentials
│   │           ├── access_key_id
│   │           └── secret_access_key
│   │
│   └── users/                             # User-scoped secrets
│       └── {tenant_id}/                   # Tenant isolation
│           └── oauth/
│               └── {provider}/
│                   └── {safe_email}/     # Encoded email (alice_at_example_dot_com)
│                       └── tokens
│                           ├── access_token
│                           ├── refresh_token
│                           ├── expires_at
│                           ├── token_type
│                           └── scopes
```

### Path Encoding

For user OAuth paths, special characters are encoded using `_safe_path()`:
```python
alice@example.com     → alice_at_example_dot_com
user+tag@host.co      → user_plus_tag_at_host_dot_co
name/with/slashes     → name_slash_with_slash_slashes
```

---

## Access Control Strategy

### OpenBao Policies

#### 1. Bootstrap Policy (`nexus-bootstrap`)

Used during server startup to load infrastructure secrets.

```hcl
# Read-only access to infrastructure secrets
path "secret/data/nexus/infrastructure/*" {
  capabilities = ["read", "list"]
}

# Read OAuth provider credentials
path "secret/data/nexus/oauth-providers/*" {
  capabilities = ["read", "list"]
}

# Read external service API keys
path "secret/data/nexus/external-services/*" {
  capabilities = ["read", "list"]
}

# Read cloud storage credentials
path "secret/data/nexus/cloud-storage/*" {
  capabilities = ["read", "list"]
}
```

#### 2. Runtime Policy (`nexus-runtime`)

Used by Nexus server for normal operations including user OAuth token management.

```hcl
# Read infrastructure secrets
path "secret/data/nexus/infrastructure/*" {
  capabilities = ["read", "list"]
}

# Read OAuth provider credentials
path "secret/data/nexus/oauth-providers/*" {
  capabilities = ["read", "list"]
}

# Read external service API keys
path "secret/data/nexus/external-services/*" {
  capabilities = ["read", "list"]
}

# Read cloud storage credentials
path "secret/data/nexus/cloud-storage/*" {
  capabilities = ["read", "list"]
}

# Full CRUD access to user OAuth tokens
path "secret/data/nexus/users/+/oauth/*/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}

# List user tenants
path "secret/metadata/nexus/users/*" {
  capabilities = ["list"]
}
```

#### 3. Admin Policy (`nexus-admin`)

Used for secret management, rotation, and administration.

```hcl
# Full access to all secrets
path "secret/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}

# Manage policies
path "sys/policies/acl/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}

# Manage authentication
path "auth/*" {
  capabilities = ["create", "read", "update", "delete", "list", "sudo"]
}
```

### Authentication Methods

#### Development
- **Root token**: `OPENBAO_DEV_ROOT_TOKEN_ID=dev-root-token`
- Simple, no setup required
- **Never use in production**

#### Production
- **AppRole**: Recommended for server-to-server authentication
- **Kubernetes Auth**: For Kubernetes deployments
- **Token renewal**: Automatic token renewal with TTL

### Token Management

```python
# Development mode
OPENBAO_TOKEN = os.getenv("OPENBAO_TOKEN", "dev-root-token")

# Production mode (AppRole)
# 1. Authenticate with role_id and secret_id
# 2. Receive client token with TTL
# 3. Auto-renew before expiry
# 4. Handle renewal failures with retry
```

---

## Migration Strategy

### Phase 1: Infrastructure Secrets (Week 1-2)

**Goal**: Migrate high-value, low-risk secrets

#### Secrets to Migrate
- ✅ PostgreSQL credentials
- ✅ OAuth encryption key
- ✅ OAuth provider credentials
- ✅ Admin API key

#### Migration Steps

**Step 1: Populate OpenBao**
```bash
# PostgreSQL credentials
openbao kv put secret/nexus/infrastructure/database/postgres-main \
  user="postgres" \
  password="nexus" \
  host="postgres" \
  port="5432" \
  database="nexus"

# OAuth encryption key (generate new or migrate existing)
OAUTH_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
openbao kv put secret/nexus/infrastructure/encryption/oauth-key \
  key="$OAUTH_KEY"

# Google OAuth credentials
openbao kv put secret/nexus/oauth-providers/google/credentials \
  client_id="${NEXUS_OAUTH_GOOGLE_CLIENT_ID}" \
  client_secret="${NEXUS_OAUTH_GOOGLE_CLIENT_SECRET}" \
  redirect_uri="http://localhost:2026/oauth/google/callback"

# Microsoft OAuth credentials
openbao kv put secret/nexus/oauth-providers/microsoft/credentials \
  client_id="${NEXUS_OAUTH_MICROSOFT_CLIENT_ID}" \
  client_secret="${NEXUS_OAUTH_MICROSOFT_CLIENT_SECRET}" \
  redirect_uri="http://localhost:2026/oauth/microsoft/callback"

# Anthropic API key
openbao kv put secret/nexus/external-services/llm/anthropic \
  api_key="${ANTHROPIC_API_KEY}"

# OpenAI API key
openbao kv put secret/nexus/external-services/llm/openai \
  api_key="${OPENAI_API_KEY}" \
  org_id="${OPENAI_ORG_ID:-}"
```

**Step 2: Update Code**

Create `src/nexus/server/secrets/openbao_loader.py`:
```python
"""Load configuration from OpenBao secrets backend."""

import logging
from typing import Any

from nexus.server.secrets.base import SecretsBackend

logger = logging.getLogger(__name__)


async def load_config_from_openbao(backend: SecretsBackend) -> dict[str, Any]:
    """Load configuration from OpenBao secrets backend.

    Args:
        backend: OpenBao secrets backend instance

    Returns:
        Configuration dictionary with all secrets loaded
    """
    config: dict[str, Any] = {}

    # Load PostgreSQL credentials
    try:
        db_secret = await backend.get_secret("nexus/infrastructure/database/postgres-main")
        if db_secret:
            config["database_url"] = (
                f"postgresql://{db_secret['user']}:{db_secret['password']}"
                f"@{db_secret['host']}:{db_secret['port']}/{db_secret['database']}"
            )
            logger.info("Loaded PostgreSQL credentials from OpenBao")
    except Exception as e:
        logger.warning(f"Failed to load PostgreSQL credentials from OpenBao: {e}")

    # Load OAuth encryption key
    try:
        oauth_key = await backend.get_secret("nexus/infrastructure/encryption/oauth-key")
        if oauth_key:
            config["oauth_encryption_key"] = oauth_key["key"]
            logger.info("Loaded OAuth encryption key from OpenBao")
    except Exception as e:
        logger.warning(f"Failed to load OAuth encryption key from OpenBao: {e}")

    # Load OAuth provider credentials
    for provider in ["google", "microsoft", "slack", "x"]:
        try:
            creds = await backend.get_secret(f"nexus/oauth-providers/{provider}/credentials")
            if creds:
                config[f"oauth_{provider}_client_id"] = creds["client_id"]
                config[f"oauth_{provider}_client_secret"] = creds["client_secret"]
                if "redirect_uri" in creds:
                    config[f"oauth_{provider}_redirect_uri"] = creds["redirect_uri"]
                logger.info(f"Loaded {provider} OAuth credentials from OpenBao")
        except Exception as e:
            logger.warning(f"Failed to load {provider} OAuth credentials from OpenBao: {e}")

    # Load LLM API keys
    try:
        anthropic = await backend.get_secret("nexus/external-services/llm/anthropic")
        if anthropic:
            config["anthropic_api_key"] = anthropic["api_key"]
            logger.info("Loaded Anthropic API key from OpenBao")
    except Exception as e:
        logger.warning(f"Failed to load Anthropic API key from OpenBao: {e}")

    try:
        openai = await backend.get_secret("nexus/external-services/llm/openai")
        if openai:
            config["openai_api_key"] = openai["api_key"]
            if "org_id" in openai and openai["org_id"]:
                config["openai_org_id"] = openai["org_id"]
            logger.info("Loaded OpenAI API key from OpenBao")
    except Exception as e:
        logger.warning(f"Failed to load OpenAI API key from OpenBao: {e}")

    return config
```

**Step 3: Update `OAuthCrypto`**

Modify `src/nexus/server/auth/oauth_crypto.py`:
```python
async def _load_key_from_openbao(self) -> str | None:
    """Load OAuth encryption key from OpenBao.

    Returns:
        Encryption key string, or None if not found
    """
    try:
        from nexus.server.secrets import create_secrets_backend

        backend = create_secrets_backend(backend_type="openbao")
        secret_data = await backend.get_secret("nexus/infrastructure/encryption/oauth-key")
        await backend.close()

        if secret_data and "key" in secret_data:
            return secret_data["key"]
        return None
    except Exception as e:
        logger.warning(f"Failed to load OAuth key from OpenBao: {e}")
        return None

# Update __init__ to try OpenBao first
def __init__(self, encryption_key: str | None = None, db_url: str | None = None):
    # Priority 1: Explicit encryption key
    if encryption_key is not None:
        logger.debug("OAuthCrypto: Using explicit encryption key")
        self._init_fernet(encryption_key)
        return

    # Priority 2: Environment variable
    env_key = os.environ.get("NEXUS_OAUTH_ENCRYPTION_KEY", "").strip()
    if env_key:
        logger.debug("OAuthCrypto: Using env var NEXUS_OAUTH_ENCRYPTION_KEY")
        self._init_fernet(env_key)
        return

    # Priority 3: OpenBao (if NEXUS_SECRETS_BACKEND=openbao)
    if os.environ.get("NEXUS_SECRETS_BACKEND") == "openbao":
        import asyncio
        openbao_key = asyncio.run(self._load_key_from_openbao())
        if openbao_key:
            logger.debug("OAuthCrypto: Loaded key from OpenBao")
            self._init_fernet(openbao_key)
            return

    # Priority 4: Load from database
    if db_url:
        # ... existing database logic ...
```

**Step 4: Test**
```bash
# Set environment
export NEXUS_SECRETS_BACKEND=openbao
export OPENBAO_ADDR=http://localhost:8200
export OPENBAO_TOKEN=dev-root-token

# Test configuration loading
python -c "
from nexus.server.secrets import create_secrets_backend
from nexus.server.secrets.openbao_loader import load_config_from_openbao
import asyncio

async def test():
    backend = create_secrets_backend('openbao')
    config = await load_config_from_openbao(backend)
    print('Loaded config:', config.keys())
    await backend.close()

asyncio.run(test())
"
```

### Phase 2: External Service API Keys (Week 3)

**Goal**: Migrate external API keys for LLM, tools, observability

#### Secrets to Migrate
- ✅ Anthropic, OpenAI, OpenRouter API keys
- ✅ E2B, Tavily, Firecrawl API keys
- ✅ Unstructured, LlamaCloud API keys
- ✅ LangSmith API key

#### Migration Steps
Similar to Phase 1, populate OpenBao and update code to read from `nexus/external-services/*`.

### Phase 3: User OAuth Tokens (Week 4-6)

**Goal**: Gradual migration of user OAuth tokens with zero downtime

#### Strategy: Dual-Write Pattern

**Step 1: Implement Dual-Write**

When storing new OAuth credentials, write to both database AND OpenBao:

```python
async def store_oauth_credentials(
    self,
    provider: str,
    user_email: str,
    tenant_id: str,
    access_token: str,
    refresh_token: str | None,
    expires_at: datetime | None,
):
    """Store OAuth credentials with dual-write to DB and OpenBao."""

    # 1. Encrypt and store in database (existing logic for backward compatibility)
    encrypted_access = self.crypto.encrypt_token(access_token)
    encrypted_refresh = self.crypto.encrypt_token(refresh_token) if refresh_token else None

    # Save to database...
    db_credential = await self._save_to_database(
        provider, user_email, tenant_id,
        encrypted_access, encrypted_refresh, expires_at
    )

    # 2. ALSO store in OpenBao (if OpenBao backend is enabled)
    if self.secrets_backend.backend_type == "openbao":
        try:
            from nexus.server.secrets.openbao_backend import make_credential_path

            path = make_credential_path(tenant_id, provider, user_email)
            await self.secrets_backend.set_secret(path, {
                "access_token": access_token,  # Store plaintext in OpenBao (it encrypts)
                "refresh_token": refresh_token,
                "expires_at": expires_at.isoformat() if expires_at else None,
                "token_type": "Bearer",
                "scopes": db_credential.scopes,
            })

            # Mark in database that this credential is in OpenBao
            # Update DB record: encrypted_access_token = f"openbao:{path}"
            await self._mark_as_openbao(db_credential, path)
            logger.info(f"Stored OAuth credentials in OpenBao: {path}")
        except Exception as e:
            logger.warning(f"Failed to store credentials in OpenBao, using database only: {e}")
```

**Step 2: Implement Read-Through Pattern**

When reading credentials, check if they're in OpenBao:

```python
async def get_oauth_credentials(
    self,
    provider: str,
    user_email: str,
    tenant_id: str,
):
    """Retrieve OAuth credentials with OpenBao read-through."""

    # Get database record
    db_record = await self._get_db_record(provider, user_email, tenant_id)

    if not db_record:
        return None

    # Check if credential is marked as stored in OpenBao
    if db_record.encrypted_access_token.startswith("openbao:"):
        # Read from OpenBao
        path = db_record.encrypted_access_token[8:]  # Strip "openbao:" prefix
        try:
            secret_data = await self.secrets_backend.get_secret(path)
            if secret_data:
                logger.debug(f"Retrieved OAuth credentials from OpenBao: {path}")
                return {
                    "access_token": secret_data["access_token"],
                    "refresh_token": secret_data.get("refresh_token"),
                    "expires_at": datetime.fromisoformat(secret_data["expires_at"])
                                 if secret_data.get("expires_at") else None,
                    "token_type": secret_data.get("token_type", "Bearer"),
                }
        except Exception as e:
            logger.error(f"Failed to read from OpenBao, falling back to database: {e}")
            # Fall through to database decryption

    # Read from database (legacy or fallback)
    try:
        decrypted_access = self.crypto.decrypt_token(db_record.encrypted_access_token)
        decrypted_refresh = None
        if db_record.encrypted_refresh_token:
            decrypted_refresh = self.crypto.decrypt_token(db_record.encrypted_refresh_token)

        logger.debug(f"Retrieved OAuth credentials from database (legacy)")
        return {
            "access_token": decrypted_access,
            "refresh_token": decrypted_refresh,
            "expires_at": db_record.expires_at,
            "token_type": db_record.token_type or "Bearer",
        }
    except Exception as e:
        logger.error(f"Failed to decrypt credentials from database: {e}")
        return None
```

**Step 3: Background Migration Job**

Create migration script to move existing credentials:

```python
# scripts/migrate_oauth_to_openbao.py

async def migrate_oauth_credentials():
    """Migrate existing OAuth credentials from database to OpenBao."""

    backend = create_secrets_backend("openbao")
    crypto = OAuthCrypto(db_url=DATABASE_URL)

    # Query all credentials from database
    credentials = await fetch_all_oauth_credentials()

    total = len(credentials)
    migrated = 0
    failed = 0

    for cred in credentials:
        try:
            # Skip if already migrated
            if cred.encrypted_access_token.startswith("openbao:"):
                logger.info(f"Skipping already migrated credential: {cred.credential_id}")
                continue

            # Decrypt from database
            access_token = crypto.decrypt_token(cred.encrypted_access_token)
            refresh_token = None
            if cred.encrypted_refresh_token:
                refresh_token = crypto.decrypt_token(cred.encrypted_refresh_token)

            # Store in OpenBao
            path = make_credential_path(cred.zone_id, cred.provider, cred.user_email)
            await backend.set_secret(path, {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": cred.expires_at.isoformat() if cred.expires_at else None,
                "token_type": cred.token_type or "Bearer",
                "scopes": cred.scopes,
            })

            # Update database to mark as migrated
            await mark_as_openbao(cred, path)

            migrated += 1
            logger.info(f"Migrated {migrated}/{total}: {cred.provider}/{cred.user_email}")

        except Exception as e:
            failed += 1
            logger.error(f"Failed to migrate credential {cred.credential_id}: {e}")

    logger.info(f"Migration complete: {migrated} migrated, {failed} failed, {total} total")
    await backend.close()
```

**Step 4: Monitor Migration Progress**

```sql
-- Check migration status
SELECT
    provider,
    COUNT(*) as total,
    SUM(CASE WHEN encrypted_access_token LIKE 'openbao:%' THEN 1 ELSE 0 END) as migrated_to_openbao,
    SUM(CASE WHEN encrypted_access_token NOT LIKE 'openbao:%' THEN 1 ELSE 0 END) as in_database
FROM oauth_credentials
GROUP BY provider;
```

### Phase 4: Cleanup (Week 7-8)

**When migration reaches 100%:**

1. **Remove database fallback** - Remove legacy decryption code
2. **Archive old credentials** - Export database credentials for backup
3. **Update documentation** - Reflect OpenBao as primary secret store

---

## Configuration

### Environment Variables

```bash
# ============================================
# OpenBao Secrets Management
# ============================================

# Secrets backend: "database" (default) or "openbao"
NEXUS_SECRETS_BACKEND=openbao

# OpenBao connection
OPENBAO_ADDR=http://openbao:8200

# Development: Use root token (NEVER in production)
OPENBAO_TOKEN=dev-root-token

# Production: Use AppRole authentication
# OPENBAO_ROLE_ID=<your-role-id>
# OPENBAO_SECRET_ID=<your-secret-id>

# ============================================
# Legacy (when NEXUS_SECRETS_BACKEND=database)
# ============================================

# Database URL (legacy mode)
# NEXUS_DATABASE_URL=postgresql://postgres:nexus@localhost:5432/nexus

# OAuth encryption key (legacy mode)
# NEXUS_OAUTH_ENCRYPTION_KEY=<fernet-key>

# OAuth provider credentials (legacy mode)
# NEXUS_OAUTH_GOOGLE_CLIENT_ID=<client-id>
# NEXUS_OAUTH_GOOGLE_CLIENT_SECRET=<client-secret>
```

### Docker Compose

```yaml
# docker-compose.yml

services:
  openbao:
    image: ghcr.io/openbao/openbao:2.1.0
    container_name: nexus-openbao
    ports:
      - "8200:8200"
    environment:
      OPENBAO_DEV_ROOT_TOKEN_ID: "dev-root-token"
      OPENBAO_DEV_LISTEN_ADDRESS: "0.0.0.0:8200"
    command: server -dev
    networks:
      - nexus-network
    # Production: Use persistent storage
    # volumes:
    #   - openbao-data:/vault/data
    #   - ./config/openbao.hcl:/vault/config/openbao.hcl
    # command: server -config=/vault/config/openbao.hcl

  nexus-server:
    image: nexus-server:latest
    ports:
      - "2026:2026"
    environment:
      # OpenBao secrets backend
      NEXUS_SECRETS_BACKEND: "openbao"
      OPENBAO_ADDR: "http://openbao:8200"
      OPENBAO_TOKEN: "dev-root-token"

      # Other configuration
      NEXUS_PORT: "2026"
    depends_on:
      - postgres
      - openbao
    networks:
      - nexus-network

  postgres:
    image: postgres:15
    environment:
      POSTGRES_DB: nexus
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: nexus
    ports:
      - "5432:5432"
    volumes:
      - postgres-data:/var/lib/postgresql/data
    networks:
      - nexus-network

networks:
  nexus-network:
    driver: bridge

volumes:
  postgres-data:
  # openbao-data:  # Uncomment for production
```

---

## Security Considerations

### Encryption Layers

| Layer | Technology | Purpose | Notes |
|-------|-----------|---------|-------|
| **Transport** | TLS (HTTPS) | Encrypt data in transit to OpenBao | Required in production |
| **At-Rest** | OpenBao Storage Encryption | Encrypt data in OpenBao's storage | Automatic with OpenBao |
| **Application** | Fernet (optional) | Double encryption for sensitive tokens | Can be removed for OpenBao-stored secrets |

**Recommendation**: Remove Fernet encryption for secrets stored in OpenBao to simplify architecture. Keep Fernet only for database-stored secrets during migration.

### Access Control Best Practices

1. **Principle of Least Privilege**
   - Bootstrap token: Read-only access to infrastructure secrets
   - Runtime token: Read infrastructure + CRUD user secrets
   - Admin token: Full access (use sparingly)

2. **Audit Logging**
   - Enable OpenBao audit logs: `openbao audit enable file file_path=/vault/logs/audit.log`
   - Monitor for suspicious access patterns
   - Alert on failed authentication attempts

3. **Secret Rotation**
   - OAuth tokens: Auto-refresh via OAuth flow (handled by Nexus)
   - Database credentials: Manual rotation with downtime
   - API keys: Periodic manual rotation
   - Future: Automated rotation with version tracking

4. **Network Security**
   - Production: Use TLS for OpenBao (HTTPS)
   - Firewall: Restrict OpenBao port 8200 to trusted networks
   - Docker: Use internal network, no public exposure

### Disaster Recovery

#### Backup Strategy

```bash
# Backup OpenBao data (development - file backend)
openbao kv list -format=json secret/nexus/ | \
  jq -r '.[]' | \
  xargs -I {} openbao kv get -format=json secret/nexus/{} > openbao-backup.json

# Production: Use OpenBao snapshots
openbao operator raft snapshot save nexus-backup-$(date +%Y%m%d).snap
```

#### Recovery Procedures

1. **OpenBao Unavailable**: Fallback to database backend
   - Set `NEXUS_SECRETS_BACKEND=database`
   - Restart Nexus server
   - Investigate OpenBao issue

2. **Secrets Lost**: Restore from backup
   - Restore OpenBao snapshot
   - Verify secrets with `openbao kv get`
   - Test Nexus connection

3. **Token Compromised**: Rotate immediately
   - Revoke compromised token
   - Generate new token
   - Update server configuration
   - Restart services

---

## Operational Guide

### Development Setup

```bash
# 1. Start OpenBao in dev mode
docker run -d --name openbao -p 8200:8200 \
  -e OPENBAO_DEV_ROOT_TOKEN_ID=dev-root-token \
  ghcr.io/openbao/openbao:2.1.0 server -dev

# 2. Set environment
export OPENBAO_ADDR=http://localhost:8200
export OPENBAO_TOKEN=dev-root-token

# 3. Populate secrets
./scripts/populate_openbao_dev.sh

# 4. Run Nexus with OpenBao backend
export NEXUS_SECRETS_BACKEND=openbao
python -m nexus.server.rpc_server
```

### Production Setup

```bash
# 1. Initialize OpenBao
openbao operator init -key-shares=5 -key-threshold=3

# 2. Unseal OpenBao (requires 3 of 5 keys)
openbao operator unseal <key-1>
openbao operator unseal <key-2>
openbao operator unseal <key-3>

# 3. Create policies
openbao policy write nexus-bootstrap ./config/policies/nexus-bootstrap.hcl
openbao policy write nexus-runtime ./config/policies/nexus-runtime.hcl
openbao policy write nexus-admin ./config/policies/nexus-admin.hcl

# 4. Enable AppRole authentication
openbao auth enable approle

# 5. Create AppRole for Nexus
openbao write auth/approle/role/nexus-server \
  token_policies="nexus-runtime" \
  token_ttl=1h \
  token_max_ttl=4h

# 6. Get role credentials
openbao read auth/approle/role/nexus-server/role-id
openbao write -f auth/approle/role/nexus-server/secret-id

# 7. Configure Nexus
export NEXUS_SECRETS_BACKEND=openbao
export OPENBAO_ADDR=https://openbao.example.com:8200
export OPENBAO_ROLE_ID=<role-id>
export OPENBAO_SECRET_ID=<secret-id>
```

### Monitoring

#### Health Checks

```bash
# OpenBao health
curl http://localhost:8200/v1/sys/health

# Nexus secrets backend health
curl http://localhost:2026/health | jq '.secrets_backend'
```

#### Metrics to Track

- `openbao_request_latency_ms` - Latency of OpenBao requests
- `openbao_error_rate` - Error rate for OpenBao operations
- `openbao_cache_hit_rate` - Cache hit rate (if caching enabled)
- `oauth_token_refresh_rate` - OAuth token refresh frequency
- `secret_migration_progress_pct` - % of secrets migrated to OpenBao

#### Alerts

- OpenBao unreachable for > 1 minute
- OpenBao request latency > 500ms (p95)
- OpenBao error rate > 1%
- Failed authentication attempts > 10/minute
- Disk space on OpenBao volume < 20%

---

## Testing Strategy

### Unit Tests

```python
# tests/unit/server/secrets/test_openbao_loader.py

import pytest
from nexus.server.secrets.openbao_loader import load_config_from_openbao
from nexus.server.secrets.openbao_backend import OpenBaoSecretsBackend

@pytest.mark.asyncio
async def test_load_postgres_credentials(mock_openbao):
    """Test loading PostgreSQL credentials from OpenBao."""
    backend = OpenBaoSecretsBackend(addr="http://localhost:8200", token="test")

    # Populate mock
    await backend.set_secret("nexus/infrastructure/database/postgres-main", {
        "user": "testuser",
        "password": "testpass",
        "host": "localhost",
        "port": "5432",
        "database": "testdb",
    })

    # Load config
    config = await load_config_from_openbao(backend)

    assert "database_url" in config
    assert "postgresql://testuser:testpass@localhost:5432/testdb" == config["database_url"]

@pytest.mark.asyncio
async def test_load_oauth_encryption_key(mock_openbao):
    """Test loading OAuth encryption key from OpenBao."""
    backend = OpenBaoSecretsBackend(addr="http://localhost:8200", token="test")

    # Populate mock
    await backend.set_secret("nexus/infrastructure/encryption/oauth-key", {
        "key": "test-fernet-key-base64==",
    })

    # Load config
    config = await load_config_from_openbao(backend)

    assert "oauth_encryption_key" in config
    assert "test-fernet-key-base64==" == config["oauth_encryption_key"]
```

### Integration Tests

```python
# tests/integration/test_openbao_integration.py

import pytest
from nexus.server.secrets import create_secrets_backend
from nexus.server.secrets.openbao_loader import load_config_from_openbao

@pytest.mark.integration
@pytest.mark.asyncio
async def test_openbao_real_connection():
    """Test real connection to OpenBao dev server."""
    backend = create_secrets_backend("openbao")

    # Test health
    healthy = await backend.health_check()
    assert healthy, "OpenBao should be healthy"

    # Test write
    await backend.set_secret("nexus/test/example", {"foo": "bar"})

    # Test read
    data = await backend.get_secret("nexus/test/example")
    assert data == {"foo": "bar"}

    # Test delete
    deleted = await backend.delete_secret("nexus/test/example")
    assert deleted

    # Verify deleted
    data = await backend.get_secret("nexus/test/example")
    assert data is None

    await backend.close()
```

### End-to-End Tests

```python
# tests/e2e/test_oauth_with_openbao.py

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_oauth_flow_with_openbao():
    """Test full OAuth flow using OpenBao for secret storage."""

    # 1. Configure Nexus with OpenBao
    os.environ["NEXUS_SECRETS_BACKEND"] = "openbao"

    # 2. Populate OpenBao with OAuth provider credentials
    backend = create_secrets_backend("openbao")
    await backend.set_secret("nexus/oauth-providers/google/credentials", {
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "redirect_uri": "http://localhost:2026/oauth/google/callback",
    })

    # 3. Start OAuth flow
    # ... (test OAuth flow)

    # 4. Verify tokens stored in OpenBao
    path = make_credential_path("default", "google", "test@example.com")
    tokens = await backend.get_secret(path)
    assert tokens is not None
    assert "access_token" in tokens
    assert "refresh_token" in tokens

    await backend.close()
```

---

## Future Enhancements

### Short-term (Next 3-6 months)

1. **Dynamic Database Credentials**
   - Use OpenBao database secrets engine
   - Generate temporary PostgreSQL credentials with TTL
   - Auto-rotate credentials without downtime

2. **Secret Caching**
   - Cache frequently accessed secrets in memory
   - TTL-based cache expiration
   - Reduce OpenBao load and latency

3. **Secret Versioning UI**
   - Web UI to view secret versions
   - Rollback to previous versions
   - Audit log visualization

### Long-term (6-12 months)

1. **Automated Secret Rotation**
   - Scheduled rotation for API keys
   - Zero-downtime rotation workflow
   - Notification system for rotation events

2. **Multi-Region OpenBao**
   - OpenBao cluster with replication
   - Regional failover capabilities
   - Consistent secret access across regions

3. **Hardware Security Module (HSM)**
   - HSM integration for key storage
   - FIPS 140-2 compliance
   - Enhanced security for enterprise deployments

---

## References

### OpenBao Documentation
- [OpenBao KV v2 Secrets Engine](https://openbao.org/docs/secrets/kv/kv-v2/)
- [OpenBao Policies](https://openbao.org/docs/concepts/policies/)
- [OpenBao AppRole Authentication](https://openbao.org/docs/auth/approle/)

### Nexus Documentation
- [Architecture Overview](../ARCHITECTURE.md)
- [Authentication System](./auth-system.md)
- [User Authentication](./user-authentication-system.md)

### Related Issues
- OpenBao integration (#TBD)
- Secret rotation automation (#TBD)
- Dynamic database credentials (#TBD)

### External Resources
- [HashiCorp Vault Best Practices](https://learn.hashicorp.com/tutorials/vault/production-hardening)
- [Secrets Management Patterns](https://www.cncf.io/blog/2020/02/05/best-practices-for-secrets-management/)
- [12-Factor App: Config](https://12factor.net/config)

---

**Document History**:
- 2026-02-09: Initial draft created
- TBD: Review and feedback
- TBD: Implementation started
