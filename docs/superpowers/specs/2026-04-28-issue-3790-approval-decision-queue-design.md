# Issue #3790 — Approval Decision Queue + Event API (Nexus server-side)

**Status:** Draft
**Owner:** windoliver
**Issue:** [#3790](https://github.com/nexi-lab/nexus/issues/3790)
**Epic:** [#3777](https://github.com/nexi-lab/nexus/issues/3777) — Nexus as Context Layer for Secure Agent Runtimes
**Depends on:** #3784 (hub mode, closed), #3787 (policy presets, moved to agentenv), #3792 (SSRF validation)

## Context

Per epic #3777, ownership of operator-facing approval surfaces is split between repos:

| Concern | Owner |
| --- | --- |
| Server-side approval decision queue + audit log + event API | **Nexus (this repo)** |
| Operator TUI, CLI, Slack, webhook | **`agentenv`** |

This spec covers only the Nexus side. agentenv consumes the gRPC event API and provides operator surfaces.

## Problem

Policy presets apply a baseline at sandbox creation. Real agents continuously encounter resources outside the baseline:

- Tool calls that egress to a host not in the allowlist
- Hub zone-access requests where the token's ReBAC scope doesn't include the zone
- (deferred client) OpenShell gateway egress denials

Hard-failing every time is unusable; silently allowing is unsafe. The right answer is an operator-in-the-loop approval flow with a decision queue, audit trail, and decisions remembered for the session.

## Scope

### In scope

- Persistent approval request queue (Postgres) with coalesce semantics
- Append-only decision audit log
- Session-scoped decision cache
- gRPC event API (List / Watch / Decide / Get / Cancel / Submit)
- Diagnostic HTTP `GET /hub/approvals/dump`
- Integration hooks (v1 — kinds wired):
  - **MCP tool middleware** — pause-and-wait when a tool's egress hits an unlisted endpoint (kind `egress_host`)
  - **Hub zone-access resolver** — pause-and-wait when a token requests a zone not in its ReBAC scope (kind `zone_access`)
  - **Push-API for OpenShell** — gRPC `Submit(ApprovalRequest)` so the OpenShell binary can synchronously request a decision (kind `egress_host`); client integration deferred to agentenv
- `ApprovalKind` enum carries all four values from the issue (`egress_host`, `mcp_tool`, `zone_access`, `package_install`) for forward compatibility; **only `egress_host` and `zone_access` have wired hooks in v1**. `mcp_tool` and `package_install` are reserved — schema accepts them via `Submit`, but no internal hook produces them.
- Auto-deny TTL (default 60 s; per-token + per-request override)
- ReBAC capabilities (`approvals:read`, `approvals:decide`, `approvals:request`)

### Out of scope (in agentenv or other issues)

- Operator TUI / CLI for browsing pending requests
- Slack / webhook outbound integrations
- Writing to sandbox overlay policy or baseline preset files (Nexus emits a `decision_persisted` event; agentenv applies it)
- Decision-time policy diff proposals (UI flow lives in agentenv)
- `OpenShell` client implementation (only the API contract lives here)

## Design

### Architecture overview

```
┌─ Gate hooks (MCP middleware, hub auth, push API) ──┐
│   detect unlisted resource → call PolicyGate       │
└────────────────────┬───────────────────────────────┘
                     ▼
┌─ PolicyGate (sync facade) ─────────────────────────┐
│   1. Look up cached session/once decision          │
│   2. If hit → return immediately                   │
│   3. Else → ApprovalService.request_and_wait()     │
└────────────────────┬───────────────────────────────┘
                     ▼
┌─ ApprovalService (async core) ─────────────────────┐
│   - Coalesce by (zone, kind, subject)              │
│   - Persist pending row + futures map              │
│   - Wait on asyncio.Future w/ timeout              │
│   - Postgres LISTEN/NOTIFY for multi-worker        │
│   - On decide() → resolve futures + emit event     │
└────────────────────┬───────────────────────────────┘
                     ▼
┌─ Postgres (3 tables) + gRPC API ───────────────────┐
│   approval_requests, approval_decisions,           │
│   approval_session_allow + WatchApprovals stream   │
└────────────────────────────────────────────────────┘
```

Boundaries:

- New brick `src/nexus/bricks/approvals/` owns: data model, service, gate facade, gRPC server, sweeper.
- Existing `mcp` brick adds a small hook in the SSRF/egress validator (#3792) calling `PolicyGate.check`.
- Existing hub auth resolver adds a small hook on zone-scope miss calling `PolicyGate.check`.
- agentenv code is not modified by this spec; it consumes the gRPC API.

Reuses existing infrastructure:

- `governance.approval.ApprovalWorkflow[T]` (state machine + status enum) is extended; we add the policy-gate domain on top instead of duplicating.
- Hub bearer-token auth is reused for gRPC.
- Existing alembic migrations harness owns schema rollout.

### Component layout

```
src/nexus/bricks/approvals/
  __init__.py
  models.py          # ApprovalRequest, ApprovalKind, DecisionScope, Decision dataclasses
  db_models.py       # SQLAlchemy ORM mirroring tables
  service.py         # ApprovalService — async core
  policy_gate.py     # PolicyGate — sync facade for hooks
  sweeper.py         # background task: expire pending past expires_at
  events.py          # in-process pub/sub + Postgres LISTEN/NOTIFY bridge
  grpc_server.py     # gRPC servicer
  http_diag.py       # GET /hub/approvals/dump
  config.py          # ApprovalConfig
  errors.py          # ApprovalDenied, ApprovalTimeout, GatewayClosed
  tests/...
```

### Data model

```sql
CREATE TABLE approval_requests (
    id              TEXT PRIMARY KEY,           -- "req_<ulid>"
    zone_id         TEXT NOT NULL,
    kind            TEXT NOT NULL,              -- egress_host|mcp_tool|zone_access|package_install
    subject         TEXT NOT NULL,              -- "api.stripe.com:443" | tool name | zone name
    agent_id        TEXT,
    token_id        TEXT,
    session_id      TEXT,                       -- "<token_id>:<agent_session_id>"
    reason          TEXT,
    metadata        JSONB,                      -- url, port, args snapshot, hub source, etc.
    status          TEXT NOT NULL,              -- pending|approved|rejected|expired
    created_at      TIMESTAMPTZ NOT NULL,
    decided_at      TIMESTAMPTZ,
    decided_by      TEXT,
    decision_scope  TEXT,                       -- once|session|persist_sandbox|persist_baseline
    expires_at      TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX approval_requests_pending_coalesce
    ON approval_requests (zone_id, kind, subject)
    WHERE status = 'pending';
CREATE INDEX ON approval_requests (status, expires_at);
CREATE INDEX ON approval_requests (zone_id, status);

CREATE TABLE approval_decisions (
    id              BIGSERIAL PRIMARY KEY,
    request_id      TEXT NOT NULL REFERENCES approval_requests(id),
    decided_at      TIMESTAMPTZ NOT NULL,
    decided_by      TEXT NOT NULL,              -- operator token_id, or "system" for timeout
    decision        TEXT NOT NULL,              -- approved|rejected|expired
    scope           TEXT NOT NULL,              -- once|session|persist_sandbox|persist_baseline
    reason          TEXT,
    source          TEXT NOT NULL               -- grpc|http|system_timeout|push_api
);
CREATE INDEX ON approval_decisions (request_id);

CREATE TABLE approval_session_allow (
    id              BIGSERIAL PRIMARY KEY,
    session_id      TEXT NOT NULL,
    zone_id         TEXT NOT NULL,
    kind            TEXT NOT NULL,
    subject         TEXT NOT NULL,
    decided_at      TIMESTAMPTZ NOT NULL,
    decided_by      TEXT NOT NULL,
    request_id      TEXT REFERENCES approval_requests(id),
    UNIQUE (session_id, zone_id, kind, subject)
);
CREATE INDEX ON approval_session_allow (session_id);
```

Notes:

- The partial unique index on `(zone_id, kind, subject) WHERE status='pending'` enforces coalesce in DB; an `INSERT … ON CONFLICT DO NOTHING` race is safe.
- `approval_requests.status` is mutated by decisions/expiry; every state change also writes a row to `approval_decisions` (append-only).
- `expires_at` is wall-clock; the sweeper expires stale rows.
- `persist_sandbox` / `persist_baseline` decisions are recorded in `approval_decisions` and emitted as events; **no policy file is written by Nexus** — that is agentenv's job.

### Public Python APIs

```python
# policy_gate.py
class PolicyGate:
    async def check(
        self,
        *,
        kind: ApprovalKind,
        subject: str,
        zone_id: str,
        token_id: str,
        session_id: str | None,
        agent_id: str | None,
        reason: str,
        metadata: dict[str, object],
    ) -> Decision:
        """Returns Decision.APPROVED | Decision.DENIED. Blocks until decided or auto-deny.
        Raises GatewayClosed on DB unreachable. Never returns APPROVED on timeout."""
```

```python
# service.py
class ApprovalService:
    async def request_and_wait(self, req: ApprovalRequest) -> Decision: ...
    async def list_pending(self, zone_id: str | None) -> list[ApprovalRequest]: ...
    async def get(self, request_id: str) -> ApprovalRequest | None: ...
    async def decide(
        self,
        request_id: str,
        decision: Decision,
        decided_by: str,
        scope: DecisionScope,
        reason: str | None,
        source: DecisionSource,
    ) -> ApprovalRequest: ...
    async def cancel(self, request_id: str, by: str) -> None: ...   # for stale callers
    async def watch(self, zone_id: str | None) -> AsyncIterator[Event]: ...
```

### gRPC API

```proto
service ApprovalsV1 {
  rpc ListPending(ListPendingRequest) returns (ListPendingResponse);
  rpc Get(GetRequest)                 returns (ApprovalRequestProto);
  rpc Decide(DecideRequest)           returns (ApprovalRequestProto);
  rpc Cancel(CancelRequest)           returns (CancelResponse);
  rpc Watch(WatchRequest)             returns (stream ApprovalEvent);
  rpc Submit(SubmitRequest)           returns (Decision);  // push API for OpenShell
}
```

Auth: hub bearer token, mapped to subject; ReBAC capability check enforced before service call.

| RPC | Capability |
| --- | --- |
| ListPending / Get / Watch | `approvals:read` |
| Decide / Cancel | `approvals:decide` |
| Submit | `approvals:request` (default for any sandbox-scoped token) |

`ApprovalEvent` carries `{type: pending|decided, request_id, zone_id, kind, decision?, scope?}`. Stream uses bounded buffer per client; on overflow the server closes with `RESOURCE_EXHAUSTED` and the client reconnects + replays via `ListPending`.

### Diagnostic HTTP endpoint

`GET /hub/approvals/dump?zone_id=…` — returns JSON snapshot of pending + last 100 decisions. Read-only, requires `approvals:read`. Provided so ops can verify the queue without spinning up agentenv.

### Integration hooks

| Site | Edit |
| --- | --- |
| `mcp/middleware.py` (or sibling SSRF middleware from #3792) | On unlisted egress: `await policy_gate.check(kind=egress_host, subject=host_port, …)`; on Approved continue, on Denied return existing not-found error |
| `mcp/server.py` | Wire `PolicyGate` into FastMCP context state at startup |
| Hub auth resolver | On token-zone-scope miss: `await policy_gate.check(kind=zone_access, subject=zone_id, …)` before returning 403 |
| `proto/nexus/grpc/` | Add `approvals.proto`; register servicer with existing gRPC bootstrap |

### Auto-deny / sweeper

- `sweeper.py` runs as an asyncio task started with the brick.
- Every `sweeper_interval` (default 5 s):
  `UPDATE approval_requests SET status='expired', decided_at=now(), decided_by='system' WHERE status='pending' AND expires_at < now() RETURNING id;`
  followed by a per-row insert into `approval_decisions` and a `NOTIFY approvals_decided` per row.
- Default `auto_deny_after = 60 s` (per issue). Per-token override via token metadata; per-request override via `metadata.timeout_override_seconds` (capped at config max).

### Multi-worker coordination

- All Nexus workers `LISTEN approvals_new, approvals_decided` on Postgres.
- Each worker maintains an in-process dispatcher only for futures it created.
- A single `NOTIFY approvals_decided` arrives at every worker; each checks if it owns a matching future and resolves it.
- On reconnect (after a dropped LISTEN), the worker reconciles by SELECTing current status of every in-flight `request_id` and resolving futures whose row is no longer `pending`. This handles missed notifications.

### Configuration

```yaml
approvals:
  enabled: true
  auto_deny_after_seconds: 60
  auto_deny_max_seconds: 600       # cap per-request override
  sweeper_interval_seconds: 5
  watch_buffer_size: 256
  diag_dump_history_limit: 100
```

## Data flow (representative)

Happy path: agent calls `nexus_fetch(url="https://api.stripe.com/v1/charges")`; stripe.com is not allowlisted in the active zone.

1. FastMCP middleware `on_call_tool()` fires.
2. SSRF/egress validator (#3792) checks `url` → not in allowlist for this zone.
3. Validator calls `PolicyGate.check(kind=egress_host, subject="api.stripe.com:443", zone_id, token_id, session_id, agent_id, reason=tool_name, metadata={url})`.
4. `ApprovalService`:
   1. Look up `approval_session_allow(session_id, zone, kind, subject)` → miss.
   2. `INSERT approval_requests … ON CONFLICT DO NOTHING` (the partial unique index `approval_requests_pending_coalesce` produces the conflict for an existing pending row with the same `(zone_id, kind, subject)`).
      - On conflict: `SELECT` the existing pending row; attach future to the dispatcher entry.
      - On insert: `NOTIFY approvals_new` with `{request_id, zone_id}`.
   3. Create `asyncio.Future`, register in dispatcher keyed by `request_id`.
   4. `await asyncio.wait_for(future, timeout=auto_deny_after)`.
5. Operator (agentenv TUI/CLI) calls gRPC `Decide(request_id, scope=session, decision=approved)`.
   1. `UPDATE approval_requests SET status='approved', decided_*, decision_scope='session' WHERE id=… AND status='pending'`.
   2. `INSERT approval_decisions` (audit).
   3. If scope=session: `INSERT approval_session_allow`.
   4. `NOTIFY approvals_decided` with `{request_id, decision}`.
6. Each Nexus worker's LISTEN handler receives the NOTIFY; the owning worker resolves the future.
7. `PolicyGate.check` returns `Approved` → SSRF validator passes → tool call proceeds → result returned to agent.

Sad paths:

- **Operator denies** → future resolves to `Denied` → SSRF validator returns existing not-found error.
- **Sweeper fires before decision** → status=`expired` → future resolves to `Denied(reason=timeout)`.
- **Multiple agents same coalesce key** → step 4.2 finds existing pending → all futures resolved by single decision in step 6.
- **Agent disconnects mid-wait** → MCP middleware cancels the future via `ApprovalService.cancel()` (does not change DB row; just unregisters dispatcher entry). The DB row stays pending until decided or swept.
- **Hub zone-access path** → same flow, gate called from hub auth resolver instead of MCP middleware.

## Error handling

| Failure | Behavior |
| --- | --- |
| Postgres unreachable on insert | `PolicyGate.check` raises `GatewayClosed`; MCP middleware maps to MCP error (no false-allow) |
| LISTEN/NOTIFY drop (worker reconnect) | On reconnect, `ApprovalService` reconciles by SELECTing current status of every in-flight `request_id`; resolves futures that already terminated |
| Sweeper crashes | Watchdog logs + restarts the task; DB rows are not stale because next decision-time UPDATE is gated on `status='pending'` (idempotent) and a re-launched sweeper catches up |
| Decide called for nonexistent / non-pending request | gRPC returns `FAILED_PRECONDITION` |
| Decide called by token without `approvals:decide` | gRPC returns `PERMISSION_DENIED` |
| Two operators decide same request simultaneously | DB `UPDATE … WHERE status='pending'` makes one win atomically; loser receives `FAILED_PRECONDITION` |
| Cancel on already-decided request | No-op + returns `OK` (idempotent) |
| Watch stream client lag | Bounded buffer per stream; on overflow drop stream with `RESOURCE_EXHAUSTED`; client reconnects + replays via `ListPending` |
| Submit (push API) called without auth | gRPC returns `UNAUTHENTICATED` |

## Testing

Unit (`tests/unit/bricks/approvals/`):

- `test_models.py` — dataclass invariants, `ApprovalKind` / `DecisionScope` enums.
- `test_service.py` — coalesce on duplicate insert, future resolution by NOTIFY simulation, sweeper expiry, cancel idempotency.
- `test_policy_gate.py` — `session_allow` cache hit short-circuits service.request, raises `GatewayClosed` on DB error.
- `test_state_machine.py` — confirms wiring to `governance.approval` state machine.

Integration (`tests/integration/approvals/`, live Postgres):

- `test_coalesce_pg.py` — two concurrent inserts hit `ON CONFLICT`; one row.
- `test_listen_notify.py` — two `ApprovalService` instances on same DB; decide on one, future resolves on the other.
- `test_sweeper_pg.py` — insert pending past `expires_at`, sweeper expires it.
- `test_session_scope.py` — approve scope=session; second matching call hits cache; no new request.
- `test_reconnect_reconcile.py` — drop LISTEN, decide row, reconnect; pending future resolves.

E2E (`tests/e2e/self_contained/approvals/`):

- `test_mcp_egress_e2e.py` — spin nexus + stub MCP client; agent calls `nexus_fetch` on disallowed host; gRPC client decides; tool returns success/denied accordingly.
- `test_hub_zone_access_e2e.py` — token without zone hits hub; zone_access request appears; approve; second call succeeds.
- `test_grpc_watch_e2e.py` — Watch stream emits new pending + decisions across two workers.
- `test_diagnostic_dump_e2e.py` — `GET /hub/approvals/dump` returns expected JSON.
- `test_push_api_submit_e2e.py` — gRPC `Submit` from a stub OpenShell client receives a Decision.

Smoke perf (`tests/benchmarks/`):

- `bench_coalesce_burst.py` — 100 agents simultaneous on same subject; one DB row, one decide, all unblocked < 50 ms after notify.

Mocking policy: integration tests use the real Postgres test harness — no DB mocks (per project memory, mocked-DB tests have masked production divergence before).

## Acceptance criteria mapping

| Issue criterion | Where covered |
| --- | --- |
| Pending requests visible via API + decisions persisted | gRPC `ListPending` / `Get` / `Watch`; `approval_requests` + `approval_decisions` |
| Non-interactive approve/deny via API | gRPC `Decide` |
| MCP tool calls with unlisted egress pause and wait | MCP middleware + SSRF validator hook calling `PolicyGate.check` |
| Hub zone access requests surface in same queue | Hub auth resolver hook calling `PolicyGate.check(kind=zone_access)` |
| Audit trail (who, when, scope, reason) | `approval_decisions` append-only table |
| `--auto-deny-after 60s` default | `ApprovalConfig.auto_deny_after_seconds = 60` + sweeper |
| TUI / Slack / webhook | **Out of scope** — owned by agentenv per epic split |

## Migration / rollout

- Alembic migration adds the three tables.
- `ApprovalConfig.enabled` defaults to **false** in the first release; enabled in hub-mode deployments only.
- Integration hooks use feature flag: when disabled, fall through to existing deny/error paths (no behavior change).
- Once agentenv ships its consumer UI, deployments enable the flag.

## Open questions

None at design time; all surfaced in brainstorm have been resolved.

## Out-of-repo dependencies

- agentenv: must implement gRPC client for `ApprovalsV1` (List/Watch/Decide) and the operator UI surfaces. Tracked in agentenv's umbrella epic.
- OpenShell: Submit-API client integration tracked separately; not blocking this issue.
