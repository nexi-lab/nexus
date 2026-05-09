# Issue #4081 — Agent Self-Observability via Mounted JSONL Log

**Status:** Draft
**Date:** 2026-05-09
**Issue:** https://github.com/nexi-lab/nexus/issues/4081

## Problem

Nexus has rich operator observability (OpenTelemetry, Sentry, structlog), but agents
themselves cannot introspect their own actions through the same tools they already use
(`cat`, `grep`, `jq`). An agent has no in-band way to answer "what files did I read in
the last hour?" without a separate API.

## Goal

Surface every op (`OpRecord`) and every shell command (`ExecutionRecord`) emitted by
an agent as JSONL lines in a per-agent mount at `/.activity/{utc_date}/{agent_id}.jsonl`,
with ReBAC restricting each agent to its own log.

## Non-Goals

- Disk-backed archive, gzip rotation, long-term retention beyond N days (RAM only).
- Aggregate views across agents (operators already have OTel/structlog).
- Streaming reads (`tail -f`); the first version returns snapshot bytes only.
- Cross-agent or per-tenant aggregate buckets.
- Search indexing; `grep`/`jq` is the UX.

## Architecture

Four pieces, three new + one extension to the existing `services/activity/` pipeline.

```
                                            ┌─────────────────────────────┐
op completes → post-hook builds             │ services/activity/          │
ActivityEvent(actor=agent_X, ...) ─────────▶│ QueueEmitter → Worker       │
                                            │   ├─ existing sinks (sqlite,│
                                            │   │  recording, ...)        │
                                            │   └─ JsonlActivitySink (new)│ ─┐
                                            └─────────────────────────────┘  │
                                                                              ▼
                                                       MemoryBackend.append_line(
                                                          agent_id=X, date=D, line=…)
                                                                              │
                                                                              ▼
                                            ┌─────────────────────────────┐
agent_X: cat /.activity/D/X.jsonl           │ MemoryBackend (new)         │
   → router → ReBAC ALLOW                  │ dict[(agent, date)]→deque   │
   → MemoryBackend.read_path ──────────────▶│ ring-buffered, capped       │
                                            └─────────────────────────────┘
```

### Components

#### 1. `MemoryBackend` (new, `src/nexus/backends/memory.py`)

Implements the existing backend protocol used by other mount-backed resources.

- Storage: `dict[(agent_id: str, date: str), Deque[bytes]]` plus a parallel
  `dict[(agent_id, date), int]` of byte counts for cheap cap checks.
- File model: paths take the shape `/.activity/{date}/{agent_id}.jsonl`. Each
  `(agent_id, date)` deque is one virtual file.
- Methods:
  - `append_line(agent_id, date, line: bytes) -> None` — direct API used by the sink.
    Enforces cap inline (ring eviction, see below).
  - `read_path(path) -> bytes` — concatenates the deque. Resolves
    `(agent_id, date)` from the path.
  - `list_dir(path) -> Iterable[str]` — supports `ls /.activity/` and
    `ls /.activity/{date}/`.
  - `stat(path)` — synthesised mtime = newest line ts; size = byte count.
- Concurrency: `ActivityWorker` is the single consumer thread, so writes do not
  contend. Reads happen on request threads — wrap each `(agent_id, date)` access
  with a per-key `threading.Lock` to safely snapshot the deque while reading.

#### 2. `JsonlActivitySink` (new, `src/nexus/services/activity/sinks/jsonl.py`)

Implements the existing sink protocol (see neighbouring sinks: `sqlite.py`,
`recording.py`, `noop.py`).

- Receives a batch of `ActivityEvent` from the worker.
- For each event:
  1. **Recursion guard:** if `event.path` is set and starts with `/.activity/`,
     drop and increment `agent_log_recursion_skipped_total`. Exec events have no
     `path` and pass this check unconditionally.
  2. If `event.actor.agent_id` is empty (system op), drop and increment
     `agent_log_lines_dropped_no_agent`.
  3. Build the JSONL line per the schema below.
  4. Call `MemoryBackend.append_line(agent_id, utc_date(event.ts), line)`.
