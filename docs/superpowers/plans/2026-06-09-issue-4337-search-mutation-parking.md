# Search Mutation Parking (#4337) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop one unresolvable mutation event from head-of-line blocking search indexing forever: classify resolution failures, bound retries, park exhausted events durably, and give admins REST endpoints to re-drive/discard parked events or force-advance a consumer checkpoint.

**Architecture:** All four durable consumers (`bm25`, `fts`, `embedding`, `txtai`) funnel through `SearchDaemon._resolve_mutations`; a bounded-retry gate lives there (spec: `docs/superpowers/specs/2026-06-09-issue-4337-search-mutation-parking-design.md`). A new `MutationParkStore` mirrors the checkpoint dual-persistence pattern (settings store primary, JSON file fallback). The `MutationResolver` learns to classify read failures permanent vs transient instead of swallowing them. New admin REST endpoints land in the existing search router.

**Tech Stack:** Python 3.12+, asyncio, pytest + pytest-asyncio, FastAPI + TestClient, prometheus_client, dataclasses. Test runner: `uv run pytest`.

**Worktree gotchas (from prior sessions):** if pre-commit fails oddly in this worktree, `rm -rf .venv` (bare `.venv` dir breaks it). Run `uv sync --all-extras` once if `uv run pytest` can't import the package.

---

## File map

| File | Action | Responsibility |
| --- | --- | --- |
| `src/nexus/bricks/search/consumer_metrics.py` | Create | Prometheus metrics (4337), precedent `bricks/auth/consumer_metrics.py` |
| `src/nexus/bricks/search/mutation_parking.py` | Create | `UnresolvedMutationError`, `ParkedEvent`, `MutationParkStore` (dual persistence, cap, dedupe) |
| `src/nexus/bricks/search/mutation_resolver.py` | Modify | `failure_kind`/`failure_detail` classification; stop caching unresolved |
| `src/nexus/bricks/search/daemon.py` | Modify | Config knobs; gate in `_resolve_mutations`; park-store wiring + stats; monotonic checkpoint; `force_checkpoint`/`list_parked`/`retry_parked`/`discard_parked`; delete per-consumer unresolved branches |
| `src/nexus/server/api/v2/routers/search.py` | Modify | `GET /parked`, `POST /parked/retry`, `POST /parked/discard`, `POST /consumers/{name}/skip-to` (admin) |
| `docs/operations/search-mutation-parking.md` | Create | Runbook |
| `tests/unit/bricks/search/test_mutation_parking_store.py` | Create | Store unit tests |
| `tests/unit/bricks/search/test_mutation_resolver_classification.py` | Create | Resolver classification tests |
| `tests/unit/bricks/search/test_daemon_parking_gate.py` | Create | Gate + consumer + checkpoint + retry/discard/stats tests |
| `tests/unit/server/routers/test_search_parked_admin.py` | Create | REST endpoint tests |

Key existing anchors (verified on this branch):

- `DaemonConfig` fields end at `path_context_max_zones: int = 2048` (daemon.py:186).
- `SearchDaemon.__init__` consumer state block at daemon.py:281-293 (`_consumer_last_sequence`, `_checkpoint_file`, `_checkpoint_lock`).
- `_save_consumer_checkpoint` at daemon.py:2952.
- `_run_mutation_consumer` at daemon.py:3071 (handler → `_save_consumer_checkpoint(events[-1].sequence_number)`).
- `_resolve_mutations` at daemon.py:3116; call sites: `_delete_indexes_for_paths` (legacy, :2442), `_consume_bm25_mutations` (:3148), `_consume_fts_mutations` (:3207), `_consume_embedding_mutations` (:3274), `_consume_txtai_mutations` (:3351).
- Unresolved-raise branches to delete: bm25 :3161-3167, fts :3209-3218, embedding :3276-3285.
- `_index_refresh_loop` consumer dict at :2366-2371; reconcile handler dict at :2833-2837.
- `get_stats` returns `"mutation_consumers": self.stats.mutation_consumers` at :3767.
- Router deps: `_get_search_daemon` (search.py:61), `require_admin` exists in `nexus.server.dependencies` (used as `Depends(require_admin)` in `routers/path_contexts.py:223`).
- `SystemSettingDTO` from `get_setting` exposes `.value` (see checkpoint read, daemon.py:2707-2716).

---

### Task 0: Environment sanity check

**Files:** none modified.

- [ ] **Step 0.1: Verify the test environment runs existing search tests**

Run: `uv run pytest tests/unit/bricks/search/test_daemon_bm25_only.py -x -q`
Expected: all tests pass. If imports fail, run `uv sync --all-extras` and retry. If pre-commit later fails with a venv error, `rm -rf .venv`.

---

### Task 1: Prometheus metrics module

**Files:**
- Create: `src/nexus/bricks/search/consumer_metrics.py`

- [ ] **Step 1.1: Create the metrics module**

```python
"""Prometheus metrics for search mutation consumers (#4337).

Low-cardinality labels only:
  - consumer ∈ {bm25, fts, embedding, txtai}
  - kind ∈ {permanent, transient}

Split of responsibilities: the parking gate in ``daemon.py`` increments
``MUTATION_PARKED_TOTAL`` / ``MUTATION_UNRESOLVED_RETRIES_TOTAL``; the
``MutationParkStore`` owns ``MUTATION_PARKED`` (gauge synced on load /
park / remove) and ``MUTATION_PARKED_EVICTED_TOTAL``.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge

MUTATION_PARKED_TOTAL = Counter(
    "nexus_search_mutation_parked_total",
    "Search mutation events parked after exhausting their retry budget",
    labelnames=("consumer", "kind"),
)

MUTATION_PARKED = Gauge(
    # Not "..._parked": prometheus_client strips "_total" from Counter
    # names, so MUTATION_PARKED_TOTAL above already claims the
    # "nexus_search_mutation_parked" base name in the registry.
    "nexus_search_mutation_parked_current",
    "Search mutation events currently parked",
    labelnames=("consumer",),
)

MUTATION_UNRESOLVED_RETRIES_TOTAL = Counter(
    "nexus_search_mutation_unresolved_retries_total",
    "Retry passes that observed an unresolved mutation (early warning before parking)",
    labelnames=("consumer", "kind"),
)

MUTATION_PARKED_EVICTED_TOTAL = Counter(
    "nexus_search_mutation_parked_evicted_total",
    "Parked entries evicted by the per-consumer cap",
    labelnames=("consumer",),
)
```

- [ ] **Step 1.2: Smoke-test the import**

Run: `uv run python -c "from nexus.bricks.search import consumer_metrics as m; m.MUTATION_PARKED.labels(consumer='bm25').set(0); print('ok')"`
Expected: `ok`

- [ ] **Step 1.3: Commit**

```bash
git add src/nexus/bricks/search/consumer_metrics.py
git commit -m "feat(#4337): prometheus metrics for search mutation parking"
```

---

### Task 2: ParkedEvent + MutationParkStore

**Files:**
- Create: `src/nexus/bricks/search/mutation_parking.py`
- Test: `tests/unit/bricks/search/test_mutation_parking_store.py`

- [ ] **Step 2.1: Write the failing tests**

