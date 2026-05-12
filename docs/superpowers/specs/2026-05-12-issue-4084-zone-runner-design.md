# Issue #4084 - Per-Zone Runner Bulkhead Isolation

**Date:** 2026-05-12
**Issue:** [#4084](https://github.com/nexi-lab/nexus/issues/4084)
**Scope:** Full issue in one implementation plan

## 1. Context

Nexus already carries zone identity through the server stack with
`OperationContext.zone_id`, `zone_set`, and `zone_perms`. Multi-zone auth,
zone-scoped paths, and per-zone permission gates exist. The missing piece is
runtime isolation inside one Python server process: FastAPI request handlers,
gRPC `Call`, direct REST v2 handlers, search services, connector calls, and
kernel syscall wrappers can still share the same server event loop.

The issue proposes a per-zone runner: one OS thread and one asyncio event loop
per active zone. Zone-scoped work runs on that zone's loop. A slow or wedged
operation for zone A should not park zone B's loop or the server's main request
loop.

The codebase layout differs from the issue sketch:

- Server entrypoints live under `src/nexus/server/`, not top-level `server/`.
- Runtime/factory wiring lives under `src/nexus/factory/` and
  `src/nexus/server/lifespan/`.
- The strongest dispatch seams are `server/api/core/rpc.py`,
  `server/rpc/dispatch.py`, `grpc/servicer.py`, and selected direct v2 REST
  routers.

## 2. Goals

- Add `ZoneRunner` with `start`, `call`, `call_sync`, and idempotent `stop`.
- Add `ZoneRegistry` that owns one runner per active zone and can stop all
  runners.
- Route existing zone-scoped server paths through the target zone runner,
  including deprecated HTTP RPC, gRPC `Call`, kernel syscall dispatch, direct
  REST v2 surfaces, and daemon/background work that carries a concrete
  `zone_id`.
- Keep the FastAPI main loop focused on auth, parsing, response encoding, and
  non-zone-scoped orchestration.
- Prove with a stress test that a slow zone A operation does not affect zone B
  median latency.
- Drain/stop all runners during FastAPI shutdown.
- Document cross-zone call discipline.

## 3. Non-Goals

- Process-level tenant isolation. This design stays in one Python process.
- Duplicating `NexusFS` or rebuilding the full service graph per zone.
- Changing token, ReBAC, or `zone_perms` semantics.
- Solving blocking CPU-bound Python code. Blocking work still needs explicit
  thread/process offload inside the zone runner.
- Making every admin/root operation zone-bound. Root/admin operations without a
  concrete target zone remain on the main loop unless they operate on a
  zone-scoped path or explicit zone.

## 4. Architecture

### 4.1 Runtime Module

Create `src/nexus/runtime/zone_runner.py`.

`ZoneRunner` owns:

- `zone_id: str`
- one daemon OS thread named `nexus-zone-{zone_id}`
- one asyncio event loop created and run on that thread
- lifecycle state guarded by a `threading.RLock`
- a thread-local marker so code can detect when it is already running on the
  same runner

Public API:

```python
class ZoneRunner:
    def __init__(self, zone_id: str): ...
    def start(self) -> None: ...
    async def call(self, work: Callable[[], Awaitable[T]]) -> T: ...
    def call_sync(self, work: Callable[[], Awaitable[T]]) -> T: ...
    def stop(self) -> None: ...
```

`call` accepts an awaitable factory, not an already-created coroutine. The
factory is invoked on the zone loop, which prevents accidental binding of
coroutines, async generators, clients, or futures to the caller's event loop.

`call_sync` is for synchronous callers outside the owning zone thread. It
raises a clear `RuntimeError` if called from the owning runner thread, because
blocking that thread would deadlock the zone loop.

`ZoneRegistry` owns runners:

```python
class ZoneRegistry:
    def runner_for(self, zone_id: str) -> ZoneRunner: ...
    def all(self) -> tuple[ZoneRunner, ...]: ...
    def stop_all(self) -> None: ...
```

Runner creation is lazy for the first full implementation: an active zone is a
zone that receives work in this process. Existing zone discovery can warm known
zones later, but lazy creation avoids boot-time database coupling and keeps
root/startup paths simple.

### 4.2 Target Zone Resolution

Add `src/nexus/runtime/zone_resolution.py` with small pure helpers:

- `zone_from_path(value: str) -> str | None`
- `zone_from_params(params: Any) -> str | None`
- `target_zone_for_context(context: OperationContext, params: Any | None) -> str | None`

Resolution order:

1. Explicit zone argument or attribute (`zone`, `zone_id`, `target_zone_id`) if
   present and non-root.
2. Embedded `/zone/<id>/...` prefix in common path fields (`path`, `src`,
   `dst`, `old_path`, `new_path`, `files`, batch entries).
3. `context.zone_id` when it is not `root`.
4. `None` for root/admin/global operations with no concrete zone.

The resolver does not replace permission checks. Existing gates still decide
whether the request may target a zone. The resolver only selects the runner.

### 4.3 FastAPI State And Lifespan

Add `zone_registry` to `NexusAppState` and `LifespanServices`.

During `create_app`, initialize:

```python
app.state.zone_registry = ZoneRegistry()
```

During shutdown, call `app.state.zone_registry.stop_all()` before `NexusFS`
engine disposal and close. Shutdown order matters: active zone loops may still
be holding async generators or service tasks, so runners must stop before the
kernel/services are torn down.

### 4.4 RPC And gRPC Dispatch

HTTP RPC:

- In `server/api/core/rpc.py`, after auth and zone scoping, resolve the target
  zone from the scoped params and context.
- If no concrete zone is found, keep current behavior.
- If a zone is found, run the actual dispatch through
  `zone_registry.runner_for(zone).call(lambda: dispatch...)`.
- Apply this to both kernel syscall dispatch and `dispatch_method`.

gRPC `Call`:

- In `grpc/servicer.py`, `VFSCallDispatcher._dispatch_async` resolves the target
  zone after params are decoded and scoped.
- `dispatch_call_sync` already bridges Rust to the FastAPI loop. The FastAPI
  loop should then hop to the target zone runner for the zone-scoped dispatch.
- Static-key/auth behavior remains unchanged.

The generic dispatch layer (`server/rpc/dispatch.py`) stays mostly unchanged.
It remains the dispatcher used inside whichever loop owns the work.

### 4.5 Direct REST v2 Surfaces

Several v2 routers bypass JSON-RPC and call services directly. Full issue scope
requires explicit runner coverage for zone-scoped endpoints. The implementation
must audit the following surfaces and wrap every endpoint in those files that
performs concrete zone work:

- `server/api/v2/routers/async_files.py`: read, write, delete, exists, list,
  mkdir, metadata, stream, rename, copy, batch read/write, glob, grep.
- `server/api/v2/routers/batch.py`: batch filesystem operations.
- `server/api/v2/routers/search.py` and `mobile_search.py`: zone-scoped search
  and federated search calls.
- `server/api/v2/routers/connectors.py`: connector mount and connector file
  operations that carry `zone_id`.
- `server/api/v2/routers/subscriptions.py` and `events_replay.py`: zone-scoped
  subscription/event replay reads.
- `server/api/v2/routers/snapshots.py` and `tus_uploads.py`: snapshot/upload
  operations tied to a zone.
- `server/api/v2/routers/secrets.py`, `password_vault.py`, `credentials.py`,
  and `workspace.py`: zone-scoped service operations where the router derives
  an `OperationContext` or `zone_id`.

Use a helper such as:

```python
async def run_zone_scoped(request: Request, zone_id: str | None, work: Callable[[], Awaitable[T]]) -> T:
    registry = getattr(request.app.state, "zone_registry", None)
    if registry is None or zone_id is None:
        return await work()
    return await registry.runner_for(zone_id).call(work)
```

This keeps router changes mechanical and preserves existing permission logic.

### 4.6 Same-Runner Reentry

If code already running on zone A calls `runner_for("zone-a").call(...)`, execute
the factory directly on the current loop. This prevents self-deadlock and keeps
cross-service calls within the same zone cheap.

If code running on zone A calls zone B, it submits to zone B and awaits that
future. The documented rule is:

> Start on the caller's zone runner. For cross-zone work, explicitly await the
> target zone via `ZoneRegistry.runner_for(other_zone).call(...)`.

### 4.7 Failure And Cancellation

- Exceptions raised inside a zone loop propagate to the caller.
- If the caller task is cancelled, cancel the submitted concurrent future when
  possible.
- Stopping a runner rejects new work and cancels/drains pending tasks.
- Loop shutdown runs `shutdown_asyncgens()` before closing the event loop.
- Thread join has a bounded timeout and logs if a runner fails to terminate.

The first implementation should not hide exceptions behind wrapper-specific
types. Existing HTTP/gRPC error mapping should continue to see the original
exception classes.

### 4.8 Daemon And Background Work

Daemon-managed zone work should use the same registry rather than spawning a
separate isolation mechanism. Any background consumer, scheduler task, search
daemon operation, upload cleanup, cache warmup, or connector sync that carries a
concrete `zone_id` should submit its zone-scoped service call through
`ZoneRegistry.runner_for(zone_id).call(...)`.

Background work without a concrete zone stays on its existing loop. If the work
later discovers a target zone from a path or database row, only the discovered
zone-scoped unit should hop to the runner.

## 5. Testing Strategy

Use TDD for implementation.

### 5.1 Runtime Unit Tests

Create `tests/unit/runtime/test_zone_runner.py`.

Coverage:

- `ZoneRunner.call()` runs work on a different thread and a different loop from
  the caller.
- `ZoneRunner.call_sync()` works from synchronous code.
- Exceptions propagate.
- `stop()` is idempotent.
- Calling the same runner from within its own loop executes without deadlock.
- `call_sync()` from the owning runner thread raises.

Create `tests/unit/runtime/test_zone_registry.py`.

Coverage:

- repeated `runner_for("a")` returns the same runner.
- different zones get different runners.
- `all()` returns created runners.
- `stop_all()` stops every runner and is idempotent.

### 5.2 Dispatch Tests

Add focused tests around the seams rather than full server e2e for every method:

- HTTP RPC kernel syscall uses the target zone runner.
- HTTP RPC `dispatch_method` uses the target zone runner.
- gRPC `Call` uses the target zone runner.
- root/global RPC with no concrete target zone does not create a runner.

Use a fake registry/runner in `app.state.zone_registry` to record calls without
spawning real threads in all dispatch tests.

### 5.3 REST Router Tests

Add representative tests that prove direct routers do not bypass the runner:

- async files read/write/list
- batch read/write
- search
- connector zone operation
- subscription or event replay read
- snapshot or upload zone operation

The goal is not one test per endpoint. The goal is coverage for each direct
router pattern.

### 5.4 Stress Test

Add `tests/integration/server/test_zone_runner_bulkhead.py`.

Scenario:

- install a test-only route or use a fake service method that sleeps for 10s
  inside zone A's runner.
- concurrently issue repeated fast calls for zone B.
- assert zone B median latency remains low while zone A is sleeping.

Keep this test deterministic by using local sleeps and in-process ASGI clients,
not real Slack/GitHub/network calls.

### 5.5 Shutdown Test

Add a lifespan test that starts work on two zones, exits the app lifespan, and
asserts all runners are stopped and their threads are no longer alive.

## 6. Documentation

Add `docs/architecture/zone-runner.md`.

Contents:

- Why runners exist and what they isolate.
- How target zones are resolved.
- Cross-zone discipline.
- Why coroutine factories are required.
- What is not isolated: CPU-bound Python, process memory, global singleton
  state, shared database pools, and shared Rust kernel state.
- Shutdown behavior and operational considerations for many zones.

## 7. Risks And Mitigations

- **Loop-bound objects captured too early.** Mitigation: `ZoneRunner.call`
  accepts a factory and invokes it on the zone loop.
- **Hidden one-loop assumptions.** Mitigation: route through dispatch seams
  first, add direct REST coverage, and document any later audit findings.
- **Deadlock from sync calls on the owning runner.** Mitigation: detect owning
  runner thread and raise from `call_sync`.
- **Memory growth with many zones.** Mitigation: lazy runner creation first;
  later add idle eviction or caps if measurements require it.
- **Database/driver loop affinity.** Mitigation: keep existing session factory
  patterns; do not share async sessions across loops. Router code should create
  async work inside the target runner.
- **Cancellation semantics.** Mitigation: caller cancellation cancels the
  submitted future, but blocking sync code running inside the zone remains
  bounded only by existing backend timeouts.

## 8. Acceptance Criteria Mapping

- `ZoneRunner` with `start`/`call`/`call_sync`/`stop`: runtime unit tests.
- `ZoneRegistry` instantiates one runner per active zone touched in the
  process: registry unit tests and lifespan wiring.
- All existing zone-scoped server paths route through `runner.call(...)`:
  dispatch tests, direct REST router tests, and daemon/background tests for
  zone-carrying work.
- Stress test: zone A slow op does not affect zone B median latency:
  `test_zone_runner_bulkhead.py`.
- Shutdown drains all runners cleanly: lifespan shutdown test.
- Doc on cross-zone call discipline: `docs/architecture/zone-runner.md`.
