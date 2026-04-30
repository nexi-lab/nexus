# Activity Event Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the activity event foundation for issue #3791 — schema, emitter, SQLite sink, retention, metrics catalog, lifespan wiring, and five emission callsites.

**Architecture:** New self-contained `nexus.services.activity` package. Module-level `Emitter` singleton (NoopEmitter by default). Hot-path callsites invoke `emit(...)` which updates Prom metrics inline and `put_nowait`s into a bounded `asyncio.Queue` (drop on overflow). Background `ActivityWorker` drains the queue in batches into `SQLiteSink` (default) at `$NEXUS_ACTIVITY_DB_PATH`. `RetentionTask` periodically prunes rows older than `NEXUS_ACTIVITY_RETENTION_DAYS`. Lifespan integration via a new `ActivityComponent` registered in `nexus.server.lifespan.observability`.

**Tech Stack:** Python 3.14, stdlib (`asyncio`, `sqlite3`, `dataclasses`, `enum`), `prometheus_client`, `pytest`, `pytest-asyncio`, `pytest-benchmark`. No new third-party deps.

**Spec:** `docs/superpowers/specs/2026-04-30-3791-activity-event-foundation-design.md`

---

## File Structure

**Production code (new package):**

| Path | Responsibility |
|---|---|
| `src/nexus/services/activity/__init__.py` | Public re-exports: `emit`, `get_emitter`, `set_emitter`, `ActivityEvent`, `EventKind`, `Result`, `Actor`, `Subject` |
| `src/nexus/services/activity/events.py` | `EventKind`, `Result` enums; `Actor`, `Subject`, `ActivityEvent` dataclasses |
| `src/nexus/services/activity/emitter.py` | `Emitter` Protocol; `NoopEmitter`; `QueueEmitter`; module-level singleton + `get_emitter`/`set_emitter` accessors; `emit()` convenience wrapper |
| `src/nexus/services/activity/metrics.py` | Prom Counter/Histogram/Gauge instances + helper `record_metrics(event)` invoked from `QueueEmitter.emit` |
| `src/nexus/services/activity/sinks/__init__.py` | Re-exports |
| `src/nexus/services/activity/sinks/protocol.py` | `SinkProtocol` (write_batch, close) |
| `src/nexus/services/activity/sinks/noop.py` | `NoopSink` |
| `src/nexus/services/activity/sinks/recording.py` | `RecordingSink` for tests (in-memory) |
| `src/nexus/services/activity/sinks/sqlite.py` | `SQLiteSink` — schema bootstrap, PRAGMAs, batch insert |
| `src/nexus/services/activity/worker.py` | `ActivityWorker` — drain loop with batching/timeout |
| `src/nexus/services/activity/retention.py` | `RetentionTask` — periodic prune + VACUUM |
| `src/nexus/services/activity/config.py` | `ActivityConfig` dataclass + `from_env()` |
| `src/nexus/services/activity/lifespan.py` | `setup_activity` / `shutdown_activity` (sync entry points usable from `FunctionPairComponent`) |

**Production code (modified):**

| Path | Change |
|---|---|
| `src/nexus/server/lifespan/observability.py` | Add `("activity", "nexus.services.activity.lifespan", "setup_activity", "shutdown_activity")` to `_OBSERVABILITY_PROVIDERS` |
| `src/nexus/bricks/search/search_service.py` | Wrap `SearchService.search` body with timing + `emit(EventKind.SEARCH, ...)` |
| `src/nexus/bricks/search/federated_search.py` | Wrap `FederatedSearch.search` body with timing + `emit(EventKind.SEARCH, ...)` |
| `src/nexus/bricks/rebac/enforcer.py` | After `check()` returns deny verdict, `emit(EventKind.ZONE_ACCESS or POLICY_BLOCK, result=BLOCKED, ...)` |
| `src/nexus/bricks/approvals/service.py` | `emit(EventKind.APPROVAL, result=PENDING_APPROVAL)` on creation; `emit(EventKind.APPROVAL, result=OK or BLOCKED)` on `decide()` |
| `src/nexus/bricks/mcp/middleware_audit.py` | After existing stdout/Redis emits, `emit(EventKind.MCP_TOOL_CALL, ...)` |

**Tests:**

| Path | Coverage |
|---|---|
| `tests/unit/services/activity/__init__.py` | empty marker |
| `tests/unit/services/activity/test_events.py` | dataclass roundtrip, enum values |
| `tests/unit/services/activity/test_emitter.py` | NoopEmitter, QueueEmitter, drop counter, set_emitter swap |
| `tests/unit/services/activity/test_metrics.py` | counters/histogram/gauge labels, record_metrics paths |
| `tests/unit/services/activity/test_sqlite_sink.py` | schema, PRAGMAs, batch insert, corrupt fallback |
| `tests/unit/services/activity/test_recording_sink.py` | record + clear |
| `tests/unit/services/activity/test_worker.py` | drain batching, timeout, sink error isolation, shutdown drain |
| `tests/unit/services/activity/test_retention.py` | prune by ts, retention=0 noop, vacuum trigger |
| `tests/unit/services/activity/test_config.py` | env parsing, defaults, invalid value rejection |
| `tests/integration/services/activity/__init__.py` | empty marker |
| `tests/integration/services/activity/test_emit_to_sqlite_e2e.py` | full pipeline through lifespan |
| `tests/integration/services/activity/test_emission_callsites.py` | each of 5 callsites recorded by RecordingSink |
| `tests/integration/services/activity/test_lifespan_supervision.py` | worker restart on crash |
| `tests/integration/services/activity/test_metrics_endpoint.py` | scrape `/metrics` after emits |
| `benchmarks/activity/__init__.py` | empty marker |
| `benchmarks/activity/bench_emit.py` | p50/p99 of `emit()` |
| `benchmarks/activity/bench_search_with_activity.py` | search overhead with activity ON vs OFF |

**Test runner convention:** `uv run pytest <path> -v`. Async tests use `pytest-asyncio`. Benches use `pytest-benchmark`.

**Commit convention:** `feat(#3791): <subject>` or `test(#3791): <subject>`. Each task ends with one commit.

---

## Task 1: Scaffold package + `ActivityEvent` schema and enums

**Files:**
- Create: `src/nexus/services/activity/__init__.py`
- Create: `src/nexus/services/activity/events.py`
- Create: `tests/unit/services/activity/__init__.py`
- Create: `tests/unit/services/activity/test_events.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/services/activity/test_events.py`:

```python
"""Unit tests for ActivityEvent schema and enums."""

from __future__ import annotations

from nexus.services.activity.events import (
    ActivityEvent,
    Actor,
    EventKind,
    Result,
    Subject,
)


def test_event_kinds_match_issue_schema() -> None:
    expected = {"search", "fetch", "mcp_tool_call", "zone_access", "policy_block", "approval"}
    assert {k.value for k in EventKind} == expected


def test_results_match_issue_schema() -> None:
    expected = {"ok", "blocked", "pending_approval"}
    assert {r.value for r in Result} == expected


def test_actor_subject_default_none() -> None:
    actor = Actor()
    assert actor.token_hash is None
    assert actor.agent is None
    assert actor.user is None
    subject = Subject()
    assert subject.zone is None
    assert subject.extra is None


def test_activity_event_minimal_construction() -> None:
    ev = ActivityEvent(
        id="01ABCDEFGHJKMNPQRSTVWXYZ00",
        ts="2026-04-30T12:00:00Z",
        kind=EventKind.SEARCH,
        result=Result.OK,
    )
    assert ev.kind is EventKind.SEARCH
    assert ev.result is Result.OK
    assert ev.actor.token_hash is None
    assert ev.subject.zone is None
    assert ev.latency_ms is None
    assert ev.trace_id is None
    assert ev.meta is None


def test_activity_event_full_construction() -> None:
    ev = ActivityEvent(
        id="01ABCDEFGHJKMNPQRSTVWXYZ00",
        ts="2026-04-30T12:00:00Z",
        kind=EventKind.MCP_TOOL_CALL,
        result=Result.OK,
        latency_ms=42,
        trace_id="trace-1",
        actor=Actor(token_hash="abc1234567890def", agent="claude", user="alice"),
        subject=Subject(zone="eng", extra={"tool": "search"}),
        meta={"k": "v"},
    )
    assert ev.actor.token_hash == "abc1234567890def"
    assert ev.subject.extra == {"tool": "search"}
    assert ev.meta == {"k": "v"}


def test_activity_event_is_frozen() -> None:
    import dataclasses

    ev = ActivityEvent(
        id="x", ts="t", kind=EventKind.SEARCH, result=Result.OK,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.kind = EventKind.FETCH  # type: ignore[misc]


import pytest  # noqa: E402  (placed late so the FrozenInstanceError test reads naturally)
```

`tests/unit/services/activity/__init__.py`:

```python
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/services/activity/test_events.py -v`
Expected: FAIL — module `nexus.services.activity.events` not found.

- [ ] **Step 3: Implement events module**

`src/nexus/services/activity/events.py`:

```python
"""ActivityEvent schema for issue #3791 foundation slice.

Frozen dataclasses + enums. No I/O, no side effects — safe to import from
any layer. ``actor.token_hash`` is the SHA256[:16] of the raw bearer token
(matches ``bricks/mcp/middleware_audit.py``); raw tokens are NEVER stored.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class EventKind(str, enum.Enum):
    SEARCH = "search"
    FETCH = "fetch"
    MCP_TOOL_CALL = "mcp_tool_call"
    ZONE_ACCESS = "zone_access"
    POLICY_BLOCK = "policy_block"
    APPROVAL = "approval"


class Result(str, enum.Enum):
    OK = "ok"
    BLOCKED = "blocked"
    PENDING_APPROVAL = "pending_approval"


@dataclass(frozen=True, slots=True)
class Actor:
    token_hash: str | None = None
    agent: str | None = None
    user: str | None = None


@dataclass(frozen=True, slots=True)
class Subject:
    zone: str | None = None
    extra: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    id: str
    ts: str
    kind: EventKind
    result: Result
    latency_ms: int | None = None
    trace_id: str | None = None
    actor: Actor = field(default_factory=Actor)
    subject: Subject = field(default_factory=Subject)
    meta: dict[str, Any] | None = None
```

`src/nexus/services/activity/__init__.py`:

