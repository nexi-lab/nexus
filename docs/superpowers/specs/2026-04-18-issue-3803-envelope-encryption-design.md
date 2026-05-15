# Envelope encryption layer for `PostgresAuthProfileStore` (issue #3803)

Phase C of epic #3788. PR 2 of 3. Lands the crypto rails on top of the store shipped in #3802.

## Scope

Add envelope encryption to `PostgresAuthProfileStore` so rows can carry resolved credentials alongside the routing metadata they already carry. `AuthProfile` itself stays routing-metadata-only (SQLite / in-memory stores unchanged). Ciphertext storage is opt-in per call via a new sub-protocol; Postgres rows written by PR 1 remain readable with zero migration.

Out of scope (explicit):

- Daemon-side client encryption — Phase D (PR 3/3) moves encryption to a client that pushes over the wire. This PR does the server-side envelope only.
- HSM integration — overkill at current scale.
- Custom AES in-process — use Vault Transit or AWS KMS as the KEK.
- Feature-flag wiring / consumer code paths — no caller yet. Phase D wires the first one.

## Architecture

Two new surfaces; everything else is isolated to the `auth` brick.

### `EncryptionProvider` trait

```python
class EncryptionProvider(Protocol):
    def current_version(self, *, tenant_id: uuid.UUID) -> int: ...
    def wrap_dek(self, dek: bytes, *, tenant_id: uuid.UUID, aad: bytes) -> tuple[bytes, int]:
        """Return (wrapped_bytes, kek_version). wrap always uses the provider's current version."""
        ...
    def unwrap_dek(self, wrapped: bytes, *, tenant_id: uuid.UUID, aad: bytes, kek_version: int) -> bytes: ...
```

`wrap_dek` always uses the provider's current version and returns it alongside the wrapped bytes — matches how Vault Transit (`encrypt_data` always picks the latest key version unless explicitly overridden) and AWS KMS (`Encrypt` always uses the primary version of the CMK) behave natively. `unwrap_dek` takes the stored `kek_version` because rows persist history — Vault Transit takes `key_version` on decrypt; AWS KMS decrypt ignores it (the ciphertext blob encodes the key version internally) so our `AwsKmsProvider` uses `kek_version` for tracking only.

`tenant_id` is a first-class param on every op so per-tenant key derivation (Vault Transit `derived=true` context, AWS KMS `EncryptionContext`) threads through from day 1. A single-KEK provider simply ignores the arg.

`aad` is passed through to the provider where supported (AWS KMS `EncryptionContext`) so AAD tamper is caught at KMS, not just locally. Providers that don't surface AAD at the wrap level (Vault Transit has only `context`) still bind AAD at the AESGCM layer — the belt survives with or without the suspenders.

### `CredentialCarryingProfileStore` sub-protocol

```python
class CredentialCarryingProfileStore(AuthProfileStore, Protocol):
    def upsert_with_credential(self, profile: AuthProfile, credential: ResolvedCredential) -> None: ...
    def get_with_credential(self, profile_id: str) -> tuple[AuthProfile, ResolvedCredential | None]: ...
```

Only `PostgresAuthProfileStore` implements it. Callers that don't need ciphertext keep the base Protocol. `get_with_credential` returns `None` for the credential when a row was written via plain `upsert` (PR 1 shape, NULL ciphertext columns) — so the sub-protocol is read-compatible with the existing contract.

### Trust boundary

Plaintext exists only between `ResolvedCredential → AESGCM.encrypt()` and `AESGCM.decrypt() → ResolvedCredential`. Never persisted; never logged. `DEKCache` holds unwrapped DEKs in memory for up to 5 min, keyed by `(tenant_id, kek_version, sha256(wrapped_dek))` — the wrapped-DEK hash, not the DEK itself, so debug logging of the cache key is safe.

## Components

```
src/nexus/bricks/auth/
├── envelope.py                       # Provider trait, AESGCMEnvelope, DEKCache, EnvelopeError hierarchy
├── envelope_providers/
│   ├── __init__.py
│   ├── in_memory.py                 # InMemoryEncryptionProvider (test fake + default)
│   ├── vault_transit.py             # VaultTransitProvider (lazy hvac)
│   └── aws_kms.py                   # AwsKmsProvider (lazy boto3)
├── envelope_metrics.py               # Prometheus counters / histograms
├── postgres_profile_store.py        # extended: new columns + encrypted upsert/get + rotate batch
├── profile.py                        # CredentialCarryingProfileStore added
├── cli_commands.py                   # rotate-kek subcommand added
└── tests/
    ├── test_envelope.py
    ├── test_envelope_contract.py
    ├── test_envelope_providers_vault.py         @pytest.mark.vault
    ├── test_envelope_providers_aws_kms.py       @pytest.mark.kms
    ├── test_postgres_envelope_integration.py    @pytest.mark.postgres
    └── test_rotate_kek_cli.py                   @pytest.mark.postgres
```