- Holds a reference to the `MemoryBackend` instance — wired up at brick startup
  (see #3). **Sink writes do not go through dispatch**, so no further
  `ActivityEvent` is produced — recursion is impossible by construction.
- The path predicate is belt-and-suspenders for the read path and any future
  emitter that does not bypass dispatch.

#### 3. `bricks/agent_log/` (new, minimal)

A startup brick. No request-path code.

- Constructs the `MemoryBackend` instance.
- Registers it as a mount at `/.activity/` via the existing
  `mount_service.add_mount_sync` (see `src/nexus/bricks/mount/mount_service.py:283`).
- Wires the `MemoryBackend` reference into the `JsonlActivitySink`, then
  registers the sink with `services/activity/`'s sink list at startup.
- Defines the ReBAC grant template `(agent:{X}, can-read,
  /.activity/*/{X}.jsonl)`.
  - Hooks into the existing identity onboarding in `bricks/identity/` so
    new agents auto-receive the grant.
  - On brick startup, scans the agent registry and idempotently ensures the
    grant exists for each pre-existing agent.

#### 4. `services/activity/events.py` extension

Add `kind="exec"` to `ActivityEvent`:

- New optional fields: `cmd: str | None`, `exit_code: int | None`,
  `cmd_truncated: bool` (set when `cmd` length > 4 KB).
- Sandbox exec entry in `bricks/sandbox/` emits an `ActivityEvent(kind="exec",
  actor, cmd, exit_code, ms)` on completion via the existing `QueueEmitter`.
- Discovery note: the Explore pass located `bricks/sandbox/events.py` but did
  not pin down the exec entry call site. The implementation plan must begin by
  finding it (could be a single point or split host/container shells).

### JSONL schema (one record per line)

```json
{"ts": "2026-05-09T23:42:11.043Z", "kind": "op",   "op": "read",  "path": "/s3/bucket/foo.txt", "bytes": 12834, "ms": 43}
{"ts": "2026-05-09T23:42:12.110Z", "kind": "exec", "cmd": "grep needle /gh/owner/repo/README.md", "exit_code": 0, "ms": 215}
{"ts": "2026-05-09T23:42:14.221Z", "kind": "op",   "op": "write", "path": "/local/notes.md", "bytes": 412, "ms": 8}
```

`ts` is ISO-8601 UTC with millisecond precision and `Z` suffix. Field order is
fixed for greppability. `cmd` is truncated to 4 KB with `…` when needed and the
record then carries `"cmd_truncated": true`.

## Data Flow

### Write path (op recording)

```
agent A: cat /s3/foo.txt
  → NexusFS.cat_path → OpsRegistry dispatch → backend handler
  → post-hook builds ActivityEvent(kind="op", op="read", path="/s3/foo.txt",
                                   actor.agent_id=A, ts, ms, bytes)
  → QueueEmitter.emit(event)            [existing non-blocking emitter]
  → ActivityWorker drains a batch
  → JsonlActivitySink.consume(batch):
      for e in batch:
          if e.path and e.path.startswith("/.activity/"): drop ; counter++
          if not e.actor.agent_id:                         drop ; counter++
          line = json.dumps({...}, separators=(",", ":"))
          MemoryBackend.append_line(e.actor.agent_id, utc_date(e.ts), line)
```

### Read path (agent introspection)

```
agent A: cat /.activity/2026-05-09/A.jsonl
  → NexusFS.cat_path → router resolves /.activity/ to MemoryBackend mount
  → ReBAC check: (agent:A, can-read, /.activity/2026-05-09/A.jsonl) → ALLOW
  → MemoryBackend.read_path: snapshot deque, concatenate, return
  → handler returns bytes to agent
  → post-hook emits ActivityEvent(path="/.activity/...") → sink filters → no line
```

### Recursion safety

```
sink → MemoryBackend.append_line   (direct call, NOT through dispatch)
                                   → no OpsRegistry, no ActivityEvent emitted
                                   → infinite-loop class eliminated
```

The path predicate in the sink is the second line of defense; it catches:

- Reads of `/.activity/...` (which DO go through dispatch and emit events).
- Any future emitter that does not bypass dispatch.

Writes from agents to `/.activity/...` are denied at the ReBAC layer — only the
sink can append, and the sink does not go through dispatch.

## Storage Cap and Rotation

### Per-(agent, date) cap

- Default: **10 MB** per agent per day. Configurable via
  `services/activity/config.py` (`agent_log_cap_bytes`).
- The 100 MB number from the issue is too high as a default given that
  N agents × M days × cap is the total RAM budget. Operators can raise.

### Ring-buffer enforcement (inline on `append_line`)

```python
def append_line(self, agent_id: str, date: str, line: bytes) -> None:
    key = (agent_id, date)
    with self._lock_for(key):
        buf = self._buffers.setdefault(key, deque())
        buf.append(line)
        self._bytes[key] = self._bytes.get(key, 0) + len(line)
        while self._bytes[key] > self._cap_bytes and len(buf) > 1:
            old = buf.popleft()
            self._bytes[key] -= len(old)
            self._evicted_lines += 1
```

Always retains at least one line so that `read_path` never returns empty for an
active agent.

### Date rollover

The date string is part of the key. New UTC date → new key, new buffer. No
explicit rollover logic.

### Retention

Reuse the existing `services/activity/retention.py` worker. Add a sweep:
every N hours, drop `(agent, date)` keys whose date is older than
`agent_log_retention_days` (default **7**). RAM-only — no disk archive in
this issue.

### Metrics

Extend `services/activity/metrics.py`:

- `agent_log_bytes_total{agent_id}` — current size per (agent, today).
- `agent_log_lines_dropped_total{reason}` where `reason` ∈ {`ring_evict`,
  `recursion`, `no_agent`}.

## ReBAC

### Grant template

`(subject=agent:X, relation=can-read, object=path:/.activity/*/X.jsonl)`

- Wildcard covers all UTC dates.
- **Read-only** for agents. No write/delete grants.
- Operator/admin reads: existing `is_admin` bypass in
  `_get_context_identity` (see `src/nexus/core/nexus_fs_internal.py`)
  covers cross-agent inspection.

### Lifecycle

- Onboarding: hook into `bricks/identity/` so new agents auto-receive the grant.
- Backfill: brick startup scans the agent registry and idempotently writes
  the grant for each existing agent. Batch in chunks for large registries.

### No-agent ops

System / raft / replication ops have no `actor.agent_id`. They are dropped
silently by the sink with the `no_agent` counter incremented. The "every op
recorded" acceptance line is interpreted as "every agent-actor op recorded";
this is documented.

## Configuration

In `services/activity/config.py`:

| key                           | type | default | meaning                              |
|-------------------------------|------|---------|--------------------------------------|
| `agent_log_enabled`           | bool | `true`  | feature flag                         |
| `agent_log_cap_bytes`         | int  | `10 MB` | per (agent, date) ring cap           |
| `agent_log_retention_days`    | int  | `7`     | days kept in RAM                     |
| `agent_log_cmd_max_bytes`     | int  | `4 KB`  | exec record `cmd` truncation point   |
| `agent_log_mount_path`        | str  | `/.activity/` | mount root                     |

## Testing

- `test_memory_backend_ring_buffer.py` — append past cap; oldest evicted; never
  empty; per-key independence.
- `test_memory_backend_paths.py` — path → (agent, date) parsing; `list_dir` for
  `/.activity/` and `/.activity/{date}/`; `stat` returns plausible mtime/size.
- `test_jsonl_sink_serialization.py` — schema matches the spec exactly; ts is
  ISO-8601 UTC with `Z`; field order stable; `cmd_truncated` set above 4 KB.
- `test_jsonl_sink_recursion_guard.py` — events with `path` under `/.activity/`
  are filtered.
- `test_jsonl_sink_no_agent.py` — events with empty `actor.agent_id` dropped;
  `no_agent` counter incremented.
- `test_agent_log_rebac_isolation.py` (integration) — agent A reads its file;
  reading B's file → `PermissionDenied`; writing own file → `PermissionDenied`.
- `test_recursion_smoke.py` (integration) — drive 1k mixed ops including
  attempted writes at `/.activity/`; assert no infinite loop, byte total ≤ cap.
- `test_exec_record.py` (integration) — sandbox exec produces an ExecutionRecord
  line with correct `cmd`, `exit_code`, `ms`.

## Documentation

New page `docs/agents/self-observability.md`:

- The three example queries from the issue, verbatim.
- ReBAC behavior (each agent isolated; admins see all).
- Cap defaults and how to raise them.
- Rotation policy (RAM, N-day retention, no archive in v1).

Add a link from `docs/architecture/` (next to `ops-dispatch-registry.md`).

## Acceptance Criteria

- [x] Every agent-actor op + exec recorded to `/.activity/{date}/{agent_id}.jsonl`.
- [x] Agent A cannot read Agent B's log (ReBAC enforced).
- [x] Recursion broken: writing to `/.activity/...` does not generate further records
      (denied at ReBAC; sink filters as second line of defense).
- [x] Capped storage; rotation policy configurable.
- [x] Doc page with example queries.

## Risks

1. **Memory blowup at scale.** N agents × 7 days × 10 MB. At 100 agents, ~7 GB.
   Mitigation: 10 MB default; documented operator dial.
2. **Sandbox exec instrumentation site discovery.** The exec call site was not
   pinned down in exploration. The plan must begin with a discovery step; if
   the entry is split (host shell vs container), wire each.
3. **agent_id absent for system ops.** Mitigation: silent drop with counter,
   documented.
4. **Date boundary.** Use `event.ts`, not `now()`, to compute the date key, so
   late-processed events land in their original day's bucket.
5. **ReBAC backfill cost.** Idempotent batch updates at brick startup.

## Out of Scope (deferred)

- Disk-backed archive / gzip rollover after N days.
- Per-tenant aggregate (`/.activity/_zone/{date}/all.jsonl`).
- `/.activity/me.jsonl` ergonomic alias.
- Streaming reads (`tail -f`).
- Search index.
