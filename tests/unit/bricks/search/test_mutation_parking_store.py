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
    # Persist-then-commit: the failed park must not leave a phantom record.
    assert not store.contains("bm25", "search:op-1")
    assert store.count("bm25") == 0


@pytest.mark.asyncio
async def test_remove_rolls_back_when_persistence_fails(tmp_path, monkeypatch) -> None:
    store = MutationParkStore(
        settings_store=None,
        fallback_file=tmp_path / "mutation-parked.json",
        max_entries_per_consumer=10,
    )
    await store.load()
    await store.park(_entry())

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.write_text", _boom)
    with pytest.raises(OSError):
        await store.remove("bm25", ["search:op-1"])
    # Entry must still be present — a failed remove must not vanish records.
    assert store.contains("bm25", "search:op-1")
    assert store.count("bm25") == 1
