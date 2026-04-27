# PasswordVaultService integration — consumer / implementer split

> Why this doc exists: a kernel-arch transition email landed at password-agent
> asking the consumer to choose Rust vs Python for the SecretsService rewrite.
> password-agent is the *consumer*, not the *implementer* of SecretsService /
> PasswordVaultService — both live in this nexus repo. Capturing the split
> here so future cross-repo coordination doesn't get the role wrong.

## Who owns what

| Component | Repo | Path |
|---|---|---|
| `SecretsService` (encrypted KV brick — Fernet, versioning, audit, soft-delete) | nexus | `src/nexus/bricks/secrets/service.py` |
| `PasswordVaultService` (domain wrapper, namespace="passwords", VaultEntry schema validation) | nexus | `src/nexus/services/password_vault/{service,schema}.py` |
| REST router `/api/v2/password_vault/*` | nexus | `src/nexus/server/api/v2/routers/password_vault.py` |
| gRPC service contract (this PR) | nexus | `proto/nexus/password_vault/v1/password_vault.proto` |
| `NexusClient` HTTP client | password-agent (sudoprivacy/password-agent) | `src/password_agent/nexus_client.py` |
| Tier-3 admin CLIs (vault_*, auth_*, quip_*) | password-agent | `tools/*.py` |
| Tier-1/2 agent tools (`pwd_login`, `pwd_rotate`, ...) — *future* | sudowork (sudoprivacy/sudowork) | TBD |

password-agent talks to PasswordVaultService over HTTP today. Sudowork's
Tier-1 `pwd_login` tools (when they ship) will talk to the same service —
the design intent is one server-side surface used by all consumer tiers.

## Why a dedicated PasswordVaultService instead of generic SecretService

`PasswordVaultService` is a thin layer over `SecretsService` with
`NAMESPACE = "passwords"`, but the layer earns its keep:

1. **Domain schema (`VaultEntry`)** — title/username/password/url/notes/
   tags/totp_secret/extra. Consumers serialize against this. Without the
   wrapper, every consumer (password-agent, sudowork's pwd_login, future
   integrations) re-implements the JSON envelope for the
   "blob inside SecretsService.value" round-trip.