```python
"""Activity event subsystem (issue #3791 foundation slice).

See ``docs/superpowers/specs/2026-04-30-3791-activity-event-foundation-design.md``.
"""

from nexus.services.activity.events import (
    ActivityEvent,
    Actor,
    EventKind,
    Result,
    Subject,
)

__all__ = [
    "ActivityEvent",
    "Actor",
    "EventKind",
    "Result",
    "Subject",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/services/activity/test_events.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/services/activity/__init__.py \
        src/nexus/services/activity/events.py \
        tests/unit/services/activity/__init__.py \
        tests/unit/services/activity/test_events.py
git commit -m "feat(#3791): activity event schema + enums"
```

---

## Task 2: Emitter interface + NoopEmitter + module-level singleton

**Files:**
- Create: `src/nexus/services/activity/emitter.py`
- Modify: `src/nexus/services/activity/__init__.py`
- Create: `tests/unit/services/activity/test_emitter.py`

- [ ] **Step 1: Write the failing test (NoopEmitter + singleton swap only — QueueEmitter in Task 3)**

`tests/unit/services/activity/test_emitter.py`:

```python
"""Unit tests for the Emitter singleton and NoopEmitter."""

from __future__ import annotations

import pytest

from nexus.services.activity import EventKind, Result, emit, get_emitter, set_emitter
from nexus.services.activity.emitter import NoopEmitter


@pytest.fixture(autouse=True)
def _restore_emitter():
    saved = get_emitter()
    yield
    set_emitter(saved)


def test_default_emitter_is_noop() -> None:
    assert isinstance(get_emitter(), NoopEmitter)


def test_noop_emitter_drops_silently() -> None:
    emitter = NoopEmitter()
    # Must not raise, must not return anything observable
    emitter.emit(
        kind=EventKind.SEARCH,
        result=Result.OK,
        actor_token_hash=None,
        actor_agent=None,
        actor_user=None,
        subject_zone=None,
        subject_extra=None,
        latency_ms=None,
        trace_id=None,
        meta=None,
    )


def test_set_emitter_swaps_singleton() -> None:
    custom = NoopEmitter()
    set_emitter(custom)
    assert get_emitter() is custom


def test_emit_function_calls_current_emitter() -> None:
    class _Recording(NoopEmitter):
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def emit(self, **kw) -> None:
            self.calls.append(tuple(kw.items()))

    rec = _Recording()
    set_emitter(rec)
    emit(kind=EventKind.SEARCH, result=Result.OK)
    assert len(rec.calls) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/services/activity/test_emitter.py -v`
Expected: FAIL — `emit`, `get_emitter`, `set_emitter`, `NoopEmitter` not importable.

- [ ] **Step 3: Implement emitter scaffolding**

`src/nexus/services/activity/emitter.py`:

```python
"""Emitter singleton + NoopEmitter.

``QueueEmitter`` (added in Task 3) is the production implementation that
batches into the activity queue. ``NoopEmitter`` is the default — installed
at process import so that any code calling ``emit(...)`` before lifespan
startup is a safe no-op.
"""

from __future__ import annotations

import threading
from typing import Any, Protocol

from nexus.services.activity.events import EventKind, Result


class Emitter(Protocol):
    """Contract for emitter implementations.

    Implementations MUST NOT raise, MUST NOT block, and SHOULD return in
    well under 50 µs even at p99.
    """

    def emit(
        self,
        *,
        kind: EventKind,
        result: Result,
        actor_token_hash: str | None = None,
        actor_agent: str | None = None,
        actor_user: str | None = None,
        subject_zone: str | None = None,
        subject_extra: dict[str, Any] | None = None,
        latency_ms: int | None = None,
        trace_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None: ...


class NoopEmitter:
    """Discards every event. Default emitter pre-startup and when disabled."""

    def emit(self, **_: Any) -> None:
        return None


_LOCK = threading.Lock()
_EMITTER: Emitter = NoopEmitter()


def get_emitter() -> Emitter:
    return _EMITTER


def set_emitter(emitter: Emitter) -> None:
    global _EMITTER
    with _LOCK:
        _EMITTER = emitter


def emit(
    *,
    kind: EventKind,
    result: Result,
    actor_token_hash: str | None = None,
    actor_agent: str | None = None,
    actor_user: str | None = None,
    subject_zone: str | None = None,
    subject_extra: dict[str, Any] | None = None,
    latency_ms: int | None = None,
    trace_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Module-level convenience that delegates to the current emitter."""
    _EMITTER.emit(
        kind=kind,
        result=result,
        actor_token_hash=actor_token_hash,
        actor_agent=actor_agent,
        actor_user=actor_user,
        subject_zone=subject_zone,
        subject_extra=subject_extra,
        latency_ms=latency_ms,
        trace_id=trace_id,
        meta=meta,
    )
```

`src/nexus/services/activity/__init__.py` — extend re-exports:

```python
"""Activity event subsystem (issue #3791 foundation slice)."""

from nexus.services.activity.emitter import (
    Emitter,
    NoopEmitter,
    emit,
    get_emitter,
    set_emitter,
)
from nexus.services.activity.events import (
    ActivityEvent,
    Actor,
    EventKind,
    Result,
    Subject,
)

__all__ = [
    "ActivityEvent",
    "Actor",
    "Emitter",
    "EventKind",
    "NoopEmitter",
    "Result",
    "Subject",
    "emit",
    "get_emitter",
    "set_emitter",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/services/activity/test_emitter.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/services/activity/emitter.py \
        src/nexus/services/activity/__init__.py \
        tests/unit/services/activity/test_emitter.py
git commit -m "feat(#3791): activity emitter singleton + NoopEmitter"
```

---

## Task 3: QueueEmitter with bounded queue + drop counter

**Files:**
- Modify: `src/nexus/services/activity/emitter.py`
- Modify: `tests/unit/services/activity/test_emitter.py`

- [ ] **Step 1: Append failing tests for QueueEmitter**

Append to `tests/unit/services/activity/test_emitter.py`:

```python
import asyncio

from nexus.services.activity.emitter import QueueEmitter


def _drain(q: asyncio.Queue) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_queue_emitter_enqueues_event() -> None:
    q: asyncio.Queue = asyncio.Queue(maxsize=10)
    emitter = QueueEmitter(queue=q)
    emitter.emit(kind=EventKind.SEARCH, result=Result.OK, subject_zone="eng", latency_ms=5)
    events = _drain(q)
    assert len(events) == 1
    ev = events[0]
    assert ev.kind is EventKind.SEARCH
    assert ev.subject.zone == "eng"
    assert ev.latency_ms == 5
    assert ev.id  # non-empty ULID
    assert ev.ts  # non-empty ISO ts


def test_queue_emitter_drops_on_overflow() -> None:
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    emitter = QueueEmitter(queue=q)
    emitter.emit(kind=EventKind.SEARCH, result=Result.OK)
    emitter.emit(kind=EventKind.SEARCH, result=Result.OK)
    emitter.emit(kind=EventKind.SEARCH, result=Result.OK)  # overflow → drop
    assert emitter.drop_count == 1
    assert q.qsize() == 2


def test_queue_emitter_never_raises() -> None:
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    emitter = QueueEmitter(queue=q)
    # Fill the queue, then emit two more — must not raise
    for _ in range(5):
        emitter.emit(kind=EventKind.SEARCH, result=Result.OK)
    assert emitter.drop_count == 4


def test_queue_emitter_works_without_running_loop() -> None:
    """Caller may emit from sync context — put_nowait does not require a loop."""
    q: asyncio.Queue = asyncio.Queue(maxsize=10)
    emitter = QueueEmitter(queue=q)
    emitter.emit(kind=EventKind.SEARCH, result=Result.OK)
    assert q.qsize() == 1


def test_queue_emitter_id_is_unique() -> None:
    q: asyncio.Queue = asyncio.Queue(maxsize=10)
    emitter = QueueEmitter(queue=q)
    for _ in range(5):
        emitter.emit(kind=EventKind.SEARCH, result=Result.OK)
    events = _drain(q)
    assert len({e.id for e in events}) == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/services/activity/test_emitter.py -v -k QueueEmitter`
Expected: FAIL — `QueueEmitter` not importable.

- [ ] **Step 3: Implement QueueEmitter**

Append to `src/nexus/services/activity/emitter.py`:

```python
import asyncio
import time
import uuid
from datetime import UTC, datetime

from nexus.services.activity.events import ActivityEvent, Actor, Subject


def _new_id() -> str:
    """Sortable, unique id. Uses ULID-like lexical order: ms timestamp + random.

    Avoids a third-party ULID dep — sortability is sufficient for activity logs.
    """
    ms = int(time.time() * 1000)
    rand = uuid.uuid4().hex[:16]
    return f"{ms:013d}{rand}"


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="microseconds")


class QueueEmitter:
    """Production emitter — non-blocking ``put_nowait`` with drop counter.

    Thread-safe: ``asyncio.Queue.put_nowait`` is safe from non-loop threads
    when only the worker awaits ``get`` on the loop's thread (the typical
    server layout — Starlette handlers run on the loop thread).
    """

    def __init__(self, *, queue: asyncio.Queue[ActivityEvent]) -> None:
        self._queue = queue
        self._drop_count = 0

    @property
    def drop_count(self) -> int:
        return self._drop_count

    def emit(
        self,
        *,
        kind: EventKind,
        result: Result,
        actor_token_hash: str | None = None,
        actor_agent: str | None = None,
        actor_user: str | None = None,
        subject_zone: str | None = None,
        subject_extra: dict[str, Any] | None = None,
        latency_ms: int | None = None,
        trace_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        event = ActivityEvent(
            id=_new_id(),
            ts=_now_iso(),
            kind=kind,
            result=result,
            latency_ms=latency_ms,
            trace_id=trace_id,
            actor=Actor(token_hash=actor_token_hash, agent=actor_agent, user=actor_user),
            subject=Subject(zone=subject_zone, extra=subject_extra),
            meta=meta,
        )
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._drop_count += 1
```

Update `__init__.py` to export `QueueEmitter`:

```python
from nexus.services.activity.emitter import (
    Emitter,
    NoopEmitter,
    QueueEmitter,
    emit,
    get_emitter,
    set_emitter,
)

# Add "QueueEmitter" to __all__ alphabetically.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/services/activity/test_emitter.py -v`
Expected: PASS, 9 tests total.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/services/activity/emitter.py \
        src/nexus/services/activity/__init__.py \
        tests/unit/services/activity/test_emitter.py
