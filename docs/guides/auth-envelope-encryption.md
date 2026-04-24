# Envelope encryption for auth profiles

`PostgresAuthProfileStore` supports storing resolved credentials encrypted alongside their routing metadata. This guide covers picking and configuring an `EncryptionProvider` for your deployment. Issue: #3803. Spec: `docs/superpowers/specs/2026-04-18-issue-3803-envelope-encryption-design.md`.

## Pick a provider

| Deployment shape | Provider | Rationale |
|---|---|---|
| Vault-native shop | `VaultTransitProvider` | Free self-hosted transit engine; per-tenant scoping via `context` parameter with `derived=true`. |
| AWS-native shop | `AwsKmsProvider` | Managed CMK; AWS-managed rotation; IAM-scoped access; per-tenant CMK keeps blast radius per-tenant. |
| Development only | `InMemoryEncryptionProvider` | Keys live in process memory — never use in production. |

## Vault Transit setup

1. Enable the transit mount: `vault secrets enable transit`
2. Create a derived-context key: `vault write -f transit/keys/nexus derived=true`
3. Grant the Nexus role the `encrypt` and `decrypt` policies on `transit/*/nexus`.
4. Construct the provider: `VaultTransitProvider(hvac.Client(...), key_name="nexus")`.

Rotation:

1. `vault write -f transit/keys/nexus/rotate` — bumps the key version.
2. `nexus auth rotate-kek --tenant acme --apply` — per tenant, sweeps rows at the old version.

## AWS KMS setup

1. Create a CMK per tenant (customer-managed key). Enable automatic key rotation: `aws kms enable-key-rotation --key-id <id>`.
2. Grant the Nexus IAM principal `kms:Encrypt`, `kms:Decrypt`, `kms:DescribeKey` on the CMK.
3. Constrain via `kms:EncryptionContext:tenant_id` in the key policy to match the tenant value the provider sends — prevents cross-tenant decrypt even if IAM is too broad.
4. Construct the provider: `AwsKmsProvider(boto3.client("kms"), key_id="arn:aws:kms:...")`.

Rotation: AWS rotates the underlying key material annually with `EnableKeyRotation`. Our `kek_version` in that case tracks provider-config changes (e.g. swapping the CMK alias) — `nexus auth rotate-kek` is a no-op as long as `config_version` is unchanged.

## Rotation CLI

```
nexus auth rotate-kek --db-url <url> (--tenant <name>|--tenant-id <uuid>) \
  --provider (vault|aws-kms) [provider options] \
  [--apply] [--allow-failures] [--batch-size 100] [--max-rows N]
```

Dry-run by default: reports how many rows are stuck at old `kek_version`. `--apply` rewraps them in `SKIP LOCKED` batches — resumable, doesn't block concurrent writers. Per-row unwrap failures continue the batch; the command exits non-zero when any row fails unless `--allow-failures` is set. Wrap failures at the new version abort the batch (zero rows mutated, non-zero exit).

**Tenant identifier:** pass `--tenant-id <uuid>` under least-privilege DB roles. `--tenant <name>` requires read access to `tenants.name`, which FORCE RLS blocks for non-BYPASSRLS roles.

**Schema preflight:** rotation is read-only for DDL. Run `ensure_schema()` once at deploy time under a role with ALTER privileges; rotation then works under the least-privilege role.

## Metrics

Provider-level metrics exposed under `/metrics`:

- `auth_dek_cache_hits_total{tenant_id}`
- `auth_dek_cache_misses_total{tenant_id}`
- `auth_dek_unwrap_errors_total{tenant_id,error_class}`
- `auth_dek_unwrap_latency_seconds{tenant_id}`
- `auth_kek_rotate_rows_total{tenant_id,from_version,to_version}`

Low-cardinality by design: `principal_id` and `profile_id` are deliberately not labels.

## Threat model coverage

- **Ciphertext swap across tenants/principals** — AAD binds `tenant_id|principal_id|profile_id`; decrypt on a row moved to a different tenant raises `AADMismatch` (stored AAD column mismatch) or `WrappedDEKInvalid` (provider derivation/encryption-context mismatch), depending on where the attacker copies from.
- **`kek_version` downgrade** — each provider binds `kek_version` into the wrap path (Vault Transit: `key_version` on decrypt; AWS KMS: version embedded in the opaque blob; `InMemoryEncryptionProvider`: mixed into the derivation AAD). Claiming a wrapped DEK was produced at a different version fails.
- **Plaintext in logs / errors** — every `EnvelopeError` subclass has a `__repr__` that carries only `(tenant_id, profile_id, kek_version, cause)`; a test regex asserts no 16+ byte base64/hex blob appears in error text.
- **Transient KMS/Vault errors** — the DEK cache never caches negative results, so a temporary IAM blip does not pin decrypt-failed for the TTL window.

## Token exchange (server-side credential consumption)

The `/v1/auth/token-exchange` endpoint exposes envelope-stored credentials to authenticated daemons via RFC 8693 token exchange. Issue: #3818. Spec: `docs/superpowers/specs/2026-04-23-issue-3818-server-credential-consumption-design.md`.

### Environment variables

