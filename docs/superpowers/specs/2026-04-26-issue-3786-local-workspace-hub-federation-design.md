# Design: Local Workspace + Remote Hub Federation in Thin Client

**Issue:** #3786
**Epic:** #3777 (Phase 3-3)
**Depends on:** #3778 (lightweight profile, merged), #3784 (hub mode, merged)
**Date:** 2026-04-26

---

## Problem

An agent working on an engineer's code needs both local files (the project being edited) and company knowledge (docs, APIs, policies from the hub). A thin nexus instance inside a sandbox must federate with a remote hub to serve both without sacrificing local disk speed.

---

## Approach

**gRPC Proxy Zone** — thin client has three zones backed by two backend types:

- `local` → `PathLocalBackend(workspace)` — read-write, disk speed, never on hub
- `company` → `RemoteZoneBackend(hub, perm=r)` — read-only, proxied via gRPC
- `shared` → `RemoteZoneBackend(hub, perm=rw)` — read-write, writes back to hub

`ZoneSearchRegistry.register_remote()` (already exists) wires remote zones into federated search. No Raft peer membership — hub stays clean.

Alternatives rejected:
- **Full Raft + DT_MOUNT**: too heavy for ephemeral sandboxes, bloats hub peer table
- **MCP-only**: bypasses search layer entirely, no unified ranking

---

## Architecture

```
nexus up --profile sandbox \
  --workspace ~/myapp \
  --hub-url https://hub.co \
  --hub-token $TOKEN          (or NEXUS_HUB_TOKEN env var)

Sandbox process (SQLite, lightweight profile #3778)
┌─────────────────────────────────────────────────────┐
│  Zone: local   → PathLocalBackend(~/myapp)    r/w   │
│  Zone: company → RemoteZoneBackend(hub)       r     │
│  Zone: shared  → RemoteZoneBackend(hub)       r/w   │
│                                                     │
│  ZoneSearchRegistry                                 │
│    local   → LocalSearchDaemon (BM25S)              │
│    company → register_remote(RPCTransport)          │
│    shared  → register_remote(RPCTransport)          │
│                                                     │
│  FileWatcherIndexer: ~/myapp → local daemon         │
│  BootIndexer: walks ~/myapp on first start          │
└──────────────────┬──────────────────────────────────┘
                   │ gRPC (bearer token, per-zone perms)
         ┌─────────▼────────────────┐
         │  Nexus Hub               │
         │  Zone: company  (r)      │
         │  Zone: shared   (r/w)    │
         └──────────────────────────┘
```

---

## Components

### `RemoteZoneBackend` — `src/nexus/backends/storage/remote_zone.py`

Wraps `RPCTransport` to proxy read (and optionally write) ops to a hub zone. Initialized with `zone_id`, `transport`, and `permission` (`r` or `rw`). Implements the same backend interface as `PathLocalBackend`. Write ops check permission first — raises `ZoneReadOnlyError` before any RPC if permission is `r`.

### `FederationHandshake` — `src/nexus/remote/federation_handshake.py`

Called at boot. Authenticates to hub via bearer token. Returns `HubSession` containing transport + list of `{zone_id, permission}` pairs for the token's allowed zones.

Failure modes:
- 401 → `HandshakeAuthError`
- Unreachable → `HandshakeConnectionError`

Both are non-fatal: sandbox boots in local-only mode with a `WARN` log.

Note: zone IDs (`company`, `shared`) are not hardcoded — they come from the hub token's allowed zone list. The names above are illustrative; a token might grant `{eng, r}` and `{scratch, rw}` instead.

### `SandboxBootstrapper` — `src/nexus/daemon/sandbox_bootstrap.py`

Orchestrates the full sandbox boot sequence:

1. Create `local` zone → `PathLocalBackend(workspace)`
2. Run `FederationHandshake(hub_url, token)` → `HubSession`
3. For each `{zone_id, permission}` in session: create `RemoteZoneBackend`, register zone
4. Register all zones in `ZoneSearchRegistry` (local → local daemon; remote → `register_remote`)
5. Start `BootIndexer` in background thread

### `BootIndexer` — `src/nexus/core/boot_indexer.py`

Walks workspace directory on first boot, feeds files to local search daemon. Runs in background thread. Updates `/health` state: `indexing` → `ready` on completion. After initial walk, hands off to existing `FileWatcherIndexer` for incremental updates.

Failure handling: if workspace walk fails (permissions, missing dir), logs error and transitions to `ready` anyway — partial index is acceptable, `FileWatcherIndexer` fills gaps on access.

