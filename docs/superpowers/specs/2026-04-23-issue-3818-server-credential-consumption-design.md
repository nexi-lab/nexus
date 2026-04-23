# Issue #3818 — Server-side Credential Consumption (Read Path)

**Status:** Draft (brainstorm-approved 2026-04-23)
**Epic:** #3788 (multi-tenant Postgres-backed auth)
**Predecessors:** #3802 (PR 1: schema), #3809 (PR 2: envelope encryption), #3816 (PR 3: nexus-bot daemon write path)
**Successors:** Gmail + gcloud adapters (separate issues), OIDC device-code join, Windows daemon

## Problem

PR 3 (#3816) shipped the daemon write path: local CLI auth (`~/.codex/auth.json`, `gcloud`, `gh`, `gws`) is envelope-encrypted on the laptop and pushed to `auth_profiles`. The server can persist these envelopes but cannot consume them. `/v1/auth/token-exchange` returns HTTP 501. No code path exists to:

1. Authenticate a caller asking for a credential
2. Decrypt the envelope and materialize a provider-native bearer
3. Hand the bearer to the caller
4. Refresh the upstream credential when it nears expiry
5. Record an audit row for the read

Until this lands, the daemon is observable but not useful — server-side workloads cannot act as the user.

## Scope

**In:**
- `/v1/auth/token-exchange` (RFC 8693) — daemon JWT as `subject_token`, returns provider-native bearer
- `CredentialConsumer` in `bricks/auth/consumer.py` — orchestrates decrypt → adapter → cache → audit
- `ResolvedCredCache` — TTL = `min(300s, expires_at - 60s)`
- AWS + GitHub provider adapters (validates two distinct credential shapes: STS short-lived vs PAT long-lived)
- `auth_profile_reads` table mirroring `auth_profile_writes` — 100% on cache-miss, 1% sample on cache-hit
- `decrypt_profile()` helper on `PostgresAuthProfileStore`
- Daemon-driven refresh: server returns 409 `stale_source` when envelope's `last_synced_at` is past TTL; daemon's existing watcher re-pushes
- E2E tests (LocalStack + PAT in PR CI; live AWS + GitHub nightly)

**Deferred (separate issues):**
- Gmail adapter (OAuth refresh via existing `TokenManager`)
- gcloud adapter (token refresh via daemon CLI re-poll)
- Cross-machine sync ("log in on laptop A → authed on laptop B")
- Delegation chains (agent-A asks agent-B to call upstream on user's behalf)
- UI for revocation / audit inspection
- Server-driven OAuth refresh (option B from brainstorm — not needed if A is sufficient)
- Service identities distinct from daemon JWTs (option B from brainstorm Q1)

## Architectural decisions

| # | Decision | Reasoning |
|---|----------|-----------|
| 1 | Caller identity = daemon JWT (`JwtSigner.verify`) | Reuses existing infra. `DaemonClaims` already carries `(tenant_id, principal_id, machine_id)`. No new identity issuer. |
| 2 | Refresh = daemon-driven (push-to-refresh) | Refresh stays on user's actual machine. No long-lived `refresh_token` storage server-side. AWS STS doesn't have refresh_tokens anyway. |
| 3 | Provider scope = AWS + GitHub | Two distinct shapes (STS vs PAT). Smallest cut that exercises full wire. Gmail/gcloud follow-ups become trivial after framework lands. |
| 4 | Decrypted-cred cache TTL = `min(300s, expires_at - 60s)` | Bounds plaintext lifetime by both ceiling and upstream expiry. Matches `DEKCache` ceiling. |
| 5 | Read-audit retention = indefinite + monthly partitions; 100% miss + 1% hit sample | Cache misses are real credential access. Cache hits are operational telemetry. Sampling keeps storage bounded without losing audit story. |
| 6 | E2E infra = LocalStack + PAT in PR CI; nightly live | Same skip-when-env-absent pattern as PR 2's `VaultTransitProvider` / `AwsKmsProvider` tests. |
| 7 | Layout = `bricks/auth/consumer*` (not `server/auth/resolve`) | Reuses `brick_factory.py` wiring for `EncryptionProvider` / `DEKCache` / `PostgresAuthProfileStore`. Keeps `server/auth/` focused on OAuth handshake. |

## Components & file layout

```
src/nexus/bricks/auth/
  consumer.py                       # ResolvedCredential, CredentialConsumer
  consumer_cache.py                 # ResolvedCredCache
  consumer_providers/
    __init__.py
    base.py                         # ProviderAdapter Protocol
    aws.py                          # boto3-shaped payload → bearer
    github.py                       # PAT/fine-grained payload → bearer
  read_audit.py                     # write_read_audit() w/ 1% sampling
  postgres_profile_store.py         # +decrypt_profile() helper, +auth_profile_reads schema
  brick_factory.py                  # +CredentialConsumer wiring

src/nexus/server/api/v1/routers/
  token_exchange.py                 # rewrite: verify JWT → consumer.resolve → audit → respond

src/nexus/bricks/auth/tests/
  test_consumer.py
  test_consumer_cache.py
  test_consumer_providers_aws.py    # LocalStack-gated (LOCALSTACK_ENDPOINT=...)
  test_consumer_providers_github.py # PAT-gated (NEXUS_TEST_GITHUB_PAT=...)
  test_read_audit.py
  test_postgres_decrypt_integration.py  # decrypt_profile() against running PG

src/nexus/server/api/v1/tests/
  test_token_exchange_router.py     # rewrite (was 501-only)

tests/e2e/auth_consumption/
  test_s3_as_user.py                # nightly, live AWS sandbox sub-account
  test_github_as_user.py            # nightly, live GitHub PAT
```

## Schema

One migration in `postgres_profile_store.ensure_schema()` — same idempotent `CREATE TABLE IF NOT EXISTS` pattern used by PR 1/2/3.

```sql
CREATE TABLE IF NOT EXISTS auth_profile_reads (
    id                 BIGSERIAL,
    read_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tenant_id          UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    principal_id       UUID NOT NULL,
    auth_profile_id    TEXT NOT NULL,
    caller_machine_id  UUID NOT NULL,            -- from daemon JWT (attested)
    caller_kind        TEXT NOT NULL,            -- "daemon" for MVP
    provider           TEXT NOT NULL,            -- "aws" | "github"
    purpose            TEXT NOT NULL,            -- RFC 8693 scope, capped 256 chars
    cache_hit          BOOLEAN NOT NULL,
    kek_version        INTEGER NOT NULL,         -- which KEK unwrapped this read
    PRIMARY KEY (read_at, id)
) PARTITION BY RANGE (read_at);

CREATE TABLE IF NOT EXISTS auth_profile_reads_default
    PARTITION OF auth_profile_reads DEFAULT;

ALTER TABLE auth_profile_reads ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth_profile_reads FORCE ROW LEVEL SECURITY;

CREATE POLICY auth_profile_reads_tenant_isolation ON auth_profile_reads
    USING (tenant_id = current_setting('app.current_tenant')::UUID);

CREATE INDEX IF NOT EXISTS idx_auth_profile_reads_tenant_principal_provider
    ON auth_profile_reads(tenant_id, principal_id, provider, read_at DESC);
```

Monthly partition creation is operator/cron concern; default partition catches all writes until a partition manager is wired (same posture as `auth_profile_writes`).

## Wire contract — `POST /v1/auth/token-exchange`

**Request (`application/x-www-form-urlencoded`, RFC 8693 §2.1):**

| Field | Required | Value |
|---|---|---|
| `grant_type` | yes | `urn:ietf:params:oauth:grant-type:token-exchange` |
| `subject_token` | yes | Daemon JWT (ES256, audience matches `JwtSigner` config) |
| `subject_token_type` | yes | `urn:ietf:params:oauth:token-type:jwt` |
| `resource` | yes | `urn:nexus:provider:aws` or `urn:nexus:provider:github` |
| `scope` | yes | Freetext purpose ≤256 chars; truncated, never rejected |
| `audience` | no | Ignored for MVP |

**Custom extension:**

| Field | Value |
|---|---|
| `nexus_force_refresh` | `"true"` to bypass `ResolvedCredCache` (still hits envelope decrypt). Honored only when subject_token came from bound-keypair flow (the only path today). |

**Response 200:**
```json
{
  "access_token": "<provider-native bearer>",
  "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
  "token_type": "Bearer",
  "expires_in": 3287,
  "nexus_credential_metadata": {
    "...provider-specific fields..."
  }
}
```

**Multi-part credentials (AWS):** RFC 8693 mandates a single `access_token` string. AWS needs a 3-tuple (`access_key_id`, `secret_access_key`, `session_token`). Resolution:

- `access_token` carries the `session_token` (the time-bounded part)
- `nexus_credential_metadata` carries `{access_key_id, secret_access_key, region, account_id}` as a sibling object — non-RFC extension, namespaced to avoid collision
- The Nexus client SDK builds `boto3.Session(aws_access_key_id=meta.access_key_id, aws_secret_access_key=meta.secret_access_key, aws_session_token=access_token, region_name=meta.region)` from this pair
- Documented as an explicit RFC 8693 extension; non-Nexus callers asking for AWS get the same response shape but must know to read both fields

For GitHub (single bearer), `nexus_credential_metadata` carries `{scopes_csv, token_type}` — informational only, not required to use the token.

**Errors:**

| HTTP | RFC code (body `error`) | Cause |
|---|---|---|
| 400 | `invalid_request` | Missing/unknown grant_type, missing subject_token, unknown `resource` |
| 401 | `invalid_token` | JWT signature / exp / issuer / audience fail |
| 403 | `access_denied` | Principal has no profile for that provider |
| 409 | `stale_source` | Envelope present but `last_synced_at` past TTL → daemon offline |
| 500 | `envelope_error` | KMS/Vault unreachable, AAD mismatch, ciphertext corrupt — body has no internals |
| 503 | `provider_unreachable` | Reserved; MVP adapters do no upstream call |

Errors follow RFC 8693 §2.2.2 / RFC 6749 §5.2 shape: `{"error": "...", "error_description": "..."}`.

## Public Python surfaces

```python
# bricks/auth/consumer.py
@dataclass(frozen=True)
class ResolvedCredential:
    provider: str
    access_token: str
    expires_at: datetime | None
    metadata: dict[str, str]

class CredentialConsumer:
    def __init__(
        self, *,
        store: PostgresAuthProfileStore,
        encryption: EncryptionProvider,
        dek_cache: DEKCache,
        cred_cache: ResolvedCredCache,
        adapters: dict[str, ProviderAdapter],
        audit: ReadAuditWriter,
    ) -> None: ...

    def resolve(
        self, *,
        claims: DaemonClaims,
        provider: str,
        purpose: str,
        force_refresh: bool = False,
    ) -> ResolvedCredential: ...
```

```python
# bricks/auth/consumer_providers/base.py
class ProviderAdapter(Protocol):
    name: str

    def materialize(self, plaintext_payload: bytes) -> ResolvedCredential:
        """Pure deserialization. No network calls."""
```

**AWS adapter** — payload JSON: `{access_key_id, secret_access_key, session_token, expiration, region, account_id?}`. Returns `ResolvedCredential(access_token=session_token, expires_at=expiration, metadata={region, account_id, access_key_id})`. Caller side is responsible for assembling `boto3.Session(aws_access_key_id=..., aws_secret_access_key=..., aws_session_token=session_token)`.

**GitHub adapter** — payload JSON: `{token, scopes, expires_at?, token_type?}`. Returns `ResolvedCredential(access_token=token, expires_at=expires_at_or_none, metadata={scopes_csv, token_type})`.

## Data flow

### Write path (existing, for context)
```
daemon → POST /v1/auth-profiles → PostgresAuthProfileStore.upsert() → auth_profile_writes
```

### Read path (this issue)
```
caller
  ↓ POST /v1/auth/token-exchange (subject_token=daemon JWT, resource, scope)
router (token_exchange.py)
  ↓ JwtSigner.verify(subject_token) → DaemonClaims
  ↓ SET LOCAL app.current_tenant = claims.tenant_id
consumer.resolve(claims, provider, purpose, force_refresh)
  ↓ key = (claims.tenant_id, claims.principal_id, provider)
  ↓ cred_cache.get(key)
    → hit + (now < expires_at - 60): return cached
  ↓ store.decrypt_profile(claims, provider)
    → row = SELECT ... WHERE (tenant_id, principal_id, provider) = ...
    → if row.last_synced_at < (now - sync_ttl_seconds): raise StaleSource
    → wrapped_dek_key = DEKCache.make_key(...)
    → dek = dek_cache.get(...) or encryption.unwrap_dek(...)
    → plaintext = AESGCMEnvelope().decrypt(dek, nonce, ciphertext, aad=row.aad)
    → return (plaintext, row.kek_version, row.profile_id)
  ↓ adapter = adapters[provider]
  ↓ resolved = adapter.materialize(plaintext)
  ↓ ttl = min(300, (resolved.expires_at - now).total_seconds() - 60) if expires_at else 300
  ↓ cred_cache.put(key, resolved, ttl)
router
  ↓ audit.write(claims, profile_id, provider, purpose, cache_hit=False, kek_version)
  ↓ return 200 {access_token, ...}

stale upstream cred (caller's downstream call returns 401):
  ↓ caller retries token-exchange with nexus_force_refresh=true
  ↓ consumer skips cred_cache, re-runs decrypt + materialize
  ↓ if last_synced_at also stale → 409 stale_source → daemon catches up via fsnotify
```

## Error taxonomy

```
ConsumerError
  ├─ ProfileNotFound          → 403 access_denied
  ├─ ProviderNotConfigured    → 403 access_denied
  ├─ StaleSource              → 409 stale_source
  └─ AdapterMaterializeFailed → 500 envelope_error

EnvelopeError (from PR 2, unchanged)
  ├─ AADMismatch          → 500 envelope_error
  ├─ WrappedDEKInvalid    → 500 envelope_error
  └─ CiphertextCorrupted  → 500 envelope_error

JwtVerifyError (from PR 3, unchanged) → 401 invalid_token
```

All `ConsumerError` subclasses implement `from_row(*, tenant_id, principal_id, provider, cause)` classmethod — same no-plaintext-in-repr discipline as `EnvelopeError`. Inline f-strings forbidden.

## Security properties

- **JWT verify before any DB hit.** Invalid token → 401, no `app.current_tenant` set, no audit row written, no cache lookup.
- **Tenant isolation via RLS.** `SET LOCAL app.current_tenant = claims.tenant_id` before every consumer query. Tests use `NOSUPERUSER NOBYPASSRLS` role to prove isolation (same posture as PR 3).
- **Cache key includes tenant_id.** Prevents cross-tenant cache poisoning even if a future bug forgets RLS.
- **No plaintext in repr/logs.** Adapters / consumer errors use `from_row` constructors. `ResolvedCredential.__repr__` masks `access_token` (`"***"`).
- **Plaintext lifetime ≤ min(300s, expires_at-60s).** Document `mlockall(MCL_FUTURE)` recommendation in deployment guide for production.
- **`nexus_force_refresh` honored only for bound-keypair JWTs** — currently the only path; future service-identity JWTs (Q1 option B) would need explicit opt-in.
- **Audit `purpose` is freetext.** Deployment guide MUST warn callers to never include credentials/PII. Truncated to 256 chars (not rejected — fail-open on length).
- **Replay surface unchanged.** Subject_token = daemon JWT with its own 1h `exp`. Token-exchange does not introduce new replay vectors.

## Observability

Prometheus metrics (low-cardinality labels only):

| Metric | Labels | Purpose |
|---|---|---|
| `nexus_token_exchange_requests_total` | `provider, result` | Result ∈ `ok\|stale\|denied\|invalid_token\|envelope_error` |
| `nexus_token_exchange_latency_seconds` | `provider, cache` | Cache ∈ `hit\|miss` |
| `nexus_consumer_cache_size` | (none) | Gauge of resolved-cred cache size |
| `nexus_consumer_cache_evictions_total` | `reason` | Reason ∈ `ttl\|lru\|expires_at` |
| `nexus_read_audit_writes_total` | `cache` | Confirms 1% sampling actually samples |

Existing `nexus_dek_cache_*` and `nexus_envelope_unwrap_*` from PR 2 cover envelope path; no duplication.

Tracing: one span per `/v1/auth/token-exchange`; child spans `verify_jwt`, `cache_lookup`, `decrypt_profile`, `adapter_materialize`, `audit_write`. Span attrs: `tenant_id`, `provider`, `cache_hit`, `kek_version`. No token bytes.

## Test plan

| File | Type | What it proves |
|---|---|---|
| `test_consumer.py` | unit | resolve happy / force_refresh / not-found / stale / adapter raise / AAD mismatch propagation |
| `test_consumer_cache.py` | unit | TTL cap, expires_at cap, LRU eviction, thread-safety |
| `test_read_audit.py` | unit | 100% on miss, 1% sample on hit (seeded RNG), partition routing |
| `test_postgres_decrypt_integration.py` | integration (PG) | `decrypt_profile()` honors RLS, returns plaintext + kek_version |
| `test_consumer_providers_aws.py` | unit (LocalStack-gated) | AWS payload JSON → ResolvedCredential; metadata fields populated |
| `test_consumer_providers_github.py` | unit (PAT-gated) | GitHub payload JSON → ResolvedCredential; expires_at None vs date |
| `test_token_exchange_router.py` | router | All HTTP error mappings; RLS-set-tenant assertion; force_refresh path |
| `tests/e2e/auth_consumption/test_s3_as_user.py` | e2e (nightly, live AWS) | daemon push → token-exchange → real `s3.list_buckets()` returns ≥1 bucket |
| `tests/e2e/auth_consumption/test_github_as_user.py` | e2e (nightly, live GitHub) | daemon push → token-exchange → real `octokit /user` returns expected login |

E2E gating: skip cleanly when env absent, run when `LOCALSTACK_ENDPOINT` / `NEXUS_TEST_GITHUB_PAT` / `NEXUS_TEST_AWS_LIVE=1` set.

## Migration / rollout

1. Schema migration is additive — `auth_profile_reads` is a new table; `decrypt_profile()` reads existing envelope columns
2. `/v1/auth/token-exchange` flips from 501 to live; existing `enabled` flag (`NEXUS_TOKEN_EXCHANGE_ENABLED`) gates the new behavior. Default off until ops verifies KMS/Vault wiring in their environment.
3. No daemon changes required — daemon already pushes the envelope shape this consumer expects.
4. Provider adapters are loaded lazily via `consumer_providers/__init__.py` registry; missing provider name = `ProviderNotConfigured` (403), not import error.

## Out-of-scope (explicit, with justification)

- **Server-driven OAuth refresh** — option B from Q2 brainstorm. Adds `refresh_token` storage burden and per-provider refresh code. Q2 picked A; revisit if daemon offline becomes a bottleneck.
- **Service-identity JWTs** — option B from Q1. Daemon JWT is sufficient for MVP. Add `caller_kind="service"` to audit when this lands.
- **Gmail / gcloud adapters** — separate issues. Gmail needs OAuth refresh path; gcloud needs gcloud CLI shape decoded. Framework supports both via `ProviderAdapter` Protocol.
- **Delegation chains** — agent A asks agent B to call upstream as user. RFC 8693 supports `actor_token`; defer until a real use case exists.
- **UI for revocation / audit inspection** — raw SQL is the MVP interface.

## SOC 2 mapping

- **CC6.1** logical access → tenant RLS + principal scoping on every read
- **CC6.7** transmission → TLS termination upstream of router; envelope still encrypted in DB at rest
- **CC7.2** monitoring → `auth_profile_reads` row per credential access (with caller machine_id attested by Ed25519 keypair from PR 3)
- **CC6.8** malicious software → plaintext capped at `min(300s, expires_at-60s)` in process; no plaintext on disk; `force_refresh` requires bound-keypair JWT

## References

- Epic: #3788
- Predecessor: #3816 (daemon write path)
- Stub being replaced: `src/nexus/server/api/v1/routers/token_exchange.py`
- RFC 8693: https://datatracker.ietf.org/doc/html/rfc8693
- RFC 6749 §5.2 (error response shape): https://datatracker.ietf.org/doc/html/rfc6749#section-5.2
- PR 2 envelope spec: `docs/superpowers/specs/2026-04-18-issue-3803-envelope-encryption-design.md`
- PR 3 daemon spec: `docs/superpowers/specs/2026-04-19-nexus-bot-daemon-design.md`
