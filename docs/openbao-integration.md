# OpenBao Integration Guide

This guide explains how to integrate [OpenBao](https://openbao.org/) with Nexus for enterprise-grade secrets management.

## Overview

**OpenBao** is an open-source, community-driven fork of HashiCorp Vault managed by the Linux Foundation. It provides secure secret storage, dynamic credentials, and encryption as a service.

### Why OpenBao for Nexus?

| Current Approach | OpenBao Approach |
|------------------|------------------|
| API keys in environment variables | Centralized, encrypted secret storage |
| Manual credential rotation | Automatic secret rotation with leases |
| Fernet encryption for OAuth tokens | Transit engine for encryption |
| No audit trail for secret access | Full audit logging |
| Per-instance key management | Centralized key management |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Nexus Server                            │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────────┐    ┌─────────────────┐                     │
│  │  OpenBaoClient  │───▶│  Secret Cache   │                     │
│  └────────┬────────┘    └─────────────────┘                     │
│           │                                                      │
│           │ (authenticated requests)                             │
│           ▼                                                      │
│  ┌─────────────────────────────────────────┐                     │
│  │            OpenBao Server               │                     │
│  ├─────────────────────────────────────────┤                     │
│  │  ┌─────────┐ ┌─────────┐ ┌───────────┐  │                     │
│  │  │   KV    │ │ Transit │ │  Database │  │                     │
│  │  │ Engine  │ │ Engine  │ │  Engine   │  │                     │
│  │  └─────────┘ └─────────┘ └───────────┘  │                     │
│  └─────────────────────────────────────────┘                     │
└─────────────────────────────────────────────────────────────────┘
```

### Integration Points

1. **KV Secrets Engine** - Store API keys (OpenAI, Anthropic, etc.)
2. **Transit Engine** - Replace Fernet for OAuth token encryption
3. **Database Engine** - Dynamic PostgreSQL credentials
4. **PKI Engine** - TLS certificate management
5. **Kubernetes Auth** - For K8s deployments

---

## Quick Start

### 1. Deploy OpenBao

**Docker (Development)**:
```bash
docker run -d \
  --name openbao-dev \
  -p 8200:8200 \
  -e BAO_DEV_ROOT_TOKEN_ID=dev-only-token \
  openbao/openbao
```

**Docker Compose (with Nexus)**:
```yaml
# Add to docker-compose.demo.yml
services:
  openbao:
    image: openbao/openbao:latest
    container_name: openbao
    ports:
      - "8200:8200"
    environment:
      - BAO_DEV_ROOT_TOKEN_ID=${OPENBAO_DEV_TOKEN:-dev-only-token}
    cap_add:
      - IPC_LOCK
    volumes:
      - openbao-data:/openbao/data

volumes:
  openbao-data:
```

### 2. Configure Nexus

Add OpenBao settings to your configuration:

**Environment Variables**:
```bash
# OpenBao connection
export NEXUS_OPENBAO_ADDR="http://localhost:8200"
export NEXUS_OPENBAO_TOKEN="dev-only-token"  # For dev only

# Or use AppRole authentication (production)
export NEXUS_OPENBAO_ROLE_ID="your-role-id"
export NEXUS_OPENBAO_SECRET_ID="your-secret-id"

# Enable OpenBao for secrets
export NEXUS_SECRETS_BACKEND="openbao"
```

**Config File** (`nexus.yaml`):
```yaml
secrets:
  backend: openbao
  openbao:
    address: http://localhost:8200
    # Authentication (choose one)
    token: ${OPENBAO_TOKEN}  # Dev only
    # Or AppRole (recommended for production)
    auth_method: approle
    role_id: ${OPENBAO_ROLE_ID}
    secret_id: ${OPENBAO_SECRET_ID}
    # Mount paths
    kv_mount: secret
    transit_mount: transit
    # Cache settings
    cache_ttl: 300  # seconds
```

### 3. Store Secrets in OpenBao

```bash
# Set up environment
export BAO_ADDR='http://127.0.0.1:8200'
export BAO_TOKEN='dev-only-token'

# Enable KV secrets engine
bao secrets enable -path=secret kv-v2

# Store Nexus secrets
bao kv put -mount=secret nexus/api-keys \
  openai_api_key="sk-..." \
  anthropic_api_key="sk-ant-..." \
  tavily_api_key="tvly-..."

# Store OAuth encryption key
bao kv put -mount=secret nexus/oauth \
  encryption_key="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
```

---

## Detailed Configuration

### KV Secrets Engine (API Keys)

Store all API keys in OpenBao's KV secrets engine:

**Path Structure**:
```
secret/
├── nexus/
│   ├── api-keys           # LLM provider keys
│   │   ├── openai_api_key
│   │   ├── anthropic_api_key
│   │   ├── tavily_api_key
│   │   └── e2b_api_key
│   ├── oauth              # OAuth encryption key
│   │   └── encryption_key
│   ├── database           # Database credentials (if not using dynamic)
│   │   └── url
│   └── mcp/               # Per-MCP-mount secrets
│       ├── github/
│       │   └── personal_access_token
│       └── slack/
│           └── bot_token
```

**Reading Secrets in Python**:
```python
from nexus.secrets.openbao import OpenBaoClient

client = OpenBaoClient()

# Get API keys
api_keys = client.read_secret("nexus/api-keys")
openai_key = api_keys["openai_api_key"]

# Get OAuth encryption key
oauth = client.read_secret("nexus/oauth")
encryption_key = oauth["encryption_key"]
```

### Transit Engine (Encryption)

Replace Fernet with OpenBao Transit for OAuth token encryption:

```bash
# Enable Transit engine
bao secrets enable transit

# Create encryption key for OAuth tokens
bao write -f transit/keys/nexus-oauth

# Create encryption key for sensitive data
bao write -f transit/keys/nexus-data
```

**Encrypt/Decrypt with Transit**:
```python
from nexus.secrets.openbao import OpenBaoClient

client = OpenBaoClient()

# Encrypt OAuth token
encrypted = client.encrypt("nexus-oauth", access_token)

# Decrypt OAuth token
decrypted = client.decrypt("nexus-oauth", encrypted)
```

### Database Engine (Dynamic Credentials)

Generate dynamic PostgreSQL credentials with automatic rotation:

```bash
# Enable database engine
bao secrets enable database

# Configure PostgreSQL connection
bao write database/config/nexus-db \
  plugin_name=postgresql-database-plugin \
  allowed_roles="nexus-role" \
  connection_url="postgresql://{{username}}:{{password}}@localhost:5432/nexus?sslmode=disable" \
  username="openbao-admin" \
  password="admin-password"

# Create role with credential template
bao write database/roles/nexus-role \
  db_name=nexus-db \
  creation_statements="CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'; \
    GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO \"{{name}}\";" \
  default_ttl="1h" \
  max_ttl="24h"
```

**Get Dynamic Credentials**:
```python
from nexus.secrets.openbao import OpenBaoClient

client = OpenBaoClient()

# Get dynamic database credentials
creds = client.get_database_credentials("nexus-role")
db_url = f"postgresql://{creds['username']}:{creds['password']}@localhost:5432/nexus"

# Credentials auto-expire after TTL
# Nexus handles renewal automatically
```

---

## Authentication Methods

### Token Auth (Development Only)

```python
from nexus.secrets.openbao import OpenBaoClient

client = OpenBaoClient(
    address="http://localhost:8200",
    token="dev-only-token"
)
```

### AppRole Auth (Production)

```bash
# Enable AppRole
bao auth enable approle

# Create policy for Nexus
bao policy write nexus-policy - <<EOF
# Read API keys
path "secret/data/nexus/*" {
  capabilities = ["read", "list"]
}

# Use Transit encryption
path "transit/encrypt/nexus-*" {
  capabilities = ["update"]
}
path "transit/decrypt/nexus-*" {
  capabilities = ["update"]
}

# Get database credentials
path "database/creds/nexus-role" {
  capabilities = ["read"]
}
EOF

# Create AppRole
bao write auth/approle/role/nexus \
  token_policies="nexus-policy" \
  token_ttl=1h \
  token_max_ttl=4h \
  secret_id_ttl=720h

# Get role ID (save this)
bao read auth/approle/role/nexus/role-id

# Generate secret ID (save this securely)
bao write -f auth/approle/role/nexus/secret-id
```

```python
from nexus.secrets.openbao import OpenBaoClient

client = OpenBaoClient(
    address="http://openbao:8200",
    auth_method="approle",
    role_id="your-role-id",
    secret_id="your-secret-id"
)
```

### Kubernetes Auth (K8s Deployments)

```bash
# Enable Kubernetes auth
bao auth enable kubernetes

# Configure Kubernetes auth
bao write auth/kubernetes/config \
  kubernetes_host="https://kubernetes.default.svc" \
  kubernetes_ca_cert=@/var/run/secrets/kubernetes.io/serviceaccount/ca.crt

# Create role for Nexus pods
bao write auth/kubernetes/role/nexus \
  bound_service_account_names=nexus \
  bound_service_account_namespaces=default \
  policies=nexus-policy \
  ttl=1h
```

```python
from nexus.secrets.openbao import OpenBaoClient

client = OpenBaoClient(
    address="http://openbao:8200",
    auth_method="kubernetes",
    role="nexus"
)
```

---

## Migration Guide

### Step 1: Install OpenBao

```bash
# Add OpenBao to docker-compose
docker-compose up -d openbao
```

### Step 2: Migrate Existing Secrets

```bash
# Export current environment variables to OpenBao
bao kv put -mount=secret nexus/api-keys \
  openai_api_key="${OPENAI_API_KEY}" \
  anthropic_api_key="${ANTHROPIC_API_KEY}" \
  tavily_api_key="${TAVILY_API_KEY}"

# Migrate OAuth encryption key
bao kv put -mount=secret nexus/oauth \
  encryption_key="${NEXUS_OAUTH_ENCRYPTION_KEY}"
```

### Step 3: Update Nexus Configuration

```yaml
# nexus.yaml
secrets:
  backend: openbao
  openbao:
    address: http://openbao:8200
    auth_method: approle
    role_id: ${OPENBAO_ROLE_ID}
    secret_id: ${OPENBAO_SECRET_ID}
```

### Step 4: Restart Nexus

```bash
docker-compose restart nexus
```

### Step 5: Verify

```bash
# Check health
curl http://localhost:2026/health

# Verify secrets are loaded
docker logs nexus 2>&1 | grep -i openbao
```

---

## Security Best Practices

### 1. Never Use Dev Mode in Production

```bash
# WRONG - Dev mode (in-memory, unsealed)
bao server -dev

# CORRECT - Production mode
bao server -config=/etc/openbao/config.hcl
```

### 2. Seal/Unseal Properly

```bash
# Initialize with key shares
bao operator init -key-shares=5 -key-threshold=3

# Store unseal keys securely (separate locations)
# Unseal requires 3 of 5 keys
bao operator unseal <key-1>
bao operator unseal <key-2>
bao operator unseal <key-3>
```

### 3. Use Least-Privilege Policies

```hcl
# nexus-policy.hcl - Minimal permissions
path "secret/data/nexus/api-keys" {
  capabilities = ["read"]
}

# Deny all else by default
path "*" {
  capabilities = ["deny"]
}
```

### 4. Enable Audit Logging

```bash
bao audit enable file file_path=/var/log/openbao/audit.log
```

### 5. Rotate Secrets Regularly

```bash
# Rotate Transit encryption key
bao write -f transit/keys/nexus-oauth/rotate

# Database credentials rotate automatically via TTL
```

---

## Troubleshooting

### Connection Refused

```bash
# Check OpenBao is running
docker ps | grep openbao

# Check address is correct
curl http://localhost:8200/v1/sys/health
```

### Permission Denied

```bash
# Check token permissions
bao token capabilities <token> secret/data/nexus/api-keys

# Verify policy is attached
bao token lookup
```

### Sealed Vault

```bash
# Check seal status
bao status

# Unseal if needed
bao operator unseal
```

### Token Expired

```python
# OpenBaoClient handles token renewal automatically
# Check logs for renewal errors
docker logs nexus 2>&1 | grep -i "token"
```

---

## API Reference

### OpenBaoClient

```python
from nexus.secrets.openbao import OpenBaoClient

class OpenBaoClient:
    def __init__(
        self,
        address: str = None,          # OpenBao address (default: $NEXUS_OPENBAO_ADDR)
        token: str = None,             # Token auth (dev only)
        auth_method: str = None,       # "approle", "kubernetes", or None for token
        role_id: str = None,           # AppRole role ID
        secret_id: str = None,         # AppRole secret ID
        role: str = None,              # Kubernetes role
        kv_mount: str = "secret",      # KV secrets engine mount
        transit_mount: str = "transit", # Transit engine mount
        cache_ttl: int = 300,          # Secret cache TTL (seconds)
    ): ...

    def read_secret(self, path: str) -> dict:
        """Read a secret from KV engine."""
        ...

    def write_secret(self, path: str, data: dict) -> None:
        """Write a secret to KV engine."""
        ...

    def encrypt(self, key_name: str, plaintext: str) -> str:
        """Encrypt data using Transit engine."""
        ...

    def decrypt(self, key_name: str, ciphertext: str) -> str:
        """Decrypt data using Transit engine."""
        ...

    def get_database_credentials(self, role: str) -> dict:
        """Get dynamic database credentials."""
        ...
```

---

## Resources

- [OpenBao Documentation](https://openbao.org/docs/)
- [OpenBao GitHub](https://github.com/openbao/openbao)
- [Nexus Documentation](./index.md)
- [Nexus Authentication](./authentication.md)
