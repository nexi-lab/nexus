# OpenBao Quick Start Guide

**For full design details, see**: [openbao-secrets-management.md](./openbao-secrets-management.md)

---

## What is This?

This is a quick reference for getting started with OpenBao secrets management in Nexus. OpenBao centralizes all secrets (database credentials, OAuth tokens, API keys) in one secure vault.

---

## Quick Start (Development)

### 1. Start OpenBao

```bash
docker run -d --name openbao -p 8200:8200 \
  -e OPENBAO_DEV_ROOT_TOKEN_ID=dev-root-token \
  ghcr.io/openbao/openbao:2.1.0 server -dev
```

### 2. Populate Secrets

```bash
export OPENBAO_ADDR=http://localhost:8200
export OPENBAO_TOKEN=dev-root-token

# PostgreSQL credentials
openbao kv put secret/nexus/infrastructure/database/postgres-main \
  user="postgres" \
  password="nexus" \
  host="localhost" \
  port="5432" \
  database="nexus"

# OAuth encryption key
OAUTH_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
openbao kv put secret/nexus/infrastructure/encryption/oauth-key \
  key="$OAUTH_KEY"

# Google OAuth
openbao kv put secret/nexus/oauth-providers/google/credentials \
  client_id="your-client-id" \
  client_secret="your-client-secret" \
  redirect_uri="http://localhost:2026/oauth/google/callback"

# Anthropic API key
openbao kv put secret/nexus/external-services/llm/anthropic \
  api_key="sk-ant-..."
```

### 3. Configure Nexus

```bash
export NEXUS_SECRETS_BACKEND=openbao
export OPENBAO_ADDR=http://localhost:8200
export OPENBAO_TOKEN=dev-root-token

# Run Nexus
python -m nexus.server.rpc_server
```

---

## Secret Organization

```
secret/
└── nexus/
    ├── infrastructure/          # Database, encryption keys
    ├── oauth-providers/         # Google, Microsoft, Slack, X
    ├── external-services/       # LLM APIs, tools, observability
    ├── cloud-storage/           # GCS, S3 credentials
    └── users/{tenant}/oauth/    # Per-user OAuth tokens
```

---

## Common Operations

### Read a Secret
```bash
openbao kv get secret/nexus/infrastructure/database/postgres-main
```

### Update a Secret
```bash
openbao kv put secret/nexus/infrastructure/database/postgres-main \
  password="new-password"
```

### List Secrets
```bash
openbao kv list secret/nexus/infrastructure/
```

### Delete a Secret
```bash
openbao kv delete secret/nexus/infrastructure/database/postgres-main
```

---

## Migration Phases

### ✅ Phase 1: Infrastructure Secrets (Week 1-2)
- PostgreSQL credentials
- OAuth encryption key
- OAuth provider credentials
- Admin API key

### ⏳ Phase 2: External Service API Keys (Week 3)
- LLM API keys (Anthropic, OpenAI, OpenRouter)
- Tool API keys (E2B, Tavily, Firecrawl)

### ⏳ Phase 3: User OAuth Tokens (Week 4-6)
- Dual-write pattern (write to both DB and OpenBao)
- Read-through pattern (read from OpenBao, fallback to DB)
- Background migration job

### ⏳ Phase 4: Cleanup (Week 7-8)
- Remove database fallback
- Archive old credentials
- Update documentation

---

## Troubleshooting

### OpenBao Not Reachable
```bash
# Check OpenBao health
curl http://localhost:8200/v1/sys/health

# Check OpenBao logs
docker logs openbao
```

### Secrets Not Loading
```bash
# Verify environment variables
echo $NEXUS_SECRETS_BACKEND
echo $OPENBAO_ADDR
echo $OPENBAO_TOKEN

# Test secret read
openbao kv get secret/nexus/infrastructure/database/postgres-main
```

### Fallback to Database
```bash
# Temporarily use database backend
export NEXUS_SECRETS_BACKEND=database
export NEXUS_DATABASE_URL=postgresql://postgres:nexus@localhost:5432/nexus
```

---

## Production Setup

See [openbao-secrets-management.md - Production Setup](./openbao-secrets-management.md#production-setup) for:
- OpenBao initialization and unsealing
- Policy creation (bootstrap, runtime, admin)
- AppRole authentication
- TLS configuration
- High availability setup

---

## Resources

- **Full Design**: [openbao-secrets-management.md](./openbao-secrets-management.md)
- **OpenBao Docs**: https://openbao.org/docs/
- **Branch**: `feat/openbao-secrets-service`