2. **Server-side TOTP generation (PR #3847)** — `POST /{title}/totp`
   computes a one-shot HOTP from the entry's stored seed and returns 6
   digits + window remaining. Seed never leaves the server (oracle
   rate-limited 30s LRU per `(subject_id, entry_id, window_index)`).
   This is fundamentally domain-specific; generic SecretService can't host it.

3. **Domain-typed audit (`access_context` enum)** — admin_cli /
   auto_login / auto_rotate / reveal_approved / agent_direct. Lets
   downstream rate-limit / ACL / alerting branch on caller intent.
   Generic SecretsService audit is a single "secret_read" line.

4. **Tier 1/2/3 routing affordance** — sudowork's pwd_login is in scope
   to call the same service password-agent does. A domain service makes
   the contract obvious; "call SecretService with namespace=passwords"
   leaks SecretsService internals to consumer surface.

So even after the SecretsService gRPC migration, PasswordVaultService
keeps being its own thing — it's a peer service that *uses* SecretService
internally, not a routing alias.

## Migration plan (proposed)

### Phase 0 — proto contract (this PR)

- `proto/nexus/password_vault/v1/password_vault.proto` lands. No code
  changes; pure contract artifact.
- nexus-side and password-agent-side teams agree the shape matches
  current REST 1:1 plus the access_context bits.

### Phase 1 — gRPC server (nexus side, Rust preferred)

**Implementation language: Rust where feasible.** The kernel-arch direction
is a single ~5–8 MB nexusd-cluster Rust binary; new services should land in
the Rust workspace from day one rather than rewriting later.

Concrete shape (matches the rust/services/ peer-crate pattern from PR #3921):

- `rust/services/src/password_vault/` peer crate.
  - Depends only on `kernel` + `contracts` (preserves §services⊥backends
    invariant per `docs/architecture/KERNEL-ARCHITECTURE.md` §6).
  - Storage: same redb tables SecretsService uses (namespace="passwords"),
    or pass-through to a Rust SecretsService once that lands.
  - Crypto: `aes-gcm` (Fernet-equivalent) with master-key derivation —
    coordinate with the SecretsService Rust port so master key handling
    stays SSOT (currently OAuthCrypto reads from `system_settings` SQL
    per #3850).
  - Audit hook: `kernel.register_native_hook(POST_WRITE)` for
    `access_context`-tagged events; matches AuditHook pattern.
- gRPC binding lives next to the impl, exposed via the same gRPC server
  scaffold the SecretsService Rust port uses (or `start_vfs_grpc_server`
  if that's still the integration point).
- Binary delta target: **< 250 KB** (200 KB cited for SecretsService;
  this layer is thinner — proto + dispatch + JSON envelope).

**Python fallback (only if Rust resources are the gating constraint):**
- Keep the existing Python `PasswordVaultService` class as the impl.
- Add a thin Python gRPC handler (`grpc.aio.server` or piggyback on the
  existing `start_vfs_grpc_server` `Call(method, payload)` dispatcher).
- Treated as a temporary bridge; flagged `# DEPRECATED — port to Rust`.
- Means sudowork's wheel still bundles Python for this surface, blocking
  the pure-Rust binary goal. Don't ship this unless Rust is genuinely
  blocked.

**REST router stays for one minor release** in either path, so password-agent
has an overlap window to migrate. Mark it `Deprecated` in OpenAPI.

### Phase 2 — gRPC client (password-agent side)

- password-agent depends on the published proto (subscription mechanism
  TBD — see open question below).
- Generate Python gRPC stubs.
- Replace `NexusClient` (urllib HTTP) with a gRPC-backed one. Same
  external interface to `vault.py` so the 16 tools and 314 vault entries
  carry over without consumer-side schema changes.
- Verify against dev nexusd first, then v0.10.x sudowork-shipped binary
  with gRPC enabled.

### Phase 3 — REST removal

- After password-agent is on gRPC end-to-end (and any other REST
  consumers we discover), nexus drops the REST router.

## Open questions (for nexus kernel team to answer)

1. **Proto distribution mechanism**. How will password-agent (and other
   downstream consumers) consume `proto/nexus/password_vault/v1/`? Options:
   a. nexus repo publishes generated Python stubs to a private PyPI / artifact registry.
   b. password-agent vendors the .proto files and runs codegen at build time.
   c. buf module / Connect-RPC distribution.

   Whatever the answer, it should match what SecretsService gRPC does so
   we don't have two patterns.

2. **Transport endpoint**. Is gRPC served on the same port (12012) as REST,
   or a separate gRPC port? sudowork's `DynamicNexusService` will need to
   know how to expose / proxy it.

3. **HTTP REST deprecation timeline**. password-agent just switched to
   v0.9.43 binary's REST surface. Need at least one minor release window
   where both REST and gRPC are available, or a flagged opt-in for gRPC.

4. **`access_context` enforcement**. Current REST treats unknown values
   as 400. gRPC uses a typed enum; is it OK to treat
   `ACCESS_CONTEXT_UNSPECIFIED` as the default `ADMIN_CLI` (matches
   current REST default-when-omitted), or should server reject?

## What this PR does

- Adds the proto file (`proto/nexus/password_vault/v1/password_vault.proto`).
- Adds this doc.
- **Does not** change REST router, services/, or bricks/.
- **Does not** generate stubs. (Codegen wiring depends on Q1 above.)

## What this PR does NOT block

- password-agent's current M2 work (auth_login + cookie injection over REST).
  We keep the REST surface live until the gRPC migration is end-to-end
  ready on both sides.