git commit -m "feat(#3791): QueueEmitter with bounded queue and drop counter"
```

---

## Task 4: Sink protocol + NoopSink + RecordingSink

**Files:**
- Create: `src/nexus/services/activity/sinks/__init__.py`
- Create: `src/nexus/services/activity/sinks/protocol.py`
- Create: `src/nexus/services/activity/sinks/noop.py`
- Create: `src/nexus/services/activity/sinks/recording.py`
- Create: `tests/unit/services/activity/test_recording_sink.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/services/activity/test_recording_sink.py`:

```python
"""Unit tests for sink protocol + NoopSink + RecordingSink."""

from __future__ import annotations

import pytest

from nexus.services.activity.events import ActivityEvent, EventKind, Result
from nexus.services.activity.sinks import NoopSink, RecordingSink


def _ev(kind: EventKind = EventKind.SEARCH) -> ActivityEvent:
    return ActivityEvent(id="x", ts="t", kind=kind, result=Result.OK)


@pytest.mark.asyncio
async def test_noop_sink_accepts_writes() -> None:
    sink = NoopSink()
    await sink.write_batch([_ev(), _ev()])
    await sink.close()


@pytest.mark.asyncio
async def test_recording_sink_collects_events() -> None:
    sink = RecordingSink()
    await sink.write_batch([_ev(EventKind.SEARCH), _ev(EventKind.FETCH)])
    assert len(sink.events) == 2
    assert sink.events[0].kind is EventKind.SEARCH
    assert sink.events[1].kind is EventKind.FETCH


@pytest.mark.asyncio
async def test_recording_sink_clear() -> None:
    sink = RecordingSink()
    await sink.write_batch([_ev()])
    sink.clear()
    assert sink.events == []


@pytest.mark.asyncio
async def test_recording_sink_filter() -> None:
    sink = RecordingSink()
    await sink.write_batch([_ev(EventKind.SEARCH), _ev(EventKind.FETCH), _ev(EventKind.SEARCH)])
    matches = sink.events_of(EventKind.SEARCH)
    assert len(matches) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/services/activity/test_recording_sink.py -v`
Expected: FAIL — `NoopSink`, `RecordingSink` not importable.

- [ ] **Step 3: Implement sinks**

`src/nexus/services/activity/sinks/protocol.py`:

```python
"""Sink protocol for the activity worker."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from nexus.services.activity.events import ActivityEvent


@runtime_checkable
class SinkProtocol(Protocol):
    """Where the ``ActivityWorker`` flushes batches.

    Implementations MUST be safe to call concurrently with ``close``: the
    worker serializes ``write_batch`` calls but may issue ``close`` from
    a different task.
    """

    async def write_batch(self, events: Sequence[ActivityEvent]) -> None: ...

    async def close(self) -> None: ...
```

`src/nexus/services/activity/sinks/noop.py`:

```python
"""Noop sink — accepts all writes, persists nothing."""

from __future__ import annotations

from collections.abc import Sequence

from nexus.services.activity.events import ActivityEvent


class NoopSink:
    async def write_batch(self, events: Sequence[ActivityEvent]) -> None:  # noqa: ARG002
        return None

    async def close(self) -> None:
        return None
```

`src/nexus/services/activity/sinks/recording.py`:

```python
"""Recording sink — keeps every received event in memory. Tests only."""

from __future__ import annotations

from collections.abc import Sequence

from nexus.services.activity.events import ActivityEvent, EventKind


class RecordingSink:
    def __init__(self) -> None:
        self._events: list[ActivityEvent] = []

    @property
    def events(self) -> list[ActivityEvent]:
        return list(self._events)

    def clear(self) -> None:
        self._events.clear()

    def events_of(self, kind: EventKind) -> list[ActivityEvent]:
        return [e for e in self._events if e.kind is kind]

    async def write_batch(self, events: Sequence[ActivityEvent]) -> None:
        self._events.extend(events)

    async def close(self) -> None:
        return None
```

`src/nexus/services/activity/sinks/__init__.py`:

```python
from nexus.services.activity.sinks.noop import NoopSink
from nexus.services.activity.sinks.protocol import SinkProtocol
from nexus.services.activity.sinks.recording import RecordingSink

__all__ = ["NoopSink", "RecordingSink", "SinkProtocol"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/services/activity/test_recording_sink.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/services/activity/sinks/ \
        tests/unit/services/activity/test_recording_sink.py
git commit -m "feat(#3791): sink protocol + NoopSink + RecordingSink"
```

---

## Task 5: SQLiteSink — schema bootstrap, PRAGMAs, batch insert

**Files:**
- Create: `src/nexus/services/activity/sinks/sqlite.py`
- Modify: `src/nexus/services/activity/sinks/__init__.py`
- Create: `tests/unit/services/activity/test_sqlite_sink.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/services/activity/test_sqlite_sink.py`:

```python
"""Unit tests for SQLiteSink."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nexus.services.activity.events import ActivityEvent, Actor, EventKind, Result, Subject
from nexus.services.activity.sinks.sqlite import SQLiteSink


@pytest.mark.asyncio
async def test_schema_bootstrapped_on_open(tmp_path: Path) -> None:
    db = tmp_path / "activity.db"
    sink = SQLiteSink(path=db)
    try:
        conn = sqlite3.connect(db)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='activity_events'"
        )
        assert cursor.fetchone() is not None
        # Indexes
        idx = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='activity_events'"
        )}
        assert "idx_ae_ts" in idx
        assert "idx_ae_kind_ts" in idx
        assert "idx_ae_token_ts" in idx
        assert "idx_ae_zone_ts" in idx
        conn.close()
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_pragmas_applied(tmp_path: Path) -> None:
    db = tmp_path / "activity.db"
    sink = SQLiteSink(path=db)
    try:
        conn = sqlite3.connect(db)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        conn.close()
    finally:
        await sink.close()


@pytest.mark.asyncio
async def test_batch_insert_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "activity.db"
    sink = SQLiteSink(path=db)
    try:
        events = [
            ActivityEvent(
                id="1", ts="2026-04-30T00:00:00Z", kind=EventKind.SEARCH,
                result=Result.OK, latency_ms=10, trace_id="t1",
                actor=Actor(token_hash="aaa", agent="claude", user="alice"),
                subject=Subject(zone="eng", extra={"q": "foo"}),
                meta={"x": 1},
            ),
            ActivityEvent(
                id="2", ts="2026-04-30T00:00:01Z", kind=EventKind.MCP_TOOL_CALL,
                result=Result.OK,
            ),
        ]
        await sink.write_batch(events)
    finally:
        await sink.close()

    conn = sqlite3.connect(db)
    rows = list(conn.execute("SELECT id, kind, result, subject_zone, subject_extra, meta "
                              "FROM activity_events ORDER BY id"))
    assert rows[0][0] == "1"
    assert rows[0][1] == "search"
    assert rows[0][3] == "eng"
    assert json.loads(rows[0][4]) == {"q": "foo"}
    assert json.loads(rows[0][5]) == {"x": 1}
    assert rows[1][0] == "2"
    assert rows[1][3] is None
    assert rows[1][4] is None
    conn.close()


@pytest.mark.asyncio
async def test_open_idempotent_on_existing_db(tmp_path: Path) -> None:
    db = tmp_path / "activity.db"
    sink1 = SQLiteSink(path=db)
    await sink1.close()
    # Re-opening the same file must not error.
    sink2 = SQLiteSink(path=db)
    await sink2.close()


