# PasswordVaultService integration

How the password-vault domain service splits across nexus and its
consumers, and where the gRPC contract sits in the workspace.

Cross-references:
- [KERNEL-ARCHITECTURE](https://github.com/nexi-lab/nexus-vfs/blob/main/README.md) §6.1 (workspace composition + peer-crate
  invariants) — explains why a new domain service ships as a peer
  crate under `rust/services/`.
- [KERNEL-ARCHITECTURE](https://github.com/nexi-lab/nexus-vfs/blob/main/README.md) §7.1 (profile binaries) — the cluster
  binary is one consumer of this service through its in-process
  syscall surface.
- `proto/nexus/exchange/v1/common.proto` — `NexusErrorCode` enum the
  proto contract reuses for domain errors.

## Components and ownership

| Component | Repo | Path |
|---|---|---|
| `SecretsService` (encrypted KV brick — Fernet, versioning, audit, soft-delete) | nexus | `src/nexus/bricks/secrets/service.py` |
| `PasswordVaultService` (domain wrapper, `namespace="passwords"`, `VaultEntry` schema validation) | nexus | `src/nexus/services/password_vault/{service,schema}.py` |
| REST router `/api/v2/password_vault/*` (7 endpoints) | nexus | `src/nexus/server/api/v2/routers/password_vault.py` |
| gRPC service contract | nexus | `proto/nexus/password_vault/v1/password_vault.proto` |
| `AccessContext` cross-language SSOT | nexus | `src/nexus/contracts/secrets_access.py` |
| Master encryption key handling | nexus | `lib/oauth/crypto.py::OAuthCrypto` (reads from `system_settings` SQL per #3850) |
| `NexusClient` HTTP client | password-agent (sudoprivacy/password-agent) | `src/password_agent/nexus_client.py` |
| Tier-3 admin CLIs (`vault_*`, `auth_*`, `quip_*`) | password-agent | `tools/*.py` |
| Tier-1/2 agent tools (`pwd_login`, `pwd_rotate`, …) | sudowork (sudoprivacy/sudowork) | TBD |

password-agent and sudowork's Tier-1 tools both reach the same
server-side surface — the design intent is one service for every
consumer tier.

## Why a dedicated PasswordVaultService

`PasswordVaultService` is a thin layer over `SecretsService` with
`NAMESPACE = "passwords"`, but the layer earns its keep on four
counts:

1. **Domain schema (`VaultEntry`)** — title / username / password /
   url / notes / tags / totp_secret / extra.  Consumers serialize
   against this directly instead of re-implementing a JSON envelope
   for the "blob inside `SecretsService.value`" round-trip.

2. **Server-side TOTP generation** (PR #3847) — `GenerateTotp`
   computes a one-shot HOTP from the entry's stored seed and returns
   the 6-digit code plus the seconds remaining in the current 30 s
   window.  The seed never leaves the server; an LRU keyed by
   `(subject_id, entry_id, window_index)` rate-limits oracle attempts
   from a hostile read-only client.  This is fundamentally
   domain-specific behaviour that a generic SecretsService cannot
   host.

3. **Domain-typed audit (`AccessContext` enum)** — admin_cli /
   auto_login / auto_rotate / reveal_approved / agent_direct.  Lets
   downstream rate-limit / ACL / alerting branch on caller intent.
   `SecretsService`'s generic audit emits a single `secret_read` line.

4. **Tier 1/2/3 routing affordance** — sudowork's `pwd_login` calls
   the same service password-agent does.  A domain-named service
   makes the contract obvious; "call SecretService with
   `namespace=passwords`" leaks SecretsService internals to the
   consumer surface.

So `PasswordVaultService` is its own peer service that *uses*
`SecretsService` internally, not a routing alias.

## Deployment model

The vault runs as a **dylib plugin** inside `nexusd-cluster`, loaded
at startup via the `--plugin-dir` CLI flag. The plugin is compiled as
a cdylib (`libnexus_vault.so` / `.dylib` / `.dll`) and exports the
standard `declare_service_plugin!` C ABI symbols.

At load time, `PluginLoader` calls `nexus_service_create` which:
1. Creates a private `Kernel` instance for vault storage
2. Sets up a `PathLocalBackend` under `$NEXUS_DATA_DIR/vault/content/`
3. Auto-generates a 32-byte AES-256 master key at
   `$NEXUS_DATA_DIR/vault/master.key` on first load
4. Registers as `RustService` named `"password-vault"` in the
   kernel's `ServiceRegistry`

Plugin dispatch maps method strings (`"put_entry"`, `"get_entry"`,
etc.) to the existing `PasswordVaultService` gRPC trait impl with
protobuf-encoded payloads. Zero logic changes from the trait impl —
the plugin is a deployment wrapper only.

## Rust implementation placement

The service logic lives at `rust/services/src/password_vault/`.

- The crate depends only on `kernel` + `contracts`, preserving the
  `services ⊥ backends ⊥ transport ⊥ raft` invariant from
  [KERNEL-ARCHITECTURE](https://github.com/nexi-lab/nexus-vfs/blob/main/README.md) §6.1.
- Storage uses kernel syscalls through a private Kernel instance
  with a `PathLocalBackend` for persistent content.
- The vault profile (`rust/profiles/vault/`) compiles to a cdylib
  that uses `nexus-plugin-abi`'s `declare_service_plugin!` macro.
- Audit is wired through `kernel::Kernel::register_native_hook`
  with `AccessContext`-tagged events.

## Proto contract

The proto lives at `proto/nexus/password_vault/v1/password_vault.proto`
and exposes:

| RPC | REST equivalent |
|---|---|
| `PutEntry` | `PUT /{title}` |
| `GetEntry` | `GET /{title}` |
| `ListEntries` | `GET ""` |
| `DeleteEntry` | `DELETE /{title}` |
| `RestoreEntry` | `POST /{title}/restore` |
| `ListVersions` | `GET /{title}/versions` |
| `GenerateTotp` | `POST /{title}/totp` |

Errors follow `nexus.exchange.v1.NexusErrorCode` so all nexus gRPC
services share one error vocabulary.

The `/v1/` segment in the package path matches `nexus.exchange.v1` —
both are cross-repo public protocols (consumed by sudoprivacy/* repos),
where versioning the wire format buys an upgrade path.  Intra-cluster
protos (`nexus.core`, `nexus.raft`, `nexus.grpc.vfs`) skip the version
segment because they ship in lockstep with the kernel.

## Open questions

1. **Proto distribution to consumers.**  password-agent and any other
   downstream repo needs `.proto` files at codegen time.  Options:
   (a) nexus publishes generated Python / TypeScript stubs to a
   private artifact registry; (b) consumers vendor the `.proto`
   files and run codegen at build time; (c) buf module +
   Connect-RPC distribution.  Whatever the answer, the same
   mechanism applies to every cross-repo proto in
   `proto/nexus/*/v1/`.

2. **REST deprecation overlap.**  password-agent just switched to
   v0.9.43's REST surface.  Recommended overlap is two minor releases
   — one matches password-agent's `v0.9.43 → v0.10.x` cycle and the
   second covers Tier-1 sudowork tools.  REST router is marked
   `deprecated=True` in OpenAPI when the gRPC server lands and
   removed on the next major.
