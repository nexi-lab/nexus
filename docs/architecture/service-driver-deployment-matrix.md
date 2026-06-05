# Service & Driver Deployment Matrix

> Global view of every service, hook, and driver across both repos,
> classified by deployment mode. See KERNEL-ARCHITECTURE.md (nexus-vfs) §1
> for the three deployment modes and their trade-offs.

## Deployment Modes

| Mode | Mechanism | Hot-swap | Perf | VFS hooks |
|------|-----------|----------|------|-----------|
| **Compiled-in** | Cargo feature gate / Python wiring | No (recompile) | Best | Full |
| **dylib** | `PluginLoader` + `dlopen` | Yes | Good (~ns C ABI) | Limited (dispatch OK, hooks need C ABI wrapper) |
| **gRPC sidecar** | Separate process, `ManagedServiceGrpcProxy` | Inherent | ~100us | No |

---

## 1. Kernel Hooks (NativeInterceptHook / PathResolver / MutationObserver)

All hooks are compiled-in. dylib hooks (Phase 5B) are deferred — they
would require a C ABI wrapper since Rust trait objects are not stable
across `dlopen` boundaries.

| Hook | Repo | Module | Mode | Notes |
|------|------|--------|------|-------|
| AuditHook | nexus-vfs | `rust/services/src/audit/` | Compiled-in | File operation audit trail |
| MailboxStampingHook | nexus-vfs | `rust/services/src/managed_agent/` | Compiled-in | `/chat-with-me` mailbox stamping |
| WorkspaceBoundaryHook | nexus-vfs | `rust/services/src/managed_agent/` | Compiled-in | Agent workspace boundary enforcement |
| RebacPermissionCheckHook | nexus | `src/nexus/bricks/rebac/` | Compiled-in | Permission pre-check |
| SyncPermissionWriteHook | nexus | `src/nexus/bricks/rebac/` | Compiled-in | Permission sync on write |
| DeferredPermissionHook | nexus | `src/nexus/bricks/rebac/` | Compiled-in | Deferred permission buffering |
| DynamicViewerReadHook | nexus | `src/nexus/bricks/rebac/` | Compiled-in | Dynamic viewer expansion |
| TigerCacheWriteHook | nexus | `src/nexus/bricks/rebac/cache/tiger/` | Compiled-in | Tiger cache invalidation (write) |
| TigerCacheRenameHook | nexus | `src/nexus/bricks/rebac/cache/tiger/` | Compiled-in | Tiger cache invalidation (rename) |
| ReadmeResolverHook | nexus | `src/nexus/bricks/parsers/` | Compiled-in | PathResolver: README virtual paths |
| AutoParseHook | nexus | `src/nexus/bricks/parsers/` | Compiled-in | Structured parsing on read |
| MarkdownStructureHook | nexus | `src/nexus/bricks/parsers/` | Compiled-in | Markdown AST extraction |

---

## 2. Rust Services (RustService trait)

| Service | Repo | Module | Mode | Cargo Feature | Notes |
|---------|------|--------|------|---------------|-------|
| ManagedAgentService | nexus-vfs | `rust/services/src/managed_agent/` | Compiled-in | `service-managed-agent` | Mailbox + workspace hooks |
| AcpService | nexus-vfs | `rust/services/src/acp/` | Compiled-in | `service-acp` | Subprocess + ACP-over-stdio |
| TasksService | nexus-vfs | `rust/services/src/tasks/` | Compiled-in | `service-tasks` | Durable task queue (fjall) |
| MatrixAdapterService | nexus-vfs | `rust/services/src/matrix_adapter/` | Compiled-in | `service-matrix-adapter` | Matrix CS v3 adapter |
| PasswordVaultService | nexus-vfs | `rust/services/src/password_vault/` | Compiled-in | `service-password-vault` | Password vault domain logic |
| AgentStatusResolver | nexus-vfs | `rust/services/src/agents/` | Compiled-in | `service-agents` | Procfs-style agent status |

---

## 3. Python Services (Bricks — wired via syscall enlist)