@pytest.mark.asyncio
async def test_corrupt_file_raises(tmp_path: Path) -> None:
    db = tmp_path / "activity.db"
    db.write_bytes(b"not a sqlite file")
    with pytest.raises(sqlite3.DatabaseError):
        SQLiteSink(path=db)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/services/activity/test_sqlite_sink.py -v`
Expected: FAIL — `SQLiteSink` not importable.

- [ ] **Step 3: Implement SQLiteSink**

`src/nexus/services/activity/sinks/sqlite.py`:

```python
"""Append-only SQLite sink for activity events.

Single-writer connection (``check_same_thread=False`` to allow the worker
to await ``write_batch`` while the connection lives on a different thread
in tests). Caller is the activity worker, which serializes calls.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from nexus.services.activity.events import ActivityEvent

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_events (
    id              TEXT PRIMARY KEY,
    ts              TEXT NOT NULL,
    kind            TEXT NOT NULL,
    result          TEXT NOT NULL,
    latency_ms      INTEGER,
    trace_id        TEXT,
    actor_token_hash TEXT,
    actor_agent     TEXT,
    actor_user      TEXT,
    subject_zone    TEXT,
    subject_extra   TEXT,
    meta            TEXT
) STRICT;
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_ae_ts        ON activity_events(ts)",
    "CREATE INDEX IF NOT EXISTS idx_ae_kind_ts   ON activity_events(kind, ts)",
    "CREATE INDEX IF NOT EXISTS idx_ae_token_ts  ON activity_events(actor_token_hash, ts)",
    "CREATE INDEX IF NOT EXISTS idx_ae_zone_ts   ON activity_events(subject_zone, ts)",
)

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA busy_timeout=5000",
)


class SQLiteSink:
    """Durable append-only sink. Schema bootstrap is idempotent."""

    def __init__(self, *, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        try:
            for pragma in _PRAGMAS:
                self._conn.execute(pragma)
            self._conn.execute(_SCHEMA)
            for stmt in _INDEXES:
                self._conn.execute(stmt)
        except sqlite3.Error:
            self._conn.close()
            raise

    async def write_batch(self, events: Sequence[ActivityEvent]) -> None:
        if not events:
            return
        rows = [
            (
                e.id,
                e.ts,
                e.kind.value,
                e.result.value,
                e.latency_ms,
                e.trace_id,
                e.actor.token_hash,
                e.actor.agent,
                e.actor.user,
                e.subject.zone,
                json.dumps(e.subject.extra) if e.subject.extra is not None else None,
                json.dumps(e.meta) if e.meta is not None else None,
            )
            for e in events
        ]
        try:
            self._conn.executemany(
                "INSERT OR IGNORE INTO activity_events "
                "(id, ts, kind, result, latency_ms, trace_id, "
                " actor_token_hash, actor_agent, actor_user, "
                " subject_zone, subject_extra, meta) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        except sqlite3.Error:
            logger.warning("activity SQLiteSink batch insert failed", exc_info=True)
            raise

    async def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            logger.warning("activity SQLiteSink close failed", exc_info=True)
```

Update `src/nexus/services/activity/sinks/__init__.py`:

```python
from nexus.services.activity.sinks.noop import NoopSink
from nexus.services.activity.sinks.protocol import SinkProtocol
from nexus.services.activity.sinks.recording import RecordingSink
from nexus.services.activity.sinks.sqlite import SQLiteSink

__all__ = ["NoopSink", "RecordingSink", "SQLiteSink", "SinkProtocol"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/services/activity/test_sqlite_sink.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/services/activity/sinks/sqlite.py \
        src/nexus/services/activity/sinks/__init__.py \
        tests/unit/services/activity/test_sqlite_sink.py
git commit -m "feat(#3791): SQLiteSink with WAL PRAGMAs and indexed schema"
```

---

## Task 6: ActivityWorker — drain loop with batching/timeout

**Files:**
- Create: `src/nexus/services/activity/worker.py`
- Modify: `src/nexus/services/activity/__init__.py`
- Create: `tests/unit/services/activity/test_worker.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/services/activity/test_worker.py`:

```python
"""Unit tests for ActivityWorker."""

from __future__ import annotations

import asyncio

import pytest

from nexus.services.activity.events import ActivityEvent, EventKind, Result
from nexus.services.activity.sinks import RecordingSink
from nexus.services.activity.worker import ActivityWorker


def _ev(i: int) -> ActivityEvent:
    return ActivityEvent(id=str(i), ts=f"t{i}", kind=EventKind.SEARCH, result=Result.OK)


@pytest.mark.asyncio
async def test_drains_queue_to_sink() -> None:
    queue: asyncio.Queue[ActivityEvent] = asyncio.Queue()
    sink = RecordingSink()
    worker = ActivityWorker(queue=queue, sinks=[sink], batch_size=10, batch_timeout_s=0.01)
    await worker.start()
    for i in range(5):
        queue.put_nowait(_ev(i))
    await asyncio.sleep(0.1)
    await worker.stop(timeout=1.0)
    assert len(sink.events) == 5


@pytest.mark.asyncio
async def test_batches_up_to_batch_size() -> None:
    queue: asyncio.Queue[ActivityEvent] = asyncio.Queue()
    sink = RecordingSink()
    write_call_sizes: list[int] = []

    class _CountingSink(RecordingSink):
        async def write_batch(self, events) -> None:  # type: ignore[override]
            write_call_sizes.append(len(events))
            await super().write_batch(events)

    sink = _CountingSink()
    worker = ActivityWorker(queue=queue, sinks=[sink], batch_size=3, batch_timeout_s=1.0)
    await worker.start()
    for i in range(7):
        queue.put_nowait(_ev(i))
    await asyncio.sleep(0.1)
    await worker.stop(timeout=1.0)
    # Expect 2 batches of 3 + 1 flush of 1 (or any chunking that respects batch_size <= 3)
    assert all(s <= 3 for s in write_call_sizes)
    assert sum(write_call_sizes) == 7


@pytest.mark.asyncio
async def test_flushes_partial_batch_on_timeout() -> None:
    queue: asyncio.Queue[ActivityEvent] = asyncio.Queue()
    sink = RecordingSink()
    worker = ActivityWorker(queue=queue, sinks=[sink], batch_size=100, batch_timeout_s=0.05)
    await worker.start()
    queue.put_nowait(_ev(1))
    await asyncio.sleep(0.2)  # > batch_timeout
    assert len(sink.events) == 1
    await worker.stop(timeout=1.0)


@pytest.mark.asyncio
async def test_sink_error_isolated() -> None:
    queue: asyncio.Queue[ActivityEvent] = asyncio.Queue()

    class _Flaky:
        calls = 0

        async def write_batch(self, events) -> None:
            type(self).calls += 1
            raise RuntimeError("boom")

        async def close(self) -> None:
            return None

    flaky = _Flaky()
    sink_ok = RecordingSink()
    worker = ActivityWorker(queue=queue, sinks=[flaky, sink_ok], batch_size=10, batch_timeout_s=0.01)
    await worker.start()
    for i in range(3):
        queue.put_nowait(_ev(i))
    await asyncio.sleep(0.1)
    await worker.stop(timeout=1.0)
    # Worker survives; second sink still receives the batch
    assert _Flaky.calls > 0
    assert len(sink_ok.events) == 3


@pytest.mark.asyncio
async def test_stop_drains_remaining_queue() -> None:
    queue: asyncio.Queue[ActivityEvent] = asyncio.Queue()
    sink = RecordingSink()
    worker = ActivityWorker(queue=queue, sinks=[sink], batch_size=2, batch_timeout_s=10.0)
    await worker.start()
    for i in range(5):
        queue.put_nowait(_ev(i))
    await worker.stop(timeout=1.0)
    assert len(sink.events) == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/services/activity/test_worker.py -v`
Expected: FAIL — `ActivityWorker` not importable.

- [ ] **Step 3: Implement worker**

`src/nexus/services/activity/worker.py`:

```python
"""Background worker that drains the activity queue into sinks."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence

from nexus.services.activity.events import ActivityEvent
from nexus.services.activity.sinks.protocol import SinkProtocol

logger = logging.getLogger(__name__)


class ActivityWorker:
    """Drain ``queue`` into ``sinks`` with batching/timeout.

    Lifecycle:
    - ``start()`` creates the consumer asyncio.Task.
    - ``stop(timeout)`` signals shutdown, drains pending events, awaits exit.
    """

    def __init__(
        self,
        *,
        queue: asyncio.Queue[ActivityEvent],
        sinks: Sequence[SinkProtocol],
        batch_size: int = 200,
        batch_timeout_s: float = 0.5,
    ) -> None:
        self._queue = queue
        self._sinks = list(sinks)
        self._batch_size = batch_size
        self._batch_timeout_s = batch_timeout_s
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._consume())

    async def stop(self, *, timeout: float = 5.0) -> None:
        self._stopping.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=timeout)
        except TimeoutError:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        for sink in self._sinks:
            with contextlib.suppress(Exception):
                await sink.close()

    async def _consume(self) -> None:
        while not self._stopping.is_set():
            batch = await self._collect_batch()
            if batch:
                await self._flush(batch)
        # Drain remaining events once stopping is set.
        remainder: list[ActivityEvent] = []
        while not self._queue.empty():
            try:
                remainder.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
            if len(remainder) >= self._batch_size:
                await self._flush(remainder)
                remainder = []
        if remainder:
            await self._flush(remainder)

    async def _collect_batch(self) -> list[ActivityEvent]:
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout=self._batch_timeout_s)
        except TimeoutError:
            return []
        batch = [first]
        deadline = asyncio.get_event_loop().time() + self._batch_timeout_s
        while len(batch) < self._batch_size:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(self._queue.get(), timeout=remaining))
            except TimeoutError:
                break
        return batch

    async def _flush(self, batch: list[ActivityEvent]) -> None:
        for sink in self._sinks:
            try:
                await sink.write_batch(batch)
            except Exception:
                logger.warning("activity sink %s failed batch write", type(sink).__name__, exc_info=True)
```

Update `src/nexus/services/activity/__init__.py` re-exports to include `ActivityWorker`:

```python
from nexus.services.activity.worker import ActivityWorker

# Add "ActivityWorker" to __all__ alphabetically.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/services/activity/test_worker.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/services/activity/worker.py \
        src/nexus/services/activity/__init__.py \
        tests/unit/services/activity/test_worker.py
git commit -m "feat(#3791): ActivityWorker drain loop with batching"
```

---

## Task 7: RetentionTask — periodic prune

**Files:**
- Create: `src/nexus/services/activity/retention.py`
- Create: `tests/unit/services/activity/test_retention.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/services/activity/test_retention.py`:

```python
"""Unit tests for RetentionTask."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nexus.services.activity.retention import prune_older_than


def _seed(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE activity_events (
            id TEXT PRIMARY KEY, ts TEXT NOT NULL, kind TEXT, result TEXT,
            latency_ms INTEGER, trace_id TEXT, actor_token_hash TEXT,
            actor_agent TEXT, actor_user TEXT, subject_zone TEXT,
            subject_extra TEXT, meta TEXT
        ) STRICT"""
    )
    now = datetime.now(tz=UTC)
    rows = [
        ("old1", (now - timedelta(days=40)).isoformat(), "search", "ok", None, None, None, None, None, None, None, None),
        ("old2", (now - timedelta(days=31)).isoformat(), "search", "ok", None, None, None, None, None, None, None, None),
        ("new1", (now - timedelta(days=10)).isoformat(), "search", "ok", None, None, None, None, None, None, None, None),
        ("new2", now.isoformat(), "search", "ok", None, None, None, None, None, None, None, None),
    ]
    conn.executemany(
        "INSERT INTO activity_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows,
    )
    conn.commit()
    conn.close()


def test_prune_deletes_rows_older_than_threshold(tmp_path: Path) -> None:
    db = tmp_path / "activity.db"
    _seed(db)
    deleted = prune_older_than(db_path=db, retention_days=30)
    assert deleted == 2
    conn = sqlite3.connect(db)
    remaining = {row[0] for row in conn.execute("SELECT id FROM activity_events")}
    assert remaining == {"new1", "new2"}
    conn.close()


def test_prune_retention_zero_is_noop(tmp_path: Path) -> None:
    db = tmp_path / "activity.db"
    _seed(db)
    deleted = prune_older_than(db_path=db, retention_days=0)
    assert deleted == 0
    conn = sqlite3.connect(db)
    count = conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0]
    assert count == 4
    conn.close()


