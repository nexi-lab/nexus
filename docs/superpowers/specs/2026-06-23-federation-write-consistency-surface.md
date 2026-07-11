# Federation Write Consistency Surface

**Status:** Active (2026-06-23).  Lands in:
- nexi-lab/nexus-vfs PR #61 (per-call EC/SC primitive + initial activation) **+ PR #63 (activation reverted, primitive kept)**
- nexi-lab/nexus-vfs PR #62 (operator CLI flag rename — `--as`, not `--as-role`)
- nexi-lab/nexus PR (this repo): runbook + E2E coverage + this spec

**Scope:** Documents the federation write surface's EC/SC consistency split, the operator-facing `nexusd-cluster join --as voter|learner` flag, and why the kernel hot path stays SC today (with a path to EC once a substrate follow-up lands).

---

## TL;DR

* The federation raft layer exposes two propose paths per zone:
  * `ZoneConsensus::propose` (SC, raft consensus, majority ACK).
  * `ZoneConsensus::propose_ec_local` (EC, WAL-first + sync local apply, async replicate to peers).
* `zone_handle::set_metadata` / `delete_metadata` carry a `Consistency: Sc | Ec` parameter; callers pick per call.
* The kernel hot path — `ZoneMetaStore::put` / `delete`, which `sys_setattr` / `sys_unlink` drive — **routes through SC today**.  The PR #61 attempt to route it through EC was reverted in PR #63 (see *Why the EC kernel-hot-path activation was deferred* below).
* `nexusd-cluster join` carries `--as voter|learner` (default `learner` for backward compat).  Operators pick `voter` for symmetric peer workflows (cc-tasks-share Mac↔Win), `learner` for the owner-pattern share-with-readers shape.

---

## Why now

The 2026-06-22 Mac↔Win cross-machine smoke (Federation L1 byte-exact read over Tailscale) was the first time the bidirectional symmetric peer pattern hit the federation surface end-to-end.  The runbook §3b join flow was building Mac as a Learner per the canonical share-with-readers shape, and `vfs_write` from Mac surfaced `not leader, peers=0` — `ZoneMetaStore::put` hardcoded SC propose which needs a leader.

The user's framing for the fix:

> 两边都是 voter 的代价是，必须都在线才能写，否则不能写吗？为什么？我理解写的时候改为 EC 不就行了吗？我认为我们当前的设计是支持 per-write level EC/SC decision 的。

Confirmed: the architecture had `Consistency::Ec | Sc` as a per-call parameter on `zone_handle.rs::set_metadata` / `delete_metadata` since the EC WAL landed.  `ZoneMetaStore` was the only caller still hardcoding SC.  PR #61 attempted to flip ZoneMetaStore to EC.  The activation broke the federation E2E suite (see deferral section below); PR #63 reverted the activation while keeping the operator-facing CLI work.

---

## The two propose paths

| Path | Latency | Quorum | RYW | Use today |
|------|---------|--------|-----|-----------|
| `propose_ec_local` (EC) | ~5–50 µs (local redb + WAL) | None — every node writes locally | Preserved on the writer node (local apply synchronous) | Caller opt-in via `zone_handle::set_metadata(.., Consistency::Ec)` — niche callers that can tolerate async cross-node visibility |
| `propose` (SC) | ~5–10 ms intra-DC (raft round-trip + majority ACK) | Required | Linearizable across all peers | Default for every metadata mutation (kernel hot path); also locks, CAS, WAL stream appends, control-plane (mount install / ConfChange) |

The split is per-call, not per-zone.  Same `ZoneMetaStore` instance exposes both surfaces — the kernel hot path goes SC, opt-in callers can reach EC.

---

## Why the EC kernel-hot-path activation was deferred

PR #61 routed `ZoneMetaStore::put` / `delete` through `propose_ec_local`.  Federation E2E on the companion nexus PR caught a regression: `test_joiner_cli_join_then_byte_exact_read_binary_chunked` + `test_joiner_stat_after_read_caches_origin_locally` both timed out at `wait_nodes_caught_up` (these tests were green on develop pre-#61).

Diagnosis: the EC drain (`transport_loop.rs::replicate_ec_entries`) has correctness / liveness issues that only surface in 1-voter + 1-learner topologies.  After a larger founder-side sys_setattr write, subsequent founder→learner writes stop reaching the learner's local state machine — per-peer exponential backoff (base 100 ms, cap 60 s) accumulates without recovery, so subsequent writes never get drained within typical wait-budgets.

Per the raft contract principle ("如果涉及raft请100%满足raft的使用契约，否则会出别的bug"), we don't activate a substrate path with open correctness issues.  PR #63 reverted `ZoneMetaStore::put` / `delete` to SC.

