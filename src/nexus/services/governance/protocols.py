"""Backward-compatible re-exports (Issue #2129).

Canonical location: ``nexus.bricks.governance.protocols``
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.bricks.governance.protocols import (
        AnomalyDetectorProtocol as AnomalyDetectorProtocol,
    )
    from nexus.bricks.governance.protocols import (
        AnomalyServiceProtocol as AnomalyServiceProtocol,
    )
    from nexus.bricks.governance.protocols import (
        CollusionServiceProtocol as CollusionServiceProtocol,
    )
    from nexus.bricks.governance.protocols import (
        GovernanceGraphProtocol as GovernanceGraphProtocol,
    )

_NAMES = {
    "AnomalyDetectorProtocol",
    "AnomalyServiceProtocol",
    "CollusionServiceProtocol",
    "GovernanceGraphProtocol",
}

__all__ = sorted(_NAMES)


def __getattr__(name: str) -> object:
    if name in _NAMES:
        import importlib

        mod = importlib.import_module("nexus.bricks.governance.protocols")
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
