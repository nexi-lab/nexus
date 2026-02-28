"""UserProvisionerProtocol — breaks auth→server circular dependency.

OAuthUserAuth._provision_oauth_user() needs to call NexusFS.provision_user()
after creating a new OAuth user. Instead of importing get_nexus_instance()
from server/auth/auth_routes.py (circular), we inject this protocol.

Issue #2281: Extract Auth/OAuth brick from server/auth.
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class UserProvisionerProtocol(Protocol):
    """Provisions resources for a newly created user.

    Responsibilities:
    - Create user zone
    - Create default directories
    - Generate API key
    - Create default agents
    Concrete implementation wraps NexusFS.provision_user() and is
    wired by factory.py / server lifespan.
    """

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
        """Provision resources for a new user.

        Args:
            user_id: Unique user identifier.
            email: User email address.
            display_name: Optional display name.
            zone_id: Override zone ID (default: derived from email).
            create_api_key: Whether to generate an API key.
            create_agents: Whether to create default agents.
            import_skills: Deprecated, no-op (skills system removed).

        Returns:
            Dict with at least ``zone_id`` key and any created resource IDs.
        """
        ...
