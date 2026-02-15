"""OpenTelemetry tracing for ReBAC permission checks.

Issue #702: OTel tracing for ReBAC permission debugging.

This module provides zero-overhead tracing helpers for the ReBAC permission
subsystem.  When OTel is disabled (``OTEL_ENABLED != true``), every public
function reduces to a no-op — no spans, no attributes, no allocations.

Span hierarchy::

    HTTP request (auto-instrumented by FastAPI)
    └── rebac.check                    (root ReBAC span)
        ├── rebac.cache_lookup         (L1/Tiger/Boundary cache probe)
        └── rebac.graph_traversal      (Zanzibar graph walk)

Attribute namespace: ``authz.*`` following emerging OTel conventions for
authorization (no official semantic convention exists yet).

Usage::

    from nexus.services.permissions.rebac_tracing import (
        start_check_span,
        record_check_result,
        start_cache_lookup_span,
        record_cache_result,
        start_graph_traversal_span,
        record_traversal_result,
        start_batch_check_span,
        record_batch_result,
    )
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any, TypeVar

_F = TypeVar("_F", bound=Callable[..., Any])

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level lazy tracer (Decision #3A — zero overhead when disabled)
# ---------------------------------------------------------------------------

_tracer_resolved = False
_tracer: Any = None  # opentelemetry.trace.Tracer | None
_tracer_lock = threading.Lock()


def _get_tracer() -> Any:
    """Return a cached tracer instance, or *None* when OTel is disabled.

    Thread-safe via double-checked locking: the fast path (already resolved)
    requires no lock acquisition.
    """
    global _tracer_resolved, _tracer
    if _tracer_resolved:
        return _tracer
    with _tracer_lock:
        if not _tracer_resolved:
            # Lazy import to break circular dependency:
            # rebac_tracing -> server.telemetry -> server.__init__ -> NexusFS -> rebac_service -> rebac_tracing
            from nexus.server.telemetry import get_tracer

            _tracer = get_tracer("nexus.rebac")
            _tracer_resolved = True
    return _tracer


def reset_tracer() -> None:
    """Reset cached tracer — only for tests."""
    global _tracer_resolved, _tracer
    _tracer_resolved = False
    _tracer = None


# ---------------------------------------------------------------------------
# Attribute keys (authz.* namespace — Decision #4A)
# ---------------------------------------------------------------------------

# Subject
ATTR_SUBJECT_TYPE = "authz.subject.type"
ATTR_SUBJECT_ID = "authz.subject.id"

# Permission / Object
ATTR_PERMISSION = "authz.permission"
ATTR_OBJECT_TYPE = "authz.object.type"
ATTR_OBJECT_ID = "authz.object.id"
ATTR_ZONE_ID = "authz.zone_id"

# Decision
ATTR_DECISION = "authz.decision"  # "ALLOW" | "DENY"
ATTR_DECISION_TIME_MS = "authz.decision_time_ms"

# Cache
ATTR_CACHE_HIT = "authz.cache.hit"
ATTR_CACHE_SOURCE = "authz.cache.source"  # "l1" | "tiger" | "boundary"
ATTR_CACHE_FALLBACK = "authz.cache.fallback"  # True when circuit breaker fallback

# Graph traversal
ATTR_TRAVERSAL_DEPTH = "authz.traversal.depth"
ATTR_TRAVERSAL_VISITED = "authz.traversal.visited_nodes"
ATTR_TRAVERSAL_QUERIES = "authz.traversal.db_queries"
ATTR_TRAVERSAL_CACHE_HITS = "authz.traversal.cache_hits"

# Engine
ATTR_ENGINE = "authz.engine"  # "rust" | "python"

# Limits / errors
ATTR_LIMIT_EXCEEDED = "authz.limit_exceeded"
ATTR_LIMIT_TYPE = "authz.limit_type"

# Consistency
ATTR_CONSISTENCY = "authz.consistency"  # "eventual" | "bounded" | "strong"

# Circuit breaker
ATTR_CIRCUIT_BREAKER = "authz.circuit_breaker"  # "open" | "closed"

# Batch
ATTR_BATCH_SIZE = "authz.batch.size"
ATTR_BATCH_ALLOWED = "authz.batch.allowed_count"
ATTR_BATCH_DENIED = "authz.batch.denied_count"
ATTR_BATCH_DURATION_MS = "authz.batch.duration_ms"


# ---------------------------------------------------------------------------
# Span helpers — rebac.check
# ---------------------------------------------------------------------------


@contextmanager
def start_check_span(
    subject: tuple[str, str],
    permission: str,
    obj: tuple[str, str],
    zone_id: str | None = None,
    consistency: str | None = None,
) -> Generator[Any, None, None]:
    """Context manager that creates the root ``rebac.check`` span.

    Yields the span (or *None* when OTel is disabled) so callers can attach
    additional attributes as they become available.

    Args:
        subject: (subject_type, subject_id)
        permission: Permission being checked
        obj: (object_type, object_id)
        zone_id: Zone for multi-tenant isolation
        consistency: Consistency level name (eventual, bounded, strong)
    """
    tracer = _get_tracer()
    if tracer is None:
        yield None
        return

    with tracer.start_as_current_span("rebac.check") as span:
        span.set_attribute(ATTR_SUBJECT_TYPE, subject[0])
        span.set_attribute(ATTR_SUBJECT_ID, subject[1])
        span.set_attribute(ATTR_PERMISSION, permission)
        span.set_attribute(ATTR_OBJECT_TYPE, obj[0])
        span.set_attribute(ATTR_OBJECT_ID, obj[1])
        if zone_id:
            span.set_attribute(ATTR_ZONE_ID, zone_id)
        if consistency:
            span.set_attribute(ATTR_CONSISTENCY, consistency)
        yield span


def record_check_result(
    span: Any,
    *,
    allowed: bool,
    decision_time_ms: float,
    cached: bool = False,
    engine: str | None = None,
) -> None:
    """Record the final check decision on an existing span.

    Args:
        span: The span from ``start_check_span`` (may be *None*).
        allowed: Whether permission was granted.
        decision_time_ms: Wall-clock decision time in ms.
        cached: Whether result came from cache.
        engine: "rust" or "python" (if graph traversal was used).
    """
    if span is None:
        return
    span.set_attribute(ATTR_DECISION, "ALLOW" if allowed else "DENY")
    span.set_attribute(ATTR_DECISION_TIME_MS, decision_time_ms)
    span.set_attribute(ATTR_CACHE_HIT, cached)
    if engine:
        span.set_attribute(ATTR_ENGINE, engine)


# ---------------------------------------------------------------------------
# Span helpers — rebac.cache_lookup
# ---------------------------------------------------------------------------


@contextmanager
def start_cache_lookup_span() -> Generator[Any, None, None]:
    """Child span for cache probing (L1, Tiger, Boundary)."""
    tracer = _get_tracer()
    if tracer is None:
        yield None
        return

    with tracer.start_as_current_span("rebac.cache_lookup") as span:
        yield span


def record_cache_result(
    span: Any,
    *,
    hit: bool,
    source: str | None = None,
    fallback: bool = False,
) -> None:
    """Record cache probe result.

    Args:
        span: Span from ``start_cache_lookup_span``.
        hit: Whether the cache returned a result.
        source: Cache layer that answered ("l1", "tiger", "boundary").
        fallback: True if this was a circuit-breaker fallback to cached data.
    """
    if span is None:
        return
    span.set_attribute(ATTR_CACHE_HIT, hit)
    if source:
        span.set_attribute(ATTR_CACHE_SOURCE, source)
    if fallback:
        span.set_attribute(ATTR_CACHE_FALLBACK, True)


# ---------------------------------------------------------------------------
# Span helpers — rebac.graph_traversal
# ---------------------------------------------------------------------------


@contextmanager
def start_graph_traversal_span(engine: str = "python") -> Generator[Any, None, None]:
    """Child span for the Zanzibar graph walk.

    Args:
        engine: "rust" or "python".
    """
    tracer = _get_tracer()
    if tracer is None:
        yield None
        return

    with tracer.start_as_current_span("rebac.graph_traversal") as span:
        span.set_attribute(ATTR_ENGINE, engine)
        yield span


def record_traversal_result(
    span: Any,
    *,
    depth: int = 0,
    visited_nodes: int = 0,
    db_queries: int = 0,
    cache_hits: int = 0,
) -> None:
    """Record graph traversal statistics on the span.

    Args:
        span: Span from ``start_graph_traversal_span``.
        depth: Maximum depth reached during traversal.
        visited_nodes: Number of graph nodes visited.
        db_queries: Number of database queries executed.
        cache_hits: Number of cache hits during traversal.
    """
    if span is None:
        return
    span.set_attribute(ATTR_TRAVERSAL_DEPTH, depth)
    span.set_attribute(ATTR_TRAVERSAL_VISITED, visited_nodes)
    span.set_attribute(ATTR_TRAVERSAL_QUERIES, db_queries)
    span.set_attribute(ATTR_TRAVERSAL_CACHE_HITS, cache_hits)


def record_graph_limit_exceeded(span: Any, *, limit_type: str) -> None:
    """Record a graph limit violation as a span error.

    Args:
        span: Span from ``start_graph_traversal_span``.
        limit_type: Which limit was exceeded (depth, fan_out, timeout, etc.).
    """
    if span is None:
        return
    span.set_attribute(ATTR_LIMIT_EXCEEDED, True)
    span.set_attribute(ATTR_LIMIT_TYPE, limit_type)
    # If span is not None, OTel is installed — StatusCode is always available
    from opentelemetry.trace import StatusCode

    span.set_status(StatusCode.ERROR, f"Graph limit exceeded: {limit_type}")


# ---------------------------------------------------------------------------
# Span helpers — rebac.check_batch
# ---------------------------------------------------------------------------


@contextmanager
def start_batch_check_span(batch_size: int) -> Generator[Any, None, None]:
    """Root span for a batch permission check.

    Args:
        batch_size: Number of individual checks in the batch.
    """
    tracer = _get_tracer()
    if tracer is None:
        yield None
        return

    with tracer.start_as_current_span("rebac.check_batch") as span:
        span.set_attribute(ATTR_BATCH_SIZE, batch_size)
        yield span


def record_batch_result(
    span: Any,
    *,
    allowed_count: int,
    denied_count: int,
    duration_ms: float,
) -> None:
    """Record batch check summary on the span.

    Args:
        span: Span from ``start_batch_check_span``.
        allowed_count: Number of checks that were allowed.
        denied_count: Number of checks that were denied.
        duration_ms: Total batch duration.
    """
    if span is None:
        return
    span.set_attribute(ATTR_BATCH_ALLOWED, allowed_count)
    span.set_attribute(ATTR_BATCH_DENIED, denied_count)
    span.set_attribute(ATTR_BATCH_DURATION_MS, duration_ms)


# ---------------------------------------------------------------------------
# OTel context propagation helper (Decision #5A)
# ---------------------------------------------------------------------------


def propagate_otel_context(fn: _F) -> _F:
    """Wrap *fn* so that the caller's OTel context is attached in the thread.

    Use this in ``asyncio.to_thread()`` calls to ensure spans created in the
    worker thread are children of the async caller's span.

    .. note::
        The OTel context is captured at **wrap-time** (when this function is
        called), not at invocation-time of the returned wrapper.  Do not cache
        the returned wrapper across requests.

    Returns:
        A wrapper that captures the current OTel context and attaches it before
        invoking *fn*.  When OTel is not installed/available, returns *fn*
        unchanged.
    """
    try:
        from opentelemetry import context as otel_context

        ctx = otel_context.get_current()

        def _with_context(*args: Any, **kwargs: Any) -> Any:
            token = otel_context.attach(ctx)
            try:
                return fn(*args, **kwargs)
            finally:
                otel_context.detach(token)

        return _with_context  # type: ignore[return-value]
    except ImportError:
        return fn
