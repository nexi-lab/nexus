"""Google Calendar connector schemas and errors.

This module provides:
- Pydantic schemas for Calendar CUD operations
- Error registry with self-correcting messages
"""

from nexus.connectors.calendar.errors import ERROR_REGISTRY
from nexus.connectors.calendar.schemas import (
    Attendee,
    CreateEventSchema,
    DeleteEventSchema,
    TimeSlot,
    UpdateEventSchema,
)

__all__ = [
    "TimeSlot",
    "Attendee",
    "CreateEventSchema",
    "UpdateEventSchema",
    "DeleteEventSchema",
    "ERROR_REGISTRY",
]