### `envelope.py`

- `AESGCMEnvelope` — thin wrapper over `cryptography.hazmat.primitives.ciphers.aead.AESGCM`. `encrypt(plaintext, *, aad) -> (nonce, ciphertext)` generates a fresh 12-byte nonce per call; `decrypt(nonce, ciphertext, *, aad) -> plaintext` verifies the GCM tag.
- `DEKCache` — `TTL=300s`, `max=1024` entries, LRU on size eviction. Key: `(tenant_id, kek_version, sha256(wrapped_dek))`. Cache never holds negative results (failed unwraps are not cached).
- `EnvelopeError` hierarchy (`EnvelopeConfigurationError`, `DecryptionFailed`, `AADMismatch`, `WrappedDEKInvalid`, `CiphertextCorrupted`).

### `envelope_providers/in_memory.py`

`InMemoryEncryptionProvider` — test fake with real AEAD. Holds `dict[(tenant_id, kek_version), 32-byte KEK]`; wrap/unwrap uses `AESGCM` with `tenant_id|kek_version` as AAD so wrapped bytes are real ciphertext (not identity). Exposes `.wrap_count` / `.unwrap_count` for cache-amortization assertions. Supports `rotate(new_version)` so rotation tests don't need a live KMS.

### `envelope_providers/vault_transit.py`

`VaultTransitProvider(vault_client, key_name, *, mount_point="transit")`. Caller injects `hvac.Client`; `import hvac` is lazy (module-level) with a clear `ImportError` pointing at the extras group. On construction, calls `transit.read_key()` once and raises `EnvelopeConfigurationError` if `derived is not True` — fail loud at bootstrap, not on first encrypt. Uses `transit.encrypt_data(name=key_name, plaintext=b64(dek), context=b64(str(tenant_id)))` / `transit.decrypt_data(...)`. `current_version` reads `latest_version` from the key config.

### `envelope_providers/aws_kms.py`

`AwsKmsProvider(kms_client, key_id)`. Caller injects `boto3.client("kms")`; `import boto3.client` is lazy. On construction, calls `describe_key()` — missing IAM raises `EnvelopeConfigurationError` with a remediation string listing required actions (`kms:Encrypt`, `kms:Decrypt`, `kms:DescribeKey`). Uses `encrypt(KeyId=key_id, Plaintext=dek, EncryptionContext={"tenant_id": str(tenant_id), "aad_fingerprint": sha256_hex(aad)})` / `decrypt(...)`. AAD fingerprint in encryption context pins row identity at the KMS level — cross-row DEK swap fails at KMS, not just locally.

### `envelope_metrics.py`

Prometheus primitives. Low-cardinality labels only (no principal_id, no profile_id):

- `DEK_CACHE_HITS = Counter(..., labelnames=["tenant_id"])`
- `DEK_CACHE_MISSES`
- `DEK_UNWRAP_ERRORS`
- `DEK_UNWRAP_LATENCY = Histogram(...)`
- `KEK_ROTATE_ROWS = Counter(..., labelnames=["tenant_id", "from_version", "to_version"])`

### `postgres_profile_store.py` extensions

Five nullable columns added to `_TABLE_STATEMENTS`; mirrored in `_upgrade_shape_in_place` via `ADD COLUMN IF NOT EXISTS` inside the existing `pg_advisory_xact_lock` region. `CHECK` constraint enforces all-five-set or all-five-null. New methods:

- `upsert_with_credential(profile, credential)` — reuses the existing `(tenant_id, profile_id)` advisory lock; single `INSERT ... ON CONFLICT` with the 5 extra columns inlined into `_UPSERT_SQL`.
- `get_with_credential(profile_id)` — SELECT, short-circuit to `(profile, None)` when `ciphertext IS NULL`, else cache lookup → unwrap → AESGCM decrypt.
- `upsert_with_credential` / `get_with_credential` require an `encryption_provider=` kwarg at store construction; absent it, they raise `RuntimeError("encryption_provider required")` — no silent fallback to plaintext.

**Rotation lives outside the per-(tenant, principal) store**, as a module-level admin helper:

```python
def rotate_kek_for_tenant(
    engine: Engine,
    *,
    tenant_id: uuid.UUID,
    encryption_provider: EncryptionProvider,
    batch_size: int = 100,
    max_rows: int | None = None,
) -> RotationReport: ...
```

