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
