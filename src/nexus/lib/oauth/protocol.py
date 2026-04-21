"""OAuth protocols (canonical location).

Structural-subtyping (``typing.Protocol``) interfaces for OAuth providers
and token managers.  ``@runtime_checkable`` enables ``isinstance()`` checks.
"""

from typing import Any, Protocol, runtime_checkable

from nexus.lib.oauth.types import OAuthCredential


@runtime_checkable
class OAuthProviderProtocol(Protocol):
    """Duck-typed interface for OAuth providers.

    Any class that implements these methods satisfies the protocol
    without needing to inherit from it.
    """

    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: list[str]
    provider_name: str

    def get_authorization_url(self, state: str | None = None, **kwargs: Any) -> str: ...

    async def exchange_code(self, code: str, **kwargs: Any) -> OAuthCredential: ...

    async def refresh_token(self, credential: OAuthCredential) -> OAuthCredential: ...

    async def revoke_token(self, credential: OAuthCredential) -> bool: ...

    async def validate_token(self, access_token: str) -> bool: ...


@runtime_checkable
class OAuthTokenManagerProtocol(Protocol):
    """Duck-typed interface for OAuth token lifecycle management."""

    async def store_credential(
        self,
        user_id: str,
        provider_name: str,
        credential: OAuthCredential,
        *,
        zone_id: str | None = None,
    ) -> str: ...

    async def get_credential(
        self,
        user_id: str,
        provider_name: str,
        *,
        zone_id: str | None = None,
    ) -> OAuthCredential | None: ...

    async def refresh_if_needed(
        self,
        user_id: str,
        provider_name: str,
        provider: OAuthProviderProtocol,
        *,
        zone_id: str | None = None,
    ) -> OAuthCredential | None: ...

    async def revoke_credential(
        self,
        user_id: str,
        provider_name: str,
        provider: OAuthProviderProtocol,
        *,
        zone_id: str | None = None,
    ) -> bool: ...

    async def list_credentials(
        self,
        user_id: str,
        *,
        zone_id: str | None = None,
    ) -> list[dict[str, Any]]: ...
