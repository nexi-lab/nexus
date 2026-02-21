"""Tests for OAuth brick types (frozen credential, masked repr, serialization)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from nexus.bricks.auth.oauth.types import OAuthCredential, OAuthError, PendingOAuthRegistration


class TestOAuthCredentialFrozen:
    """OAuthCredential is frozen — mutation must raise."""

    def test_frozen_access_token(self) -> None:
        cred = OAuthCredential(access_token="ya29.test")
        with pytest.raises(dataclasses.FrozenInstanceError):
            cred.access_token = "new"  # type: ignore[misc]

    def test_frozen_refresh_token(self) -> None:
        cred = OAuthCredential(access_token="ya29.test", refresh_token="rt")
        with pytest.raises(dataclasses.FrozenInstanceError):
            cred.refresh_token = "new"  # type: ignore[misc]

    def test_frozen_provider(self) -> None:
        cred = OAuthCredential(access_token="ya29.test", provider="google")
        with pytest.raises(dataclasses.FrozenInstanceError):
            cred.provider = "microsoft"  # type: ignore[misc]

    def test_replace_creates_new(self) -> None:
        cred = OAuthCredential(access_token="ya29.test", provider="google")
        new_cred = dataclasses.replace(cred, provider="microsoft")
        assert new_cred.provider == "microsoft"
        assert cred.provider == "google"  # original unchanged

    def test_scopes_are_tuple(self) -> None:
        cred = OAuthCredential(
            access_token="ya29.test",
            scopes=("scope1", "scope2"),
        )
        assert isinstance(cred.scopes, tuple)


class TestOAuthCredentialExpiry:
    def test_not_expired_when_no_expires_at(self) -> None:
        cred = OAuthCredential(access_token="ya29.test")
        assert cred.is_expired() is False

    def test_not_expired_when_future(self) -> None:
        cred = OAuthCredential(
            access_token="ya29.test",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        assert cred.is_expired() is False

    def test_expired_when_past(self) -> None:
        cred = OAuthCredential(
            access_token="ya29.test",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        assert cred.is_expired() is True

    def test_needs_refresh_true(self) -> None:
        cred = OAuthCredential(
            access_token="ya29.test",
            refresh_token="rt",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        assert cred.needs_refresh() is True

    def test_needs_refresh_false_no_refresh_token(self) -> None:
        cred = OAuthCredential(
            access_token="ya29.test",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        assert cred.needs_refresh() is False


class TestOAuthCredentialRepr:
    def test_masks_access_token(self) -> None:
        cred = OAuthCredential(access_token="ya29.a0ARrdaMxyz123")
        r = repr(cred)
        assert "ya29.a0ARrdaMxyz123" not in r
        assert "ya29" in r  # first 4 chars shown
        assert "..." in r

    def test_masks_refresh_token(self) -> None:
        cred = OAuthCredential(access_token="ya29.test12345", refresh_token="1//0e_long_refresh")
        r = repr(cred)
        assert "1//0e_long_refresh" not in r
        assert "1//0" in r  # first 4 chars shown

    def test_short_token_fully_masked(self) -> None:
        cred = OAuthCredential(access_token="short")
        r = repr(cred)
        assert "***" in r


class TestOAuthCredentialSerialization:
    def test_to_dict_roundtrip(self) -> None:
        cred = OAuthCredential(
            access_token="ya29.test",
            refresh_token="rt",
            token_type="Bearer",
            expires_at=datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC),
            scopes=("drive", "gmail"),
            provider="google",
            user_email="alice@example.com",
            client_id="cid",
            token_uri="https://oauth2.googleapis.com/token",
            metadata={"key": "value"},
        )
        d = cred.to_dict()
        restored = OAuthCredential.from_dict(d)
        assert restored.access_token == cred.access_token
        assert restored.refresh_token == cred.refresh_token
        assert restored.scopes == cred.scopes
        assert restored.provider == cred.provider
        assert restored.expires_at == cred.expires_at

    def test_from_dict_scopes_converted_to_tuple(self) -> None:
        d = {"access_token": "ya29.test", "scopes": ["a", "b"]}
        cred = OAuthCredential.from_dict(d)
        assert isinstance(cred.scopes, tuple)
        assert cred.scopes == ("a", "b")

    def test_to_dict_scopes_converted_to_list(self) -> None:
        cred = OAuthCredential(access_token="ya29.test", scopes=("a", "b"))
        d = cred.to_dict()
        assert isinstance(d["scopes"], list)


class TestOAuthError:
    def test_is_exception(self) -> None:
        err = OAuthError("test error")
        assert isinstance(err, Exception)
        assert str(err) == "test error"


class TestPendingOAuthRegistration:
    def test_frozen(self) -> None:
        reg = PendingOAuthRegistration(
            provider="google",
            provider_user_id="123",
            provider_email="a@b.com",
            email_verified=True,
            name="Alice",
            picture=None,
            oauth_credential=None,
            expires_at=0.0,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            reg.provider = "x"  # type: ignore[misc]

    def test_to_dict(self) -> None:
        reg = PendingOAuthRegistration(
            provider="google",
            provider_user_id="123",
            provider_email="a@b.com",
            email_verified=True,
            name="Alice",
            picture="http://pic",
            oauth_credential=None,
            expires_at=0.0,
        )
        d = reg.to_dict()
        assert d["provider"] == "google"
        assert d["provider_user_id"] == "123"
        assert "oauth_credential" not in d
