# Mount Routing SSOT / DRY Gap (open)

Discovered 2026-04-20 while chasing the last remaining federation-E2E
flake (`TestFullFailoverRecovery::test_failover_with_delete_rename_replay`).
The gate I added (`_wait_nodes_caught_up` on `applied_index`) passes
cleanly — the raft state machine has applied every committed entry on
the follower — yet `sys_stat` on the follower still returns
`metadata: None` for paths the leader sees. This is not a raft
protocol issue; it is a pair of **SSOT (data) / DRY (code)**
violations in the kernel mount layer.

## Symptoms

- Leader's `sys_stat /corp/eng/recover-*/doc-renamed.txt` returns
  `{zone_id: 'root', backend_name: 'local@nexus-1:2028', …}`.
- Follower's `sys_stat` on the same path returns
  `{result: {metadata: None}}` after node-1 restart.
- This persists despite `applied_index` on both nodes matching for
  both root AND corp-eng zones.

## Violation 1 (data SSOT): mount routing has two stores

The mount topology is stored in **two** places:

1. **Raft state machine metastore** (authoritative, replicated, crash-safe):
   `DT_MOUNT` metadata entries under per-zone ZoneMetastores.

2. **`MountTable::entries: DashMap<String, MountEntry>`**
   (Rust, in-memory, one per kernel): the structure `route()` consults
   at read time to pick which metastore owns a given path.

These must stay in sync, but (1) is fed by raft apply, and (2) is
populated by a **Rust → Python → Rust** callback chain:

```
state_machine.apply_set_metadata(DT_MOUNT)
 └─► emit_mount_event → mount_event_tx (Rust mpsc)
      └─► run_mount_event_consumer (Rust task, grabs GIL)
           └─► _on_mount_event (Python hook)
                └─► coordinator.mount(...)          (Python)
                     └─► kernel.sys_setattr(DT_MOUNT)  (back into Rust)
                          └─► dlc.mount → kernel.add_mount
                               └─► mount_table.entries.insert  ← (2) populated here
```

Any break in this chain (Python hook not yet registered, coordinator
not wired, target zone not local yet, consumer backlog, GIL wait)
leaves `MountTable::entries` behind the state-machine metastore.
`route()` then misses the DT_MOUNT for `/corp/eng` and resolves the
path against the root mount — whose metastore has nothing at that
path. `sys_stat` returns `metadata: None`.

On follower restart the sequence is:

- `PyZoneManager::start()` synchronously runs
  `open_existing_zones_from_disk` → all zones present + applied up to
  the persisted last_applied.
- FastAPI `/healthz/ready` returns 200 when root_store exists — it does
  not block on mount_table being populated.
- Test proceeds; raft keeps replicating; the Python `_on_mount_event`
  chain fires at its own pace. During the window in between,
  `sys_stat` misroutes.

## Violation 2 (code DRY): 10+ hand-rolled `FileMetadata { zone_id: … }` sites

`rust/kernel/src/kernel.rs` constructs `FileMetadata` inline in
~10 places: `sys_write`, `sys_rename`, `sys_unlink` (the write
sibling), `setattr_create_dir`, `create_pipe`, `create_stream`,
`write_pipe_inode`, `write_stream_inode`, …

Every site stuffs `zone_id: Some(ctx.zone_id.clone())` (caller's
identity zone, always `root` for API calls) or
`zone_id: Some(contracts::ROOT_ZONE_ID.to_string())` hard-coded. The
field therefore does not reflect where the metadata actually lives —
it reflects the caller's ambient zone context, which is decoupled
from the mount routing that picked the destination metastore.

Consequence: the diagnostic
`'zone_id': 'root'` on leader was **misleading** — the entry may
actually be in `corp-eng`'s state machine, but the label says `root`.
Every tool that groups / filters / joins by `FileMetadata.zone_id`
(search, permission checks, audit) is reading a stale copy of the
routing decision.

## Proposed fix (not implemented — scope for follow-up PR)

### SSOT

1. Stop publishing mount changes via Rust → Python → Rust. Add a Rust
   apply-side callback on `FullStateMachine` (parallel to
   `invalidate_cb`) that fires on `Command::SetMetadata` /
   `DeleteMetadata` with `entry_type == DT_MOUNT`. The callback
   directly calls `kernel.add_mount` / `remove_mount` — no GIL, no
   async, no Python coordinator detour. Install it from the same
   place as `install_federation_dcache_coherence`.

2. Python's `_on_mount_event` keeps firing but only for Python-layer
   notifications (audit, search index, event bus). It is no longer
   load-bearing for routing correctness.

3. `MountTable::entries` on a fresh follower then populates
   deterministically before any RPC is served — same semantics as the
   dcache coherence invalidate, which already takes this path.

### DRY

4. Extract one helper — `Kernel::build_metadata(path, kind, …) ->
   FileMetadata` — that resolves the route once and sets
   `zone_id` from `route.mount_point`'s zone (the authoritative
   choice). Replace every inline `FileMetadata { zone_id: …}` with
   the helper. ~15 call sites collapse to one.

5. Eventually: `FileMetadata` itself should not carry `zone_id` — the
   metastore's ZoneMetastore wrapper *knows* its zone and can attach
   it on read. Storing it inside the value is the shadow; the map key
   (mount_point) is the SSOT. Schedule this with the
   `FileMetadata`-field-divergence audit already tracked as v20
   follow-up #18.

## Why the test catches it

Fresh `_wait_nodes_caught_up` gate shows `applied_index` caught up in
~1s after restart — the raft side is fine. The flake is the ~3s window
where the mount-event chain hasn't populated `MountTable::entries`
yet. On slow Docker runs (~180s total suite), the window is wider
and the test hits it deterministically.

## Status

- Architectural analysis: complete.
- Fix: **not** implemented. Needs its own PR with careful migration
  because both the callback install and the `FileMetadata.zone_id`
  refactor touch every write path.
- Workaround for PR #3765: the `applied_index` gate + tight raft
  transport keepalive shrinks the window enough that most runs pass,
  but the test remains flaky on slow hosts.