```python
"""Unit tests for MutationParkStore / ParkedEvent (#4337)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nexus.bricks.search.mutation_events import SearchMutationEvent, SearchMutationOp
from nexus.bricks.search.mutation_parking import MutationParkStore, ParkedEvent


class FakeSettingsStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_setting(self, key: str) -> Any:
        value = self.values.get(key)
        return SimpleNamespace(value=value) if value is not None else None

    def set_setting(self, key: str, value: str, *, description: str | None = None) -> None:
        self.values[key] = value


class BrokenSettingsStore:
    def get_setting(self, key: str) -> Any:
        raise RuntimeError("settings store down")

    def set_setting(self, key: str, value: str, *, description: str | None = None) -> None:
        raise RuntimeError("settings store down")


def _entry(consumer: str = "bm25", event_id: str = "search:op-1", seq: int = 7) -> ParkedEvent:
    return ParkedEvent(
        consumer=consumer,
        event_id=event_id,
        operation_id=event_id.removeprefix("search:"),
        op="upsert",
        path="/zone/z1/docs/a.md",
        zone_id="z1",
        timestamp="2026-06-09T01:02:03",
        sequence_number=seq,
        new_path=None,
        kind="permanent",
        detail="FileNotFoundError('/zone/z1/docs/a.md')",
        attempts=3,
        parked_at=1750000000.0,
    )


def test_parked_event_roundtrip_dict_and_event() -> None:
    entry = _entry()
    rebuilt = ParkedEvent.from_dict(entry.to_dict())
    assert rebuilt == entry
    event = rebuilt.to_event()
    assert isinstance(event, SearchMutationEvent)
    assert event.op == SearchMutationOp.UPSERT
    assert event.event_id == "search:op-1"
    assert event.sequence_number == 7
    assert event.virtual_path == "/docs/a.md"


@pytest.mark.asyncio
async def test_park_and_load_via_settings_store(tmp_path) -> None:
    settings = FakeSettingsStore()
    store = MutationParkStore(
        settings_store=settings,
        fallback_file=tmp_path / "mutation-parked.json",
        max_entries_per_consumer=10,
    )
    await store.load()
    await store.park(_entry())
    assert store.contains("bm25", "search:op-1")
    assert store.count("bm25") == 1

    # A fresh store (simulated restart) loads the same state back.
    store2 = MutationParkStore(
        settings_store=settings,
        fallback_file=tmp_path / "mutation-parked.json",
        max_entries_per_consumer=10,
    )
    await store2.load()
    assert store2.contains("bm25", "search:op-1")
    assert store2.last("bm25") == _entry()


@pytest.mark.asyncio
async def test_park_falls_back_to_file_when_settings_store_broken(tmp_path) -> None:
    store = MutationParkStore(
        settings_store=BrokenSettingsStore(),
        fallback_file=tmp_path / "mutation-parked.json",
        max_entries_per_consumer=10,
    )
    await store.load()
    await store.park(_entry())
    assert (tmp_path / "mutation-parked.json").exists()

    store2 = MutationParkStore(
        settings_store=None,
        fallback_file=tmp_path / "mutation-parked.json",
        max_entries_per_consumer=10,
    )
    await store2.load()
    assert store2.contains("bm25", "search:op-1")


@pytest.mark.asyncio
async def test_park_dedupes_by_consumer_and_event_id(tmp_path) -> None:
    store = MutationParkStore(
        settings_store=None,
        fallback_file=tmp_path / "mutation-parked.json",
        max_entries_per_consumer=10,
    )
    await store.load()
    await store.park(_entry())
    replacement = _entry()
    replacement = ParkedEvent.from_dict({**replacement.to_dict(), "attempts": 9})
    await store.park(replacement)
    assert store.count("bm25") == 1
    assert store.last("bm25").attempts == 9


@pytest.mark.asyncio
async def test_cap_evicts_oldest_first(tmp_path) -> None:
    store = MutationParkStore(
        settings_store=None,
        fallback_file=tmp_path / "mutation-parked.json",
        max_entries_per_consumer=2,
    )
    await store.load()
    await store.park(_entry(event_id="search:op-1", seq=1))
    await store.park(_entry(event_id="search:op-2", seq=2))
    await store.park(_entry(event_id="search:op-3", seq=3))
    assert store.count("bm25") == 2
    assert not store.contains("bm25", "search:op-1")
    assert store.contains("bm25", "search:op-2")
    assert store.contains("bm25", "search:op-3")


@pytest.mark.asyncio
async def test_remove_returns_removed_entries(tmp_path) -> None:
    store = MutationParkStore(
        settings_store=None,
        fallback_file=tmp_path / "mutation-parked.json",
        max_entries_per_consumer=10,
    )
    await store.load()
    await store.park(_entry(event_id="search:op-1"))
    await store.park(_entry(event_id="search:op-2"))
    removed = await store.remove("bm25", ["search:op-1", "search:missing"])
    assert [e.event_id for e in removed] == ["search:op-1"]
    assert store.count("bm25") == 1


@pytest.mark.asyncio
async def test_park_raises_when_both_persistence_paths_fail(tmp_path, monkeypatch) -> None:
    store = MutationParkStore(
        settings_store=BrokenSettingsStore(),
        fallback_file=tmp_path / "nope" / "mutation-parked.json",
        max_entries_per_consumer=10,
    )
    await store.load()

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.write_text", _boom)
    with pytest.raises(OSError):
        await store.park(_entry())
```

- [ ] **Step 2.2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/bricks/search/test_mutation_parking_store.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.bricks.search.mutation_parking'`

- [ ] **Step 2.3: Implement `mutation_parking.py`**

```python
"""Parked-event store for search mutation consumers (#4337).

When the bounded-retry gate in ``SearchDaemon._resolve_mutations`` exhausts
an unresolved mutation's budget, the event is parked here: skipped by the
live consumer (the checkpoint advances past it) but durably recorded so an
admin can re-drive or discard it via the REST surface.

Persistence mirrors the mutation-checkpoint pattern: settings store primary
(key ``search_mutation_parked``), JSON file fallback next to
``mutation-checkpoints.json``. If BOTH paths fail, ``park`` raises so the
caller refuses to checkpoint — an event is never skipped without a record.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from nexus.bricks.search import consumer_metrics
from nexus.bricks.search.mutation_events import SearchMutationEvent, SearchMutationOp

logger = logging.getLogger(__name__)

PARKED_SETTINGS_KEY = "search_mutation_parked"


class UnresolvedMutationError(RuntimeError):
    """Unresolved mutation still within its retry budget — batch must retry."""


@dataclass(frozen=True)
class ParkedEvent:
    """A skipped mutation event plus enough context to re-drive it."""

    consumer: str
    event_id: str
    operation_id: str
    op: str  # SearchMutationOp value
    path: str
    zone_id: str
    timestamp: str  # ISO-8601, naive UTC (matches SearchMutationEvent.timestamp)
    sequence_number: int
    new_path: str | None
    kind: str  # "permanent" | "transient"
    detail: str
    attempts: int
    parked_at: float

    @classmethod
    def from_mutation(
        cls,
        consumer: str,
        mutation: Any,
        *,
        kind: str,
        detail: str,
        attempts: int,
    ) -> "ParkedEvent":
        event = mutation.event
        return cls(
            consumer=consumer,
            event_id=event.event_id,
            operation_id=event.operation_id,
            op=event.op.value,
            path=event.path,
            zone_id=event.zone_id,
            timestamp=event.timestamp.isoformat(),
            sequence_number=event.sequence_number,
            new_path=event.new_path,
            kind=kind,
            detail=detail,
            attempts=attempts,
            parked_at=time.time(),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ParkedEvent":
        return cls(
            consumer=str(data["consumer"]),
            event_id=str(data["event_id"]),
            operation_id=str(data["operation_id"]),
            op=str(data["op"]),
            path=str(data["path"]),
            zone_id=str(data["zone_id"]),
            timestamp=str(data["timestamp"]),
            sequence_number=int(data["sequence_number"]),
            new_path=data.get("new_path"),
            kind=str(data.get("kind", "transient")),
            detail=str(data.get("detail", "")),
            attempts=int(data.get("attempts", 0)),
            parked_at=float(data.get("parked_at", 0.0)),
        )

    def to_event(self) -> SearchMutationEvent:
        return SearchMutationEvent(
            event_id=self.event_id,
            operation_id=self.operation_id,
            op=SearchMutationOp(self.op),
            path=self.path,
            zone_id=self.zone_id,
            timestamp=datetime.fromisoformat(self.timestamp),
            sequence_number=self.sequence_number,
            new_path=self.new_path,
        )


class MutationParkStore:
    """Durable per-consumer list of parked mutation events.

    In-memory dict is the source of truth after ``load``; every mutation
    re-persists the full document (small: capped per consumer).
    """

    def __init__(
        self,
        *,
        settings_store: Any | None,
        fallback_file: Path,
        max_entries_per_consumer: int = 200,
    ) -> None:
        self._settings_store = settings_store
        self._fallback_file = fallback_file
        self._max_entries = max(1, int(max_entries_per_consumer))
        self._lock = asyncio.Lock()
        self._entries: dict[str, list[ParkedEvent]] = {}

    # -- queries (sync, in-memory) -----------------------------------------

    def contains(self, consumer: str, event_id: str) -> bool:
        return any(e.event_id == event_id for e in self._entries.get(consumer, []))

    def count(self, consumer: str) -> int:
        return len(self._entries.get(consumer, []))

    def last(self, consumer: str) -> ParkedEvent | None:
        entries = self._entries.get(consumer, [])
        return entries[-1] if entries else None

    def list_entries(self, consumer: str | None = None) -> dict[str, list[ParkedEvent]]:
        if consumer is not None:
            return {consumer: list(self._entries.get(consumer, []))}
        return {name: list(entries) for name, entries in self._entries.items()}

    # -- mutations ----------------------------------------------------------

    async def load(self) -> None:
        """Load persisted state (best-effort) and seed the parked gauge."""
        async with self._lock:
            payload: str | None = None
            if self._settings_store is not None:
                try:
                    setting = self._settings_store.get_setting(PARKED_SETTINGS_KEY)
                    if setting is not None:
                        payload = getattr(setting, "value", None)
                except Exception as exc:
                    logger.warning("Parked-event store read falling back to file storage: %s", exc)
                    self._settings_store = None
            if payload is None:
                payload = await asyncio.to_thread(self._read_file)
            if payload:
                try:
                    raw = json.loads(payload)
                    self._entries = {
                        str(consumer): [ParkedEvent.from_dict(item) for item in items]
                        for consumer, items in raw.items()
                    }
                except Exception as exc:
                    logger.error(
                        "Parked-event store unreadable; starting empty "
                        "(previous parked records lost): %s",
                        exc,
                    )
                    self._entries = {}
            for consumer in self._entries:
                self._sync_gauge(consumer)

    async def park(self, entry: ParkedEvent) -> None:
        """Insert/replace an entry; evict oldest beyond the cap; persist.

        Raises if BOTH persistence paths fail — callers must not skip an
        event that has no durable record.
        """
        async with self._lock:
            entries = [
                e for e in self._entries.get(entry.consumer, []) if e.event_id != entry.event_id
            ]
            entries.append(entry)
            while len(entries) > self._max_entries:
                evicted = entries.pop(0)
                consumer_metrics.MUTATION_PARKED_EVICTED_TOTAL.labels(consumer=entry.consumer).inc()
                logger.error(
                    "Parked-event cap (%d) exceeded for consumer=%s — evicting "
                    "oldest entry event_id=%s path=%s (parking storm?)",
                    self._max_entries,
                    entry.consumer,
                    evicted.event_id,
                    evicted.path,
                )
            self._entries[entry.consumer] = entries
            await self._persist()
            self._sync_gauge(entry.consumer)

    async def remove(self, consumer: str, event_ids: list[str]) -> list[ParkedEvent]:
        """Remove entries by id; returns the removed entries."""
        wanted = set(event_ids)
        async with self._lock:
            entries = self._entries.get(consumer, [])
            removed = [e for e in entries if e.event_id in wanted]
            if not removed:
                return []
            self._entries[consumer] = [e for e in entries if e.event_id not in wanted]
            await self._persist()
            self._sync_gauge(consumer)
            return removed

    # -- internals ----------------------------------------------------------

    def _read_file(self) -> str | None:
        try:
            if not self._fallback_file.exists():
                return None
            return self._fallback_file.read_text()
        except OSError as exc:
            logger.warning("Parked-event fallback file unreadable: %s", exc)
            return None

    def _sync_gauge(self, consumer: str) -> None:
        consumer_metrics.MUTATION_PARKED.labels(consumer=consumer).set(
            len(self._entries.get(consumer, []))
        )

    async def _persist(self) -> None:
        payload = json.dumps(
            {
                consumer: [e.to_dict() for e in entries]
                for consumer, entries in self._entries.items()
                if entries
            },
            sort_keys=True,
        )
        if self._settings_store is not None:
            try:
                self._settings_store.set_setting(
                    PARKED_SETTINGS_KEY,
                    payload,
                    description="Parked search mutation events (#4337)",
                )
                return
            except Exception as exc:
                logger.warning("Parked-event store falling back to file storage: %s", exc)
                self._settings_store = None

        def _write() -> None:
            self._fallback_file.parent.mkdir(parents=True, exist_ok=True)
            self._fallback_file.write_text(payload)

        await asyncio.to_thread(_write)
```

