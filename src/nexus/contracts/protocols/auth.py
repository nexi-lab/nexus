"""Authentication service protocol interfaces (Issue #1519, 3A).

Defines protocols for auth operations that the kernel (core/) needs to call
without importing from the server layer. Implementations live in
server/auth/ and are injected via factory.py.

Convention (Issue #1291):
- All protocols use @runtime_checkable for test-time isinstance() checks.
- Do NOT use isinstance(obj, Protocol) in production hot paths.
"""

from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class APIKeyCreatorProtocol(Protocol):
    """Protocol for creating API keys.

    Abstracts the DatabaseAPIKeyAuth.create_key() static method so that
    core/ modules can create API keys without importing from server/auth/.

    Implementation: nexus.server.auth.database_key.DatabaseAPIKeyAuth
    Storage Affinity: RecordStore (API keys stored in PostgreSQL/SQLite)
    """

    def create_key(
        self,
        session: Any,
        user_id: str,
        name: str,
        subject_type: str = "user",
        subject_id: str | None = None,
        zone_id: str | None = None,
        is_admin: bool = False,
        expires_at: datetime | None = None,
        inherit_permissions: bool = False,
    ) -> tuple[str, str]:
        """Create a new API key.

        Args:
            session: SQLAlchemy session
            user_id: User identifier (owner of the key)
            name: Human-readable key name
            subject_type: Type of subject ("user" or "agent")
            subject_id: Custom subject ID (for agents)
            zone_id: Optional zone identifier
            is_admin: Whether this key has admin privileges
            expires_at: Optional expiry datetime (UTC)
            inherit_permissions: Whether agent inherits owner's permissions

        Returns:
            Tuple of (key_id, raw_key)
        """
        ...
