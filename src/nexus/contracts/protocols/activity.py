"""Brick-facing emit API for the activity subsystem (issue #3791).

Bricks call ``emit(...)`` from this module. The implementation registered
via ``set_emitter`` lives in ``nexus.services.activity`` (lifespan owns it),
but bricks must never import that module — this contract is the only
dependency they need.

Hot-path contract (binds every Emitter implementation):
- MUST NOT raise.
- MUST NOT block.
- SHOULD return well under 50µs at p99.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class EventKind(StrEnum):
    SEARCH = "search"
    FETCH = "fetch"
    MCP_TOOL_CALL = "mcp_tool_call"
    ZONE_ACCESS = "zone_access"
    POLICY_BLOCK = "policy_block"
    APPROVAL = "approval"
    OP = "op"
    EXEC = "exec"


class Result(StrEnum):
    OK = "ok"
    BLOCKED = "blocked"
    PENDING_APPROVAL = "pending_approval"


@runtime_checkable
class Emitter(Protocol):
    """Contract for emitter implementations."""

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
        kind: EventKind,  # noqa: ARG002
        result: Result,  # noqa: ARG002
        actor_token_hash: str | None = None,  # noqa: ARG002
        actor_agent: str | None = None,  # noqa: ARG002
        actor_user: str | None = None,  # noqa: ARG002
        subject_zone: str | None = None,  # noqa: ARG002
        subject_extra: dict[str, Any] | None = None,  # noqa: ARG002
        latency_ms: int | None = None,  # noqa: ARG002
        trace_id: str | None = None,  # noqa: ARG002
        meta: dict[str, Any] | None = None,  # noqa: ARG002
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


_PendingGaugeSetter = Callable[[int], None]
_pending_gauge_setter: _PendingGaugeSetter | None = None


def register_approvals_pending_gauge(setter: _PendingGaugeSetter) -> None:
    """Register the gauge setter from the services layer.

    The contracts layer cannot import from nexus.services (architecture
    boundary). The services activity package calls this at module import
    time so brick callers can reseed the gauge through ``reseed_approvals_pending``
    without crossing the boundary.
    """
    global _pending_gauge_setter
    _pending_gauge_setter = setter


def reseed_approvals_pending(count: int) -> None:
    """Reset the APPROVALS_PENDING gauge from durable repository state.

    Bricks call this from ApprovalService at startup and after every
    transition so the gauge matches the database. No-op until the
    services layer registers a setter — this is intentional so the
    contracts module can be loaded standalone without importing services.
    """
    if _pending_gauge_setter is None:
        return
    # Telemetry must never break the approval flow — swallow any setter error.
    with contextlib.suppress(Exception):
        _pending_gauge_setter(count)