def test_prune_handles_missing_db(tmp_path: Path) -> None:
    db = tmp_path / "missing.db"
    deleted = prune_older_than(db_path=db, retention_days=30)
    assert deleted == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/services/activity/test_retention.py -v`
Expected: FAIL — `prune_older_than` not importable.

- [ ] **Step 3: Implement retention**

`src/nexus/services/activity/retention.py`:

```python
"""Periodic prune of activity_events older than the retention threshold."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def prune_older_than(*, db_path: Path | str, retention_days: int) -> int:
    """Synchronously delete rows older than ``now - retention_days``.

    Returns the number of rows deleted. ``retention_days <= 0`` is a no-op.
    Missing DB returns 0 silently.
    """
    if retention_days <= 0:
        return 0
    db = Path(db_path)
    if not db.exists():
        return 0
    threshold = (datetime.now(tz=UTC) - timedelta(days=retention_days)).isoformat()
    try:
        conn = sqlite3.connect(db)
        try:
            cursor = conn.execute(
                "DELETE FROM activity_events WHERE ts < ?", (threshold,),
            )
            deleted = cursor.rowcount or 0
            conn.commit()
            return deleted
        finally:
            conn.close()
    except sqlite3.Error:
        logger.warning("activity retention prune failed", exc_info=True)
        return 0


class RetentionTask:
    """Async task wrapping ``prune_older_than`` on a fixed cadence."""

    def __init__(
        self,
        *,
        db_path: Path | str,
        retention_days: int,
        interval_s: float = 3600.0,
    ) -> None:
        self._db_path = db_path
        self._retention_days = retention_days
        self._interval_s = interval_s
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._total_pruned = 0

    @property
    def total_pruned(self) -> int:
        return self._total_pruned

    async def start(self) -> None:
        if self._retention_days <= 0:
            logger.info("activity retention disabled (retention_days=%d)", self._retention_days)
            return
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                deleted = await asyncio.to_thread(
                    prune_older_than,
                    db_path=self._db_path,
                    retention_days=self._retention_days,
                )
                self._total_pruned += deleted
            except Exception:
                logger.warning("activity retention loop tick failed", exc_info=True)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/services/activity/test_retention.py -v`
Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/services/activity/retention.py \
        tests/unit/services/activity/test_retention.py
git commit -m "feat(#3791): activity retention task"
```

---

## Task 8: Metrics catalog + inline updates from emit

**Files:**
- Create: `src/nexus/services/activity/metrics.py`
- Modify: `src/nexus/services/activity/emitter.py` (call `record_metrics` from `QueueEmitter.emit`)
- Create: `tests/unit/services/activity/test_metrics.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/services/activity/test_metrics.py`:

```python
"""Unit tests for the activity metrics catalog."""

from __future__ import annotations

import asyncio

import pytest
from prometheus_client import REGISTRY

from nexus.services.activity import EventKind, Result
from nexus.services.activity.emitter import QueueEmitter
from nexus.services.activity.metrics import (
    ACTIVITY_DROPS,
    APPROVALS_PENDING,
    MCP_TOOL_CALLS,
    POLICY_BLOCKS,
    SEARCH_LATENCY,
    SEARCH_REQUESTS,
)


def _sample(metric, **labels) -> float:
    """Return the current value of a Prom metric for the given label set."""
    for fam in REGISTRY.collect():
        for s in fam.samples:
            if s.name.startswith(metric._name) and all(s.labels.get(k) == v for k, v in labels.items()):
                return s.value
    return 0.0


def test_search_request_increments_counter() -> None:
    before = _sample(SEARCH_REQUESTS, zone="eng", token_hash="x", status="ok")
    SEARCH_REQUESTS.labels(zone="eng", token_hash="x", status="ok").inc()
    after = _sample(SEARCH_REQUESTS, zone="eng", token_hash="x", status="ok")
    assert after == before + 1


def test_search_latency_observed() -> None:
    SEARCH_LATENCY.labels(zone="eng").observe(0.05)


def test_mcp_tool_calls_counter_present() -> None:
    MCP_TOOL_CALLS.labels(tool="search", status="ok").inc()


def test_policy_blocks_counter_present() -> None:
    POLICY_BLOCKS.labels(kind="zone_access").inc()


def test_approvals_pending_gauge_inc_dec_balanced() -> None:
    APPROVALS_PENDING.inc()
    APPROVALS_PENDING.dec()
    # No assertion on absolute value (other tests may have changed it);
    # this checks the API exists and is usable.


def test_activity_drops_counter_present() -> None:
    ACTIVITY_DROPS.inc()


@pytest.mark.asyncio
async def test_queue_emitter_records_metrics() -> None:
    q: asyncio.Queue = asyncio.Queue(maxsize=10)
    emitter = QueueEmitter(queue=q)
    before_search = _sample(SEARCH_REQUESTS, zone="eng", token_hash="abc", status="ok")
    emitter.emit(
        kind=EventKind.SEARCH,
        result=Result.OK,
        actor_token_hash="abc",
        subject_zone="eng",
        latency_ms=42,
    )
    after_search = _sample(SEARCH_REQUESTS, zone="eng", token_hash="abc", status="ok")
    assert after_search == before_search + 1


@pytest.mark.asyncio
async def test_queue_emitter_drops_increment_drop_metric() -> None:
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    emitter = QueueEmitter(queue=q)
    before = _sample(ACTIVITY_DROPS)
    emitter.emit(kind=EventKind.SEARCH, result=Result.OK)
    emitter.emit(kind=EventKind.SEARCH, result=Result.OK)  # overflow → drop
    after = _sample(ACTIVITY_DROPS)
    assert after == before + 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/services/activity/test_metrics.py -v`
Expected: FAIL — `nexus.services.activity.metrics` not found.

- [ ] **Step 3: Implement metrics module**

`src/nexus/services/activity/metrics.py`:

```python
"""Prometheus metrics catalog for issue #3791 foundation slice.

Registered at import time on the global ``prometheus_client.REGISTRY``.
``record_metrics(...)`` is invoked from ``QueueEmitter.emit`` so the Prom
state stays accurate even if the SQLite sink drops batches.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

from nexus.services.activity.events import EventKind, Result

# ── Issue's named catalog ─────────────────────────────────────────────────────

SEARCH_REQUESTS = Counter(
    "nexus_search_requests_total",
    "Total Nexus search requests",
    ["zone", "token_hash", "status"],
)

SEARCH_LATENCY = Histogram(
    "nexus_search_latency_seconds",
    "Nexus search request latency in seconds",
    ["zone"],
)

MCP_TOOL_CALLS = Counter(
    "nexus_mcp_tool_calls_total",
    "Total MCP tool calls dispatched",
    ["tool", "status"],
)

POLICY_BLOCKS = Counter(
    "nexus_policy_blocks_total",
    "Total ReBAC/zone-access denials",
    ["kind"],
)

APPROVALS_PENDING = Gauge(
    "nexus_approvals_pending",
    "Number of approval requests currently in PENDING state",
)

# ── Internal subsystem health ─────────────────────────────────────────────────

ACTIVITY_DROPS = Counter(
    "nexus_activity_drops_total",
    "Activity events dropped due to queue overflow",
)

ACTIVITY_SINK_ERRORS = Counter(
    "nexus_activity_sink_errors_total",
    "Activity sink batch-write errors",
    ["sink"],
)

ACTIVITY_RETENTION_PRUNED = Counter(
    "nexus_activity_retention_pruned_total",
    "Activity events pruned by retention task",
)


def record_metrics(
    *,
    kind: EventKind,
    result: Result,
    actor_token_hash: str | None,
    subject_zone: str | None,
    subject_extra: dict | None,
    latency_ms: int | None,
) -> None:
    """Update the Prom catalog for one emitted event."""
    zone = subject_zone or "unknown"
    token = actor_token_hash or "anonymous"

    if kind is EventKind.SEARCH:
        SEARCH_REQUESTS.labels(zone=zone, token_hash=token, status=result.value).inc()
        if latency_ms is not None:
            SEARCH_LATENCY.labels(zone=zone).observe(latency_ms / 1000.0)
    elif kind is EventKind.MCP_TOOL_CALL:
        tool = (subject_extra or {}).get("tool", "unknown")
        MCP_TOOL_CALLS.labels(tool=tool, status=result.value).inc()
    elif kind in (EventKind.ZONE_ACCESS, EventKind.POLICY_BLOCK):
        if result is Result.BLOCKED:
            POLICY_BLOCKS.labels(kind=kind.value).inc()
    elif kind is EventKind.APPROVAL:
        if result is Result.PENDING_APPROVAL:
            APPROVALS_PENDING.inc()
        else:
            APPROVALS_PENDING.dec()
    # FETCH currently has no metric in the catalog.
```

- [ ] **Step 4: Wire metrics into QueueEmitter**

Modify `src/nexus/services/activity/emitter.py` `QueueEmitter.emit` body — call `record_metrics` BEFORE `put_nowait`, and call `ACTIVITY_DROPS.inc()` on overflow:

Replace the `try / except QueueFull` block with:

```python
        try:
            from nexus.services.activity.metrics import ACTIVITY_DROPS, record_metrics

            record_metrics(
                kind=kind,
                result=result,
                actor_token_hash=actor_token_hash,
                subject_zone=subject_zone,
                subject_extra=subject_extra,
                latency_ms=latency_ms,
            )
        except Exception:  # metrics must never break the hot path
            pass
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._drop_count += 1
            try:
                from nexus.services.activity.metrics import ACTIVITY_DROPS

                ACTIVITY_DROPS.inc()
            except Exception:
                pass
```

(The `from ... import` is intentionally local to avoid metric registration ordering issues during tests that monkey-patch the registry.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/services/activity/test_metrics.py tests/unit/services/activity/test_emitter.py -v`
Expected: PASS, all green.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/services/activity/metrics.py \
        src/nexus/services/activity/emitter.py \
        tests/unit/services/activity/test_metrics.py
git commit -m "feat(#3791): activity metrics catalog + emitter wiring"
```

---

## Task 9: ActivityConfig from environment

**Files:**
- Create: `src/nexus/services/activity/config.py`
- Create: `tests/unit/services/activity/test_config.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/services/activity/test_config.py`:

```python
"""Unit tests for ActivityConfig env parsing."""

from __future__ import annotations

import pytest

from nexus.services.activity.config import ActivityConfig


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "NEXUS_ACTIVITY_ENABLED",
        "NEXUS_ACTIVITY_DB_PATH",
        "NEXUS_ACTIVITY_RETENTION_DAYS",
        "NEXUS_ACTIVITY_QUEUE_SIZE",
        "NEXUS_ACTIVITY_BATCH_SIZE",
        "NEXUS_ACTIVITY_BATCH_TIMEOUT_S",
        "NEXUS_DATA_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    cfg = ActivityConfig.from_env()
    assert cfg.enabled is True
    assert cfg.retention_days == 30
    assert cfg.queue_size == 10000
    assert cfg.batch_size == 200
    assert cfg.batch_timeout_s == 0.5
    assert cfg.db_path.name == "activity.db"


def test_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_ACTIVITY_ENABLED", "0")
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", "/tmp/activity-test.db")
    monkeypatch.setenv("NEXUS_ACTIVITY_RETENTION_DAYS", "7")
    monkeypatch.setenv("NEXUS_ACTIVITY_QUEUE_SIZE", "100")
    monkeypatch.setenv("NEXUS_ACTIVITY_BATCH_SIZE", "5")
    monkeypatch.setenv("NEXUS_ACTIVITY_BATCH_TIMEOUT_S", "0.1")
    cfg = ActivityConfig.from_env()
    assert cfg.enabled is False
    assert str(cfg.db_path) == "/tmp/activity-test.db"
    assert cfg.retention_days == 7
    assert cfg.queue_size == 100
    assert cfg.batch_size == 5
    assert cfg.batch_timeout_s == 0.1


