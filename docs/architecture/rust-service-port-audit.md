# Rust service port — `@rpc_expose` audit

Roadmap for deleting the entire Python RPC envelope (`@rpc_expose` /
`generate_rpc_params.py` / `dispatch.py` / `handlers/` /
`_rpc_params_*.py` / `rpc_decorator.py` / `parse_method_params` /
`VFSCallDispatcher` / the kernel-syscall thin dispatcher) and moving
every dispatch-touching surface into Rust.

Final architecture target:

```
[Caller]
    ↓ gRPC Call(method, payload) — single wire envelope, kept
[Rust tonic]
    ↓ resolve_rust_dispatch(method)
    ↓ Kernel::dispatch_rust_call(svc, method, payload)
[Rust ServiceRegistry → Rust service]
    business logic in Rust (or `Python::with_gil` FFI for stragglers)
```

In-process Python callers go through PyO3:

```
[Python factory / lifecycle / runtime]
    ↓ kernel.sys_xxx(...)              for syscalls
    ↓ nx_kernel_dispatch_rust_call(    for service methods
       kernel, svc, method, payload)
```

## Disposition codes

| Code | Meaning                                                                |
|------|------------------------------------------------------------------------|
| **P**| **Port** business logic to a Rust service crate                        |
| **C**| **Collapse** into an existing syscall (delete the wrapper layer)       |
| **D**| **Delete** as deadcode — no remote caller, no internal caller          |
| **F**| Rust dispatcher with **FFI fallback** to Python via `Python::with_gil` |

The `C` and `D` buckets shrink the port surface; only `P` and `F`
methods cost real Rust implementation work.

## Service-tier audit

### `federation_rpc.py` (11 methods)
Federation control plane — already mostly thin wrappers over the Rust
kernel's federation HAL.

| Method                       | Disposition | Notes                                                   |
|------------------------------|-------------|---------------------------------------------------------|
| `federation_client_whoami`   | P           | Subject identity from auth context — Rust trivially     |
| `federation_export_zone`     | P           | Wraps `ZoneExportService`; needs Rust port              |
| `federation_import_zone`     | P           | Wraps `ZoneImportService`; needs Rust port              |
| `federation_create_zone`     | C           | == `sys_setattr DT_MOUNT` no source. Already collapses  |
| `federation_remove_zone`     | P           | `coordinator.remove_zone` HAL call — Rust thin port     |
| `federation_join`            | C           | == `sys_setattr DT_MOUNT` with `source=` (joiner-side)  |
| `federation_mount`           | C           | == `sys_setattr DT_MOUNT backend_name=federation`       |
| `federation_unmount`         | C           | == `sys_unlink` on the mount path                       |
| `federation_share`           | P           | Wraps kernel `federation_share_zone`; trivial           |
| `federation_list_zones`      | P           | Wraps kernel federation HAL list                        |
| `federation_cluster_info`    | P           | Wraps `coordinator.cluster_info`                        |

**5 P, 6 C, 0 D.** First service to port — most of the work is delete-
the-Python-wrapper.

### `mount_service.py` (13 methods)
Old mount-management UX. Most operations are syscall-equivalent.

| Method               | Disposition | Notes                                                      |
|----------------------|-------------|------------------------------------------------------------|
| `add_mount`          | C           | == `sys_setattr DT_MOUNT`                                  |
| `remove_mount`       | C           | == `sys_unlink` on the mount path                          |
| `update_mount`       | C           | == `sys_setattr` re-apply with new params                  |
| `reauth_mount`       | C           | == `sys_setattr` with refreshed auth params                |
| `list_mounts`        | C           | == `sys_readdir` on the synthetic mount-listing namespace  |
| `get_mount`          | C           | == `sys_stat` on a mount path                              |
| `has_mount`          | C           | == `access` on a mount path                                |
| `delete_connector`   | P           | Connector-specific cleanup beyond the unmount              |
| `list_connectors`    | P           | Inventory of connector kinds — small Rust port             |
| `save_mount`         | P           | `mount_persist` Rust service — store under metastore       |
| `list_saved_mounts`  | P           | Same                                                        |
| `load_mount`         | P           | Same                                                        |
| `delete_saved_mount` | P           | Same                                                        |

**5 P, 8 C.** Heavy collapse-to-syscall — most of `mount_service.py`
disappears.

