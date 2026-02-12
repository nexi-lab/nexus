"""Kernel Protocol interfaces — the "stud shapes" of the LEGO architecture.

Each Protocol defines a component boundary in the Nexus kernel baseplate.
Implementations (bricks) plug into these interfaces.

See: NEXUS-LEGO-ARCHITECTURE.md Section 2.2
Tracked by: #1383 (Define 6 kernel Protocol interfaces)
"""

from nexus.core.protocols.event_log import EventLogConfig, EventLogProtocol

__all__ = [
    "EventLogConfig",
    "EventLogProtocol",
]