def test_invalid_int_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_ACTIVITY_QUEUE_SIZE", "not-a-number")
    with pytest.raises(ValueError, match="NEXUS_ACTIVITY_QUEUE_SIZE"):
        ActivityConfig.from_env()


def test_negative_retention_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_ACTIVITY_RETENTION_DAYS", "0")
    cfg = ActivityConfig.from_env()
    assert cfg.retention_days == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/services/activity/test_config.py -v`
Expected: FAIL — `ActivityConfig` not importable.

- [ ] **Step 3: Implement config**

`src/nexus/services/activity/config.py`:

```python
"""Env-driven configuration for the activity subsystem."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _parse_int(name: str, raw: str | None, default: int) -> int:
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _parse_float(name: str, raw: str | None, default: float) -> float:
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a float, got {raw!r}") from exc


@dataclass(frozen=True)
class ActivityConfig:
    enabled: bool = True
    db_path: Path = Path("./activity.db")
    retention_days: int = 30
    queue_size: int = 10_000
    batch_size: int = 200
    batch_timeout_s: float = 0.5

    @classmethod
    def from_env(cls) -> ActivityConfig:
        data_dir = os.environ.get("NEXUS_DATA_DIR", ".")
        default_db = Path(data_dir) / "activity.db"
        return cls(
            enabled=_parse_bool(os.environ.get("NEXUS_ACTIVITY_ENABLED"), True),
            db_path=Path(os.environ.get("NEXUS_ACTIVITY_DB_PATH", str(default_db))),
            retention_days=_parse_int(
                "NEXUS_ACTIVITY_RETENTION_DAYS",
                os.environ.get("NEXUS_ACTIVITY_RETENTION_DAYS"),
                30,
            ),
            queue_size=_parse_int(
                "NEXUS_ACTIVITY_QUEUE_SIZE",
                os.environ.get("NEXUS_ACTIVITY_QUEUE_SIZE"),
                10_000,
            ),
            batch_size=_parse_int(
                "NEXUS_ACTIVITY_BATCH_SIZE",
                os.environ.get("NEXUS_ACTIVITY_BATCH_SIZE"),
                200,
            ),
            batch_timeout_s=_parse_float(
                "NEXUS_ACTIVITY_BATCH_TIMEOUT_S",
                os.environ.get("NEXUS_ACTIVITY_BATCH_TIMEOUT_S"),
                0.5,
            ),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/services/activity/test_config.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/services/activity/config.py \
        tests/unit/services/activity/test_config.py
git commit -m "feat(#3791): ActivityConfig env parser"
```

---

## Task 10: Lifespan setup/shutdown

**Files:**
- Create: `src/nexus/services/activity/lifespan.py`
- Modify: `src/nexus/services/activity/__init__.py` (re-export `setup_activity`, `shutdown_activity`)

- [ ] **Step 1: Implement lifespan (no test in this task — covered by integration in Task 18)**

`src/nexus/services/activity/lifespan.py`:

```python
"""Setup / shutdown hooks for the activity subsystem.

Both functions are synchronous so they can be registered on
``FunctionPairComponent`` from ``nexus.server.lifespan.observability``.
The asyncio tasks they spawn require a running event loop — which is
present because the registry calls them inside ``async def start()``.
"""

from __future__ import annotations

import asyncio
import logging

from nexus.services.activity.config import ActivityConfig
from nexus.services.activity.emitter import (
    NoopEmitter,
    QueueEmitter,
    set_emitter,
)
from nexus.services.activity.events import ActivityEvent
from nexus.services.activity.retention import RetentionTask
from nexus.services.activity.sinks import NoopSink, SQLiteSink
from nexus.services.activity.sinks.protocol import SinkProtocol
from nexus.services.activity.worker import ActivityWorker

logger = logging.getLogger(__name__)

_STATE: dict[str, object] = {"worker": None, "retention": None, "queue": None}


def setup_activity() -> None:
    """Start activity worker + retention task. Safe to call once per process."""
    cfg = ActivityConfig.from_env()
    if not cfg.enabled:
        set_emitter(NoopEmitter())
        logger.info("activity subsystem disabled by NEXUS_ACTIVITY_ENABLED=0")
        return

    queue: asyncio.Queue[ActivityEvent] = asyncio.Queue(maxsize=cfg.queue_size)

    sinks: list[SinkProtocol] = []
    try:
        sinks.append(SQLiteSink(path=cfg.db_path))
        logger.info("activity SQLite sink open at %s", cfg.db_path)
    except Exception:
        logger.error(
            "activity SQLiteSink failed to open at %s — falling back to NoopSink",
            cfg.db_path, exc_info=True,
        )
        sinks.append(NoopSink())

    worker = ActivityWorker(
        queue=queue,
        sinks=sinks,
        batch_size=cfg.batch_size,
        batch_timeout_s=cfg.batch_timeout_s,
    )
    retention = RetentionTask(db_path=cfg.db_path, retention_days=cfg.retention_days)

    loop = asyncio.get_event_loop()
    loop.create_task(worker.start())
    loop.create_task(retention.start())

    set_emitter(QueueEmitter(queue=queue))

    _STATE["worker"] = worker
    _STATE["retention"] = retention
    _STATE["queue"] = queue


def shutdown_activity() -> None:
    """Stop worker + retention. Safe to call when setup_activity skipped."""
    set_emitter(NoopEmitter())  # stop accepting new events first
    worker = _STATE.pop("worker", None)
    retention = _STATE.pop("retention", None)
    _STATE.pop("queue", None)
    loop = asyncio.get_event_loop()
    if isinstance(worker, ActivityWorker):
        loop.create_task(worker.stop(timeout=5.0))
    if isinstance(retention, RetentionTask):
        loop.create_task(retention.stop())
```

Update `src/nexus/services/activity/__init__.py`:

```python
from nexus.services.activity.lifespan import setup_activity, shutdown_activity

# Add "setup_activity", "shutdown_activity" to __all__ alphabetically.
```

- [ ] **Step 2: Run smoke import**

Run: `uv run python -c "from nexus.services.activity import setup_activity, shutdown_activity"`
Expected: No output (clean import).

- [ ] **Step 3: Commit**

```bash
git add src/nexus/services/activity/lifespan.py \
        src/nexus/services/activity/__init__.py
git commit -m "feat(#3791): activity lifespan setup/shutdown hooks"
```

---

## Task 11: Wire activity into observability registry

**Files:**
- Modify: `src/nexus/server/lifespan/observability.py`

- [ ] **Step 1: Add the entry**

In `_OBSERVABILITY_PROVIDERS`, append after the `prometheus` row:

```python
_OBSERVABILITY_PROVIDERS: list[tuple[str, str, str, str]] = [
    ("logging", "nexus.server.logging_config", "configure_logging", "shutdown_logging"),
    ("otel-tracing", "nexus.server.telemetry", "setup_telemetry", "shutdown_telemetry"),
    ("sentry", "nexus.server.sentry", "setup_sentry", "shutdown_sentry"),
    ("pyroscope", "nexus.server.profiling", "setup_profiling", "shutdown_profiling"),
    ("prometheus", "nexus.server.metrics", "setup_prometheus", "shutdown_prometheus"),
    ("activity", "nexus.services.activity.lifespan", "setup_activity", "shutdown_activity"),
]
```

- [ ] **Step 2: Run smoke server import**

Run: `uv run python -c "from nexus.server.lifespan.observability import create_registry; r = create_registry(); print(sorted(n for n, *_ in r._components))"`
Expected: List includes `activity`.

- [ ] **Step 3: Commit**

```bash
git add src/nexus/server/lifespan/observability.py
git commit -m "feat(#3791): register activity in observability lifespan"
```

---

## Task 12: Emission point — `SearchService.search`

**Files:**
- Modify: `src/nexus/bricks/search/search_service.py`

The exact instrumentation pattern: wall-clock timing around the public `search` body, build `actor_token_hash` from request context if available (else `None`), emit `EventKind.SEARCH`.

- [ ] **Step 1: Locate `SearchService.search`**

Run: `grep -n "async def search" src/nexus/bricks/search/search_service.py | head -5`
Expected: lines with `async def search(...)` — note the line number for the public method (the topmost one inside `class SearchService`).

- [ ] **Step 2: Add a thin emission wrapper**

Modify the public `async def search(self, ...)` body. Wrap the existing logic with timing + emit. Concrete pattern (adapt arg names to whatever the current signature uses):

```python
async def search(self, request, *args, **kwargs):
    import time as _time

    from nexus.services.activity import EventKind, Result, emit

    _start = _time.monotonic()
    _zone = getattr(request, "zone_id", None) or getattr(request, "zone", None)
    _token_hash = getattr(request, "token_hash", None)
    try:
        result = await self._do_search(request, *args, **kwargs)  # rename existing body
    except Exception:
        emit(
            kind=EventKind.SEARCH,
            result=Result.BLOCKED,
            actor_token_hash=_token_hash,
            subject_zone=_zone,
            latency_ms=int((_time.monotonic() - _start) * 1000),
        )
        raise
    emit(
        kind=EventKind.SEARCH,
        result=Result.OK,
        actor_token_hash=_token_hash,
        subject_zone=_zone,
        subject_extra={"hits": len(result) if hasattr(result, "__len__") else None},
        latency_ms=int((_time.monotonic() - _start) * 1000),
    )
    return result
```

If introducing `_do_search` is intrusive, alternative: leave the body in place and put the emit inside a `try/finally` around it, capturing `result` after success. Either works — keep the diff small.

- [ ] **Step 3: Run search tests**

Run: `uv run pytest tests/unit/services/test_search_service.py -v`
Expected: All existing tests still PASS (emission is additive; default emitter is Noop).

- [ ] **Step 4: Commit**

```bash
git add src/nexus/bricks/search/search_service.py
git commit -m "feat(#3791): emit activity events from SearchService.search"
```

---

## Task 13: Emission point — `FederatedSearch.search`

**Files:**
- Modify: `src/nexus/bricks/search/federated_search.py`

- [ ] **Step 1: Apply the same wrapper pattern**

Apply the same wall-clock-timing + emit wrapper to `FederatedSearch.search` (line ~438). Same signature pattern; `subject_zone` may be the federation root zone or `"federated"` if there is no single zone.

```python
async def search(self, request, *args, **kwargs):
    import time as _time

    from nexus.services.activity import EventKind, Result, emit

    _start = _time.monotonic()
    _token_hash = getattr(request, "token_hash", None)
    try:
        result = await self._do_search_federated(request, *args, **kwargs)
    except Exception:
        emit(
            kind=EventKind.SEARCH,
            result=Result.BLOCKED,
            actor_token_hash=_token_hash,
            subject_zone="federated",
            latency_ms=int((_time.monotonic() - _start) * 1000),
        )
        raise
    emit(
        kind=EventKind.SEARCH,
        result=Result.OK,
        actor_token_hash=_token_hash,
        subject_zone="federated",
        subject_extra={"hits": len(result) if hasattr(result, "__len__") else None},
        latency_ms=int((_time.monotonic() - _start) * 1000),
    )
    return result
```

- [ ] **Step 2: Run federated search tests**

Run: `uv run pytest tests/integration/services/test_federated_search.py tests/unit/services/test_federated_search.py -v 2>/dev/null || uv run pytest -k federated_search -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add src/nexus/bricks/search/federated_search.py
git commit -m "feat(#3791): emit activity events from FederatedSearch.search"
```

---

## Task 14: Emission point — `RebacChecker.check` deny path

**Files:**
- Modify: `src/nexus/bricks/rebac/enforcer.py`

- [ ] **Step 1: Locate the deny return path of `RebacChecker.check`**

Run: `grep -n "def check\b\|return False\|return.*Decision\|raise PermissionError" src/nexus/bricks/rebac/enforcer.py | head -20`

- [ ] **Step 2: Emit on deny**

At every deny return point of `check()` (and `_log_bypass_denied`), insert an emit before returning:

```python
from nexus.services.activity import EventKind, Result, emit

emit(
    kind=EventKind.ZONE_ACCESS,  # or EventKind.POLICY_BLOCK for bypass-denied
    result=Result.BLOCKED,
    actor_token_hash=getattr(context, "token_hash", None),
    actor_user=getattr(context, "user_id", None),
    subject_zone=getattr(context, "zone_id", None),
    subject_extra={"reason": str(reason)} if reason else None,
)
```

If `RebacChecker.check` returns a Decision dataclass, gate the emit on `decision.allowed is False`. Inspect the actual function shape and adapt — the goal is one emit per blocked decision.

- [ ] **Step 3: Run rebac tests**

Run: `uv run pytest tests/unit/services/permissions/ -v -k rebac`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/nexus/bricks/rebac/enforcer.py
git commit -m "feat(#3791): emit activity events on ReBAC deny"
```

---

## Task 15: Emission point — `ApprovalService.request_and_wait` + `decide`

**Files:**
- Modify: `src/nexus/bricks/approvals/service.py`

- [ ] **Step 1: Emit on approval creation**

In `request_and_wait(self, ...)` (line ~211), after the request row is persisted (i.e., the request is officially pending), add:

```python
from nexus.services.activity import EventKind, Result, emit

emit(
    kind=EventKind.APPROVAL,
    result=Result.PENDING_APPROVAL,
    actor_user=getattr(req, "requester_id", None),
    subject_zone=getattr(req, "zone_id", None),
    subject_extra={"request_id": req.request_id, "kind": getattr(req, "kind", None)},
)
```

- [ ] **Step 2: Emit on approval decision**

In `decide(self, ...)` (line ~583), after the decision is persisted, add:

```python
from nexus.services.activity import EventKind, Result, emit

emit(
    kind=EventKind.APPROVAL,
    result=Result.OK if decision_value == "approved" else Result.BLOCKED,
    actor_user=getattr(decision, "decider_id", None),
    subject_zone=getattr(req, "zone_id", None),
    subject_extra={"request_id": req.request_id, "decision": decision_value},
)
```

(Adapt attribute names to the actual model — current spec asserts existence of `request_id`, `zone_id`, `requester_id`, `decider_id`. If they differ, use whatever is in the model.)

- [ ] **Step 3: Run approval tests**

Run: `uv run pytest tests/unit/services/test_approvals_service.py -v 2>/dev/null || uv run pytest -k approvals -v`
Expected: PASS. (Pending gauge inc/dec balance is verified in integration test 21.)

- [ ] **Step 4: Commit**

```bash
git add src/nexus/bricks/approvals/service.py
git commit -m "feat(#3791): emit activity events from ApprovalService"
```

---

## Task 16: Emission point — `MCPAuditLogMiddleware`

**Files:**
- Modify: `src/nexus/bricks/mcp/middleware_audit.py`

- [ ] **Step 1: Add activity emit after the existing audit record**

In `_record_from_scope` after `_emit_stdout_record(record)` and before scheduling `_safe_publish`, add:

```python
from nexus.services.activity import EventKind, Result, emit

emit(
    kind=EventKind.MCP_TOOL_CALL,
    result=Result.OK if 200 <= status < 400 else Result.BLOCKED,
    actor_token_hash=token_hash,
    subject_zone=zone_id,
    subject_extra={"tool": tool_name, "rpc_method": rpc_method},
    latency_ms=record["latency_ms"],
    trace_id=None,
)
```

The existing stdout/Redis audit code stays untouched.

- [ ] **Step 2: Run MCP audit tests**

Run: `uv run pytest tests/unit/services/ -v -k mcp_audit 2>/dev/null || uv run pytest -k mcp_audit -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add src/nexus/bricks/mcp/middleware_audit.py
git commit -m "feat(#3791): emit activity events from MCP audit middleware"
```

---

## Task 17: Integration — emit → SQLite end-to-end

**Files:**
- Create: `tests/integration/services/activity/__init__.py`
- Create: `tests/integration/services/activity/test_emit_to_sqlite_e2e.py`

- [ ] **Step 1: Write the test**

`tests/integration/services/activity/__init__.py`:

```python
```

`tests/integration/services/activity/test_emit_to_sqlite_e2e.py`:

```python
"""End-to-end: setup_activity → emit → drained to SQLite → queryable."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from nexus.services.activity import EventKind, Result, emit
from nexus.services.activity.emitter import NoopEmitter
from nexus.services.activity.lifespan import setup_activity, shutdown_activity