### `share_link_service.py` (6 methods)
HMAC URL signing.

| Method                      | Disposition | Notes                                              |
|-----------------------------|-------------|----------------------------------------------------|
| `create_share_link`         | P           | Rust `hmac` + `sha2` crates — pure crypto          |
| `get_share_link`            | P           | Metastore-backed lookup                            |
| `list_share_links`          | P           | Metastore prefix scan                              |
| `revoke_share_link`         | P           | Metastore tombstone                                |
| `access_share_link`         | P           | HMAC verify + caps check                           |
| `get_share_link_access_logs`| P           | Audit-stream subscription                          |

**6 P.** All Rust port — no Python deps that can't move over.

### `credential_service.py` (oauth, 6 methods)
OAuth flows + credential storage.

| Method               | Disposition | Notes                                                          |
|----------------------|-------------|----------------------------------------------------------------|
| `list_providers`     | P           | Static config — trivial                                        |
| `get_auth_url`       | F           | Rust HTTP + URL building, but provider quirks (Google scopes)  |
| `exchange_code`      | F           | Rust HTTP token-exchange; `google-auth` JWT verify still Python|
| `list_credentials`   | P           | Metastore-backed                                               |
| `revoke_credential`  | P           | Metastore tombstone                                            |
| `test_credential`    | F           | Provider HTTP probe — Rust where simple, Python for Google    |

**3 P, 3 F.** OAuth library coverage in Rust is good (`oauth2`,
`jsonwebtoken`) but Google ID-token quirks are easier to keep Python.

### `mcp_service.py` (6 methods)
MCP protocol over stdio/SSE.

| Method               | Disposition | Notes                                                      |
|----------------------|-------------|------------------------------------------------------------|
| `mcp_list_mounts`    | P           | Trivial — registry over metastore                          |
| `mcp_list_tools`     | F           | Forward to MCP server's `tools/list` — `rmcp` crate        |
| `mcp_mount`          | F           | Spawn MCP server subprocess + handshake — `rmcp` crate     |
| `mcp_unmount`        | F           | Tear down — `rmcp` crate                                   |
| `mcp_sync`           | F           | Pull MCP server tool list — `rmcp` crate                   |
| `mcp_connect`        | F           | OAuth + handshake — uses `credential_service` provider info |

**1 P, 5 F.** `rmcp` crate handles the protocol; FFI for the existing
Python MCP-server-launching infrastructure during transition.

### `rebac_service.py` (27 methods)
Largest service. Tiger cache + namespace + tuples already partly in
`rust/shared/lib/src/rebac/`.

| Method                          | Disposition | Notes                                                   |
|---------------------------------|-------------|---------------------------------------------------------|
| `rebac_create`                  | P           | tuple insert — Rust                                     |
| `rebac_check`                   | P           | already mostly Rust (`rust/shared/lib/src/rebac/`)      |
| `rebac_expand`                  | P           | already mostly Rust                                     |
| `rebac_explain`                 | P           | trace tuple resolution — Rust                           |
| `rebac_check_batch`             | P           | bulk variant of `rebac_check`                           |
| `rebac_delete`                  | P           | tuple delete                                            |
| `rebac_list_tuples`             | P           | tuple scan                                              |
| `rebac_list_objects`            | P           | object-side index scan                                  |
| `set_rebac_option`              | P           | metastore put                                           |
| `get_rebac_option`              | P           | metastore get                                           |
| `register_namespace`            | P           | namespace registration                                  |
| `get_namespace`                 | P           | namespace lookup                                        |
| `namespace_list`                | P           | namespace list                                          |
| `namespace_delete`              | P           | namespace delete                                        |
| `rebac_expand_with_privacy`     | P           | wraps `rebac_expand` + privacy filter                   |
| `grant_consent`                 | P           | consent table mutation                                  |
| `revoke_consent`                | P           | consent table mutation                                  |
| `make_public`                   | C           | == `grant_consent` with public principal                |
| `make_private`                  | C           | == `revoke_consent` with public principal               |
| `share_with_user`               | C           | == `grant_consent` for user principal                   |
| `share_with_group`              | C           | == `grant_consent` for group principal                  |
| `revoke_share`                  | C           | == `revoke_consent` resolved by params                  |
| `revoke_share_by_id`            | C           | == `revoke_consent` resolved by id                      |
| `list_outgoing_shares`          | C           | == `rebac_list_tuples` filtered by subject              |
| `list_incoming_shares`          | C           | == `rebac_list_tuples` filtered by object               |
| `get_dynamic_viewer_config`     | D           | Dead — viewer config ABI never shipped                  |
| `read_with_dynamic_viewer`      | D           | Dead — viewer config ABI never shipped                  |

