# Zone Runner Runtime Isolation

Nexus uses one `ZoneRunner` per active zone inside a Python server process. A
runner owns an OS thread and an asyncio event loop. Zone-scoped server work is
submitted to the target zone runner so a slow operation in one zone does not
park the FastAPI request loop or another zone's loop.

## What Runs On A Zone Runner

Auth, request parsing, and response encoding stay on the FastAPI loop. The
zone-scoped unit of work runs on the target runner:

- HTTP RPC dispatch
- gRPC `Call` dispatch
- direct v2 file, batch, search, connector, event, snapshot, upload, secret,
  credential, password-vault, and workspace operations that carry a concrete
  zone
- background work that already carries a concrete `zone_id`

Root or admin operations without a concrete target zone stay on their current
loop.

## Target Zone Resolution

The server selects a runner from the first concrete target it can find:

1. explicit `zone`, `zone_id`, or `target_zone_id`
2. embedded `/zone/<id>/...` path
3. non-root `OperationContext.zone_id`
4. no runner for root/global work

Permission checks remain separate. Existing `zone_perms`, ReBAC, and router
guards still decide whether the caller may access the zone.

## Coroutine Factories

`ZoneRunner.call()` accepts `Callable[[], Awaitable[T]]` rather than an already
created coroutine. The factory is invoked on the zone loop. This avoids binding
coroutines, futures, async generators, or async clients to the caller's loop.

## Cross-Zone Discipline

Start work on the caller's zone runner. When work must call another zone,
explicitly await that zone:

```python
result = await zone_registry.runner_for(other_zone).call(lambda: do_other_zone_work())
```

Calling the same runner from inside its own loop executes inline. Calling
`call_sync()` from inside the owning runner thread raises because it would
deadlock the loop.

## Limits

Zone runners isolate event loops, not process memory or CPU. These remain shared:

- Python process memory
- Rust kernel state
- database servers and pools
- global singleton objects
- CPU-bound Python running without its own thread or process offload

Blocking network or filesystem clients still need backend timeouts. Runner
isolation prevents a slow zone from parking other zone loops; it does not make
an unbounded blocking call cancellable.

## Shutdown

FastAPI lifespan shutdown stops all zone runners before closing `NexusFS` and
database engines. Each runner rejects new work, cancels pending asyncio tasks,
runs `shutdown_asyncgens()`, closes its loop, and joins its thread with a
bounded timeout.
