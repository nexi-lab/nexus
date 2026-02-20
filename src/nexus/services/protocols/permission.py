"""Backward-compat alias — canonical: nexus.services.protocols.rebac.

``PermissionProtocol`` is now an alias for ``ReBACBrickProtocol``.
The two protocols have been merged per Issue #1891.
"""

from nexus.services.protocols.rebac import ReBACBrickProtocol

PermissionProtocol = ReBACBrickProtocol

__all__ = ["PermissionProtocol"]
