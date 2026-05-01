"""Activity event subsystem (issue #3791 foundation slice).

See ``docs/superpowers/specs/2026-04-30-3791-activity-event-foundation-design.md``.
"""

# Side-effect import: registers the APPROVALS_PENDING gauge setter with the
# contracts-side reseed entrypoint so brick callers can update the gauge
# without crossing the contracts→services boundary. Imported here (not lazily)
# so the wiring is in place before any brick calls reseed_approvals_pending().
from nexus.services.activity import metrics as _metrics  # noqa: F401
from nexus.services.activity.emitter import (
    Emitter,
    NoopEmitter,
    QueueEmitter,
    emit,
    get_emitter,
    set_emitter,
)
from nexus.services.activity.events import (
    ActivityEvent,
    Actor,
    EventKind,
    Result,
    Subject,
)
from nexus.services.activity.lifespan import setup_activity, shutdown_activity
from nexus.services.activity.worker import ActivityWorker

__all__ = [
    "ActivityEvent",
    "ActivityWorker",
    "Actor",
    "Emitter",
    "EventKind",
    "NoopEmitter",
    "QueueEmitter",
    "Result",
    "Subject",
    "emit",
    "get_emitter",
    "set_emitter",
    "setup_activity",
    "shutdown_activity",
]