**16 P, 9 C, 2 D.** Big port but lots of collapse + deadcode.

### `search_service.py` (8 methods)
Glob/grep + semantic search.

| Method                         | Disposition | Notes                                                |
|--------------------------------|-------------|------------------------------------------------------|
| `list`                         | C           | == `sys_readdir` (already collapsed via thin dispatch)|
| `glob`                         | P           | already in `rust/shared/lib/src/search/`             |
| `glob_batch`                   | P           | bulk variant — Rust                                  |
| `grep`                         | P           | already in `rust/shared/lib/src/search/`             |
| `semantic_search`              | F           | txtai backend stays Python; Rust dispatcher          |
| `semantic_search_index`        | F           | txtai indexing stays Python                          |
| `semantic_search_stats`        | F           | txtai stats query                                    |
| `initialize_semantic_search`   | F           | txtai backend init                                   |

**3 P, 4 F, 1 C.** Glob/grep go full Rust; semantic stays via FFI
until SearchDaemon is ported separately.

### `version_service.py` (4 methods)
File version history.

| Method            | Disposition | Notes                                            |
|-------------------|-------------|--------------------------------------------------|
| `get_version`     | P           | metastore version lookup                         |
| `list_versions`   | P           | metastore version scan                           |
| `rollback`        | P           | metastore version restore + content_id rewrite   |
| `diff_versions`   | P           | content fetch + Rust diff (similar to `nexus_fs_content` edit) |

**4 P.**

### `audit_rpc.py` + `snapshots_rpc.py` (8 methods)
Already partly in `rust/services/audit/`.

| Method                  | Disposition | Notes                                       |
|-------------------------|-------------|---------------------------------------------|
| `audit_list`            | P           | extend `rust/services/audit/`               |
| `audit_export`          | P           | extend `rust/services/audit/`               |
| `snapshot_create`       | P           | wraps `workspace_snapshot` (managed_agent)  |
| `snapshot_list`         | P           | metastore scan                              |
| `snapshot_restore`      | P           | wraps `workspace_restore` (managed_agent)   |
| `snapshot_get`          | P           | metastore lookup                            |
| `snapshot_commit`       | P           | finalize a snapshot session                 |
| `snapshot_list_entries` | P           | metastore prefix scan                       |

**8 P.**

### `agent_rpc_service.py` (8 methods)
Wraps managed_agent, which is already Rust.

| Method               | Disposition | Notes                                              |
|----------------------|-------------|----------------------------------------------------|
| `register_agent`     | P           | trivial — extend `managed_agent` Rust service      |
| `update_agent`       | P           | metastore put                                      |
| `list_agents`        | P           | metastore scan                                     |
| `get_agent`          | P           | metastore get                                      |
| `delete_agent`       | P           | metastore tombstone                                |
| `agent_transition`   | P           | state-machine transition — Rust                    |
| `agent_heartbeat`    | P           | metastore put with TTL                             |
| `agent_list_by_zone` | P           | filtered scan                                      |

**8 P.**

### `workspace_rpc_service.py` (13 methods)

| Method                  | Disposition | Notes                                            |
|-------------------------|-------------|--------------------------------------------------|
| `workspace_snapshot`    | P           | wraps `managed_agent`                            |
| `workspace_restore`     | P           | wraps `managed_agent`                            |
| `workspace_log`         | P           | audit-trail filter                               |
| `workspace_diff`        | P           | content diff                                     |
| `snapshot_begin`        | C           | == `workspace_snapshot` (overlap)                |
| `snapshot_commit`       | C           | == `snapshots_rpc.snapshot_commit`               |
| `snapshot_rollback`     | C           | == `workspace_restore`                           |
| `load_workspace_config` | P           | metastore get                                    |
| `register_workspace`    | P           | metastore put                                    |
| `unregister_workspace`  | P           | metastore tombstone                              |
| `update_workspace`      | P           | metastore put                                    |
| `list_workspaces`       | P           | metastore scan                                   |
| `get_workspace_info`    | P           | metastore get                                    |

