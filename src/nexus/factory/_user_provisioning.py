"""User provisioning service factory — server-layer RPC (Issue #635)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def create_user_provisioning_service(nx: Any) -> Any:
    """Create UserProvisioningService for server-layer RPC dispatch (Issue #635).

    This is a **server-layer** factory function — the kernel (NexusFS) has
    zero knowledge of UserProvisioningService.  The server calls this once
    during ``create_app()`` and registers the result as an additional RPC source.

    Args:
        nx: A NexusFS instance (used for kernel ops and attribute access).

    Returns:
        UserProvisioningService instance, or None if dependencies are unavailable.
    """
    try:
        from nexus.services.user_provisioning import UserProvisioningService

        svc = UserProvisioningService(nx=nx)
        logger.info("[FACTORY] UserProvisioningService created for server-layer RPC")
        return svc
    except Exception as exc:
        logger.debug("[FACTORY] UserProvisioningService unavailable: %s", exc)
        return None