What needs to land before re-attempting the activation:

* Diagnose the per-peer backoff accumulation in `replicate_ec_entries` and either bound it or add a recovery path (the issue isn't that backoff exists — it's that it doesn't recover when peers ARE reachable).
* Confirm deterministic founder→learner delivery for both small and large sys_setattr payloads in a 1V+1L topology.
* Stand up a regression test that pins "joiner reads founder's EC write within Xs" specifically — not just a generic raft catch-up.

Tracking: file a nexi-lab/nexus-vfs issue for the EC drain hardening before reopening the activation conversation.

---

## Failure modes the current shape DOES close

| Symptom | Root cause | Mitigation |
|---------|-----------|------------|
| Operator joins without specifying role, can't tell what they got | `as_learner=true` was hardcoded in `run_join`; no operator visibility | `nexusd-cluster join --as voter|learner` makes the choice explicit; success line includes the role |
| Operator surface flag clap-derived to wrong name | `#[arg(long, ...)]` on field `as_role` derived `--as-role`; runbook + docs all said `--as` | `#[arg(long = "as", ...)]` override; regression unit test in `nexus-vfs/rust/profiles/cluster/src/main.rs` pins both `--as voter` and `--as learner` parse |

---

## Failure modes the current shape does **NOT** close

* **Learner cannot propose SC writes** — every `vfs_write` on a learner surfaces `NotLeader`.  Mitigation: pick `--as voter` for workflows that need symmetric write authority.  Long-term: see the EC kernel-hot-path activation deferral above.
* **2-voter cluster needs both peers online for any write** — raft majority quorum.  Mitigation: accept the constraint for cc-tasks-share-style symmetric peer workflows (both peers are typically online together), or add a witness for HA (3-voter setup).
* **Wipe-rejoin of a voter** — voter joiners re-introduce the pre-PR #57 hazard if they go through SSD swap without first transferring their voter slot away.  Mitigation: documented (the `--as voter` runbook callout names this); operator must remove + re-add the voter manually.

---

## Operator decision matrix (mirrors runbook §3b)

| Workload | `--as` | Why |
|----------|--------|-----|
| Single authoritative writer publishing to many mirror peers (canonical `nexus share` semantics) | `learner` (default) | Wipe-rejoin safe; readers don't affect the owner's quorum |
| Symmetric 2-peer share, each writes its own subpath (cc-tasks-share Mac↔Win) | `voter` | Equal SC write authority via raft consensus; both peers can propose writes (raft serializes), no contention since each owns its own subpath |
| Root cluster bootstrap (≥3 voters + optional witness) | `voter` (used by founder + every joiner) | Quorum essential; single-node loss must not lose quorum |

---

## Test coverage

* `nexus-vfs` `rust/profiles/cluster/src/main.rs` → `join_cli_accepts_as_voter_and_as_learner_flags` — unit-level pin that `Args::try_parse_from` accepts `--as voter` / `--as learner` and defaults to learner.  Cheap regression sentinel for clap signature drift.
* `nexus` `tests/e2e/docker/test_federation_runbook.py` → `TestFederationWriteConsistencyContract.test_join_as_voter_flag_round_trips_through_cli` — Docker-level CLI smoke that `nexusd-cluster join --help` keeps mentioning `--as` plus both role enum values.

When the EC kernel-hot-path activation is reattempted, the originally-planned `test_learner_joiner_writes_via_ec_when_founder_offline` should be reintroduced: stop founder, joiner writes via gRPC, joiner reads its own write (RYW), founder comes back, async replication catches up, founder reads byte-exact.

---

## Lineage / pointers

* nexus-vfs PR #57 — landed Learner-default-on-join to fix the F5 wipe-rejoin quorum stall.
* nexus-vfs PR #61 — added per-call EC routing primitives + attempted kernel hot path activation.  Activation reverted in PR #63 after federation E2E caught the drain bug; primitives kept.
* nexus-vfs PR #62 — operator CLI flag rename (`--as`, not `--as-role`).
* nexus-vfs PR #63 — kernel hot path activation revert + doc / regression-test sync.
* nexus-vfs `propose_ec_local` (rust/raft/src/raft/node.rs) — the EC write primitive ZoneMetaStore would route to once the EC drain is hardened.
* Companion runbook section: `docs/architecture/federation-cross-machine-runbook.html` "Consistency model" (between §3g and §4).
* Manual smoke that motivated the change: 2026-06-22 Mac↔Win Federation L1 over Tailscale.  Substrate proven both directions byte-exact via SC; the bidirectional-write gap for `--as learner` joiners is documented but currently the operator workaround is `--as voter`.
