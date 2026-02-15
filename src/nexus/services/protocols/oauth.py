"""OAuth service protocol (Issue #1287: Extract domain services).

Defines the contract for OAuth credential management.
Existing implementation: ``nexus.core.nexus_fs_oauth.NexusFSOAuthMixin``.

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md
    - Issue #1287: Extract NexusFS domain services from god object
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext


@runtime_checkable
class OAuthProtocol(Protocol):
    """Service contract for OAuth credential management.

    Provides:
    - Authorization URL generation (with PKCE support)
    - Code exchange for tokens
    - Provider discovery and listing
    - Credential lifecycle (list, test, revoke)
    - MCP provider connection via Klavis
    """

    async def oauth_get_auth_url(
        self,
        provider: str,
        redirect_uri: str = "http://localhost:3000/oauth/callback",
        scopes: list[str] | None = None,
    ) -> dict[str, Any]: ...

    async def oauth_exchange_code(
        self,
        provider: str,
        code: str,
        user_email: str | None = None,
        state: str | None = None,
        redirect_uri: str | None = None,
        code_verifier: str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]: ...

    async def oauth_list_providers(
        self,
        context: OperationContext | None = None,
    ) -> list[dict[str, Any]]: ...

    async def oauth_list_credentials(
        self,
        provider: str | None = None,
        include_revoked: bool = False,
        context: OperationContext | None = None,
    ) -> list[dict[str, Any]]: ...

    async def oauth_revoke_credential(
        self,
        provider: str,
        user_email: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]: ...

    async def oauth_test_credential(
        self,
        provider: str,
        user_email: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]: ...

    async def mcp_connect(
        self,
        provider: str,
        redirect_url: str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]: ...