- `NEXUS_TOKEN_EXCHANGE_ENABLED` — default `0` (off). Set to `1`, `true`, or `yes` to mount the read path.
- `NEXUS_JWT_SIGNING_KEY` — filesystem path to the ES256 private key PEM used to validate daemon subject tokens (prereq from PR #3816).
- `NEXUS_ENROLL_TOKEN_SECRET` — HMAC secret for daemon enrollment (prereq from PR #3816).

Wire a production-grade `EncryptionProvider` on the FastAPI app state before `create_app` returns:

```python
# In your startup hook (before create_app returns):
from nexus.bricks.auth.envelope_providers.aws_kms import AwsKmsProvider
app.state.encryption_provider = AwsKmsProvider(key_id="arn:aws:kms:...")
```

**CRITICAL WARNING — InMemoryEncryptionProvider footgun.** Enabling `NEXUS_TOKEN_EXCHANGE_ENABLED=1` WITHOUT wiring `app.state.encryption_provider` falls back to `InMemoryEncryptionProvider`, which generates a fresh random KEK per process. This means:

- In multi-worker deployments (e.g. `uvicorn --workers 4`), each worker has a different KEK — daemon-written envelopes will fail `WrappedDEKInvalid` on any other worker.
- On server restart, every existing envelope row becomes unreadable because the KEK is lost.

For production use you MUST wire Vault Transit or AWS KMS via `app.state.encryption_provider`. Single-worker dev/test deployments can use the default.

### What it does

Implements RFC 8693 token exchange on a single endpoint. The router accepts a daemon JWT as the `subject_token`, validates it against `NEXUS_JWT_SIGNING_KEY`, reads the envelope row for `(tenant_id, principal_id, provider)`, unwraps the DEK via the configured `EncryptionProvider`, decrypts the profile plaintext with AES-GCM (AAD bound to tenant/principal/profile), and returns the provider-native bearer:

- `github`: `access_token` is the PAT.
- `aws`: multi-part credentials — `access_token` holds the `session_token`; a sibling `nexus_credential_metadata` field carries `access_key_id`, `secret_access_key`, `expiration`, and (if present) `region`.

See the spec file for wire-format examples and the full state machine.

### Audit / SOC 2

Every resolve writes to the `auth_profile_reads` table — 100% on cache-miss, 1% sampled on cache-hit. The table is partitioned monthly (default partition `auth_profile_reads_default` holds everything until ops provisions per-month partitions).

Sample query for "who read credential X when":

```sql
SET LOCAL app.current_tenant = '<tenant-uuid>';
SELECT read_at, principal_id, provider, caller_machine_id, caller_kind, purpose, cache_hit
FROM auth_profile_reads
WHERE auth_profile_id = 'github-default'
ORDER BY read_at DESC
LIMIT 20;
```

The `purpose` column is capped at 256 characters. Deployment guide warns callers MUST NOT include credentials or PII in the scope string (see next section).

### `purpose` field discipline

Callers pass the RFC 8693 `scope` field on the exchange request; the router stores it verbatim in `auth_profile_reads.purpose`. This is freetext and will be retained indefinitely, so callers MUST NOT pass credentials, PII, or other sensitive data. The router truncates at 256 characters but does not otherwise sanitize.

Use descriptive, low-cardinality purpose strings:

- Good: `"list-repos"`, `"s3-backup-nightly"`, `"ci-fetch-artifacts"`.
- Bad: `"user token abc123"`, `"retry for user@example.com"`, anything containing a secret.

### Cache architecture

Two in-process, per-worker caches front the envelope read path:

- `DEKCache` — caches unwrapped DEKs with a 5-minute TTL (default 300s). Keyed on `(tenant_id, principal_id, profile_id, kek_version)`.
- `ResolvedCredCache` — caches the final materialized credential with TTL = `min(300s, expires_at - 60s)`. Short-lived AWS STS tokens cap their own TTL; static GitHub PATs use the 300s ceiling.

Multi-worker deployments have independent caches per worker — cache-hit ratio degrades with worker count but correctness is unaffected. In production, consider `mlockall(MCL_FUTURE)` at process startup to prevent swap-to-disk of plaintext credentials sitting in the caches.

### Provider scope

Currently wired adapters:

- `aws` — STS payload → `MaterializedCredential` with multi-part metadata.
- `github` — PAT payload → `MaterializedCredential`.

`gmail` and `gcloud` adapters are planned follow-ups (tracked under separate issues). The `ProviderAdapter` Protocol at `nexus.bricks.auth.consumer_providers.base.ProviderAdapter` is open for extension — new providers drop in by implementing the Protocol and registering with the router.

### Failure modes and remediation

| HTTP status | `error` | Meaning | Remediation |
|---|---|---|---|
| 401 | `invalid_token` | Daemon JWT signature, expiry, or issuer check failed | Daemon should refresh its token; verify signer key rotation on the daemon side matches the server's `NEXUS_JWT_SIGNING_KEY`. |
| 403 | `access_denied` | No envelope row for `(tenant_id, principal_id, provider)` | Verify the daemon has pushed that provider for that principal; inspect `auth_profiles` directly. |
| 409 | `stale_source` | Envelope row present but `last_synced_at + sync_ttl_seconds` is in the past | Daemon is offline or failing — check daemon logs. Self-heals on the next successful push. |
| 500 | `envelope_error` | KMS unreachable, AAD tamper, or AES-GCM tag failure | Check server logs — `repr()` masks plaintext; inspect the `envelope_error` cause field to distinguish KMS-transient vs. tamper. |
