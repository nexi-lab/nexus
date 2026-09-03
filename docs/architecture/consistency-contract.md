# Consistency Contract: kernel, server, tenant

Issue: nexi-lab/nexus#4737 (read-your-writes revision token). Companions:
`mount-routing-ssot-gap.md` (mount table vs metastore), #4736 (write → searchable,
`index_seq`), #4741 (conformance suite).

This document is the published consistency contract for a Nexus deployment.
Section 2 records what the pinned kernel (`nexus-vfs` at the rev in
`Cargo.lock`) guarantees today, with file pointers so the claims can be
re-verified. Section 3 defines the **revision token** the server exposes,
which needs nothing beyond that kernel. Section 5 lists optional kernel
work that would extend the token. Cross-node read fixes that were previously
narrated only in `Cargo.toml` comments (PR #108 `try_remote_fetch` for
`sys_readdir`, PR #138 "read ⊥ cache ⊥ materialize", content_id stamping)
are summarised in §2.3 and §2.4; the `Cargo.toml` comments remain a
changelog, not a contract.

## 1. Vocabulary

| Term | Meaning |
|------|---------|
| zone | One raft group with its own log and state machine (`root`, `corp-eng`, …). A zone is mounted into the global VFS at one or more paths (`DT_MOUNT` rows). |
| `gen` | Per-path content generation stored in the file's metadata row; increments on every write to that path and is returned by `write` and `stat`. |
| `commit_index` | Highest log index a quorum has durably accepted. May run ahead of the state machine (`raft/src/raft/node.rs`, `commit_index()` doc: "Do not gate reads on this value"). |
| `applied_index` | Highest log index the **local** state machine has applied. "A reader that sees `applied_index >= N` is guaranteed to also see every state-machine effect of log entries with `index <= N`" (`node.rs`, `applied_index()`). |
| revision token | `<anchor>@<index>`: `/ws/a.txt@7` (path + gen, what writes return) or `root@1234` (zone + applied_index, optional). §3. |
| mount table | `MountTable::entries` (Rust, in-memory, per kernel) that `route()` consults to pick the metastore for a path. |
| dcache | Kernel dentry/metadata cache in front of the per-zone state machine. |

## 2. What the kernel guarantees today

### 2.1 Mutations are strongly consistent on the leader

`sys_write`, `sys_unlink`, `sys_rename`, `sys_mkdir`, `sys_setattr` on a
federation mount go through `ZoneMetaStore` → `ZoneConsensus::propose`
(`raft/src/zone_meta_store.rs`, "Write consistency": put / delete / CAS /
`append_stream_entry` are SC; the EC plane is opt-in for metadata registers
only). A proposal's completion is signalled from `apply_entries`
(`node.rs`, `proposal.tx.send(Ok(result))` inside `apply_entries`), so:

* When a mutation RPC returns on the **leader**, the leader's state machine
  has applied it.
* When the mutation was issued on a **follower**, the proposal is forwarded
  (`forward_to_leader`) and the reply comes from the leader. The follower's
  own state machine may still be behind when the RPC returns. Writing on a
  follower and immediately reading on the same follower is therefore *not*
  read-your-writes without the fence in §3.
* Raft applies committed entries **in log order** on every node. This is
  what makes a per-path fence sufficient for whole-zone visibility (§3.4).

### 2.2 Metadata reads are local (sequentially consistent)

`sys_stat`, `sys_readdir`, `exists`, list and every other metadata lookup
read the **local** state machine (`node.rs`, `with_state_machine`: "on a
follower may be behind the leader by up to the replication lag,
ZooKeeper-default style"). Only `get_lock` / `list_locks` use the ReadIndex
protocol (`read_linearizable`). A follower can legitimately return
`found=false` / `metadata: None` for a path the leader has already applied.
That is the gap the revision token closes.

### 2.3 Content reads: local backend, then origin fetch, no cache-back

`sys_read` resolves metadata locally, then reads content from the mount's
backend by `content_id` (`kernel/src/kernel/syscall_impl.rs`, step 5/7). On a
backend miss with metadata present it calls `try_remote_fetch`: the
*virtual path* is sent to the node named in `last_writer_address` via the
`ReadBlob` RPC and that peer self-routes through its own `VFSRouter`. The
fetch is a pure read — **no local cache-back** (nexus-vfs PR #138) — and
returns `FileNotFound` when `last_writer_address` is unset, equals this
node, or the remote call fails. `sys_readdir` has the same
"sender dispatches, remote handles" peer path (PR #108). A writer-side node
reading its own placeholder entry serves bytes from the federation cache
(`federation_cache_substitution_read`).

Consequence: once metadata is applied locally (§3 fence satisfied), a read
on any node either returns the bytes or fails loudly; it does not return
stale bytes for the new `content_id`.

### 2.4 dcache invalidation is apply-side

Each zone installs an apply-side invalidation callback
(`Kernel::install_zone_apply_invalidator`, wired from
`ZoneMetaStore::new`; `raft/src/raft/state_machine.rs`
"Apply-side invalidation callback — fires once per committed metadata
mutation"). The callback fans out to every mount point that shares the
zone's `coherence_key` (`kernel/src/core/vfs_router.rs`,
`mount_points_for_coherence_key`), so crosslink mounts of one zone are
invalidated together. A dcache entry therefore cannot outlive the apply of
the mutation that changed it, and a `sys_stat` that shows the new `gen`
is not a cache artefact.

### 2.5 Mount table can lag the metastore

Mount topology lives in two stores (raft `DT_MOUNT` rows and
`MountTable::entries`) joined by a Rust → Python → Rust callback chain. Until
the chain has run on a freshly (re)started follower, `route()` misroutes and
`sys_stat` returns `metadata: None` **even when the raft log has caught
up**. Full analysis and the kernel-side fix are in
`mount-routing-ssot-gap.md`. Under the §3 fence this window is *visible*
rather than silent: the anchor path stats as absent, so the fenced read
waits and then answers 412 instead of an empty result.

### 2.6 What the pinned kernel does *not* expose

* No gRPC response carries `applied_index`. `WriteResponse` is
  `{content_id, size, gen}`; `StatResponse` has `gen` but no zone revision.
* `Call("federation_cluster_info")` is **not** served by `nexusd-cluster`
  (`rust/transport/src/call_dispatch.rs` only dispatches agent / mount-point
  / service ops; anything else is `unknown Call method`). The Python
  `FederationRPCService.federation_cluster_info` therefore returns the
  standalone stub with `applied_index: 0`.
* `stat("/__sys__/zones/<id>")` is a placeholder (`version: 0`, `gen: 0`).
* `_nexus_raft.pyi` (`is_committed`, `set_metadata(consistency=…)`)
  describes the retired PyO3 module. Python talks to the kernel over gRPC
  (`KernelClient`), so `is_committed` is unreachable from Python by
  construction — the "zero callers" observation in #4737 is structural.

None of this blocks the token: the per-path `gen` that `write` and `stat`
already return is enough (§3).

## 3. Revision token (server ↔ tenant)

Implemented in `nexus` (`src/nexus/lib/zone_revision.py`,
`src/nexus/server/api/v2/_revision_fence.py`). Works on the pinned kernel.

### 3.1 Token

```
revision = "<path>@<gen>"          e.g. /ws/a.txt@7      (returned by writes)
revision = "<zone_id>@<index>"     e.g. root@1234        (optional, kernel-stamped)
```

The anchor is everything before the **last** `@`, so paths containing `@`
are fine. An anchor starting with `/` is a path; anything else is a zone
id; a bare integer is a zone token for `root`. Only the path form is
emitted today; the zone form is accepted so a kernel that later stamps
`zone_id` / `applied_index` on mutation responses (§5) needs no server
change.

### 3.2 Where mutations return it

| Surface | Field | Header |
|---------|-------|--------|
| `POST /api/v2/files/write` | `revision` | `X-Nexus-Revision` |
| `POST /api/v2/files/batch/write` | `results[].revision` | — |
| `POST /api/v2/files/copy` | `revision` (destination write) | `X-Nexus-Revision` |
| `DELETE /api/v2/files/delete`, `POST /api/v2/files/rename` | `revision` | `X-Nexus-Revision` |
| `NexusFS.write / sys_write / write_batch` | `"revision"` key in the returned dict | — |

Writes always carry `/path@gen`. Delete and rename have no gen to anchor on
and return `null` unless the kernel stamps a zone revision; a tenant that
needs to fence a delete waits for the parent listing to drop the entry, or
fences on the next write it makes. `mkdir` returns `None` on the Python API
and is a follow-up.

### 3.3 How reads consume it

Request (either form; header wins):

```
X-Nexus-Min-Revision: /ws/a.txt@7      ?min_revision=/ws/a.txt@7
X-Nexus-Revision-Timeout-Ms: 5000      ?revision_timeout_ms=5000
```

Default timeout 5 000 ms, maximum 30 000 ms, `0` probes once. Applied to:

* files: `read`, `metadata`, `exists`, `list`, `glob`, `grep`, `stream`,
  `batch-read`, `batch/read`
* search: `query`, `query/batch`, `grep` (GET and POST), `glob` (GET and POST)

The fence runs `sys_stat(anchor)` on the serving node **under the caller's
OperationContext** (permission hooks apply, so it is not an existence
oracle for paths the caller cannot read) and compares `gen`:

| Outcome | Status | Body / headers |
|---------|--------|----------------|
| `stat(anchor).gen >= index` within the timeout | 200 (normal response) | `X-Nexus-Revision: <anchor>@<observed gen>` |
| Not reached before the timeout (path absent counts as gen 0) | **412 Precondition Failed** | `detail.error = revision_not_applied`, `detail.min_revision`, `detail.current_revision`, `detail.waited_ms`; header `X-Nexus-Revision: <anchor>@<current>` |
| Zone token on a kernel that cannot report `applied_index` | **501 Not Implemented** | `detail.error = zone_revision_unavailable` |
| Zone token, kernel probe failed (transport) | 503 | `detail.error = zone_revision_probe_failed` |
| Malformed token / timeout | 400 | — |

A fenced read never answers with `metadata: None` or an empty listing
because the node is behind: it waits, then either serves state that
includes the write or says 412 with the revision it *does* have. Unfenced
reads are unchanged (no header, no extra round-trip).

### 3.4 What the fence guarantees

* **Same path**: `read`, `metadata`, `exists`, `stream` of the anchor see
  the write's metadata and `content_id` (§2.2, §2.4).
* **Same zone**: because raft applies in order (§2.1), a node whose stat
  shows the anchor at `gen >= G` has applied every earlier entry of that
  zone. `list`, `glob`, `grep` and `search` over that zone therefore
  include the write and everything the same client wrote before it.
* **Content**: `sys_read` may still fetch bytes from the origin node
  (§2.3). It returns the written bytes or fails; never older bytes.
* **Search**: `min_revision` on `/search/*` fences the VFS state the search
  plugin reads from. Whether the write is *indexed* is the `index_seq`
  contract of #4736 (`/search/stats.last_index_seq >= index_seq` returned by
  index-on-write). A tenant that needs "written and searchable" checks both.
* **Other zones**: a token fences only the zone that owns its anchor. A
  listing that spans mounts of several zones is fenced for the anchor's
  zone only.

Known edge: a path that is deleted and re-created starts a new `gen`
sequence, so a token from the old life may never be satisfied and yields
412 after the timeout. Use the token from the latest write.

### 3.5 Tenant guidance

1. Keep `revision` from every write response next to the `content_id`.
2. Send it as `X-Nexus-Min-Revision` on the read, list or search that must
   observe the write (any node behind the same load balancer).
3. `412` means "this node has not applied it yet": retry after the returned
   `current_revision` advances, or route to another node.
4. Batches: fence on the token of the item you are about to read, or on
   the last item's token to cover the whole batch.

Example:

```bash
curl -s -X POST "$NEXUS/api/v2/files/write" -H "Authorization: Bearer $KEY" \
  -d '{"path":"/ws/a.txt","content":"hello"}'
# {"content_id":"…","version":3,"size":5,"modified_at":"…","revision":"/ws/a.txt@3"}

curl -s -D - "$NEXUS/api/v2/files/list?path=/ws" -H "Authorization: Bearer $KEY" \
  -H "X-Nexus-Min-Revision: /ws/a.txt@3"
# HTTP/1.1 200 OK            (or 412 with current_revision)
# X-Nexus-Revision: /ws/a.txt@3
```

## 4. Readiness

`/healthz/ready` is unchanged by #4737. While a freshly restarted follower's
mount table lags the metastore (§2.5) fenced reads on that node answer 412,
so the window is observable and retryable rather than silent. Gating
readiness on mount-table convergence needs a kernel signal (§5 item 3) and
stays with `mount-routing-ssot-gap.md`.

## 5. Optional kernel work (nexus-vfs)

Nothing here is required for §3. Each item widens the token and is
consumed by the server already, tolerant of absence:

1. **Stamp mutations with a zone revision.** Add `string zone_id` and
   `uint64 applied_index` to `WriteResponse`, `BatchWriteItemResponse`,
   `DeleteResponse`, `RenameResponse`, `MkdirResponse`, `CopyResponse`
   (`proto/nexus/grpc/vfs/vfs.proto`). Gives delete and rename a token and
   lets a client fence "everything so far" with one number instead of a
   path. `KernelClient` reads the fields via `revision_fields()` and
   `revision_from_result()` prefers them over the path form.
2. **Serve `Call("federation_cluster_info", {"zone_id"})`** from
   `call_dispatch.rs` → `DistributedCoordinator::cluster_info` as JSON
   (`zone_id, node_id, has_store, is_leader, leader_id, term, commit_index,
   applied_index, voter_count, witness_count, links_count`). This is what
   `read_zone_revision` polls for zone tokens; without it they answer 501.
3. **Mount convergence signal** (`Call("federation_mount_convergence")` or
   a `/__sys__/mounts` view) plus the apply-side `add_mount` callback from
   `mount-routing-ssot-gap.md`, so readiness can gate on it.
4. **Retire `_nexus_raft.pyi`** or mark it as the description of an
   internal Rust API; nothing in `src/` can import it.

## 6. Acceptance (tracked in #4741)

* Embedded, remote and 2-node federated: write returns `revision = R`;
  `read`, `list`, `glob`, `grep` and `search/query` with `min_revision = R`
  observe the write, on the leader and on the follower.
* A follower asked for a revision it has not applied within the timeout
  returns 412 with `current_revision`, never `metadata: None`.
* A zone token on a kernel without §5 items 1–2 is 501; no test may pass by
  treating that as success.
