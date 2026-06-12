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

        Persist-then-commit: ``self._entries`` is only updated after the
        document is durably written. If BOTH persistence paths fail this
        raises and in-memory state is untouched, so ``contains`` never
        reports a phantom park — callers must not skip an event that has
        no durable record.
        """
        async with self._lock:
            entries = [
                e for e in self._entries.get(entry.consumer, []) if e.event_id != entry.event_id
            ]
            entries.append(entry)
            evicted: list[ParkedEvent] = []
            while len(entries) > self._max_entries:
                evicted.append(entries.pop(0))
            candidate = {**self._entries, entry.consumer: entries}
            await self._persist(candidate)
            self._entries = candidate
            for old in evicted:
                consumer_metrics.MUTATION_PARKED_EVICTED_TOTAL.labels(consumer=entry.consumer).inc()
                logger.error(
                    "Parked-event cap (%d) exceeded for consumer=%s — evicting "
                    "oldest entry event_id=%s path=%s (parking storm?)",
                    self._max_entries,
                    entry.consumer,
                    old.event_id,
                    old.path,
                )
            self._sync_gauge(entry.consumer)

    async def remove(self, consumer: str, event_ids: list[str]) -> list[ParkedEvent]:
        """Remove entries by id; returns the removed entries.

        Persist-then-commit, same contract as ``park``.
        """
        wanted = set(event_ids)
        async with self._lock:
            entries = self._entries.get(consumer, [])
            removed = [e for e in entries if e.event_id in wanted]
            if not removed:
                return []
            candidate = {
                **self._entries,
                consumer: [e for e in entries if e.event_id not in wanted],
            }
            await self._persist(candidate)
            self._entries = candidate
            self._sync_gauge(consumer)
            return removed

    # -- internals ----------------------------------------------------------

    def _read_file(self) -> str | None:
        try:
            if not self._fallback_file.exists():
                return None
            return self._fallback_file.read_text()
        except Exception as exc:
            logger.warning("Parked-event fallback file unreadable: %s", exc)
            return None

    def _sync_gauge(self, consumer: str) -> None:
        consumer_metrics.MUTATION_PARKED.labels(consumer=consumer).set(
            len(self._entries.get(consumer, []))
        )

    async def _persist(self, document: dict[str, list[ParkedEvent]]) -> None:
        payload = json.dumps(
            {
                consumer: [e.to_dict() for e in entries]
                for consumer, entries in document.items()
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
