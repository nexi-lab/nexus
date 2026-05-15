"""ActivityEvent schema for issue #3791 foundation slice.

Frozen dataclasses for the wire/storage shape of events. ``EventKind`` and
``Result`` live in ``nexus.contracts.protocols.activity`` so bricks can
import them without touching ``nexus.services``; this module re-exports
them for service-side imports. ``actor.token_hash`` is the SHA256[:16] of
the raw bearer token (matches ``bricks/mcp/middleware_audit.py``); raw
tokens are NEVER stored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nexus.contracts.protocols.activity import EventKind, Result

__all__ = ["ActivityEvent", "Actor", "EventKind", "Result", "Subject"]


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
