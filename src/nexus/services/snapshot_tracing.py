"""OpenTelemetry tracing for Transactional Snapshots (Issue #1752).

Zero-overhead tracing helpers for the snapshot subsystem.
When OTel is disabled, every function reduces to a no-op.

Span hierarchy::

    HTTP request (auto-instrumented by FastAPI)
    └── snapshot.begin / .commit / .rollback / .cleanup
        ├── snapshot.metadata_read       (batch metadata fetch)
        ├── snapshot.metadata_restore    (batch metadata restore)
        └── snapshot.db_write            (DB session write)

Attribute namespace: ``snapshot.*``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level lazy tracer (zero overhead when disabled)
# ---------------------------------------------------------------------------

_tracer_resolved = False
_tracer: Any = None
_tracer_lock = threading.Lock()


def _get_tracer() -> Any:
    """Return a cached tracer instance, or *None* when OTel is disabled."""
    global _tracer_resolved, _tracer
    if _tracer_resolved:
        return _tracer
    with _tracer_lock:
        if not _tracer_resolved:
            from nexus.server.telemetry import get_tracer

            _tracer = get_tracer("nexus.snapshot")
            _tracer_resolved = True
    return _tracer


def set_tracer(tracer: Any) -> None:
    """Inject a tracer instance — useful for tests."""
    global _tracer_resolved, _tracer
    _tracer = tracer
    _tracer_resolved = True


def reset_tracer() -> None:
    """Reset cached tracer — only for tests."""
    global _tracer_resolved, _tracer
    _tracer_resolved = False
    _tracer = None


# ---------------------------------------------------------------------------
# Attribute keys (snapshot.* namespace)
# ---------------------------------------------------------------------------

ATTR_SNAPSHOT_ID = "snapshot.id"
ATTR_AGENT_ID = "snapshot.agent_id"
ATTR_ZONE_ID = "snapshot.zone_id"
ATTR_PATH_COUNT = "snapshot.path_count"
ATTR_OPERATION = "snapshot.operation"
ATTR_REVERTED_COUNT = "snapshot.reverted_count"
ATTR_CONFLICT_COUNT = "snapshot.conflict_count"
ATTR_DELETED_COUNT = "snapshot.deleted_count"
ATTR_EXPIRED_COUNT = "snapshot.expired_count"
ATTR_DURATION_MS = "snapshot.duration_ms"


# ---------------------------------------------------------------------------
# Span helpers
# ---------------------------------------------------------------------------


@contextmanager
def start_snapshot_span(
    operation: str,
    *,
    agent_id: str | None = None,
    zone_id: str | None = None,
    path_count: int | None = None,
    snapshot_id: str | None = None,
) -> Generator[Any, None, None]:
    """Context manager that creates a ``snapshot.<operation>`` span.

    Yields the span (or *None* when OTel is disabled).
    """
    tracer = _get_tracer()
    if tracer is None:
        yield None
        return

    with tracer.start_as_current_span(f"snapshot.{operation}") as span:
        span.set_attribute(ATTR_OPERATION, operation)
        if agent_id is not None:
            span.set_attribute(ATTR_AGENT_ID, agent_id)
        if zone_id is not None:
            span.set_attribute(ATTR_ZONE_ID, zone_id)
        if path_count is not None:
            span.set_attribute(ATTR_PATH_COUNT, path_count)
        if snapshot_id is not None:
            span.set_attribute(ATTR_SNAPSHOT_ID, snapshot_id)
        yield span


def record_begin_result(span: Any, *, snapshot_id: str) -> None:
    """Record begin result attributes."""
    if span is None:
        return
    span.set_attribute(ATTR_SNAPSHOT_ID, snapshot_id)


def record_rollback_result(
    span: Any,
    *,
    reverted: int,
    conflicts: int,
    deleted: int,
) -> None:
    """Record rollback result attributes."""
    if span is None:
        return
    span.set_attribute(ATTR_REVERTED_COUNT, reverted)
    span.set_attribute(ATTR_CONFLICT_COUNT, conflicts)
    span.set_attribute(ATTR_DELETED_COUNT, deleted)


def record_cleanup_result(span: Any, *, expired_count: int) -> None:
    """Record cleanup result attributes."""
    if span is None:
        return
    span.set_attribute(ATTR_EXPIRED_COUNT, expired_count)