Operates on every row in the tenant regardless of principal (matches `ensure_tenant` / `ensure_principal` pattern from #3802). Sets `app.current_tenant` for RLS, then `SELECT ... FOR UPDATE SKIP LOCKED LIMIT :batch_size` loops. No principal scoping. The provider's `current_version` is consulted once at entry — if every row is already at that version, the helper returns early with zero work.

### `cli_commands.py`

```
nexus auth rotate-kek --tenant NAME [--apply] [--batch-size 100] [--max-rows N]
```

Dry-run by default; `--apply` writes. Matches `nexus auth migrate-to-postgres` pattern from PR 1. Calls `rotate_kek_for_tenant`, which loops internally over batches. "Target version" is implicit — the provider's current version at call time. Operator promotes the provider version out-of-band (Vault: rotate the transit key; AWS KMS: no-op, managed rotation); the CLI then sweeps rows at old versions.

## Data flow + schema

### Schema delta (`auth_profiles`)

```sql
ciphertext   BYTEA,
wrapped_dek  BYTEA,
nonce        BYTEA,
aad          BYTEA,
kek_version  INTEGER,

CONSTRAINT auth_profiles_envelope_all_or_none CHECK (
  (ciphertext IS NULL) = (wrapped_dek IS NULL) AND
  (ciphertext IS NULL) = (nonce IS NULL) AND
  (ciphertext IS NULL) = (aad IS NULL) AND
  (ciphertext IS NULL) = (kek_version IS NULL)
)
```

`aad` is stored — decrypt is self-contained from the row, which matters for `rotate_kek_for_tenant` (rewraps DEK without knowing the caller's current `(tenant_id, principal_id)`). Store additionally verifies at read that stored `aad == f"{tenant_id}|{principal_id}|{profile_id}".encode()` and raises `AADMismatch` on drift.

### AAD composition

`f"{tenant_id}|{principal_id}|{profile_id}".encode("utf-8")`.

Matches the composite PK exactly. Any PK-component tamper fails decrypt. Provider column omitted — it's redundant with `profile_id` (ids like `"google/alice"` already encode provider) and reduces migration surface if a provider is ever renamed.

### Payload serialization

`ResolvedCredential` → JSON with `sort_keys=True`, `separators=(",", ":")`, UTF-8. Deterministic so rotation re-encrypts byte-identical plaintext — easy to assert no semantic drift in tests.

### Write path (`upsert_with_credential`)

1. `_scoped()` transaction (sets `app.current_tenant`).
2. `SELECT pg_advisory_xact_lock(hashtextextended(tenant_id/profile_id))` — same lock as plain `upsert` to serialize against concurrent mutators.
3. `dek = secrets.token_bytes(32)`; `nonce = secrets.token_bytes(12)`; `aad = f"{tenant_id}|{principal_id}|{profile_id}".encode()`.
4. `ciphertext = AESGCM(dek).encrypt(nonce, json_bytes, aad)`.
5. `(wrapped_dek, kek_version) = provider.wrap_dek(dek, tenant_id=tenant_id, aad=aad)`.
6. Single `INSERT ... ON CONFLICT (tenant_id, principal_id, id) DO UPDATE` with routing columns + 5 encryption columns.

### Read path (`get_with_credential`)

1. `_scoped()` `SELECT *`.
2. If `ciphertext IS NULL` → return `(profile, None)`.
3. Verify `row.aad == expected_aad`; raise `AADMismatch` on drift.
4. `DEKCache.get((tenant_id, kek_version, sha256(wrapped_dek)))`; on miss, `provider.unwrap_dek(...)` → record miss + latency → cache result.
5. `plaintext = AESGCM(dek).decrypt(nonce, ciphertext, aad)`; `json.loads` → `ResolvedCredential`.
6. Return `(profile, credential)`.

### Rotation path (`rotate_kek_for_tenant`)

1. Read `target_version = provider.current_version(tenant_id=tenant_id)` once at entry. Every batch rewraps rows whose `kek_version < target_version`.
2. Per batch: open tx, `SET LOCAL app.current_tenant = :tid`, `SELECT tenant_id, principal_id, id, wrapped_dek, nonce, aad, kek_version FROM auth_profiles WHERE tenant_id = :tid AND ciphertext IS NOT NULL AND kek_version < :target ORDER BY principal_id, id FOR UPDATE SKIP LOCKED LIMIT :batch_size`. `SKIP LOCKED` lets the CLI resume after interruption without blocking concurrent writers.
3. For each row: `dek = provider.unwrap_dek(row.wrapped_dek, tenant_id=tid, aad=row.aad, kek_version=row.kek_version)`; `(new_wrapped, new_version) = provider.wrap_dek(dek, tenant_id=tid, aad=row.aad)`; `UPDATE wrapped_dek = :new_wrapped, kek_version = :new_version WHERE tenant_id = :tid AND principal_id = :pid AND id = :id`. `ciphertext`, `nonce`, `aad` untouched so any reader mid-rotation decrypts successfully regardless of which version wrote.
4. Increment `KEK_ROTATE_ROWS` with labels `(tenant_id, from_version, to_version)`.
5. Loop batches until `SELECT` returns zero rows or `max_rows` exhausted. Return `RotationReport(rows_rewrapped, rows_failed, rows_remaining, target_version)`.

### PR 1 compatibility

Nullable columns + NULL-aware read path means PR 1 rows stay readable unchanged. Callers who never pass an `encryption_provider` get exactly today's behavior.

## Error handling

### Error types

```
EnvelopeError(Exception)
├── EnvelopeConfigurationError            # setup problems (Transit key not derived, KMS IAM denied at init)
├── DecryptionFailed                      # generic — stored ciphertext/DEK couldn't be decrypted
├── AADMismatch(DecryptionFailed)         # AAD column doesn't match expected tenant|principal|profile
├── WrappedDEKInvalid(DecryptionFailed)   # provider returned an unwrap error (KMS AccessDenied, Transit 403)
└── CiphertextCorrupted(DecryptionFailed) # AESGCM tag verification failed
```

### Propagation

`get_with_credential` never swallows `DecryptionFailed`. Caller decides: retry for transient `WrappedDEKInvalid` (KMS 5xx, Vault 503), surface `AADMismatch` to the operator (never transient — bug or attack), fail the resolve for `CiphertextCorrupted`.

### No plaintext in error paths

Every `EnvelopeError.__str__` / `__repr__` includes `(tenant_id, profile_id, kek_version)` + the provider-side error class only. Never ciphertext, wrapped_dek, DEK, or plaintext. A unit test round-trips `repr()` through a regex that asserts no base64/hex blob ≥16 bytes appears.

### Cache never caches failures

Failed unwraps do not enter the cache. A transient IAM blip shouldn't pin "decrypt failed" for 5 min. Next call retries the provider.

### Rotation failure modes

- Per-row unwrap failure → CLI prints `(tenant_id, profile_id, kek_version, error_class)` and continues the batch. Row stays on old version. Operator investigates before re-running. Exit code 0 with warning on stderr.
- Wrap failure at new version → abort, non-zero exit, zero rows mutated. A new-version KEK that can't wrap is a setup error, not a row error.

### Config/bootstrap failures

Providers validate on construction, not on first encrypt. `VaultTransitProvider` asserts `derived=true`; `AwsKmsProvider` calls `describe_key()`. Both raise `EnvelopeConfigurationError` with a remediation string (`"run: vault write -f transit/keys/<name> derived=true"`, `"grant kms:Encrypt,kms:Decrypt on <arn>"`).

### CHECK-constraint violations

Surface as `sqlalchemy.exc.IntegrityError`. Test asserts this shape — silently coercing a bug into a write is worse than failing loud.

## Testing

All tests new. Gating mirrors PR 1's split (`@pytest.mark.postgres`, `xdist_group`).

### `test_envelope.py` — pure unit, fast

- AESGCM roundtrip
- nonce-reuse under same DEK raises (guardrail on DEK-per-row invariant)
- AAD tamper → `CiphertextCorrupted`
- ciphertext bit-flip → `CiphertextCorrupted`
- `DEKCache` TTL expiry, LRU eviction, hit/miss counter increment
- `repr()`/`str()` for every `EnvelopeError` subclass — regex asserts no ≥16-byte base64/hex blob appears

### `test_envelope_contract.py` — shared parametrized suite

Runs against `InMemoryEncryptionProvider` by default. Vault / KMS provider test modules import and re-parametrize this suite. Contract asserts:

- wrap/unwrap roundtrip
- unwrap with wrong `tenant_id` → `WrappedDEKInvalid`
- unwrap with wrong `aad` → `WrappedDEKInvalid`
- `current_version` advances after `rotate(new_version)` (fake only; real providers skip)
- wrap at v2, unwrap at v1 succeeds (readers tolerate mixed versions)

### `test_postgres_envelope_integration.py` — @pytest.mark.postgres

Covers every acceptance criterion from the issue:

- **Roundtrip**: `upsert_with_credential` → `get_with_credential` returns equal credential.
- **Swap attack**: write row A in tenant T1; direct SQL copies A's `(ciphertext, wrapped_dek, nonce, aad, kek_version)` into row B in tenant T2; `get_with_credential(B)` raises `AADMismatch`. Also tested with `tenant_id` swap handled via the provider side (in-memory raises `WrappedDEKInvalid` when KEK lookup keys differ).
- **PR 1 compat**: plain `upsert` writes NULL ciphertext columns; `get_with_credential` returns `(profile, None)`.
- **Mixed-version reads**: write at v1, rotate provider, write at v2, read both.
- **CHECK constraint**: direct `INSERT` with 4 of 5 encryption columns raises `IntegrityError`.
- **Cache amortization**: two `get_with_credential` calls for the same row → exactly one `unwrap_dek` call on the fake (`provider.unwrap_count == 1`).

### `test_rotate_kek_cli.py` — @pytest.mark.postgres

- Dry-run reports per-version row counts, zero writes.
- `--apply` rewraps; all rows end at `to_version`; reads during rotation (between batches) succeed.
- Per-row unwrap failure (fake provider marked to fail one `wrapped_dek`): batch continues; row stays old; exit 0 with warning.
- Wrap-side failure at target version: batch aborts; zero rows mutated; non-zero exit.

### `test_envelope_providers_vault.py` / `..._aws_kms.py` — opt-in integration

Same contract suite, re-parametrized against dev-mode Vault or LocalStack KMS. Gated behind `@pytest.mark.vault` / `@pytest.mark.kms`. Never in default CI; manual + future gated job.

### Explicitly NOT tested in this PR

- Cross-process DEK cache coordination (per-process cache is by design; Phase D decides any cross-process story).
- Key-rotation daemon (CLI is the only rotation surface).
- Vault/KMS 5xx retry policy (first hit is naive; retries land when a caller has real latency/outage data).

## Acceptance mapping (from the issue)

| Criterion | Where it's met |
|---|---|
| `EncryptionProvider` trait + both impls with contract tests | `envelope.py`, `envelope_providers/*.py`, `test_envelope_contract.py` + opt-in `test_envelope_providers_*` |
| Roundtrip test: encrypt in store, decrypt, read back plaintext | `test_postgres_envelope_integration.py::test_roundtrip` |
| Ciphertext-swap attack rejected | `test_postgres_envelope_integration.py::test_swap_attack_rejected` |
| Rotation path: bump `kek_version`, background job migrates rows, reads succeed throughout | `rotate_kek_for_tenant` + `test_rotate_kek_cli.py::test_rotation_with_concurrent_reads` |
| DEK cache hit/miss metrics | `envelope_metrics.py` + `test_envelope.py` + `test_postgres_envelope_integration.py::test_cache_amortizes` |
| Docs: Vault vs AWS-native deployment choice | §"Deployment guidance" below |

## Deployment guidance (stub for docs in repo)

- **Vault-native shops** — use `VaultTransitProvider`. Create one transit key per deployment with `derived=true`; per-tenant scoping handled via the `context` parameter at encrypt time. Vault Transit is free self-hosted; no per-call cost. Rotation: `vault write -f transit/keys/<name>/rotate`; then run `nexus auth rotate-kek --to-version N` per tenant.
- **AWS-native shops** — use `AwsKmsProvider`. One CMK per tenant (keeps blast radius per-tenant), `kms:Encrypt` / `kms:Decrypt` / `kms:DescribeKey` IAM. Per-call KMS charges apply; the 5-min DEK cache amortizes these on hot paths. Rotation: either manually create a new key and point the provider at it (key-id swap), or rely on AWS-managed annual rotation (no `kek_version` bump needed — KMS handles envelope internally; our `kek_version` then tracks provider-config version only).
- Anything else → start with `InMemoryEncryptionProvider` for development only. Not for production: process-local keys die with the process.

## Non-goals / deferred

- Configuration surface (env vars / settings file factory) — deferred to Phase D when the first real consumer exists.
- Key-rotation always-on daemon — CLI-only for now.
- AuthProfile dataclass changes — routing-only stays the shape; ciphertext travels via the sub-protocol methods.
- Metrics backend switch (OpenTelemetry) — `prometheus_client` matches existing `bricks/*_metrics.py` pattern.

## Sources

- [Envelope encryption patterns](https://docs.cloud.google.com/kms/docs/envelope-encryption)
- [Vault Transit secrets engine](https://developer.hashicorp.com/vault/docs/secrets/transit)
- [AWS KMS encryption context](https://docs.aws.amazon.com/kms/latest/developerguide/encrypt_context.html)
- [AWS cross-account secret access patterns](https://aws.amazon.com/blogs/database/design-patterns-to-access-cross-account-secrets-stored-in-aws-secrets-manager/)
