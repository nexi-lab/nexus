"""OAuth service protocol (Issue #1287: Extract domain services).

Defines the contract for OAuth credential management.
Implementation: ``nexus.bricks.auth.oauth.credential_service.OAuthCredentialService``.

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md
    - Issue #1287: Extract NexusFS domain services from god object
"""

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from nexus.contracts.constants import DEFAULT_OAUTH_REDIRECT_URI

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext


@runtime_checkable
class OAuthProtocol(Protocol):
    """Service contract for OAuth credential management.

    Provides:
    - Authorization URL generation (with PKCE support)
    - Code exchange for tokens
    - Provider discovery and listing
    - Credential lifecycle (list, test, revoke)
    """

    async def get_auth_url(
        self,
        provider: str,
        redirect_uri: str = DEFAULT_OAUTH_REDIRECT_URI,
        scopes: list[str] | None = None,
    ) -> dict[str, Any]: ...

    async def exchange_code(
        self,
        provider: str,
        code: str,
        user_email: str | None = None,
        state: str | None = None,
        redirect_uri: str | None = None,
        code_verifier: str | None = None,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]: ...

    async def list_providers(
        self,
        context: "OperationContext | None" = None,
    ) -> list[dict[str, Any]]: ...

    async def list_credentials(
        self,
        provider: str | None = None,
        include_revoked: bool = False,
        context: "OperationContext | None" = None,
    ) -> list[dict[str, Any]]: ...

    async def revoke_credential(
        self,
        provider: str,
        user_email: str,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]: ...

    async def test_credential(
        self,
        provider: str,
        user_email: str,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]: ...
