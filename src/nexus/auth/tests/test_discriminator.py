"""Unit tests for DiscriminatingAuthProvider token routing (Decision #10)."""

from __future__ import annotations

import base64
import json

import pytest

from nexus.auth.providers.base import AuthProvider, AuthResult
from nexus.auth.providers.discriminator import DiscriminatingAuthProvider

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class FakeAPIKeyProvider(AuthProvider):
    """Fake API key provider for tests."""

    async def authenticate(self, _token: str) -> AuthResult:
        return AuthResult(
            authenticated=True,
            subject_type="user",
            subject_id="api_user",
            metadata={"provider": "api_key"},
        )

    async def validate_token(self, _token: str) -> bool:
        return True

    def close(self) -> None:
        pass


class FakeJWTProvider(AuthProvider):
    """Fake JWT provider for tests."""

    async def authenticate(self, _token: str) -> AuthResult:
        return AuthResult(
            authenticated=True,
            subject_type="user",
            subject_id="jwt_user",
            metadata={"provider": "jwt"},
        )

    async def validate_token(self, _token: str) -> bool:
        return True

    def close(self) -> None:
        pass


class FailingProvider(AuthProvider):
    """Provider that always rejects."""

    async def authenticate(self, _token: str) -> AuthResult:
        return AuthResult(authenticated=False)

    async def validate_token(self, _token: str) -> bool:
        return False

    def close(self) -> None:
        pass


def _make_jwt(alg: str = "RS256") -> str:
    """Create a minimal JWT-like token (header.payload.signature)."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": alg}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "test"}).encode()).rstrip(b"=")
    sig = base64.urlsafe_b64encode(b"fakesig").rstrip(b"=")
    return f"{header.decode()}.{payload.decode()}.{sig.decode()}"


# ---------------------------------------------------------------------------
# Token routing tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sk_prefix_routes_to_api_key_provider():
    """Token with sk- prefix routes to API key provider."""
    provider = DiscriminatingAuthProvider(
        api_key_provider=FakeAPIKeyProvider(),
        jwt_provider=FakeJWTProvider(),
    )
    result = await provider.authenticate("sk-test-key-1234567890123456")
    assert result.authenticated is True
    assert result.metadata is not None
    assert result.metadata["provider"] == "api_key"


@pytest.mark.asyncio
async def test_jwt_routes_to_jwt_provider():
    """JWT-format token routes to JWT provider."""
    provider = DiscriminatingAuthProvider(
        api_key_provider=FakeAPIKeyProvider(),
        jwt_provider=FakeJWTProvider(),
    )
    result = await provider.authenticate(_make_jwt())
    assert result.authenticated is True
    assert result.metadata is not None
    assert result.metadata["provider"] == "jwt"


@pytest.mark.asyncio
async def test_empty_token_rejected():
    """Empty token is rejected immediately."""
    provider = DiscriminatingAuthProvider(
        api_key_provider=FakeAPIKeyProvider(),
        jwt_provider=FakeJWTProvider(),
    )
    result = await provider.authenticate("")
    assert result.authenticated is False


@pytest.mark.asyncio
async def test_sk_prefix_without_api_key_provider():
    """sk- token rejected when no API key provider configured."""
    provider = DiscriminatingAuthProvider(
        api_key_provider=None,
        jwt_provider=FakeJWTProvider(),
    )
    result = await provider.authenticate("sk-test-key-1234567890123456")
    assert result.authenticated is False


@pytest.mark.asyncio
async def test_jwt_without_jwt_provider():
    """JWT token rejected when no JWT provider configured."""
    provider = DiscriminatingAuthProvider(
        api_key_provider=FakeAPIKeyProvider(),
        jwt_provider=None,
    )
    result = await provider.authenticate(_make_jwt())
    assert result.authenticated is False


@pytest.mark.asyncio
async def test_both_providers_none():
    """All tokens rejected when no providers configured."""
    provider = DiscriminatingAuthProvider()
    assert (await provider.authenticate("sk-test")).authenticated is False
    assert (await provider.authenticate(_make_jwt())).authenticated is False
    assert (await provider.authenticate("")).authenticated is False


@pytest.mark.asyncio
async def test_non_jwt_non_sk_rejected():
    """Token that is neither sk- nor JWT is rejected."""
    provider = DiscriminatingAuthProvider(
        api_key_provider=FakeAPIKeyProvider(),
        jwt_provider=FakeJWTProvider(),
    )
    result = await provider.authenticate("random-opaque-token")
    assert result.authenticated is False


@pytest.mark.asyncio
async def test_validate_token_delegates():
    """validate_token delegates to authenticate."""
    provider = DiscriminatingAuthProvider(
        api_key_provider=FakeAPIKeyProvider(),
    )
    assert await provider.validate_token("sk-test-key-1234567890123456") is True
    assert await provider.validate_token(_make_jwt()) is False


# ---------------------------------------------------------------------------
# _looks_like_jwt edge cases
# ---------------------------------------------------------------------------


def test_looks_like_jwt_valid():
    """Valid JWT header is detected."""
    assert DiscriminatingAuthProvider._looks_like_jwt(_make_jwt()) is True


def test_looks_like_jwt_two_parts():
    """Two-part token is NOT a JWT."""
    assert DiscriminatingAuthProvider._looks_like_jwt("part1.part2") is False


def test_looks_like_jwt_no_alg():
    """Three-part token without alg header is NOT a JWT."""
    header = base64.urlsafe_b64encode(json.dumps({"typ": "JWT"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(b"{}").rstrip(b"=")
    sig = base64.urlsafe_b64encode(b"sig").rstrip(b"=")
    token = f"{header.decode()}.{payload.decode()}.{sig.decode()}"
    assert DiscriminatingAuthProvider._looks_like_jwt(token) is False


def test_looks_like_jwt_invalid_base64():
    """Three-part token with invalid base64 is NOT a JWT."""
    assert DiscriminatingAuthProvider._looks_like_jwt("!!!.@@@.###") is False


def test_looks_like_jwt_empty_parts():
    """Three empty parts is NOT a JWT."""
    assert DiscriminatingAuthProvider._looks_like_jwt("..") is False


# ---------------------------------------------------------------------------
# session_factory property forwarding
# ---------------------------------------------------------------------------


def test_session_factory_forwarding():
    """session_factory is forwarded from API key provider."""

    class ProviderWithFactory(FakeAPIKeyProvider):
        session_factory = "mock_factory"

    provider = DiscriminatingAuthProvider(
        api_key_provider=ProviderWithFactory(),
    )
    assert provider.session_factory == "mock_factory"


def test_session_factory_none_without_api_provider():
    """session_factory is None when no API key provider."""
    provider = DiscriminatingAuthProvider(jwt_provider=FakeJWTProvider())
    assert provider.session_factory is None


# ---------------------------------------------------------------------------
# close() propagation
# ---------------------------------------------------------------------------


def test_close_propagates():
    """close() calls close on both sub-providers."""
    api_prov = FakeAPIKeyProvider()
    jwt_prov = FakeJWTProvider()
    provider = DiscriminatingAuthProvider(
        api_key_provider=api_prov,
        jwt_provider=jwt_prov,
    )
    # Should not raise
    provider.close()