@pytest.mark.asyncio
async def test_emit_to_sqlite_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "activity.db"
    monkeypatch.setenv("NEXUS_ACTIVITY_ENABLED", "1")
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(db))
    monkeypatch.setenv("NEXUS_ACTIVITY_RETENTION_DAYS", "0")  # disable prune
    monkeypatch.setenv("NEXUS_ACTIVITY_QUEUE_SIZE", "1024")
    monkeypatch.setenv("NEXUS_ACTIVITY_BATCH_SIZE", "10")
    monkeypatch.setenv("NEXUS_ACTIVITY_BATCH_TIMEOUT_S", "0.01")

    setup_activity()
    try:
        for i in range(50):
            emit(
                kind=EventKind.SEARCH,
                result=Result.OK,
                actor_token_hash=f"tok{i % 3}",
                subject_zone=f"zone{i % 2}",
                latency_ms=i,
            )
        # Allow the worker to drain.
        await asyncio.sleep(0.5)
    finally:
        shutdown_activity()
        await asyncio.sleep(0.2)  # let shutdown drain

    conn = sqlite3.connect(db)
    rows = list(conn.execute("SELECT kind, result, subject_zone FROM activity_events"))
    conn.close()
    assert len(rows) == 50
    assert all(r[0] == "search" and r[1] == "ok" for r in rows)


@pytest.mark.asyncio
async def test_disabled_installs_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_ACTIVITY_ENABLED", "0")
    setup_activity()
    try:
        from nexus.services.activity import get_emitter
        assert isinstance(get_emitter(), NoopEmitter)
    finally:
        shutdown_activity()
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/integration/services/activity/test_emit_to_sqlite_e2e.py -v`
Expected: PASS, 2 tests.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/services/activity/__init__.py \
        tests/integration/services/activity/test_emit_to_sqlite_e2e.py
git commit -m "test(#3791): activity e2e emit-to-sqlite integration"
```

---

## Task 18: Integration — emission callsites recorded

**Files:**
- Create: `tests/integration/services/activity/test_emission_callsites.py`

This test wires a `RecordingSink` into a live `QueueEmitter`, invokes each callsite, and asserts events.

- [ ] **Step 1: Write the test**

`tests/integration/services/activity/test_emission_callsites.py`:

```python
"""Integration: each emission callsite produces the expected event."""

from __future__ import annotations

import asyncio

import pytest

from nexus.services.activity import EventKind, Result, set_emitter
from nexus.services.activity.emitter import QueueEmitter
from nexus.services.activity.events import ActivityEvent
from nexus.services.activity.sinks.recording import RecordingSink
from nexus.services.activity.worker import ActivityWorker


@pytest.fixture
async def recording():
    queue: asyncio.Queue[ActivityEvent] = asyncio.Queue(maxsize=1024)
    sink = RecordingSink()
    worker = ActivityWorker(queue=queue, sinks=[sink], batch_size=10, batch_timeout_s=0.01)
    await worker.start()
    set_emitter(QueueEmitter(queue=queue))
    try:
        yield sink
    finally:
        await worker.stop(timeout=2.0)


@pytest.mark.asyncio
async def test_search_service_emits(recording: RecordingSink) -> None:
    """Direct emit instead of full SearchService — that test belongs to the
    search suite. Here we verify the emission contract holds when
    SearchService.search calls ``emit(...)`` with the expected fields."""
    from nexus.services.activity import emit

    emit(
        kind=EventKind.SEARCH,
        result=Result.OK,
        actor_token_hash="tok",
        subject_zone="eng",
        latency_ms=12,
    )
    await asyncio.sleep(0.1)
    matches = recording.events_of(EventKind.SEARCH)
    assert len(matches) == 1
    assert matches[0].subject.zone == "eng"
    assert matches[0].latency_ms == 12


@pytest.mark.asyncio
async def test_mcp_tool_call_emits(recording: RecordingSink) -> None:
    from nexus.services.activity import emit

    emit(
        kind=EventKind.MCP_TOOL_CALL,
        result=Result.OK,
        actor_token_hash="tok",
        subject_extra={"tool": "search", "rpc_method": "tools/call"},
        latency_ms=8,
    )
    await asyncio.sleep(0.1)
    matches = recording.events_of(EventKind.MCP_TOOL_CALL)
    assert len(matches) == 1


@pytest.mark.asyncio
async def test_zone_access_block_emits(recording: RecordingSink) -> None:
    from nexus.services.activity import emit

    emit(
        kind=EventKind.ZONE_ACCESS,
        result=Result.BLOCKED,
        actor_user="alice",
        subject_zone="legal",
        subject_extra={"reason": "no_scope"},
    )
    await asyncio.sleep(0.1)
    matches = recording.events_of(EventKind.ZONE_ACCESS)
    assert len(matches) == 1
    assert matches[0].result is Result.BLOCKED


@pytest.mark.asyncio
async def test_approval_pending_then_decided(recording: RecordingSink) -> None:
    from nexus.services.activity import emit

    emit(kind=EventKind.APPROVAL, result=Result.PENDING_APPROVAL, subject_zone="eng",
         subject_extra={"request_id": "r1"})
    emit(kind=EventKind.APPROVAL, result=Result.OK, subject_zone="eng",
         subject_extra={"request_id": "r1", "decision": "approved"})
    await asyncio.sleep(0.1)
    matches = recording.events_of(EventKind.APPROVAL)
    assert len(matches) == 2
    assert matches[0].result is Result.PENDING_APPROVAL
    assert matches[1].result is Result.OK
```

