"""Parking-gate, checkpoint, and admin-method tests for SearchDaemon (#4337)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon
from nexus.bricks.search.mutation_events import SearchMutationEvent, SearchMutationOp
from nexus.bricks.search.mutation_parking import MutationParkStore, UnresolvedMutationError
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


class FakeChunkStore:
    """Records document writes from the fts consumer."""

    def __init__(self) -> None:
        self.replaced: list[str] = []

    async def replace_document_chunks(self, path_id: str, records: Any) -> None:
        self.replaced.append(path_id)

    async def delete_document_chunks(self, path_id: str) -> None:
        pass


def _daemon(tmp_path, **config_overrides: Any) -> SearchDaemon:
    config = DaemonConfig(
        database_url=None,
        txtai_model=None,
        refresh_enabled=False,
        vector_warmup_enabled=False,
        mutation_unresolved_permanent_attempts=2,
        mutation_unresolved_transient_attempts=4,
        **config_overrides,
    )
    daemon = SearchDaemon(config)
    # The checkpoint file is anchored at .nexus-data in CWD — redirect both
    # it and the park store into tmp_path so tests never touch the repo dir.
    daemon._checkpoint_file = tmp_path / "mutation-checkpoints.json"
    daemon._park_store = MutationParkStore(
        settings_store=None,
        fallback_file=tmp_path / "mutation-parked.json",
        max_entries_per_consumer=config.mutation_parked_max_entries,
    )
    return daemon


@pytest.mark.asyncio
async def test_under_budget_raises_unresolved_error(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    poison = _event("search:poison", 10)
    daemon._mutation_resolver = FakeResolver({"search:poison": _unresolved(poison)})
    with pytest.raises(UnresolvedMutationError):
        await daemon._resolve_mutations("fts", [poison])
    assert daemon._park_store.count("fts") == 0


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
        await daemon._resolve_mutations("fts", events)
    kept = await daemon._resolve_mutations("fts", events)
    assert [m.event.event_id for m in kept] == ["search:ok"]
    assert daemon._park_store.contains("fts", "search:poison")
    assert ("fts", "search:poison") not in daemon._unresolved_attempts


@pytest.mark.asyncio
async def test_transient_budget_is_larger(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    poison = _event("search:flaky", 10)
    daemon._mutation_resolver = FakeResolver(
        {"search:flaky": _unresolved(poison, kind="transient")}
    )
    for _ in range(3):  # transient budget = 4: passes 1-3 raise
        with pytest.raises(UnresolvedMutationError):
            await daemon._resolve_mutations("embedding", [poison])
    kept = await daemon._resolve_mutations("embedding", [poison])  # pass 4 parks
    assert kept == []
    assert daemon._park_store.contains("embedding", "search:flaky")


@pytest.mark.asyncio
async def test_already_parked_event_is_filtered_without_recount(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    poison = _event("search:poison", 10)
    daemon._mutation_resolver = FakeResolver({"search:poison": _unresolved(poison)})
    with pytest.raises(UnresolvedMutationError):
        await daemon._resolve_mutations("fts", [poison])
    await daemon._resolve_mutations("fts", [poison])  # parks
    kept = await daemon._resolve_mutations("fts", [poison])  # already parked
    assert kept == []
    assert ("fts", "search:poison") not in daemon._unresolved_attempts
    assert daemon._park_store.count("fts") == 1


@pytest.mark.asyncio
async def test_recovered_event_auto_unparks(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    poison = _event("search:poison", 10)
    daemon._mutation_resolver = FakeResolver({"search:poison": _unresolved(poison)})
    with pytest.raises(UnresolvedMutationError):
        await daemon._resolve_mutations("fts", [poison])
    await daemon._resolve_mutations("fts", [poison])  # parks
    daemon._mutation_resolver = FakeResolver({"search:poison": _resolved(poison)})
    kept = await daemon._resolve_mutations("fts", [poison])
    assert [m.event.event_id for m in kept] == ["search:poison"]
    assert not daemon._park_store.contains("fts", "search:poison")


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
        await daemon._resolve_mutations("fts", [poison])
    # embedding has its own counter — first pass raises even though fts counted one.
    with pytest.raises(UnresolvedMutationError):
        await daemon._resolve_mutations("embedding", [poison])
    assert daemon._unresolved_attempts[("fts", "search:poison")] == 1
    assert daemon._unresolved_attempts[("embedding", "search:poison")] == 1


@pytest.mark.asyncio
async def test_fts_consumer_unwedges_after_parking(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    poison = _event("search:poison", 10)
    healthy = _event("search:ok", 11)
    daemon._mutation_resolver = FakeResolver(
        {"search:poison": _unresolved(poison), "search:ok": _resolved(healthy)}
    )
    daemon._chunk_store = FakeChunkStore()
    events = [poison, healthy]
    # Pass 1: under budget — handler raises, nothing indexed (no checkpoint).
    with pytest.raises(UnresolvedMutationError):
        await daemon._consume_fts_mutations(events)
    assert daemon._chunk_store.replaced == []
    # Pass 2: budget (=2) hit — poison parks, healthy indexes, handler returns.
    await daemon._consume_fts_mutations(events)
    assert daemon._chunk_store.replaced == ["pid-ok"]
    assert daemon._park_store.contains("fts", "search:poison")


@pytest.mark.asyncio
async def test_get_stats_exposes_parking_state(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    poison = _event("search:poison", 10)
    daemon._mutation_resolver = FakeResolver({"search:poison": _unresolved(poison)})
    with pytest.raises(UnresolvedMutationError):
        await daemon._resolve_mutations("fts", [poison])
    stats = daemon.get_stats()
    fts = stats["mutation_consumers"]["fts"]
    assert fts["retrying"]["event_id"] == "search:poison"
    assert fts["retrying"]["attempts"] == 1
    assert fts["parked_count"] == 0
    await daemon._resolve_mutations("fts", [poison])  # parks (budget=2)
    stats = daemon.get_stats()
    fts = stats["mutation_consumers"]["fts"]
    assert fts["parked_count"] == 1
    assert fts["last_parked"]["event_id"] == "search:poison"
    assert fts["last_parked"]["kind"] == "permanent"
    assert fts["retrying"] is None
    # Consumers with no activity still report the parking keys.
    assert stats["mutation_consumers"]["embedding"]["parked_count"] == 0


@pytest.mark.asyncio
async def test_force_checkpoint_advances_and_validates(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    daemon._consumer_last_sequence["fts"] = 100
    result = await daemon.force_checkpoint("fts", 250)
    assert result == {"previous": 100, "current": 250}
    assert daemon._consumer_last_sequence["fts"] == 250

    with pytest.raises(ValueError, match="greater than current"):
        await daemon.force_checkpoint("fts", 250)
    with pytest.raises(ValueError, match="unknown consumer"):
        await daemon.force_checkpoint("nope", 999)


@pytest.mark.asyncio
async def test_save_checkpoint_is_monotonic(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    daemon._consumer_last_sequence["fts"] = 100
    await daemon.force_checkpoint("fts", 250)
    # An in-flight stale pass completing with an older batch must not rewind.
    await daemon._save_consumer_checkpoint("fts", 120)
    assert daemon._consumer_last_sequence["fts"] == 250


@pytest.mark.asyncio
async def test_force_checkpoint_prunes_retry_counters(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    poison = _event("search:poison", 10)
    daemon._mutation_resolver = FakeResolver({"search:poison": _unresolved(poison)})
    daemon._consumer_last_sequence["fts"] = 5
    with pytest.raises(UnresolvedMutationError):
        await daemon._resolve_mutations("fts", [poison])
    assert ("fts", "search:poison") in daemon._unresolved_attempts
    await daemon.force_checkpoint("fts", 50)
    # Skipped-past events must not leak attempt counters.
    assert ("fts", "search:poison") not in daemon._unresolved_attempts


async def _park_one(daemon, consumer: str = "fts", event_id: str = "search:poison"):
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
    assert list(parked.keys()) == ["fts"]
    assert parked["fts"][0]["event_id"] == "search:poison"
    assert parked["fts"][0]["kind"] == "permanent"


@pytest.mark.asyncio
async def test_retry_parked_success_unparks(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    poison = await _park_one(daemon)
    # Content recovered: resolver now resolves the event.
    daemon._mutation_resolver = FakeResolver({"search:poison": _resolved(poison)})
    daemon._chunk_store = FakeChunkStore()
    result = await daemon.retry_parked("fts", None)
    assert result["retried"] == 1
    assert result["succeeded"] == ["search:poison"]
    assert result["failed"] == []
    assert not daemon._park_store.contains("fts", "search:poison")
    assert daemon._chunk_store.replaced == ["pid-poison"]


@pytest.mark.asyncio
async def test_retry_parked_failure_reparks(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    await _park_one(daemon)  # leaves resolver returning unresolved
    daemon._chunk_store = FakeChunkStore()
    result = await daemon.retry_parked("fts", ["search:poison"])
    assert result["succeeded"] == []
    assert result["failed"][0]["event_id"] == "search:poison"
    # Still parked (re-parked after the one-shot failed) — not silently lost.
    assert daemon._park_store.contains("fts", "search:poison")
    # And the one-shot didn't leak a live retry counter.
    assert ("fts", "search:poison") not in daemon._unresolved_attempts


@pytest.mark.asyncio
async def test_discard_parked_removes_without_retry(tmp_path) -> None:
    daemon = _daemon(tmp_path)
    await _park_one(daemon)
    result = await daemon.discard_parked("fts", ["search:poison"])
    assert result == {"discarded": ["search:poison"]}
    assert daemon._park_store.count("fts") == 0

    with pytest.raises(ValueError, match="unknown consumer"):
        await daemon.discard_parked("nope", ["x"])