- [ ] **Step 2.4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/bricks/search/test_mutation_parking_store.py -q`
Expected: all PASS

- [ ] **Step 2.5: Commit**

```bash
git add src/nexus/bricks/search/mutation_parking.py tests/unit/bricks/search/test_mutation_parking_store.py
git commit -m "feat(#4337): MutationParkStore with dual persistence and cap"
```

---

### Task 3: Resolver failure classification

**Files:**
- Modify: `src/nexus/bricks/search/mutation_resolver.py`
- Test: `tests/unit/bricks/search/test_mutation_resolver_classification.py`

- [ ] **Step 3.1: Write the failing tests**

```python
"""MutationResolver failure-classification tests (#4337)."""

from __future__ import annotations

from datetime import datetime

import pytest

from nexus.bricks.search.mutation_events import SearchMutationEvent, SearchMutationOp
from nexus.bricks.search.mutation_resolver import MutationResolver
from nexus.contracts.exceptions import NexusFileNotFoundError


def _event(op: SearchMutationOp = SearchMutationOp.UPSERT, path: str = "/zone/z1/docs/a.md"):
    return SearchMutationEvent(
        event_id="search:op-1",
        operation_id="op-1",
        op=op,
        path=path,
        zone_id="z1",
        timestamp=datetime(2026, 6, 9, 1, 2, 3),
        sequence_number=7,
    )


class NotFoundReader:
    def __init__(self) -> None:
        self.calls = 0

    async def read_text(self, path: str) -> str:
        self.calls += 1
        raise FileNotFoundError(path)


class OutageReader:
    async def read_text(self, path: str) -> str:
        raise TimeoutError("backend down")


class MixedReader:
    """NotFound on scoped path, outage on virtual path."""

    async def read_text(self, path: str) -> str:
        if path.startswith("/zone/"):
            raise FileNotFoundError(path)
        raise TimeoutError("backend down")


class EmptyReader:
    async def read_text(self, path: str) -> str:
        return ""


def test_nexus_not_found_is_filenotfound_subclass() -> None:
    assert issubclass(NexusFileNotFoundError, FileNotFoundError)


@pytest.mark.asyncio
async def test_not_found_on_both_paths_is_permanent() -> None:
    resolver = MutationResolver(file_reader=NotFoundReader(), async_session_factory=None)
    [mutation] = await resolver.resolve_batch([_event()])
    assert mutation.content_resolved is False
    assert mutation.failure_kind == "permanent"
    assert mutation.failure_detail


@pytest.mark.asyncio
async def test_non_notfound_failure_is_transient() -> None:
    resolver = MutationResolver(file_reader=OutageReader(), async_session_factory=None)
    [mutation] = await resolver.resolve_batch([_event()])
    assert mutation.content_resolved is False
    assert mutation.failure_kind == "transient"


@pytest.mark.asyncio
async def test_mixed_failures_are_transient() -> None:
    resolver = MutationResolver(file_reader=MixedReader(), async_session_factory=None)
    [mutation] = await resolver.resolve_batch([_event()])
    assert mutation.failure_kind == "transient"


@pytest.mark.asyncio
async def test_missing_reader_is_transient_boot_window() -> None:
    resolver = MutationResolver(file_reader=None, async_session_factory=None)
    [mutation] = await resolver.resolve_batch([_event()])
    assert mutation.content_resolved is False
    assert mutation.failure_kind == "transient"


@pytest.mark.asyncio
async def test_empty_string_read_is_resolved_not_failure() -> None:
    resolver = MutationResolver(file_reader=EmptyReader(), async_session_factory=None)
    [mutation] = await resolver.resolve_batch([_event()])
    assert mutation.content_resolved is True
    assert mutation.content == ""
    assert mutation.failure_kind is None


@pytest.mark.asyncio
async def test_delete_events_have_no_failure() -> None:
    resolver = MutationResolver(file_reader=NotFoundReader(), async_session_factory=None)
    [mutation] = await resolver.resolve_batch([_event(op=SearchMutationOp.DELETE)])
    assert mutation.content_resolved is True
    assert mutation.failure_kind is None


@pytest.mark.asyncio
async def test_unresolved_mutations_are_not_cached() -> None:
    reader = NotFoundReader()
    resolver = MutationResolver(file_reader=reader, async_session_factory=None)
    await resolver.resolve_batch([_event()])
    first_calls = reader.calls
    assert first_calls > 0
    assert "search:op-1" not in resolver._cache
    await resolver.resolve_batch([_event()])
    assert reader.calls > first_calls  # second pass re-read (no stale cache)


@pytest.mark.asyncio
async def test_resolved_mutations_are_still_cached() -> None:
    resolver = MutationResolver(file_reader=EmptyReader(), async_session_factory=None)
    await resolver.resolve_batch([_event()])
    assert "search:op-1" in resolver._cache
```

- [ ] **Step 3.2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/bricks/search/test_mutation_resolver_classification.py -q`
Expected: FAIL — `ResolvedMutation` has no `failure_kind` (AttributeError / assertion errors)

- [ ] **Step 3.3: Add the fields to `ResolvedMutation`**

In `mutation_resolver.py`, extend the dataclass (after `content_resolved: bool = True`) and document:

```python
    event: SearchMutationEvent
    zone_id: str
    virtual_path: str
    path_id: str
    doc_id: str
    content: str | None = None
    path_id_resolved: bool = True
    content_resolved: bool = True
    # #4337: when content_resolved=False, classify WHY so the parking gate
    # can budget retries: "permanent" (every read raised FileNotFoundError —
    # the path is gone) vs "transient" (no reader yet, non-NotFound error,
    # backend outage). None when content_resolved=True.
    failure_kind: str | None = None
    failure_detail: str | None = None
```

- [ ] **Step 3.4: Replace `_read_content` with the classifying version**

Replace the whole `_read_content` method:

```python
async def _read_content(
    self, scoped_path: str, virtual_path: str
) -> tuple[str | None, str | None, str | None]:
    """Return ``(content, failure_kind, failure_detail)``.

    ``content`` is a str (possibly empty) on success. On failure it is
    ``None`` and ``failure_kind`` classifies the failure (#4337):

      * ``"permanent"`` — every attempted read raised
        ``FileNotFoundError`` (incl. ``NexusFileNotFoundError``): the
        path is gone as far as the reader is concerned.
      * ``"transient"`` — anything else: no reader attached yet (the
        server lifespan wires it post-boot), a non-string return, or a
        non-NotFound exception (backend outage, timeout).
    """
    if self._file_reader is None:
        return None, "transient", "no file_reader attached"

    candidates = [scoped_path]
    if virtual_path != scoped_path:
        candidates.append(virtual_path)

    failures: list[tuple[str, BaseException | str]] = []
    for candidate in candidates:
        try:
            content = await self._file_reader.read_text(candidate)
        except Exception as exc:
            failures.append((candidate, exc))
            continue
        if isinstance(content, str):
            return content, None, None
        failures.append((candidate, f"non-string return {type(content).__name__!r}"))

    permanent = all(isinstance(failure, FileNotFoundError) for _, failure in failures)
    kind = "permanent" if permanent else "transient"
    detail = "; ".join(f"{path}: {failure!r}" for path, failure in failures)
    return None, kind, detail
```

The old version's `with contextlib.suppress(...)` block was the module's only use of `contextlib` — delete the `import contextlib` line at the top of `mutation_resolver.py` (ruff will flag it otherwise).

- [ ] **Step 3.5: Thread failures through `_lookup_content`**

Replace `_lookup_content` so it returns both maps:

