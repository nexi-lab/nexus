# Federation Write Consistency Surface

**Status:** Active (2026-06-23).  Lands in:
- nexi-lab/nexus-vfs PR #61 (the runtime change + raft-level code + federation-architecture doc)
- nexi-lab/nexus PR (this repo): runbook + E2E coverage + this spec

**Scope:** How sys_setattr / sys_unlink metadata writes route between
EC (eventually consistent, no quorum) and SC (strongly consistent, raft
consensus) on the federation surface.  Captures the architectural
decision behind the PR pair and the operator-facing CLI change
(`nexusd-cluster join --as voter|learner`).

---

## TL;DR

* `ZoneMetaStore::put` / `delete` (the kernel `sys_setattr` /
  `sys_unlink` hot path) routes through `ZoneConsensus::propose_ec_local`
  — WAL-first append, synchronous local state-machine apply, async raft
  replication.  Any node — voter, learner, leader, follower — can write
  metadata locally without quorum.
* `put_if_version` (CAS), `append_stream_entry` (WAL), `DistributedLocks`
  keep `ZoneConsensus::propose` (SC) because they need linearizability EC
  can't provide.
* `nexusd-cluster join` grows `--as voter|learner` (default `learner`
  for backward compat).  The role choice now governs SC quorum
  participation + wipe-rejoin safety, **not** sys_setattr write
  capability — both roles can write EC.

---

## Why now

The 2026-06-22 Mac↔Win cross-machine smoke (Federation L1 byte-exact
read over Tailscale) was the first time the bidirectional symmetric
peer pattern hit the federation surface end-to-end.  The runbook §3b
join flow was building Mac as a Learner per the canonical
share-with-readers shape, and `vfs_write` from Mac surfaced `not
leader, peers=0` — `ZoneMetaStore::put` hardcoded SC propose which
needs a leader.

The user's framing for the fix:

> 两边都是 voter 的代价是，必须都在线才能写，否则不能写吗？为什么？我理解写的时候改为 EC 不就行了吗？我认为我们当前的设计是支持 per-write level EC/SC decision 的。

Confirmed: the architecture had `Consistency::Ec | Sc` as a per-call
parameter on `zone_handle.rs::set_metadata` / `delete_metadata` since
the EC WAL landed.  `ZoneMetaStore` was the only caller still
hardcoding SC.  The fix is changing the call site, not adding new
plumbing — that's why this lands as a one-PR-per-repo pair rather
than a multi-phase architecture migration.

---

## The two propose paths

| Path | Latency | Quorum | RYW | Use |
|------|---------|--------|-----|-----|
| `propose_ec_local` (EC) | ~5–50 µs (local redb + WAL) | None — every node writes locally | Preserved on the writer node (local apply synchronous) | sys_setattr / sys_unlink (the kernel hot path) |
| `propose` (SC) | ~5–10 ms intra-DC (raft round-trip + majority ACK) | Required | Linearizable across all peers | Locks, CAS, WAL stream appends, control-plane (mount install / ConfChange) |

The split is per-call, not per-zone.  Same `ZoneMetaStore` instance
exposes both surfaces depending on which trait method the caller
invokes.

EC conflict resolution: last-writer-wins on `modified_at_ms`.  Safe
for workflows where each peer owns its own subpath (cc-tasks-share:
`/shared/cc-tasks/<host>/...`); explicit-conflict workflows need to
design around LWW (write a lock first via SC if mutex semantics
matter).

---

## Failure modes the change closes

| Pre-#61 symptom | Root cause | Post-#61 fix |
|-----------------|-----------|--------------|
| Joiner `vfs_write` returns `NotLeader` when joiner is a Learner | `ZoneMetaStore::put` → `propose` (SC) → Learner can't propose | `put` → `propose_ec_local` → Learner commits locally |
| 2-voter cluster with one peer offline can't write anything (quorum loss) | All writes routed SC | sys_setattr writes route EC → single-peer-online keeps writing; only SC ops (locks, CAS) block on quorum |
| Operator confusion: "I joined and it says learner — can I write?" | `as_learner=true` was wired into `run_join` as a fixed contract | `--as voter|learner` operator surface, both roles can write EC |

---

## Failure modes the change deliberately does **not** close

* **Two peers writing the same path concurrently → LWW** — by design.
  EC is the right model for the kernel hot path; callers that need
  mutex semantics must use SC locks (`DistributedLocks`).  The
  cc-tasks-share workflow has zero contention because each peer owns
  its subpath.
* **Wipe-rejoin of a voter** — voter joiners re-introduce the
  pre-PR #57 hazard if they go through SSD swap without first
  transferring their voter slot away.  Mitigated by the documentation
  (the `--as voter` runbook callout names this) but not eliminated —
  use `--as learner` whenever wipe-rejoin safety matters more than
  symmetric SC writes.
* **`propose_ec_local` requires `ReplicationLog`** — initialized on
  every node that joins via the standard flows (founder bootstrap,
  `nexusd-cluster join`, `share`).  A node manually constructed
  without a ReplicationLog will return `RaftError::InvalidState("EC
  local writes require a ReplicationLog")` from `put` — not a
  silent failure.

---

## Operator decision matrix (mirrors runbook §3b)

| Workload | `--as` | Why |
|----------|--------|-----|
| Single authoritative writer publishing to many mirror peers (canonical `nexus share` semantics) | `learner` (default) | Wipe-rejoin safe; learner can still write `sys_setattr` metadata locally if it needs to |
| Symmetric 2-peer share, each writes its own subpath (cc-tasks-share Mac↔Win) | `voter` | Equal quorum authority for SC writes; EC-routed sys_setattr keeps working when one peer offline |
| Root cluster bootstrap (≥3 voters + optional witness) | `voter` (used by founder + every joiner) | Quorum essential; single-node loss must not lose quorum |

---

## Test coverage

* `nexus-vfs` `rust/raft/src/zone_meta_store.rs` →
  `put_and_delete_succeed_without_leader` — regression unit test that
  pins the EC contract: put + delete must succeed on a node that
  intentionally skips `node.campaign().await`.
* `nexus` `tests/e2e/docker/test_federation_runbook.py` →
  `TestFederationWriteConsistencyContract` —
    - `test_learner_joiner_writes_via_ec_when_founder_offline`:
      stops founder, joiner writes via gRPC, founder comes back,
      reads byte-exact.
    - `test_join_as_voter_flag_round_trips_through_cli`: cheap
      regression sentinel on the new `--as` CLI flag.

---

## Lineage / pointers

* nexus-vfs PR #57 (2026-06-XX) — landed Learner-default-on-join to
  fix the F5 wipe-rejoin quorum stall.  This spec extends the model:
  Learner is still the safe-by-default role for owner-pattern shares;
  Voter is a per-zone operator opt-in via `--as voter`.  Per-call EC
  routing makes the role choice about quorum participation + wipe-
  rejoin safety, not about who can write metadata.
* nexus-vfs `propose_ec_local` (rust/raft/src/raft/node.rs) — the EC
  write primitive this spec activates on the kernel hot path.  Was
  already first-class but only the `zone_handle::set_metadata` /
  `delete_metadata` API surfaced it; `ZoneMetaStore` was the missing
  caller.
* Companion runbook section: `docs/architecture/federation-cross-machine-runbook.md`
  "Consistency model" (between §3g and §4).
* Manual smoke that motivated the change: 2026-06-22 Mac↔Win
  Federation L1 over Tailscale.  Substrate proven both directions
  byte-exact before this change; this change closes the
  bidirectional-write gap so the same substrate works for
  cc-tasks-share's symmetric peer pattern.
