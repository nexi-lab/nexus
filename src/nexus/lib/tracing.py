"""Shared OTel tracing utilities for services.

Provides zero-overhead lazy tracer resolution — when telemetry is not
enabled, the tracer resolves to ``None`` and all span operations are
no-ops.
"""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any


def lazy_tracer(name: str) -> tuple:
    """Create a lazy-resolved OTel tracer pair.

    Returns ``(get_tracer, lifecycle_span)`` where:
    - ``get_tracer()`` returns the tracer (or None if unavailable)
    - ``lifecycle_span(op, brick_name, **attrs)`` is a context manager

    Usage::

        _get_tracer, _lifecycle_span = lazy_tracer("nexus.brick_lifecycle")
    """
    _tracer: list[Any] = [None]
    _resolved: list[bool] = [False]

    def get_tracer() -> Any:
        if _resolved[0]:
            return _tracer[0]
        _resolved[0] = True
        try:
            from nexus.lib.telemetry import get_tracer as _gt

            _tracer[0] = _gt(name)
        except Exception:
            _tracer[0] = None
        return _tracer[0]

    @contextmanager
    def lifecycle_span(operation: str, brick_name: str, **attrs: Any) -> Generator[Any, None, None]:
        tracer = get_tracer()
        if tracer is None:
            yield None
            return
        with tracer.start_as_current_span(f"brick.{operation}") as span:
            span.set_attribute("brick.name", brick_name)
            for k, v in attrs.items():
                span.set_attribute(f"brick.{k}", v)
            yield span

    return get_tracer, lifecycle_span


def record_span_result(span: Any, *, state: str, error: str | None = None) -> None:
    """Record final state and optional error on a span."""
    if span is None:
        return
    span.set_attribute("brick.state", state)
    if error:
        span.set_attribute("brick.error", error)
        try:
            from opentelemetry.trace import StatusCode

            span.set_status(StatusCode.ERROR, error)
        except Exception:
            pass  # Tracing must never disrupt application flow