```python
async def _lookup_content(
    self,
    events: list[SearchMutationEvent],
    unresolved_indices: list[int],
) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """Resolve UPSERT content for each unresolved event.

    Returns ``(content_map, failure_map)``: an event_id present in
    ``content_map`` is resolved (possibly to ``""`` — a valid state);
    an event_id present in ``failure_map`` failed with
    ``(failure_kind, failure_detail)``. The content_cache DB fallback
    clears a read failure when it hits.
    """
    content_map: dict[str, str] = {}
    failure_map: dict[str, tuple[str, str]] = {}
    update_events = [events[idx] for idx in unresolved_indices if events[idx].op.value == "upsert"]
    if not update_events:
        return content_map, failure_map

    missing_events: list[SearchMutationEvent] = []
    for event in update_events:
        content, kind, detail = await self._read_content(event.path, event.virtual_path)
        if isinstance(content, str):
            content_map[event.event_id] = _strip_null_bytes(content)
        else:
            failure_map[event.event_id] = (
                kind or "transient",
                detail or "unknown read failure",
            )
            missing_events.append(event)

    if missing_events and self._async_session_factory is not None:
        lookup_candidates: list[tuple[str, str, str, int]] = []
        for event in missing_events:
            lookup_candidates.extend(self._lookup_candidates(event))
        db_content = await self._lookup_content_cache(lookup_candidates)
        for event in missing_events:
            content = db_content.get(self._path_key(event.zone_id, event.virtual_path))
            if isinstance(content, str):
                content_map[event.event_id] = _strip_null_bytes(content)
                failure_map.pop(event.event_id, None)

    return content_map, failure_map
```

- [ ] **Step 3.6: Use the failure map in `resolve_batch` and stop caching unresolved**

In `resolve_batch`, change the call site:

```python
        content_map, failure_map = await self._lookup_content(events, unresolved_indices)
```

and replace the per-event block (the `if event.op == SearchMutationOp.DELETE:` /
`else:` content section through `resolved[idx] = mutation`) with:

```python
if event.op == SearchMutationOp.DELETE:
    content_resolved = True
    content_value: str | None = None
    failure_kind: str | None = None
    failure_detail: str | None = None
else:
    content_resolved = event.event_id in content_map
    content_value = content_map.get(event.event_id)
    if content_resolved:
        failure_kind = None
        failure_detail = None
    else:
        failure_kind, failure_detail = failure_map.get(event.event_id, ("transient", "unresolved"))
mutation = ResolvedMutation(
    event=event,
    zone_id=zone_id,
    virtual_path=virtual_path,
    path_id=path_id,
    doc_id=doc_id,
    content=content_value,
    path_id_resolved=path_id_resolved,
    content_resolved=content_resolved,
    failure_kind=failure_kind,
    failure_detail=failure_detail,
)
# #4337: do NOT cache unresolved mutations. A cached miss would
# be served to every retry pass inside the TTL, making the
# gate's attempt budget count cache hits instead of real reads.
if mutation.content_resolved:
    self._cache[event.event_id] = (now, mutation)
resolved[idx] = mutation
```

- [ ] **Step 3.7: Run the new tests and the existing search suite**

Run: `uv run pytest tests/unit/bricks/search/test_mutation_resolver_classification.py tests/unit/bricks/search -q`
Expected: all PASS (existing daemon tests unaffected — `ResolvedMutation` gained optional fields only)

- [ ] **Step 3.8: Commit**

```bash
git add src/nexus/bricks/search/mutation_resolver.py tests/unit/bricks/search/test_mutation_resolver_classification.py
git commit -m "feat(#4337): classify mutation read failures permanent vs transient"
```

---

### Task 4: Config knobs + parking gate in the daemon

**Files:**
- Modify: `src/nexus/bricks/search/daemon.py`
- Test: `tests/unit/bricks/search/test_daemon_parking_gate.py`

- [ ] **Step 4.1: Write the failing tests**

```python
"""Parking-gate, checkpoint, and admin-method tests for SearchDaemon (#4337)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon
from nexus.bricks.search.mutation_events import SearchMutationEvent, SearchMutationOp
from nexus.bricks.search.mutation_parking import UnresolvedMutationError
from nexus.bricks.search.mutation_resolver import ResolvedMutation


def _event(event_id: str, seq: int, op: SearchMutationOp = SearchMutationOp.UPSERT):
    return SearchMutationEvent(
        event_id=event_id,
        operation_id=event_id.removeprefix("search:"),
        op=op,
        path=f"/zone/z1/docs/{event_id.removeprefix('search:')}.md",
        zone_id="z1",
        timestamp=datetime(2026, 6, 9, 1, 2, 3),
        sequence_number=seq,
    )


def _resolved(event: SearchMutationEvent, *, content: str | None = "body") -> ResolvedMutation:
    return ResolvedMutation(
        event=event,
        zone_id=event.zone_id,
        virtual_path=event.virtual_path,
        path_id=f"pid-{event.operation_id}",
        doc_id=f"{event.zone_id}:{event.virtual_path}",
        content=content,
        content_resolved=True,
    )


def _unresolved(event: SearchMutationEvent, *, kind: str = "permanent") -> ResolvedMutation:
    return ResolvedMutation(
        event=event,
        zone_id=event.zone_id,
        virtual_path=event.virtual_path,
        path_id=event.virtual_path,
        doc_id=f"{event.zone_id}:{event.virtual_path}",
        content=None,
        path_id_resolved=False,
        content_resolved=False,
        failure_kind=kind,
        failure_detail="FileNotFoundError(...)",
    )


class FakeResolver:
    """resolve_batch returns canned ResolvedMutations keyed by event_id."""

    def __init__(self, results: dict[str, ResolvedMutation]) -> None:
        self.results = results

    async def resolve_batch(self, events: list[SearchMutationEvent]):
        return [self.results[e.event_id] for e in events if e.event_id in self.results]


def _daemon(tmp_path, **config_overrides: Any) -> SearchDaemon:
    config = DaemonConfig(
        database_url=None,
        bm25s_index_dir=str(tmp_path / "bm25s"),
        txtai_model=None,
        refresh_enabled=False,
        vector_warmup_enabled=False,
        mutation_unresolved_permanent_attempts=2,
        mutation_unresolved_transient_attempts=4,
        **config_overrides,
    )
    return SearchDaemon(config)


@pytest.mark.asyncio
async def test_under_budget_raises_unresolved_error(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    poison = _event("search:poison", 10)
    daemon._mutation_resolver = FakeResolver({"search:poison": _unresolved(poison)})
    with pytest.raises(UnresolvedMutationError):
        await daemon._resolve_mutations("bm25", [poison])
    assert daemon._park_store.count("bm25") == 0


@pytest.mark.asyncio
async def test_budget_exhaustion_parks_and_filters(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    poison = _event("search:poison", 10)
    healthy = _event("search:ok", 11)
    daemon._mutation_resolver = FakeResolver(
        {"search:poison": _unresolved(poison), "search:ok": _resolved(healthy)}
    )
    events = [poison, healthy]
    # permanent budget = 2: pass 1 raises, pass 2 parks and yields healthy only
    with pytest.raises(UnresolvedMutationError):
        await daemon._resolve_mutations("bm25", events)
    kept = await daemon._resolve_mutations("bm25", events)
    assert [m.event.event_id for m in kept] == ["search:ok"]
    assert daemon._park_store.contains("bm25", "search:poison")
    assert ("bm25", "search:poison") not in daemon._unresolved_attempts


@pytest.mark.asyncio
async def test_transient_budget_is_larger(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    poison = _event("search:flaky", 10)
    daemon._mutation_resolver = FakeResolver(
        {"search:flaky": _unresolved(poison, kind="transient")}
    )
    for _ in range(3):  # transient budget = 4: passes 1-3 raise
        with pytest.raises(UnresolvedMutationError):
            await daemon._resolve_mutations("fts", [poison])
    kept = await daemon._resolve_mutations("fts", [poison])  # pass 4 parks
    assert kept == []
    assert daemon._park_store.contains("fts", "search:flaky")


@pytest.mark.asyncio
async def test_already_parked_event_is_filtered_without_recount(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    poison = _event("search:poison", 10)
    daemon._mutation_resolver = FakeResolver({"search:poison": _unresolved(poison)})
    with pytest.raises(UnresolvedMutationError):
        await daemon._resolve_mutations("bm25", [poison])
    await daemon._resolve_mutations("bm25", [poison])  # parks
    kept = await daemon._resolve_mutations("bm25", [poison])  # already parked
    assert kept == []
    assert ("bm25", "search:poison") not in daemon._unresolved_attempts
    assert daemon._park_store.count("bm25") == 1


@pytest.mark.asyncio
async def test_recovered_event_auto_unparks(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    poison = _event("search:poison", 10)
    daemon._mutation_resolver = FakeResolver({"search:poison": _unresolved(poison)})
    with pytest.raises(UnresolvedMutationError):
        await daemon._resolve_mutations("bm25", [poison])
    await daemon._resolve_mutations("bm25", [poison])  # parks
    daemon._mutation_resolver = FakeResolver({"search:poison": _resolved(poison)})
    kept = await daemon._resolve_mutations("bm25", [poison])
    assert [m.event.event_id for m in kept] == ["search:poison"]
    assert not daemon._park_store.contains("bm25", "search:poison")


@pytest.mark.asyncio
async def test_legacy_refresh_bypasses_gate(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    poison = _event("search:poison", 10)
    daemon._mutation_resolver = FakeResolver({"search:poison": _unresolved(poison)})
    # No raise, no park, unresolved mutation passes through untouched.
    resolved = await daemon._resolve_mutations("legacy-refresh", [poison])
    assert len(resolved) == 1
    assert daemon._park_store.count("legacy-refresh") == 0


@pytest.mark.asyncio
async def test_consumers_are_independent(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    poison = _event("search:poison", 10)
    daemon._mutation_resolver = FakeResolver({"search:poison": _unresolved(poison)})
    with pytest.raises(UnresolvedMutationError):
        await daemon._resolve_mutations("bm25", [poison])
    # fts has its own counter — first pass raises even though bm25 counted one.
    with pytest.raises(UnresolvedMutationError):
        await daemon._resolve_mutations("fts", [poison])
    assert daemon._unresolved_attempts[("bm25", "search:poison")] == 1
    assert daemon._unresolved_attempts[("fts", "search:poison")] == 1
```

