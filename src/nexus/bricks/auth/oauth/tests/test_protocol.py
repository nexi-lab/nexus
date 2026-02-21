"""Tests for OAuth brick protocol conformance."""

from nexus.bricks.auth.oauth.protocol import OAuthProviderProtocol, OAuthTokenManagerProtocol
from nexus.bricks.auth.oauth.types import OAuthCredential


class TestOAuthProviderProtocolConformance:
    """runtime_checkable protocol must accept any conforming class."""

    def test_conforming_class_passes_isinstance(self) -> None:
        class FakeProvider:
            client_id: str = "cid"
            client_secret: str = "cs"
            redirect_uri: str = "http://localhost"
            scopes: list[str] = ["openid"]
            provider_name: str = "fake"

            def get_authorization_url(self, _state: str | None = None) -> str:
                return "http://auth"

            async def exchange_code(self, _code: str) -> OAuthCredential:
                return OAuthCredential(access_token="t")

            async def refresh_token(self, credential: OAuthCredential) -> OAuthCredential:
                return credential

            async def revoke_token(self, _credential: OAuthCredential) -> bool:
                return True

            async def validate_token(self, _access_token: str) -> bool:
                return True

        assert isinstance(FakeProvider(), OAuthProviderProtocol)

    def test_non_conforming_class_fails_isinstance(self) -> None:
        class NotAProvider:
            pass

        assert not isinstance(NotAProvider(), OAuthProviderProtocol)

    def test_protocol_is_runtime_checkable(self) -> None:
        assert hasattr(OAuthProviderProtocol, "__protocol_attrs__") or hasattr(
            OAuthProviderProtocol, "_is_runtime_protocol"
        )


class TestOAuthTokenManagerProtocolConformance:
    def test_is_runtime_checkable(self) -> None:
        assert hasattr(OAuthTokenManagerProtocol, "__protocol_attrs__") or hasattr(
            OAuthTokenManagerProtocol, "_is_runtime_protocol"
        )
