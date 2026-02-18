"""Re-export shim — canonical home is ``nexus.contracts.registry`` (Issue #1523).

All symbols are re-exported so existing ``from nexus.core.registry import …``
statements continue to work with zero changes.
"""

from nexus.contracts.registry import (
    BaseRegistry,
    BrickInfo,
    BrickRegistry,
    _validate_protocol_compliance,
)

__all__ = [
    "BaseRegistry",
    "BrickInfo",
    "BrickRegistry",
    "_validate_protocol_compliance",
]
