"""Base authentication provider interface for Nexus server."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class AuthResult:
    """Result of authentication attempt.

    Attributes:
        authenticated: Whether authentication succeeded
        subject_type: Type of authenticated entity ("user", "agent", "service", "session")
        subject_id: Unique identifier for the subject (e.g., "alice", "agent_123", "session_abc")
        tenant_id: Optional tenant/organization identifier (metadata only, not used for identity)
        is_admin: Whether this subject has admin privileges
        inherit_permissions: Whether agent inherits owner's permissions (v0.5.1)
        metadata: Optional additional metadata about the subject

    Note:
        The subject_type + subject_id tuple forms the ReBAC subject identity.
        tenant_id is kept for metadata/logging but does not define identity.

    Examples:
        # Human user
        AuthResult(True, "user", "alice", "org_acme", False)

        # AI agent instance
        AuthResult(True, "agent", "agent_123", "org_acme", False)

        # Service account
        AuthResult(True, "service", "backup_bot", None, True)

        # Ephemeral session
        AuthResult(True, "session", "session_xyz", "org_acme", False)
    """

    authenticated: bool
    subject_type: str = "user"  # "user", "agent", "service", "session"
    subject_id: str | None = None
    tenant_id: str | None = None
    is_admin: bool = False
    inherit_permissions: bool = True  # v0.5.1: Default True for backward compatibility
    metadata: dict[str, Any] | None = None


class AuthProvider(ABC):
    """Abstract base class for authentication providers.

    Authentication providers validate API keys/tokens and map them to user identities.
    This abstraction allows easy migration from simple API keys to SSO/OIDC.

    Implementations:
    - StaticAPIKeyAuth: Simple config-file based API keys
    - DatabaseAPIKeyAuth: Database-backed API keys with expiry
    - OIDCAuth: Future SSO/OIDC integration for SaaS
    """

    @abstractmethod
    async def authenticate(self, token: str) -> AuthResult:
        """Authenticate a request token.

        Args:
            token: API key or bearer token from Authorization header

        Returns:
            AuthResult with authentication status and user identity
        """
        pass

    @abstractmethod
    async def validate_token(self, token: str) -> bool:
        """Quick validation check without full authentication.

        Useful for health checks or simple validation.

        Args:
            token: API key or bearer token

        Returns:
            True if token is valid
        """
        pass

    @abstractmethod
    def close(self) -> None:
        """Cleanup resources (e.g., database connections).

        Override if your provider needs cleanup.
        """