**10 P, 3 C.**

### `governance_rpc.py` + `pay_rpc.py` + `events_rpc.py` (7 methods)

| Method                | Disposition | Notes                                            |
|-----------------------|-------------|--------------------------------------------------|
| `governance_alerts`   | P           | governance event-stream subscriber               |
| `governance_rings`    | P           | trust ring registry                              |
| `governance_status`   | P           | health summary                                   |
| `pay_balance`         | P           | wallet balance — small port                      |
| `pay_transfer`        | P           | x402 transfer — relies on `nexus.bricks.pay`     |
| `pay_history`         | P           | metastore scan                                   |
| `events_replay`       | P           | event-stream replay — Rust audit subscriber      |

**7 P.**

### `metadata_export.py` + `user_provisioning.py` (4 methods)

| Method              | Disposition | Notes                                            |
|---------------------|-------------|--------------------------------------------------|
| `export_metadata`   | P           | metastore dump (admin)                           |
| `import_metadata`   | P           | metastore load (admin)                           |
| `provision_user`    | P           | user record + workspace bootstrap                |
| `deprovision_user`  | P           | user record + workspace teardown                 |

**4 P.**

## NexusFS Tier 2 audit

### `nexus_fs_content.py` (9 methods)
| Method         | Disposition | Notes                                                       |
|----------------|-------------|-------------------------------------------------------------|
| `read_bulk`    | P           | move to PyKernel (`Kernel::_read_batch` exists already)     |
| `read_range`   | C           | == `sys_read` with `count`+`offset`                         |
| `stream`       | F           | streaming through gRPC server-streaming RPC, port carefully |
| `stream_range` | F           | same — streaming                                            |
| `write_stream` | F           | streaming                                                   |
| `append`       | C           | == `sys_write` with computed offset (current size)          |
| `edit`         | P           | search/replace — Rust impl already in `rust/shared/lib/src/edit/` |
| `write_batch`  | P           | move to PyKernel (`Kernel::_write_batch` exists)            |
| `read_batch`   | P           | move to PyKernel (`Kernel::_read_batch` exists)             |

**4 P, 2 C, 3 F.**

### `nexus_fs_metadata.py` (10 methods)
| Method                       | Disposition | Notes                                                      |
|------------------------------|-------------|------------------------------------------------------------|
| `get_top_level_mounts`       | C           | == `sys_readdir` of `/`                                    |
| `get_content_id`             | C           | == `sys_stat` returning `content_id` field                 |
| `stat`                       | C           | == `sys_stat`                                              |
| `stat_bulk`                  | P           | move to PyKernel batch (analogous to `_read_batch`)        |
| `exists_batch`               | P           | move to PyKernel batch                                     |
| `metadata_batch`             | P           | move to PyKernel batch                                     |
| `delete_batch`               | P           | move to PyKernel batch (`Kernel::_delete_batch` exists)    |
| `rename_batch`               | P           | move to PyKernel batch                                     |
| `backfill_directory_index`   | P           | move to PyKernel admin op                                  |
| `flush_write_observer`       | P           | move to PyKernel admin op                                  |

**7 P, 3 C.**

### `nexus_fs_watch.py` (1 method)
| Method      | Disposition | Notes                                          |
|-------------|-------------|------------------------------------------------|
| `sys_watch` | P           | move to PyKernel — file watch syscall          |

**1 P.**

## Rollup

| Bucket                                | Service-tier (110+) | NexusFS Tier 2 (20) | Total |
|---------------------------------------|---------------------|---------------------|-------|
| **P** Port to Rust                    | 80                  | 12                  | 92    |
| **C** Collapse into existing syscall  | 30                  | 5                   | 35    |
| **D** Delete as deadcode              | 2                   | 0                   | 2     |
| **F** FFI fallback (Rust + PyO3 impl) | 16                  | 3                   | 19    |
| **TOTAL**                             | 128                 | 20                  | 148   |

The active port surface is **92 P + 19 F = 111 methods** of real Rust
implementation work; the rest collapses into existing syscalls or
deletes outright.

## Execution order

Ordered by complexity / risk:

