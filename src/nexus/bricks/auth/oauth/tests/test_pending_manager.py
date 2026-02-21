"""Tests for PendingOAuthManager with TTLCache."""

import time

from nexus.bricks.auth.oauth.pending import PendingOAuthManager


class TestPendingOAuthManager:
    def test_create_returns_token(self) -> None:
        mgr = PendingOAuthManager(ttl_seconds=600, maxsize=1000)
        token = mgr.create(
            provider="google",
            provider_user_id="123",
            provider_email="a@b.com",
            email_verified=True,
            name="Alice",
            picture=None,
            oauth_credential=None,
        )
        assert isinstance(token, str)
        assert len(token) > 20

    def test_get_returns_registration(self) -> None:
        mgr = PendingOAuthManager(ttl_seconds=600, maxsize=1000)
        token = mgr.create(
            provider="google",
            provider_user_id="123",
            provider_email="a@b.com",
            email_verified=True,
            name="Alice",
            picture=None,
            oauth_credential=None,
        )
        reg = mgr.get(token)
        assert reg is not None
        assert reg.provider == "google"
        assert reg.provider_email == "a@b.com"

    def test_get_nonexistent_returns_none(self) -> None:
        mgr = PendingOAuthManager(ttl_seconds=600, maxsize=1000)
        assert mgr.get("nonexistent") is None

    def test_consume_removes_entry(self) -> None:
        mgr = PendingOAuthManager(ttl_seconds=600, maxsize=1000)
        token = mgr.create(
            provider="google",
            provider_user_id="123",
            provider_email="a@b.com",
            email_verified=True,
            name="Alice",
            picture=None,
            oauth_credential=None,
        )
        reg = mgr.consume(token)
        assert reg is not None
        assert reg.provider == "google"
        # Second consume returns None (one-time use)
        assert mgr.consume(token) is None

    def test_expired_entry_not_returned(self) -> None:
        mgr = PendingOAuthManager(ttl_seconds=0, maxsize=1000)  # instant expiry
        token = mgr.create(
            provider="google",
            provider_user_id="123",
            provider_email="a@b.com",
            email_verified=True,
            name="Alice",
            picture=None,
            oauth_credential=None,
        )
        time.sleep(0.1)
        assert mgr.get(token) is None

    def test_maxsize_eviction(self) -> None:
        mgr = PendingOAuthManager(ttl_seconds=600, maxsize=2)
        t1 = mgr.create(
            provider="google",
            provider_user_id="1",
            provider_email="a@b.com",
            email_verified=True,
            name=None,
            picture=None,
            oauth_credential=None,
        )
        t2 = mgr.create(
            provider="google",
            provider_user_id="2",
            provider_email="b@b.com",
            email_verified=True,
            name=None,
            picture=None,
            oauth_credential=None,
        )
        t3 = mgr.create(
            provider="google",
            provider_user_id="3",
            provider_email="c@b.com",
            email_verified=True,
            name=None,
            picture=None,
            oauth_credential=None,
        )
        # With maxsize=2, first entry should have been evicted
        assert mgr.get(t1) is None
        assert mgr.get(t2) is not None or mgr.get(t3) is not None
