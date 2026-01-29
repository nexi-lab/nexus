# feat: Add OpenBao for secrets management (OAuth tokens, API keys)

## Summary

Add OpenBao (open-source Vault fork) to Nexus for centralized secrets management, replacing the current database-stored encrypted tokens approach.

## Current State

Nexus currently handles secrets as follows:
- **OAuth tokens**: Encrypted with Fernet, stored in PostgreSQL (`OAuthCredentialModel`)
- **Encryption key**: Loaded from `NEXUS_OAUTH_ENCRYPTION_KEY` env var → database → auto-generated
- **API keys**: Environment variables (`NEXUS_API_KEY`)
- **Cloud credentials**: Mounted as files (GCS/AWS)
- **No existing vault integration** (though docs mention it as recommended)

### Key Files
| Path | Purpose |
|------|---------|
| `src/nexus/server/auth/token_manager.py` | OAuth credential management |
| `src/nexus/server/auth/oauth_crypto.py` | Token encryption/decryption (Fernet) |
| `src/nexus/storage/models.py` | Database schema (`OAuthCredentialModel`, `SystemSettingsModel`) |

---

## Proposed Options

### Option 1: OpenBao as KV Secrets Backend (Recommended)

Store OAuth tokens directly in OpenBao's KV secrets engine.

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│   Nexus     │────▶│   OpenBao    │────▶│  KV Store   │
│   Server    │     │   (Docker)   │     │  /secrets/  │
└─────────────┘     └──────────────┘     └─────────────┘
```

**Pros**:
- Centralized secrets management with audit logging
- Native encryption at rest
- Easy key rotation with versioned secrets
- Multi-tenant isolation via path-based policies

**Cons**:
- Requires migration of existing tokens
- OpenBao becomes a critical dependency

---

### Option 2: OpenBao Transit Engine for Encryption Only

Keep tokens in PostgreSQL, use OpenBao Transit for encryption/decryption.

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│   Nexus     │────▶│   OpenBao    │     │  PostgreSQL │
│   Server    │     │   Transit    │     │  (encrypted)│
└─────────────┘     └──────────────┘     └─────────────┘
                          │                     ▲
                          └─────────────────────┘
                            encrypt/decrypt
```

**Pros**:
- Minimal migration - just swap encryption backend
- PostgreSQL remains source of truth
- OpenBao manages keys, rotation, audit
- Simpler to implement

**Cons**:
- Two systems to manage (DB + OpenBao)
- Less comprehensive than full KV integration

---

### Option 3: Full OpenBao Integration (Dynamic Secrets)

Use OpenBao for everything - tokens, API keys, cloud credentials with dynamic secrets.

**Pros**:
- Dynamic cloud credentials (AWS STS, GCP service accounts)
- Complete audit trail
- Automatic credential rotation
- Enterprise-grade security

**Cons**:
- Most complex implementation
- Significant refactoring required
- Overkill for current use case

---

## Recommendation: Option 1

For the primary use case (OAuth refresh tokens, user secrets), **Option 1** provides the best balance of security benefits and implementation complexity.

### Implementation Plan

1. **Add OpenBao Docker service** to `docker-compose.demo.yml`
2. **Create SecretsManager abstraction** with pluggable backends:
   - `DatabaseSecretsBackend` (current behavior, for backward compatibility)
   - `OpenBaoSecretsBackend` (new)
3. **Update TokenManager** to use SecretsManager
4. **Add migration tool** for existing encrypted tokens
5. **Configure via environment**: `NEXUS_SECRETS_BACKEND=openbao|database`

### Files to Create/Modify

| File | Action |
|------|--------|
| `docker-compose.demo.yml` | Add OpenBao service |
| `src/nexus/server/auth/secrets_manager.py` | New - abstraction layer |
| `src/nexus/server/auth/secrets_backends/` | New - backend implementations |
| `src/nexus/server/auth/token_manager.py` | Modify - use SecretsManager |
| `configs/openbao/` | New - OpenBao policies and config |
| `scripts/migrate-secrets.py` | New - migration tool |

---

## Questions for Discussion

1. Should we support both backends simultaneously (database + OpenBao) for gradual migration?
2. What's the preferred authentication method for Nexus → OpenBao? (Token, AppRole, Kubernetes?)
3. Should we use OpenBao namespaces for multi-tenant isolation, or path-based policies?
4. Do we need to support HA OpenBao clusters, or is single-node sufficient for now?

---

## References

- [OpenBao Documentation](https://openbao.org/docs/)
- [OpenBao KV Secrets Engine](https://openbao.org/docs/secrets/kv/kv-v2/)
- [OpenBao Transit Engine](https://openbao.org/docs/secrets/transit/)
