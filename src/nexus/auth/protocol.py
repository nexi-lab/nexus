"""Auth brick protocol — defines the public contract (Decision #15).

All consumers depend on this protocol, never on concrete implementations.
"""

from typing import Protocol, runtime_checkable

from nexus.auth.types import AuthResult

@runtime_checkable
class AuthBrickProtocol(Protocol):
    """Public contract for the Auth brick.

    Implementations must provide authentication, validation,
    cache management, and lifecycle hooks.
    """

    async def authenticate(self, token: str) -> AuthResult:
        """Authenticate a request token.

        Args:
            token: API key or bearer token from Authorization header.

        Returns:
            AuthResult with authentication status and subject identity.
        """
        ...

    async def validate_token(self, token: str) -> bool:
        """Quick validation check without full authentication.

        Args:
            token: API key or bearer token.

        Returns:
            True if the token is valid.
        """
        ...

    def invalidate_cached_token(self, token: str) -> None:
        """Remove a token from the auth cache (Decision #15).

        Called on key revocation to ensure immediate invalidation.

        Args:
            token: The raw token to invalidate.
        """
        ...

    def initialize(self) -> None:
        """Brick lifecycle: startup initialization."""
        ...

    def shutdown(self) -> None:
        """Brick lifecycle: graceful shutdown and resource cleanup."""
        ...

    def verify_imports(self) -> dict[str, bool]:
        """Validate required and optional module imports.

        Returns:
            Dict mapping module name to import success status.
        """
        ...
