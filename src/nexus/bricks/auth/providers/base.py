"""Abstract base class for authentication providers.

Re-exports AuthResult from nexus.bricks.auth.types for backward compatibility.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from nexus.bricks.auth.types import AuthResult

# Re-export so existing code can import from here
__all__ = ["AuthProvider", "AuthResult"]


class AuthProvider(ABC):
    """Abstract base class for authentication providers.

    Authentication providers validate API keys/tokens and map them to
    user identities. This abstraction allows easy migration from simple
    API keys to SSO/OIDC.

    Implementations:
    - StaticAPIKeyAuth: Simple config-file based API keys
    - DatabaseAPIKeyAuth: Database-backed API keys with expiry
    - OIDCAuth: SSO/OIDC integration for SaaS
    """

    @abstractmethod
    async def authenticate(self, token: str) -> AuthResult:
        """Authenticate a request token.

        Args:
            token: API key or bearer token from Authorization header.

        Returns:
            AuthResult with authentication status and user identity.
        """

    @abstractmethod
    async def validate_token(self, token: str) -> bool:
        """Quick validation check without full authentication.

        Args:
            token: API key or bearer token.

        Returns:
            True if token is valid.
        """

    @abstractmethod
    def close(self) -> None:
        """Cleanup resources (e.g., database connections)."""