1. **federation_rpc** (5P, 6C) — start here; mostly thin wrappers,
   high collapse ratio, validates the per-service Rust port pattern.
2. **mount_service** (5P, 8C) — heavy collapse; helps migrate
   non-trivial Tier 2 callers to syscalls.
3. **simple services** — `version_service`, `events_rpc`,
   `governance_rpc`, `pay_rpc`, `user_provisioning`,
   `metadata_export`, `audit_rpc`, `snapshots_rpc`. Total ≈ 18 P
   methods, all small.
4. **agent_rpc + workspace_rpc** (10+8 P, 3 C) — wrap existing
   `managed_agent` Rust service.
5. **share_link** (6 P) — pure crypto port.
6. **search_service** (3 P, 4 F, 1 C) — glob/grep go Rust; semantic
   stays Python via FFI.
7. **rebac_service** (16 P, 9 C, 2 D) — biggest service but lots of
   collapse + dead drops.
8. **credential_service / oauth** (3 P, 3 F) — oauth crates +
   `google-auth` FFI fallback.
9. **mcp_service** (1 P, 5 F) — `rmcp` crate + Python FFI for
   subprocess pieces.

After the per-service ports land:

10. **NexusFS Tier 2** — port batch / streaming / `sys_watch` to
    PyKernel; collapse trivial Tier 2 helpers into thin syscall calls.
11. **Delete the Python RPC envelope** — `VFSCallDispatcher`,
    `_kernel_syscall_dispatch.py` (the thin dispatcher just built —
    also envelope-internal), `dispatch.py`, `handlers/`,
    `_rpc_params_*.py`, `generate_rpc_params.py`,
    `rpc_decorator.py`, `parse_method_params`.  Rust tonic Call
    handler drops its Python-fallback branch.
12. **Migrate in-process callers** (factory / lifecycle / runtime)
    from `nx.service("xxx").method(...)` indirection to direct PyO3
    or `nx_kernel_dispatch_rust_call`.  `RemoteServiceProxy` keeps
    serving cross-process clients via the unchanged generic Call
    envelope.
13. **Delete HTTP `/api/nfs/<method>` route** and migrate Win/Mac
    smoke scripts to gRPC.
14. **Verify** — full e2e + Win/Mac smoke + cc-tasks share workload.

## Boot pattern (open question)

`rust/services/python/mod.rs` currently uses explicit
`nx_managed_agent_install` / `nx_acp_install` / `audit install`
PyO3 hooks called from `_wired.py`.  After all services move to
Rust, two options:

* **A. Continue explicit installs** — each new Rust service exports
  an `nx_<svc>_install(py_kernel)` PyO3 function; Python boot calls
  them.  Matches today's pattern.
* **B. `Kernel::new` auto-registers** — `Kernel::new()` constructs
  every built-in Rust service and registers it.  No Python-side
  boot hooks.  Cleaner but ties kernel construction to the full
  service set.

**Recommendation: A** for the migration period (each service can be
landed independently without a kernel-construction rewrite); revisit
B as a follow-up once the surface is stable.

## Auth / zone-scoping cross-cutting

Today the Python servicer's `Call` handler runs:

* token validation (already mostly in Rust via `resolve_context`)
* `search_delegation` allowlist check (Python — federation tokens'
  per-method gate)
* `scope_params_for_zone` (Python — zone-prefix injection on `path`)
* per-RPC `permission_enforcer.check(WRITE/...)` — runs inside each
  NexusFS / service method

When the Python servicer goes away, we need:

* tonic interceptor / tower middleware for token validation +
  search-delegation allowlist (one Rust impl, applied to every
  typed RPC + the generic Call)
* zone-prefix scoping moves into each Rust service / kernel syscall
  (consistent with where the enforcer check lives today)
* `permission_enforcer` already has Rust callers (`rust/shared/lib/src/rebac/`),
  so per-method `check` calls are straight-forward

## What's NOT in this PR

* Rust port of `nexus.bricks.pay.x402_*` infra (financial logic — separate concern)
* Rust port of `txtai` semantic-search backend (FFI fallback used)
* Rust port of MCP subprocess management (`rmcp` covers protocol; subprocess lifecycle FFI)
* Migration of Python-only callers' import surface (factories,
  bricks/test scaffolding) — many of these use `nx.service("xxx")`
  which keeps working since Rust services register through the
  same registry
