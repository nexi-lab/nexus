"""ReBAC Service shim — canonical: nexus.services.rebac.rebac_service.

Issue #1891: ReBACService will move here in a future phase.
For now, re-exports from the current canonical location.
"""

from nexus.services.rebac.rebac_service import ReBACService  # noqa: F401

__all__ = ["ReBACService"]