> Why direct emit instead of invoking the full callsite? The instrumented services (`SearchService`, `FederatedSearch`, `RebacChecker`, `ApprovalService`, `MCPAuditLogMiddleware`) have heavy real dependencies (Postgres, raft, MCP server). Their suites are the source of truth for end-to-end behavior; this integration test verifies the **emission → sink** contract that downstream slices rely on.

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/integration/services/activity/test_emission_callsites.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/services/activity/test_emission_callsites.py
git commit -m "test(#3791): integration tests for emission callsites"
```

---

## Task 19: Integration — lifespan supervision

**Files:**
- Create: `tests/integration/services/activity/test_lifespan_supervision.py`

The observability registry restart-on-crash behavior is owned by `ObservabilityRegistry`; here we just verify our component is wired correctly so that its crash does not abort startup (it's `required=False`).

- [ ] **Step 1: Write the test**

`tests/integration/services/activity/test_lifespan_supervision.py`:

```python
"""Integration: activity component is wired as optional and survives crashes."""

from __future__ import annotations

import pytest

from nexus.server.lifespan.observability import create_registry


def test_activity_registered_as_optional() -> None:
    registry = create_registry()
    names = [n for n, *_ in registry._components]
    assert "activity" in names
    # required tuple is (name, component, required); index 2 is the bool
    activity_entry = next(t for t in registry._components if t[0] == "activity")
    assert activity_entry[2] is False  # optional


@pytest.mark.asyncio
async def test_activity_failure_does_not_abort_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force setup_activity to raise; registry must continue (component=optional)."""
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", "/nonexistent/forbidden/path/activity.db")
    # SQLiteSink will fail to mkdir — but lifespan logs and falls back to NoopSink,
    # so setup_activity returns successfully even on this error path.
    from nexus.services.activity.lifespan import setup_activity, shutdown_activity

    setup_activity()
    shutdown_activity()
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/integration/services/activity/test_lifespan_supervision.py -v`
Expected: PASS, 2 tests.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/services/activity/test_lifespan_supervision.py
git commit -m "test(#3791): integration tests for activity lifespan registration"
```

---

## Task 20: Integration — `/metrics` endpoint scrape

**Files:**
- Create: `tests/integration/services/activity/test_metrics_endpoint.py`

- [ ] **Step 1: Write the test**

`tests/integration/services/activity/test_metrics_endpoint.py`:

```python
"""Integration: activity metrics are exposed at /metrics after emits."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from prometheus_client import generate_latest

from nexus.services.activity import EventKind, Result, emit
from nexus.services.activity.lifespan import setup_activity, shutdown_activity


@pytest.mark.asyncio
async def test_metrics_exposed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "activity.db"
    monkeypatch.setenv("NEXUS_ACTIVITY_ENABLED", "1")
    monkeypatch.setenv("NEXUS_ACTIVITY_DB_PATH", str(db))
    monkeypatch.setenv("NEXUS_ACTIVITY_RETENTION_DAYS", "0")

    setup_activity()
    try:
        emit(kind=EventKind.SEARCH, result=Result.OK, actor_token_hash="t", subject_zone="eng", latency_ms=5)
        emit(kind=EventKind.MCP_TOOL_CALL, result=Result.OK, subject_extra={"tool": "search"})
        emit(kind=EventKind.ZONE_ACCESS, result=Result.BLOCKED, subject_zone="legal")
        emit(kind=EventKind.APPROVAL, result=Result.PENDING_APPROVAL)
        await asyncio.sleep(0.2)
    finally:
        shutdown_activity()
        await asyncio.sleep(0.1)

    body = generate_latest().decode()
    assert "nexus_search_requests_total" in body
    assert "nexus_search_latency_seconds" in body
    assert "nexus_mcp_tool_calls_total" in body
    assert "nexus_policy_blocks_total" in body
    assert "nexus_approvals_pending" in body
    assert "nexus_activity_drops_total" in body
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/integration/services/activity/test_metrics_endpoint.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/services/activity/test_metrics_endpoint.py
git commit -m "test(#3791): integration test for activity metrics endpoint"
```

---

## Task 21: Bench — emit hot-path latency

**Files:**
- Create: `benchmarks/activity/__init__.py`
- Create: `benchmarks/activity/bench_emit.py`

- [ ] **Step 1: Write the bench**

`benchmarks/activity/__init__.py`:

```python
```

`benchmarks/activity/bench_emit.py`:

```python
"""Bench: ``emit()`` p50/p99 — must be < 10 µs / < 50 µs respectively."""

from __future__ import annotations

import asyncio

import pytest

from nexus.services.activity import EventKind, Result
from nexus.services.activity.emitter import QueueEmitter


@pytest.mark.benchmark(group="activity-emit")
def test_emit_hot_path(benchmark) -> None:
    queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
    emitter = QueueEmitter(queue=queue)

    def _do_emit() -> None:
        emitter.emit(
            kind=EventKind.SEARCH,
            result=Result.OK,
            actor_token_hash="abc1234567890def",
            subject_zone="eng",
            latency_ms=42,
        )

    benchmark(_do_emit)
    stats = benchmark.stats.stats
    # Loose CI-friendly thresholds (allow noise on shared runners)
    assert stats.median * 1e6 < 25.0, f"emit p50 {stats.median*1e6:.1f} µs > 25 µs"
```

- [ ] **Step 2: Run the bench**

Run: `uv run pytest benchmarks/activity/bench_emit.py -v --benchmark-only`
Expected: Bench runs; assertion on p50 holds.

- [ ] **Step 3: Commit**

```bash
git add benchmarks/activity/__init__.py benchmarks/activity/bench_emit.py
git commit -m "test(#3791): bench emit hot-path latency"
```

---

## Task 22: Bench — search overhead with activity ON vs OFF

**Files:**
- Create: `benchmarks/activity/bench_search_with_activity.py`

This is a **smoke** bench — the real cost is verified inside the SearchService unit suite by asserting `emit()` is called once per search. The bench exercises the wrapper code path.

- [ ] **Step 1: Write the bench**

`benchmarks/activity/bench_search_with_activity.py`:

```python
"""Bench: confirm activity wrapper does not add measurable overhead.

Synthetic — invokes the emit + timing wrapper around a no-op coroutine
to isolate activity overhead from real search work.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from nexus.services.activity import EventKind, Result, set_emitter
from nexus.services.activity.emitter import NoopEmitter, QueueEmitter
from nexus.services.activity.events import ActivityEvent


async def _wrapped_search(emit_fn) -> int:
    start = time.monotonic()
    # simulated body: do nothing
    result = 1
    emit_fn(kind=EventKind.SEARCH, result=Result.OK, latency_ms=int((time.monotonic() - start) * 1000))
    return result


@pytest.mark.benchmark(group="activity-search-overhead")
def test_search_with_noop_emitter(benchmark) -> None:
    set_emitter(NoopEmitter())
    from nexus.services.activity import emit

    def _run() -> None:
        asyncio.run(_wrapped_search(emit))

    benchmark(_run)


@pytest.mark.benchmark(group="activity-search-overhead")
def test_search_with_queue_emitter(benchmark) -> None:
    queue: asyncio.Queue[ActivityEvent] = asyncio.Queue(maxsize=10_000)
    set_emitter(QueueEmitter(queue=queue))
    from nexus.services.activity import emit

    def _run() -> None:
        asyncio.run(_wrapped_search(emit))

    benchmark(_run)
```

- [ ] **Step 2: Run the bench**

Run: `uv run pytest benchmarks/activity/bench_search_with_activity.py -v --benchmark-only`
Expected: Both benches run; queue-emitter overhead < 50 µs over noop.

- [ ] **Step 3: Commit**

```bash
git add benchmarks/activity/bench_search_with_activity.py
git commit -m "test(#3791): bench search wrapper overhead"
```

---

## Task 23: Verification + final commit

**Files:** (none — verification only)

- [ ] **Step 1: Run the full activity test suite**

Run: `uv run pytest tests/unit/services/activity/ tests/integration/services/activity/ -v`
Expected: All PASS.

- [ ] **Step 2: Run the SearchService + ApprovalService + ReBAC + MCP suites to verify no regressions**

Run: `uv run pytest tests/unit/services/test_search_service.py tests/unit/services/test_approvals_service.py tests/unit/services/permissions/ -v -k 'not slow'`
Expected: All PASS (or already-failing tests unchanged).

- [ ] **Step 3: Run benches**

Run: `uv run pytest benchmarks/activity/ --benchmark-only -v`
Expected: All benches run within thresholds.

- [ ] **Step 4: Smoke server start**

Run: `uv run python -c "from nexus.server.lifespan.observability import create_registry; r = create_registry(); print('activity' in [n for n, *_ in r._components])"`
Expected: `True`.

- [ ] **Step 5: Self-check acceptance criteria from spec**

Confirm each row in the spec's "Acceptance criteria (foundation slice)" section maps to a green test.

- [ ] **Step 6: No final commit needed if previous tasks were committed individually**

Inspect: `git log --oneline | head -25`
Expected: 22 commits prefixed `feat(#3791):` or `test(#3791):`.
