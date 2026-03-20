"""Backward-compatible re-export — canonical module is event_subsystem.log.replay.

Issue #3193: consolidated to single implementation.
"""

from nexus.services.event_subsystem.log.replay import (
    EventRecord,
    EventReplayService,
    ReplayResult,
)

__all__ = ["EventRecord", "EventReplayService", "ReplayResult"]