- [ ] **Step 4.2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/bricks/search/test_daemon_parking_gate.py -q`
Expected: FAIL — `DaemonConfig` has no `mutation_unresolved_permanent_attempts` (TypeError)

- [ ] **Step 4.3: Add config knobs**

In `daemon.py`, `DaemonConfig`, after `path_context_max_zones: int = 2048` add:

```python
    # Bounded retries for unresolved-content mutations (#4337). When the
    # MutationResolver cannot obtain content for an UPSERT, the consumer
    # retries the batch (refusing to checkpoint) up to this many passes,
    # then PARKS the event (durable skip record + metric) so one poisoned
    # event cannot head-of-line block indexing forever.
    mutation_unresolved_permanent_attempts: int = 3
    mutation_unresolved_transient_attempts: int = 30
    mutation_parked_max_entries: int = 200
```

- [ ] **Step 4.4: Add module constant, imports, and instance state**

Near the existing search-brick imports in `daemon.py` add:

```python
from nexus.bricks.search import consumer_metrics
from nexus.bricks.search.mutation_parking import (
    MutationParkStore,
    ParkedEvent,
    UnresolvedMutationError,
)
```

Below the module-level `logger` add:

```python
# Durable mutation consumer names (#4337): single source for the refresh
# loop, startup reconciliation, parked-event re-drive, and admin skip-to.
MUTATION_CONSUMER_NAMES: tuple[str, ...] = ("bm25", "fts", "embedding", "txtai")
LEGACY_REFRESH_CONSUMER = "legacy-refresh"
```

In `SearchDaemon.__init__`, right after the `self._checkpoint_lock = asyncio.Lock()` line:

```python
        # #4337: bounded-retry gate state. Attempt counts are in-memory
        # (restart resets them; a poisoned event re-accumulates to budget in
        # seconds). The park store is the durable record of skipped events.
        self._unresolved_attempts: dict[tuple[str, str], int] = {}
        self._consumer_retrying: dict[str, dict[str, Any] | None] = {}
        self._park_store = MutationParkStore(
            settings_store=settings_store,
            fallback_file=Path(self.config.bm25s_index_dir).parent / "mutation-parked.json",
            max_entries_per_consumer=self.config.mutation_parked_max_entries,
        )
```

- [ ] **Step 4.5: Implement the gate and re-sign `_resolve_mutations`**

Replace the existing `_resolve_mutations` (daemon.py:3116-3122) with:

```python
async def _resolve_mutations(
    self,
    consumer_name: str,
    events: list[SearchMutationEvent],
) -> list[ResolvedMutation]:
    if self._mutation_resolver is None:
        return []
    resolved = await self._mutation_resolver.resolve_batch(events)
    if consumer_name == LEGACY_REFRESH_CONSUMER:
        # Fallback delete-propagation path: resolves DELETE events only
        # (always content_resolved) — the gate has nothing to do.
        return resolved
    return await self._gate_unresolved(consumer_name, resolved)


async def _gate_unresolved(
    self,
    consumer_name: str,
    resolved: list[ResolvedMutation],
) -> list[ResolvedMutation]:
    """Bounded-retry gate for unresolved-content UPSERTs (#4337).

    Per pass over a batch:
      * already-parked events are filtered out (no recount);
      * resolved events drop any stale attempt counter and auto-unpark;
      * unresolved upserts count one attempt against their kind's
        budget — at/over budget they are parked (durable record +
        metrics) and filtered, otherwise the lowest-sequence one is
        reported in a raised ``UnresolvedMutationError`` so the whole
        batch retries (checkpoint untouched), exactly like the
        pre-#4337 refuse-to-checkpoint behavior but bounded.
    """
    kept: list[ResolvedMutation] = []
    blocking: ResolvedMutation | None = None
    blocking_attempts = 0
    blocking_budget = 0
    for mutation in resolved:
        event = mutation.event
        key = (consumer_name, event.event_id)
        is_unresolved_upsert = event.op == SearchMutationOp.UPSERT and not mutation.content_resolved
        if not is_unresolved_upsert:
            self._unresolved_attempts.pop(key, None)
            if self._park_store.contains(consumer_name, event.event_id):
                # Content recovered while parked — heal the record.
                await self._park_store.remove(consumer_name, [event.event_id])
            kept.append(mutation)
            continue
        if self._park_store.contains(consumer_name, event.event_id):
            continue  # already parked: skip without recounting
        kind = mutation.failure_kind or "transient"
        attempts = self._unresolved_attempts.get(key, 0) + 1
        self._unresolved_attempts[key] = attempts
        budget = (
            self.config.mutation_unresolved_permanent_attempts
            if kind == "permanent"
            else self.config.mutation_unresolved_transient_attempts
        )
        consumer_metrics.MUTATION_UNRESOLVED_RETRIES_TOTAL.labels(
            consumer=consumer_name, kind=kind
        ).inc()
        if attempts >= budget:
            entry = ParkedEvent.from_mutation(
                consumer_name,
                mutation,
                kind=kind,
                detail=mutation.failure_detail or "",
                attempts=attempts,
            )
            # park() raises if it cannot persist a record — in that
            # case the error propagates, the batch retries, and the
            # event is NEVER skipped without a durable record.
            await self._park_store.park(entry)
            consumer_metrics.MUTATION_PARKED_TOTAL.labels(consumer=consumer_name, kind=kind).inc()
            self._unresolved_attempts.pop(key, None)
            logger.warning(
                "Search mutation PARKED for consumer=%s event_id=%s path=%s "
                "kind=%s after %d attempts (checkpoint will advance past it; "
                "re-drive or discard via /api/v2/search/parked): %s",
                consumer_name,
                event.event_id,
                event.path,
                kind,
                attempts,
                mutation.failure_detail,
            )
            continue
        if blocking is None or event.sequence_number < blocking.event.sequence_number:
            blocking = mutation
            blocking_attempts = attempts
            blocking_budget = budget
    if blocking is not None:
        self._consumer_retrying[consumer_name] = {
            "event_id": blocking.event.event_id,
            "path": blocking.event.path,
            "attempts": blocking_attempts,
            "budget": blocking_budget,
            "kind": blocking.failure_kind or "transient",
        }
        raise UnresolvedMutationError(
            f"{consumer_name} mutation content unresolved for "
            f"event_id={blocking.event.event_id} path={blocking.event.path} "
            f"kind={blocking.failure_kind or 'transient'} "
            f"attempt={blocking_attempts}/{blocking_budget} — refusing to "
            "checkpoint so the consumer retries on next pass"
        )
    self._consumer_retrying[consumer_name] = None
    return kept
```

- [ ] **Step 4.6: Update the five call sites to pass a consumer name**

- daemon.py:2442 (`_delete_indexes_for_paths`): `resolved = await self._resolve_mutations(LEGACY_REFRESH_CONSUMER, events)`
- `_consume_bm25_mutations`: `resolved = self._collapse_resolved_mutations(await self._resolve_mutations("bm25", events))`
- `_consume_fts_mutations`: same with `"fts"`
- `_consume_embedding_mutations`: same with `"embedding"`
- `_consume_txtai_mutations`: same with `"txtai"`

- [ ] **Step 4.7: Run gate tests**

Run: `uv run pytest tests/unit/bricks/search/test_daemon_parking_gate.py -q`
Expected: all PASS

- [ ] **Step 4.8: Commit**

```bash
git add src/nexus/bricks/search/daemon.py tests/unit/bricks/search/test_daemon_parking_gate.py
git commit -m "feat(#4337): bounded-retry parking gate in _resolve_mutations"
```

---

### Task 5: Delete per-consumer unresolved branches (gate owns the policy)

**Files:**
- Modify: `src/nexus/bricks/search/daemon.py`
- Test: extend `tests/unit/bricks/search/test_daemon_parking_gate.py`

- [ ] **Step 5.1: Write the failing test (consumer processes healthy events past a parked poison)**

Append to `test_daemon_parking_gate.py`:

```python
class FakeBM25Index:
    def __init__(self) -> None:
        self.indexed: list[str] = []

    async def index_document(self, path_id: str, virtual_path: str, content: str) -> None:
        self.indexed.append(path_id)

    async def delete_document(self, path_id: str) -> bool:
        return True