| Service | Repo | Module | Exports | Mode |
|---------|------|--------|---------|------|
| search | nexus | `src/nexus/bricks/search/` | glob, grep, list, semantic_search | Compiled-in |
| federation | nexus | `src/nexus/bricks/` | federation_* (6 methods) | Compiled-in |
| rebac | nexus | `src/nexus/bricks/rebac/` | rebac_check, rebac_create, rebac_list_tuples, rebac_expand | Compiled-in |
| mount | nexus | `src/nexus/bricks/mount/` | add_mount, remove_mount, list_mounts | Compiled-in |
| mount_persist | nexus | `src/nexus/bricks/mount/` | save/load/list/delete saved mounts | Compiled-in |
| mcp | nexus | `src/nexus/bricks/mcp/` | mcp_list_mounts, mcp_connect | Compiled-in |
| oauth | nexus | `src/nexus/bricks/auth/oauth/` | list_providers, list/revoke credentials | Compiled-in |
| share_link | nexus | `src/nexus/bricks/share_link/` | create/get/list/revoke share links | Compiled-in |
| time_travel | nexus | `src/nexus/bricks/versioning/` | get/list files at operation | Compiled-in |
| operations | nexus | `src/nexus/bricks/` | list/get/undo operations | Compiled-in |
| agent_rpc | nexus | `src/nexus/bricks/` | register/update/list/get/delete agents | Compiled-in |
| acp_rpc | nexus | `src/nexus/bricks/` | acp_call, acp_list_agents, etc. | Compiled-in |
| sandbox_rpc | nexus | `src/nexus/bricks/sandbox/` | sandbox_create/run/pause/resume/stop/list | Compiled-in |
| metadata_export | nexus | `src/nexus/bricks/` | export/import metadata | Compiled-in |

---

## 4. Drivers / Backends (ObjectStore trait)

### Storage Backends

| Backend | Repo | Module | `backend_type` | Cargo Feature | Mode |
|---------|------|--------|----------------|---------------|------|
| PathLocalBackend | nexus-vfs | `rust/backends/src/storage/` | `path_local` | `driver-path-local` | Compiled-in |
| CasLocalBackend | nexus-vfs | `rust/backends/src/storage/` | `cas-local` | `driver-cas-local` | Compiled-in |
| LocalConnectorBackend | nexus-vfs | `rust/backends/src/storage/` | `local_connector` | `driver-local-connector` | Compiled-in |
| RemoteBackend | nexus-vfs | `rust/backends/src/storage/` | `remote` | `driver-remote` | Compiled-in (gRPC client) |
| S3Transport | nexus-vfs | `rust/backends/src/transports/blob/` | `s3` | `driver-s3` | Compiled-in |
| GcsBackend | nexus-vfs | `rust/backends/src/transports/blob/` | `gcs` | `driver-gcs` | Compiled-in |

### API Connectors

| Backend | Repo | Module | `backend_type` | Mode |
|---------|------|--------|----------------|------|
| CliTransport | nexus-vfs | `rust/backends/src/transports/api/` | `cli` | Compiled-in |
| GdriveBackend | nexus-vfs | `rust/backends/src/transports/api/google/` | `gdrive` | Compiled-in |
| GmailBackend | nexus-vfs | `rust/backends/src/transports/api/google/` | `gmail` | Compiled-in |
| HNBackend | nexus-vfs | `rust/backends/src/transports/api/social/` | `hn` | Compiled-in |
| XBackend | nexus-vfs | `rust/backends/src/transports/api/social/` | `x` | Compiled-in |
| SlackBackend | nexus-vfs | `rust/backends/src/transports/api/social/` | `slack` | Compiled-in |
| OpenAIBackend | nexus-vfs | `rust/backends/src/transports/api/ai/` | `openai` | Compiled-in |
| AnthropicBackend | nexus-vfs | `rust/backends/src/transports/api/ai/` | `anthropic` | Compiled-in |

---

## 5. dylib Plugins (Future)

| Plugin | Repo | Module | Status | Notes |
|--------|------|--------|--------|-------|
| vault | nexus | `rust/plugins/vault/` (planned) | Phase 6 — deferred | First dylib plugin guinea pig. Depends on `nexus-plugin-abi` |

Infrastructure in place:
- `PluginLoader` + `DylibRustService` wrapper (nexus-vfs `rust/kernel/src/kernel/plugins/`)
- `nexus-plugin-abi` crate with C ABI types + `declare_service_plugin!` macro
- `plugin.*` gRPC dispatch surface + `--plugin-dir` CLI flag
- Hook-aware service lifecycle: unhook → drain → replace → rehook

**Limitation**: VFS hooks for dylib plugins require a C ABI wrapper layer
(Phase 5B, deferred). Current dylib support covers RPC dispatch only.
Service hot-swap (load/unload/reload) works today; hook hot-swap does not.

---

## 6. gRPC Sidecar (Future)

No production gRPC sidecar services exist today. The infrastructure
(`ManagedServiceGrpcProxy`) is available for language-agnostic services
(Python, Go, etc.) that run as separate processes.

| Candidate | Rationale |
|-----------|-----------|
| Python bricks | If performance isolation is needed |
| Third-party integrations | Language-agnostic extensibility |
