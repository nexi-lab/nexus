"""Minimal in-process audit event bus (Issue #3792).

The existing nexus event-log service handles durable delivery; this
module is a tiny synchronous sink registry for security-signal events
that need to fan out to whatever the current deployment uses for
audit (log exporter, activity feed, etc.).

Audit emits must never raise. Failures are logged and swallowed.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_Sink = Callable[[str, dict[str, Any]], None]
_sinks: list[_Sink] = []


@dataclass
class SinkHandle:
    sink: _Sink

    def remove(self) -> None:
        with contextlib.suppress(ValueError):
            _sinks.remove(self.sink)


def register_audit_sink(sink: _Sink) -> SinkHandle:
    """Register a sink that receives (name, payload) for each event."""
    _sinks.append(sink)
    return SinkHandle(sink)


def emit_audit_event(name: str, payload: dict[str, Any]) -> None:
    """Emit an audit event to all registered sinks.

    Never raises — sink failures are logged at WARNING.
    """
    for sink in list(_sinks):
        try:
            sink(name, payload)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("Audit sink %r raised on event %r: %s", sink, name, exc)