@pytest.mark.asyncio
async def test_bm25_consumer_unwedges_after_parking(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    poison = _event("search:poison", 10)
    healthy = _event("search:ok", 11)
    daemon._mutation_resolver = FakeResolver(
        {"search:poison": _unresolved(poison), "search:ok": _resolved(healthy)}
    )
    daemon._bm25s_index = FakeBM25Index()
    events = [poison, healthy]
    # Pass 1: under budget — handler raises, nothing indexed (no checkpoint).
    with pytest.raises(UnresolvedMutationError):
        await daemon._consume_bm25_mutations(events)
    assert daemon._bm25s_index.indexed == []
    # Pass 2: budget (=2) hit — poison parks, healthy indexes, handler returns.
    await daemon._consume_bm25_mutations(events)
    assert daemon._bm25s_index.indexed == ["pid-ok"]
    assert daemon._park_store.contains("bm25", "search:poison")
```

- [ ] **Step 5.2: Run to verify the new test currently passes or fails for the right reason**

Run: `uv run pytest tests/unit/bricks/search/test_daemon_parking_gate.py::test_bm25_consumer_unwedges_after_parking -q`
Expected: PASS already (the gate filters before the handler loop runs — the old in-handler raise never fires because gated mutations are filtered). If it fails on the old raise, continue: Step 5.3 removes it. Either way proceed — the test pins the behavior.

- [ ] **Step 5.3: Delete the three raise branches**

In `_consume_bm25_mutations` delete:

```python
            if mutation.event.op == SearchMutationOp.UPSERT and not mutation.content_resolved:
                raise RuntimeError(
                    f"BM25 mutation content unresolved for "
                    f"event_id={mutation.event.event_id} "
                    f"path={mutation.event.path} — refusing to checkpoint "
                    "so the consumer retries on next pass"
                )
```

and update the preceding comment block (`# Codex review R8 #2 / R9 #1 / R10 #1: ...`) by replacing its bullet `* UPSERT + content_resolved=False → raise (don't checkpoint a transient read failure).` with `* UPSERT + content_resolved=False → never reaches this handler: the #4337 parking gate in _resolve_mutations retries (raise) or parks+filters it.`

In `_consume_fts_mutations` delete:

```python
            # Codex review R10 #1 (high): refuse to checkpoint
            # unresolved-content UPSERTs — see ``_consume_bm25_mutations``
            # for the rationale.
            if mutation.event.op == SearchMutationOp.UPSERT and not mutation.content_resolved:
                raise RuntimeError(
                    f"FTS mutation content unresolved for "
                    f"event_id={mutation.event.event_id} "
                    f"path={mutation.event.path} — refusing to checkpoint "
                    "so the consumer retries on next pass"
                )
```

In `_consume_embedding_mutations` delete:

```python
            # Codex review R10 #1 (high): refuse to checkpoint
            # unresolved-content UPSERTs — see ``_consume_bm25_mutations``
            # for the rationale.
            if mutation.event.op == SearchMutationOp.UPSERT and not mutation.content_resolved:
                raise RuntimeError(
                    f"Embedding mutation content unresolved for "
                    f"event_id={mutation.event.event_id} "
                    f"path={mutation.event.path} — refusing to checkpoint "
                    "so the consumer retries on next pass"
                )
```

(`_consume_txtai_mutations` has no raise branch — the gate now covers its former silent drop.)

- [ ] **Step 5.4: Run the full search unit suite**

Run: `uv run pytest tests/unit/bricks/search -q`
Expected: all PASS

- [ ] **Step 5.5: Commit**

```bash
git add src/nexus/bricks/search/daemon.py tests/unit/bricks/search/test_daemon_parking_gate.py
git commit -m "refactor(#4337): consumers rely on parking gate for unresolved upserts"
```

---

### Task 6: Boot wiring, handler map DRY, stats merge

**Files:**
- Modify: `src/nexus/bricks/search/daemon.py`
- Test: extend `tests/unit/bricks/search/test_daemon_parking_gate.py`

- [ ] **Step 6.1: Write the failing stats test**

Append to `test_daemon_parking_gate.py`:

```python
@pytest.mark.asyncio
async def test_get_stats_exposes_parking_state(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    poison = _event("search:poison", 10)
    daemon._mutation_resolver = FakeResolver({"search:poison": _unresolved(poison)})
    with pytest.raises(UnresolvedMutationError):
        await daemon._resolve_mutations("bm25", [poison])
    stats = daemon.get_stats()
    bm25 = stats["mutation_consumers"]["bm25"]
    assert bm25["retrying"]["event_id"] == "search:poison"
    assert bm25["retrying"]["attempts"] == 1
    assert bm25["parked_count"] == 0
    await daemon._resolve_mutations("bm25", [poison])  # parks (budget=2)
    stats = daemon.get_stats()
    bm25 = stats["mutation_consumers"]["bm25"]
    assert bm25["parked_count"] == 1
    assert bm25["last_parked"]["event_id"] == "search:poison"
    assert bm25["last_parked"]["kind"] == "permanent"
    assert bm25["retrying"] is None
    # Consumers with no activity still report the parking keys.
    assert stats["mutation_consumers"]["txtai"]["parked_count"] == 0
```

Run: `uv run pytest tests/unit/bricks/search/test_daemon_parking_gate.py::test_get_stats_exposes_parking_state -q`
Expected: FAIL — `KeyError: 'bm25'` (stats.mutation_consumers is empty / lacks parking keys)

- [ ] **Step 6.2: Extract `_mutation_handlers()` and reuse it (DRY)**

Add next to `_index_refresh_loop`:

```python
    def _mutation_handlers(self) -> dict[str, Any]:
        """Consumer-name → handler map (#4337): single source for the
        refresh loop, startup reconciliation, and parked-event re-drive."""
        return {
            "bm25": self._consume_bm25_mutations,
            "fts": self._consume_fts_mutations,
            "embedding": self._consume_embedding_mutations,
            "txtai": self._consume_txtai_mutations,
        }
```

In `_index_refresh_loop` replace the `consumer_specs = { ... }` dict literal with `consumer_specs = self._mutation_handlers()`, and in `_reconcile_unindexed_paths_at_startup` replace its `handlers_by_name: dict[str, Any] = { ... }` literal with `handlers_by_name: dict[str, Any] = self._mutation_handlers()`.

- [ ] **Step 6.3: Load the park store before consumers start**

In `_index_refresh_loop`, immediately before `await self._reconcile_unindexed_paths_at_startup()`:

```python
        # #4337: load parked records before consumers start so the gauge,
        # stats, and already-parked filtering reflect prior runs. Best-effort:
        # a corrupt/unreadable store logs and starts empty.
        try:
            await self._park_store.load()
        except Exception as exc:
            logger.error("Parked-event store load failed; starting empty: %s", exc)
```

- [ ] **Step 6.4: Merge parking info into `get_stats`**

Add the helper method near `get_stats`:

```python
    def _mutation_consumer_stats(self) -> dict[str, dict[str, Any]]:
        """Per-consumer loop stats merged with parking state (#4337)."""
        merged: dict[str, dict[str, Any]] = {}
        names = set(self.stats.mutation_consumers) | set(MUTATION_CONSUMER_NAMES)
        for name in sorted(names):
            entry = dict(self.stats.mutation_consumers.get(name, {}))
            entry["parked_count"] = self._park_store.count(name)
            last = self._park_store.last(name)
            entry["last_parked"] = (
                {
                    "event_id": last.event_id,
                    "path": last.path,
                    "kind": last.kind,
                    "parked_at": last.parked_at,
                }
                if last is not None
                else None
            )
            entry["retrying"] = self._consumer_retrying.get(name)
            merged[name] = entry
        return merged
```

In `get_stats` change `"mutation_consumers": self.stats.mutation_consumers,` to `"mutation_consumers": self._mutation_consumer_stats(),`.

- [ ] **Step 6.5: Run tests**

Run: `uv run pytest tests/unit/bricks/search -q`
Expected: all PASS

- [ ] **Step 6.6: Commit**

```bash
git add src/nexus/bricks/search/daemon.py tests/unit/bricks/search/test_daemon_parking_gate.py
git commit -m "feat(#4337): park-store boot load and parking stats in get_stats"
```

---

### Task 7: Monotonic checkpoint + `force_checkpoint`

**Files:**
- Modify: `src/nexus/bricks/search/daemon.py`
- Test: extend `tests/unit/bricks/search/test_daemon_parking_gate.py`

- [ ] **Step 7.1: Write the failing tests**

Append to `test_daemon_parking_gate.py`:

```python
@pytest.mark.asyncio
async def test_force_checkpoint_advances_and_validates(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    daemon._consumer_last_sequence["bm25"] = 100
    result = await daemon.force_checkpoint("bm25", 250)
    assert result == {"previous": 100, "current": 250}
    assert daemon._consumer_last_sequence["bm25"] == 250

    with pytest.raises(ValueError, match="greater than current"):
        await daemon.force_checkpoint("bm25", 250)
    with pytest.raises(ValueError, match="unknown consumer"):
        await daemon.force_checkpoint("nope", 999)


@pytest.mark.asyncio
async def test_save_checkpoint_is_monotonic(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    daemon._consumer_last_sequence["bm25"] = 100
    await daemon.force_checkpoint("bm25", 250)
    # An in-flight stale pass completing with an older batch must not rewind.
    await daemon._save_consumer_checkpoint("bm25", 120)
    assert daemon._consumer_last_sequence["bm25"] == 250
```

Run: `uv run pytest tests/unit/bricks/search/test_daemon_parking_gate.py::test_force_checkpoint_advances_and_validates -q`
Expected: FAIL — `AttributeError: 'SearchDaemon' object has no attribute 'force_checkpoint'`

- [ ] **Step 7.2: Make `_save_consumer_checkpoint` monotonic**

Replace the method (daemon.py:2952-2954):

```python
    async def _save_consumer_checkpoint(self, consumer_name: str, sequence_number: int) -> None:
        current = self._consumer_last_sequence.get(consumer_name)
        if current is not None and sequence_number < current:
            # #4337: monotonic guard — an in-flight pass that succeeds with a
            # stale batch must not rewind an admin force-advance (skip-to).
            sequence_number = current
        self._consumer_last_sequence[consumer_name] = sequence_number
        await self._persist_checkpoint(consumer_name, sequence_number)
```

- [ ] **Step 7.3: Add `force_checkpoint`**

Add after `_save_consumer_checkpoint`:

```python
    async def force_checkpoint(self, consumer_name: str, sequence_number: int) -> dict[str, int]:
        """Force-advance a consumer checkpoint past a poisoned range (#4337).

        Admin escape hatch ("skip event N"): everything with
        sequence_number <= the new value is permanently skipped for this
        consumer. Deliberately NOT capped at the current op-log max so an
        operator can pre-advance past a known-bad range.
        """
        if consumer_name not in MUTATION_CONSUMER_NAMES:
            raise ValueError(
                f"unknown consumer {consumer_name!r}; expected one of {MUTATION_CONSUMER_NAMES}"
            )
        current = self._consumer_last_sequence.get(consumer_name)
        if current is None:
            current = await self._initialize_consumer_checkpoint(consumer_name)
            self._consumer_last_sequence[consumer_name] = current
        if sequence_number <= current:
            raise ValueError(
                f"sequence {sequence_number} must be greater than current checkpoint {current}"
            )
        await self._save_consumer_checkpoint(consumer_name, sequence_number)
        logger.warning(
            "Search mutation checkpoint FORCED for consumer=%s: %d -> %d "
            "(events in between are skipped)",
            consumer_name,
            current,
            sequence_number,
        )
        return {"previous": current, "current": sequence_number}
```

- [ ] **Step 7.4: Run tests**

Run: `uv run pytest tests/unit/bricks/search/test_daemon_parking_gate.py -q`
Expected: all PASS

- [ ] **Step 7.5: Commit**

```bash
git add src/nexus/bricks/search/daemon.py tests/unit/bricks/search/test_daemon_parking_gate.py
git commit -m "feat(#4337): monotonic checkpoints and admin force_checkpoint"
```

---

### Task 8: `list_parked` / `retry_parked` / `discard_parked`

**Files:**
- Modify: `src/nexus/bricks/search/daemon.py`
- Test: extend `tests/unit/bricks/search/test_daemon_parking_gate.py`

- [ ] **Step 8.1: Write the failing tests**

Append to `test_daemon_parking_gate.py`:

```python
async def _park_one(daemon, consumer: str = "bm25", event_id: str = "search:poison"):
    poison = _event(event_id, 10)
    daemon._mutation_resolver = FakeResolver({event_id: _unresolved(poison)})
    with pytest.raises(UnresolvedMutationError):
        await daemon._resolve_mutations(consumer, [poison])
    await daemon._resolve_mutations(consumer, [poison])  # budget=2 → parks
    return poison


@pytest.mark.asyncio
async def test_list_parked_serializes_entries(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    await _park_one(daemon)
    parked = daemon.list_parked()
    assert list(parked.keys()) == ["bm25"]
    assert parked["bm25"][0]["event_id"] == "search:poison"
    assert parked["bm25"][0]["kind"] == "permanent"


@pytest.mark.asyncio
async def test_retry_parked_success_unparks(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    poison = await _park_one(daemon)
    # Content recovered: resolver now resolves the event.
    daemon._mutation_resolver = FakeResolver({"search:poison": _resolved(poison)})
    daemon._bm25s_index = FakeBM25Index()
    result = await daemon.retry_parked("bm25", None)
    assert result["retried"] == 1
    assert result["succeeded"] == ["search:poison"]
    assert result["failed"] == []
    assert not daemon._park_store.contains("bm25", "search:poison")
    assert daemon._bm25s_index.indexed == ["pid-poison"]


@pytest.mark.asyncio
async def test_retry_parked_failure_reparks(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    await _park_one(daemon)  # leaves resolver returning unresolved
    daemon._bm25s_index = FakeBM25Index()
    result = await daemon.retry_parked("bm25", ["search:poison"])
    assert result["succeeded"] == []
    assert result["failed"][0]["event_id"] == "search:poison"
    # Still parked (re-parked after the one-shot failed) — not silently lost.
    assert daemon._park_store.contains("bm25", "search:poison")
    # And the one-shot didn't leak a live retry counter.
    assert ("bm25", "search:poison") not in daemon._unresolved_attempts


@pytest.mark.asyncio
async def test_discard_parked_removes_without_retry(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    await _park_one(daemon)
    result = await daemon.discard_parked("bm25", ["search:poison"])
    assert result == {"discarded": ["search:poison"]}
    assert daemon._park_store.count("bm25") == 0

    with pytest.raises(ValueError, match="unknown consumer"):
        await daemon.discard_parked("nope", ["x"])
```

Run: `uv run pytest tests/unit/bricks/search/test_daemon_parking_gate.py::test_list_parked_serializes_entries -q`
Expected: FAIL — `AttributeError: 'SearchDaemon' object has no attribute 'list_parked'`

- [ ] **Step 8.2: Implement the three methods**

Add after `force_checkpoint` (note `from dataclasses import replace` — daemon.py already imports `dataclass, field` from dataclasses; extend that import):

```python
def list_parked(self) -> dict[str, list[dict[str, Any]]]:
    """Parked mutation events per consumer, serialized for the API (#4337)."""
    return {
        consumer: [entry.to_dict() for entry in entries]
        for consumer, entries in self._park_store.list_entries().items()
        if entries
    }


async def retry_parked(
    self,
    consumer_name: str,
    event_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Re-drive parked events through their consumer handler (#4337).

    One-shot per event: the entry is removed from the store FIRST (so
    the gate doesn't filter it as already-parked, which would make the
    handler succeed vacuously), then run through the handler. Success →
    stays unparked. Failure → re-parked with the new error detail.
    """
    if consumer_name not in MUTATION_CONSUMER_NAMES:
        raise ValueError(
            f"unknown consumer {consumer_name!r}; expected one of {MUTATION_CONSUMER_NAMES}"
        )
    entries = self._park_store.list_entries(consumer_name).get(consumer_name, [])
    if event_ids is not None:
        wanted = set(event_ids)
        entries = [entry for entry in entries if entry.event_id in wanted]
    handler = self._mutation_handlers()[consumer_name]
    succeeded: list[str] = []
    failed: list[dict[str, str]] = []
    for entry in entries:
        await self._park_store.remove(consumer_name, [entry.event_id])
        try:
            await handler([entry.to_event()])
            succeeded.append(entry.event_id)
        except Exception as exc:
            await self._park_store.park(replace(entry, detail=str(exc), parked_at=time.time()))
            failed.append({"event_id": entry.event_id, "error": str(exc)})
        finally:
            self._unresolved_attempts.pop((consumer_name, entry.event_id), None)
    return {"retried": len(entries), "succeeded": succeeded, "failed": failed}


async def discard_parked(
    self,
    consumer_name: str,
    event_ids: list[str],
) -> dict[str, Any]:
    """Drop parked events without retrying — operator accepts the loss (#4337)."""
    if consumer_name not in MUTATION_CONSUMER_NAMES:
        raise ValueError(
            f"unknown consumer {consumer_name!r}; expected one of {MUTATION_CONSUMER_NAMES}"
        )
    removed = await self._park_store.remove(consumer_name, event_ids)
    return {"discarded": [entry.event_id for entry in removed]}
```

- [ ] **Step 8.3: Run tests**

Run: `uv run pytest tests/unit/bricks/search -q`
Expected: all PASS

- [ ] **Step 8.4: Commit**

```bash
git add src/nexus/bricks/search/daemon.py tests/unit/bricks/search/test_daemon_parking_gate.py
git commit -m "feat(#4337): list/retry/discard parked mutation events"
```

---

### Task 9: REST admin endpoints

**Files:**
- Modify: `src/nexus/server/api/v2/routers/search.py`
- Test: `tests/unit/server/routers/test_search_parked_admin.py`

- [ ] **Step 9.1: Write the failing tests**

```python
"""REST tests for the parked-event admin endpoints (#4337)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.routers.search import router
from nexus.server.dependencies import require_admin


class FakeDaemon:
    def __init__(self) -> None:
        self.skip_calls: list[tuple[str, int]] = []

    def list_parked(self) -> dict[str, list[dict[str, Any]]]:
        return {"bm25": [{"event_id": "search:op-1", "kind": "permanent"}]}

    async def retry_parked(self, consumer: str, event_ids: list[str] | None) -> dict[str, Any]:
        if consumer == "nope":
            raise ValueError("unknown consumer 'nope'")
        return {"retried": 1, "succeeded": ["search:op-1"], "failed": []}

    async def discard_parked(self, consumer: str, event_ids: list[str]) -> dict[str, Any]:
        return {"discarded": event_ids}

    async def force_checkpoint(self, consumer: str, sequence: int) -> dict[str, int]:
        if sequence <= 100:
            raise ValueError("sequence 100 must be greater than current checkpoint 100")
        self.skip_calls.append((consumer, sequence))
        return {"previous": 100, "current": sequence}


def _client(daemon: FakeDaemon | None = None, *, admin: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.search_daemon = daemon or FakeDaemon()
    if admin:
        app.dependency_overrides[require_admin] = lambda: {"is_admin": True}
    return TestClient(app, raise_server_exceptions=False)


def test_parked_list_requires_admin() -> None:
    client = _client(admin=False)
    response = client.get("/api/v2/search/parked")
    assert response.status_code in (401, 403)


def test_parked_list_returns_entries() -> None:
    client = _client()
    response = client.get("/api/v2/search/parked")
    assert response.status_code == 200
    assert response.json()["parked"]["bm25"][0]["event_id"] == "search:op-1"


def test_parked_retry_happy_path() -> None:
    client = _client()
    response = client.post(
        "/api/v2/search/parked/retry",
        json={"consumer": "bm25", "event_ids": None},
    )
    assert response.status_code == 200
    assert response.json()["succeeded"] == ["search:op-1"]


def test_parked_retry_unknown_consumer_is_400() -> None:
    client = _client()
    response = client.post("/api/v2/search/parked/retry", json={"consumer": "nope"})
    assert response.status_code == 400


def test_parked_discard() -> None:
    client = _client()
    response = client.post(
        "/api/v2/search/parked/discard",
        json={"consumer": "bm25", "event_ids": ["search:op-1"]},
    )
    assert response.status_code == 200
    assert response.json()["discarded"] == ["search:op-1"]


def test_skip_to_happy_path_and_validation() -> None:
    daemon = FakeDaemon()
    client = _client(daemon)
    response = client.post("/api/v2/search/consumers/bm25/skip-to", json={"sequence": 250})
    assert response.status_code == 200
    assert response.json() == {"previous": 100, "current": 250}
    assert daemon.skip_calls == [("bm25", 250)]

    response = client.post("/api/v2/search/consumers/bm25/skip-to", json={"sequence": 100})
    assert response.status_code == 400
```

- [ ] **Step 9.2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/server/routers/test_search_parked_admin.py -q`
Expected: FAIL — 404s (routes don't exist)

- [ ] **Step 9.3: Add the endpoints**

In `routers/search.py`: extend the imports —

```python
from pydantic import BaseModel

from nexus.server.dependencies import require_admin, require_auth
```

(keep the existing `require_auth` import line if already present; just add `require_admin`). Add request models near the other module-level helpers and the endpoints after `search_daemon_stats`:

```python
class ParkedRetryRequest(BaseModel):
    consumer: str
    event_ids: list[str] | None = None


class ParkedDiscardRequest(BaseModel):
    consumer: str
    event_ids: list[str]


class ConsumerSkipToRequest(BaseModel):
    sequence: int


@router.get("/parked")
async def search_parked_list(
    search_daemon: Any = Depends(_get_search_daemon),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """List parked (poison) mutation events per consumer (#4337)."""
    return {"parked": search_daemon.list_parked()}


@router.post("/parked/retry")
async def search_parked_retry(
    body: ParkedRetryRequest,
    search_daemon: Any = Depends(_get_search_daemon),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Re-drive parked mutation events through their consumer (#4337)."""
    try:
        result: dict[str, Any] = await search_daemon.retry_parked(body.consumer, body.event_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@router.post("/parked/discard")
async def search_parked_discard(
    body: ParkedDiscardRequest,
    search_daemon: Any = Depends(_get_search_daemon),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Discard parked mutation events without retrying (#4337)."""
    try:
        result: dict[str, Any] = await search_daemon.discard_parked(body.consumer, body.event_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@router.post("/consumers/{consumer_name}/skip-to")
async def search_consumer_skip_to(
    consumer_name: str,
    body: ConsumerSkipToRequest,
    search_daemon: Any = Depends(_get_search_daemon),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Force-advance a mutation consumer checkpoint past a poisoned range (#4337)."""
    try:
        result: dict[str, int] = await search_daemon.force_checkpoint(consumer_name, body.sequence)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result
```

Also update the module docstring's endpoint list with the four new routes:

```
- GET  /api/v2/search/parked            -- parked (poison) mutation events (#4337, admin)
- POST /api/v2/search/parked/retry      -- re-drive parked events (#4337, admin)
- POST /api/v2/search/parked/discard    -- discard parked events (#4337, admin)
- POST /api/v2/search/consumers/{name}/skip-to -- force checkpoint advance (#4337, admin)
```

- [ ] **Step 9.4: Run tests**

Run: `uv run pytest tests/unit/server/routers/test_search_parked_admin.py -q`
Expected: all PASS. If the no-override test returns 422 instead of 401/403, inspect `require_admin`'s signature and adjust the assertion to the actual unauthorized status — do not weaken the admin gate itself.

- [ ] **Step 9.5: Commit**

```bash
git add src/nexus/server/api/v2/routers/search.py tests/unit/server/routers/test_search_parked_admin.py
git commit -m "feat(#4337): admin REST surface for parked events and skip-to"
```

---

### Task 10: Runbook

**Files:**
- Create: `docs/operations/search-mutation-parking.md`

- [ ] **Step 10.1: Write the runbook**

```markdown
# Runbook: Search Mutation Parking (#4337)

When the search daemon cannot resolve content for a write event, the
consumer retries the batch (the checkpoint does not advance). Since #4337
retries are **bounded**: permanent failures (file gone) park after
~3 passes (~6 s), transient failures (backend outage) after ~30 passes
(~60 s). A **parked** event is skipped by the live consumer but durably
recorded for re-drive or discard.

## Symptoms

Log lines from `nexus.bricks.search.daemon`:

- Retrying (bounded):
  `<consumer> mutation content unresolved for event_id=... path=... kind=... attempt=N/M — refusing to checkpoint so the consumer retries on next pass`
- Parked (skipped, recorded):
  `Search mutation PARKED for consumer=... event_id=... path=... kind=... after N attempts ...`
- Forced skip:
  `Search mutation checkpoint FORCED for consumer=...: A -> B`

Metrics (Prometheus):

| Metric | Meaning | Suggested alert |
| --- | --- | --- |
| `nexus_search_mutation_parked_total{consumer,kind}` | Events parked | Page on any increase |
| `nexus_search_mutation_parked_current{consumer}` | Currently parked | Warn while > 0 |
| `nexus_search_mutation_unresolved_retries_total{consumer,kind}` | Retry passes (pre-park) | Warn on sustained rate |
| `nexus_search_mutation_parked_evicted_total{consumer}` | Cap evictions (parking storm) | Page on any increase |

## Diagnose

```bash
# Consumer state: parked_count, last_parked, retrying head-of-line blocker
curl -s "$NEXUS_URL/api/v2/search/stats" | jq '.mutation_consumers'

# Full parked entries (admin)
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$NEXUS_URL/api/v2/search/parked" | jq
```

`kind=permanent` → the path's payload is gone (cf. issue #4339 forensics).
`kind=transient` → the content backend was unreachable; content may exist.

## Decide

- **Payload restored / outage over → retry** (re-drives through the
  consumer; success removes the record, failure re-parks with the error):

```bash
curl -s -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"consumer": "embedding", "event_ids": null}' \
  "$NEXUS_URL/api/v2/search/parked/retry" | jq
```

(`event_ids: null` retries everything parked for that consumer.)

- **Content permanently gone, loss accepted → discard:**

```bash
curl -s -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"consumer": "embedding", "event_ids": ["search:a39822b6-..."]}' \
  "$NEXUS_URL/api/v2/search/parked/discard" | jq
```

- **Consumer wedged on something the gate does not handle → force-advance
  the checkpoint** (skips EVERY event ≤ the new sequence for that
  consumer — use the smallest sequence that unwedges, typically the
  blocker's own `sequence_number` from `/stats`):

```bash
curl -s -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"sequence": 123456}' \
  "$NEXUS_URL/api/v2/search/consumers/embedding/skip-to" | jq
```

## Notes

- Park records live in the settings store under `search_mutation_parked`
  (file fallback `mutation-parked.json` next to the checkpoints file).
  Capped at 200 entries per consumer; evictions log ERROR and bump
  `..._parked_evicted_total` — an eviction means a parking storm, look for
  a systemic cause (e.g. #4339-scale payload loss) instead of per-event
  triage.
- Restarts reset in-flight retry counters (a poisoned event re-parks within
  seconds); parked records persist.
- A parked event whose content later resolves on its own (e.g. the same
  path is re-written) is auto-unparked by the gate.
```

- [ ] **Step 10.2: Commit**

```bash
git add docs/operations/search-mutation-parking.md
git commit -m "docs(#4337): runbook for search mutation parking"
```

---

### Task 11: Full verification

**Files:** none new.

- [ ] **Step 11.1: Run the affected test suites**

Run: `uv run pytest tests/unit/bricks/search tests/unit/server/routers -q`
Expected: all PASS

- [ ] **Step 11.2: Lint + types on touched files**

Run: `uv run ruff check src/nexus/bricks/search src/nexus/server/api/v2/routers/search.py && uv run ruff format --check src/nexus/bricks/search src/nexus/server/api/v2/routers/search.py`
Expected: clean (pre-commit re-runs ruff/mypy on commit; fix anything it flags)

Run: `uv run mypy src/nexus/bricks/search/mutation_parking.py src/nexus/bricks/search/mutation_resolver.py src/nexus/bricks/search/consumer_metrics.py`
Expected: no new errors (match the repo's existing mypy posture; pre-commit is the gate)

- [ ] **Step 11.3: Wider sanity — server lifespan tests that touch the daemon**

Run: `uv run pytest tests/unit/server/lifespan/test_search_lifespan.py tests/unit/server/lifespan/test_search_runtime_config.py -q`
Expected: all PASS (lifespan calls `_resolve_mutations` indirectly via daemon wiring; signature change is internal)

- [ ] **Step 11.4: grep for stale call sites**

Run: `grep -rn "_resolve_mutations(" src/nexus tests | grep -v "consumer_name\|\"bm25\"\|\"fts\"\|\"embedding\"\|\"txtai\"\|LEGACY_REFRESH_CONSUMER\|def _resolve_mutations"`
Expected: no hits (every call passes a consumer name)

- [ ] **Step 11.5: Commit any straggler fixes**

```bash
git status --short   # should be clean; commit fixes if verification surfaced any
```
