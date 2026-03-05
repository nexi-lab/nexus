"""NexusFS-backed UserProvisionerProtocol implementation.

Thin adapter that delegates to NexusFS.provision_user(), creating the
OperationContext internally. This lives in the auth brick's stores/
package but imports from nexus.contracts (allowed: contracts are shared
types, not kernel internals).

Issue #2281: Extract Auth/OAuth brick from server/auth.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class NexusFSUserProvisioner:
    """Concrete UserProvisionerProtocol backed by NexusFS.

    Usage (wired by factory.py)::

        from nexus.bricks.auth.stores.nexusfs_provisioner import NexusFSUserProvisioner
        provisioner = NexusFSUserProvisioner(nexus_fs_instance)
    """

    def __init__(self, nexus_fs: Any) -> None:
        self._nx = nexus_fs

    def provision_user(
        self,
        *,
        user_id: str,
        email: str,
        display_name: str | None = None,
        zone_id: str | None = None,
        create_api_key: bool = True,
        create_agents: bool = True,
        import_skills: bool = False,
    ) -> dict[str, Any]:
        """Provision resources for a new user via NexusFS."""
        from nexus.contracts.types import OperationContext

        resolved_zone_id = zone_id or (email.split("@")[0] if email else user_id)

        admin_context = OperationContext(
            user_id="system",
            groups=[],
            zone_id=resolved_zone_id,
            is_admin=True,
        )

        result: dict[str, Any] = self._nx._user_provisioning_service.provision_user(
            user_id=user_id,
            email=email,
            display_name=display_name,
            zone_id=resolved_zone_id,
            create_api_key=create_api_key,
            create_agents=create_agents,
            import_skills=import_skills,
            context=admin_context,
        )
        return result
