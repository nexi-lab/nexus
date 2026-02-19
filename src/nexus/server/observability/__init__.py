"""Observability registry and lifecycle components.

Issue #2072: Consolidate observability init/shutdown into a unified registry
following the NEXUS-LEGO-ARCHITECTURE brick lifecycle pattern
(REGISTER -> START -> USE -> SHUTDOWN).
"""

from nexus.server.observability.registry import (
    ComponentStatus,
    LifecycleComponent,
    ObservabilityRegistry,
)

__all__ = [
    "ComponentStatus",
    "LifecycleComponent",
    "ObservabilityRegistry",
]
