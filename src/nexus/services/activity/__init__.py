"""Activity event subsystem (issue #3791 foundation slice).

See ``docs/superpowers/specs/2026-04-30-3791-activity-event-foundation-design.md``.
"""

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
]
