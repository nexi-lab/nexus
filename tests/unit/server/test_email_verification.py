"""Tests for email verification (Issue #1434)."""

from __future__ import annotations

import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.server.auth.database_local import DatabaseLocalAuth
from nexus.storage.models import Base


@pytest.fixture()
def test_db():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    yield session_factory
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def auth(test_db) -> DatabaseLocalAuth:
    """Create DatabaseLocalAuth provider for testing."""
    return DatabaseLocalAuth(
        session_factory=test_db,
        jwt_secret="test-secret-key-for-verification",
        token_expiry=3600,
    )


class TestEmailVerificationLogin:
    """Test that login enforces email verification."""

    def test_login_rejected_for_unverified_user(self, auth: DatabaseLocalAuth) -> None:
        """Register a user → login → should raise ValueError (unverified)."""
        auth.register_user(
            email="alice@example.com",
            password="password123",
            username="alice",
        )
        with pytest.raises(ValueError, match="Email not verified"):
            auth.login("alice@example.com", "password123")

    def test_login_succeeds_after_verification(self, auth: DatabaseLocalAuth) -> None:
        """Register → verify email → login → should return token."""
        user = auth.register_user(
            email="bob@example.com",
            password="password123",
            username="bob",
        )
        token = auth.create_email_verification_token(user.user_id, "bob@example.com")
        auth.verify_email(user.user_id, token)

        result = auth.login("bob@example.com", "password123")
        assert result is not None
        assert isinstance(result, str)  # JWT token

    def test_already_verified_user_can_login(self, auth: DatabaseLocalAuth) -> None:
        """User with email_verified=1 logs in normally."""
        user = auth.register_user(
            email="verified@example.com",
            password="password123",
        )
        # Manually set email_verified
        with auth.session_factory() as session, session.begin():
            from nexus.server.auth.database_local import get_user_by_id

            db_user = get_user_by_id(session, user.user_id)
            assert db_user is not None
            db_user.email_verified = 1

        token = auth.login("verified@example.com", "password123")
        assert token is not None


class TestVerificationToken:
    """Test JWT-based email verification tokens."""

    def test_verification_token_roundtrip(self, auth: DatabaseLocalAuth) -> None:
        """Create token → verify → returns correct user_id and email."""
        token = auth.create_email_verification_token("user-123", "test@example.com")
        user_id, email = auth.verify_email_token(token)
        assert user_id == "user-123"
        assert email == "test@example.com"

    def test_expired_verification_token_rejected(self, auth: DatabaseLocalAuth) -> None:
        """Token with past expiry should raise ValueError."""
        import time as time_mod

        from authlib.jose import jwt as jose_jwt

        header = {"alg": "HS256"}
        payload = {
            "sub": "user-123",
            "email": "test@example.com",
            "purpose": "email_verify",
            "iat": int(time_mod.time()) - 100000,
            "exp": int(time_mod.time()) - 1,  # already expired
        }
        expired_token = jose_jwt.encode(header, payload, auth.jwt_secret)
        token_str = expired_token.decode() if isinstance(expired_token, bytes) else expired_token

        with pytest.raises(ValueError, match="Invalid verification token"):
            auth.verify_email_token(token_str)

    def test_verification_token_wrong_purpose_rejected(self, auth: DatabaseLocalAuth) -> None:
        """A regular JWT (not email_verify purpose) should be rejected."""
        from authlib.jose import jwt as jose_jwt

        header = {"alg": "HS256"}
        payload = {
            "sub": "user-123",
            "email": "test@example.com",
            "purpose": "password_reset",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        }
        bad_token = jose_jwt.encode(header, payload, auth.jwt_secret)
        token_str = bad_token.decode() if isinstance(bad_token, bytes) else bad_token

        with pytest.raises(ValueError, match="not an email verification token"):
            auth.verify_email_token(token_str)


class TestVerifyEmailEndpoint:
    """Test the verify_email method end-to-end."""

    def test_verify_email_sets_flag(self, auth: DatabaseLocalAuth) -> None:
        """verify_email() should set email_verified=1 in the database."""
        user = auth.register_user(
            email="charlie@example.com",
            password="password123",
        )
        token = auth.create_email_verification_token(user.user_id, "charlie@example.com")
        result = auth.verify_email(user.user_id, token)
        assert result is True

        # Verify database state
        info = auth.get_user_info(user.user_id)
        assert info is not None
        assert info["email_verified"] is True

    def test_verify_email_wrong_user_id(self, auth: DatabaseLocalAuth) -> None:
        """Token for user A should not verify user B."""
        token = auth.create_email_verification_token("user-a", "a@example.com")
        with pytest.raises(ValueError, match="does not match"):
            auth.verify_email("user-b", token)
