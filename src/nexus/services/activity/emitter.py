"""Emitter singleton + NoopEmitter.

QueueEmitter (added in Task 3) is the production implementation. NoopEmitter
is the default — installed at process import so any code calling emit(...)
before lifespan startup is a safe no-op.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from nexus.services.activity.events import ActivityEvent, Actor, EventKind, Result, Subject


@runtime_checkable
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


def _new_id() -> str:
    """Sortable id: ms-timestamp prefix + random suffix (no third-party dep)."""
    ms = int(time.time() * 1000)
    rand = uuid.uuid4().hex[:16]
    return f"{ms:013d}{rand}"


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="microseconds")


class QueueEmitter:
    """Production emitter — non-blocking put_nowait with drop counter.

    Thread-safe: asyncio.Queue.put_nowait is safe from non-loop threads
    when only the worker awaits get() on the loop thread.
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
