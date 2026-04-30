"""Emitter singleton + NoopEmitter.

QueueEmitter (added in Task 3) is the production implementation. NoopEmitter
is the default — installed at process import so any code calling emit(...)
before lifespan startup is a safe no-op.
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