### `nexus up` flag additions — `src/nexus/cli/commands/stack.py`

| Flag | Env var | Notes |
|---|---|---|
| `--workspace PATH` | `NEXUS_WORKSPACE` | Local dir to index and mount as `local` zone |
| `--hub-url URL` | `NEXUS_HUB_URL` | Hub gRPC endpoint |
| `--hub-token TOKEN` | `NEXUS_HUB_TOKEN` | Bearer token (prefer env var over flag for shell history) |

All three flags are only valid with `--profile sandbox`. CLI rejects at startup if used without it. `--hub-url` without a token (flag or env) is also rejected.

---

## Data Flow

### Boot sequence

```
nexus up --profile sandbox --workspace ~/myapp --hub-url ... --hub-token ...
  │
  └─ SandboxBootstrapper.run()
       ├─ create zone local  → PathLocalBackend(~/myapp)
       ├─ FederationHandshake(hub_url, token)
       │    └─ gRPC auth → [{zone:company, perm:r}, {zone:shared, perm:rw}]
       ├─ create zone company → RemoteZoneBackend(transport, perm=r)
       ├─ create zone shared  → RemoteZoneBackend(transport, perm=rw)
       ├─ ZoneSearchRegistry.register_remote(company, transport)
       ├─ ZoneSearchRegistry.register_remote(shared, transport)
       └─ BootIndexer.start_async(~/myapp)  → health: indexing → ready
```

### Search flow

```
nexus search "query"
  ├─ LocalSearchDaemon.search(query)              → results, zone=local
  ├─ RPCTransport.search(query, zone=company)     → results, zone=company
  ├─ RPCTransport.search(query, zone=shared)      → results, zone=shared
  └─ RRF merge + re-rank

Output:
  0.92  [local]    src/auth/middleware.py
  0.87  [company]  eng/docs/auth-policy.md   (read-only)
  0.81  [shared]   notes/auth-review.md
```

### Write flow

```
write to zone:company → ZoneReadOnlyError (client-side, no RPC)
write to zone:shared  → RPCTransport.write(zone=shared) → hub enforces token perm → ok
write to zone:local   → PathLocalBackend.write() → disk → FileWatcherIndexer re-indexes
```

---

## Error Handling

| Scenario | Behavior |
|---|---|
| Hub unreachable at boot | `WARN: hub federation unavailable`, local-only mode, no crash |
| Token rejected (401) at boot | Same as above |
| Hub goes offline mid-session | `ZoneUnavailableError` on reads/writes; search degrades gracefully (local results still returned) |
| Token expiry (401 mid-session) | Remote zones marked unavailable, one `WARN` log, no retry until restart |
| Write to `company` zone | `ZoneReadOnlyError` client-side, no RPC fired |
| BootIndexer walk failure | Log error, health transitions to `ready` anyway (partial index acceptable) |
| `--workspace` without `--profile sandbox` | CLI error at startup |
| `--hub-url` without token | CLI error at startup |

---

## Testing

### Unit tests

- `RemoteZoneBackend`: write to `r`-permission zone raises `ZoneReadOnlyError` without RPC; write to `rw` delegates to transport
- `FederationHandshake`: success → correct `HubSession`; 401 → `HandshakeAuthError`; unreachable → `HandshakeConnectionError`
- `SandboxBootstrapper`: handshake failure → local-only boot (no crash); success → all three zones registered

### Integration tests

- Full boot with mock hub gRPC server: zones created, search registry populated, health `indexing` → `ready`
- `nexus search` returns merged results with correct `[local]`/`[company]`/`[shared]` labels
- Write to `company` zone rejected client-side; write to `shared` reaches mock hub

### CLI tests

- `nexus up --profile sandbox --workspace /tmp/ws --hub-url ... --hub-token ...`: env vars passed correctly
- `nexus up --workspace /tmp/ws` (no `--profile sandbox`): rejected with clear error

### What we skip

Real hub in CI — mock gRPC server is sufficient. File watcher indexer covered by existing `nexus_fs_watch` tests.

---

## Acceptance Criteria Mapping

| Criterion | Component |
|---|---|
| Lightweight nexus indexes local workspace on boot | `BootIndexer` + `SandboxBootstrapper` |
| Federation handshake with hub completes | `FederationHandshake` |
| `nexus search` returns results from both local + company | `ZoneSearchRegistry` fan-out + RRF merge |
| Local file writes work at disk speed | `PathLocalBackend` (unchanged) |
| Company zone is read-only from sandbox | `RemoteZoneBackend` (client) + hub token perm (server) |
| Search results indicate source | Zone label in search result metadata |
