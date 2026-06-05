"""Tests for OAuth duplicate-email fix (Issue #3062).

Covers:
- OAuth signup blocked when unverified local account exists (1A)
- OAuth linking succeeds for verified local account
- OAuth signup succeeds when no local account exists
- IntegrityError retry helper returns correct result
"""

import asyncio
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from nexus.storage.models._base import Base
from nexus.storage.models.auth import UserModel, UserOAuthAccountModel


@pytest.fixture()
def db_session():
    """In-memory SQLite session with auth tables."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


def _make_user(
    *,
    email: str = "alice@example.com",
    email_verified: int = 0,
    is_active: int = 1,
) -> UserModel:
    return UserModel(
        user_id=str(uuid.uuid4()),
        email=email,
        username=None,
        display_name="Alice",
        password_hash="hashed",
        primary_auth_method="password",
        is_global_admin=0,
        is_active=is_active,
        email_verified=email_verified,
        created_at=datetime.now(UTC).replace(tzinfo=None),
        updated_at=datetime.now(UTC).replace(tzinfo=None),
    )


def _mock_credential() -> MagicMock:
    return MagicMock(id_token=None, metadata=None, expires_at=None)


class TestOAuthUnverifiedEmailBlocked:
    """Issue #3062: OAuth signup must be blocked when an unverified local account exists."""

    def test_unverified_local_account_blocks_oauth(self, db_session: Session) -> None:
        """When provider email matches an unverified local user, signup must raise."""
        from nexus.bricks.auth.oauth.user_auth import OAuthUserAuth

        user = _make_user(email="victim@example.com", email_verified=0)
        db_session.add(user)
        db_session.commit()

        factory = sessionmaker(bind=db_session.bind)
        auth = OAuthUserAuth(factory, {"google": MagicMock()})

        with pytest.raises(ValueError, match="not verified"):
            asyncio.run(
                auth._get_or_create_oauth_user(
                    session=factory(),
                    provider="google",
                    provider_user_id="google-123",
                    provider_email="victim@example.com",
                    email_verified=True,
                    name="Victim",
                    picture=None,
                    oauth_credential=_mock_credential(),
                )
            )

    def test_verified_local_account_links_oauth(self, db_session: Session) -> None:
        """When provider email matches a verified local user, OAuth should link."""
        from nexus.bricks.auth.oauth.user_auth import OAuthUserAuth

        user = _make_user(email="verified@example.com", email_verified=1)
        db_session.add(user)
        db_session.commit()

        factory = sessionmaker(bind=db_session.bind)
        auth = OAuthUserAuth(factory, {"google": MagicMock()})

        result_user, is_new = asyncio.run(
            auth._get_or_create_oauth_user(
                session=factory(),
                provider="google",
                provider_user_id="google-456",
                provider_email="verified@example.com",
                email_verified=True,
                name="Verified",
                picture=None,
                oauth_credential=_mock_credential(),
            )
        )
        assert result_user.user_id == user.user_id
        assert is_new is False

    def test_no_local_account_creates_new_user(self, db_session: Session) -> None:
        """When no local account matches, OAuth should create a new user."""
        from nexus.bricks.auth.oauth.user_auth import OAuthUserAuth

        factory = sessionmaker(bind=db_session.bind)
        auth = OAuthUserAuth(factory, {"google": MagicMock()})
        auth._user_provisioner = None

        result_user, is_new = asyncio.run(
            auth._get_or_create_oauth_user(
                session=factory(),
                provider="google",
                provider_user_id="google-789",
                provider_email="newuser@example.com",
                email_verified=True,
                name="New User",
                picture=None,
                oauth_credential=_mock_credential(),
            )
        )
        assert is_new is True
        assert result_user.email == "newuser@example.com"
        assert result_user.email_verified == 1

    def test_existing_oauth_returns_user(self, db_session: Session) -> None:
        """When the OAuth account already exists, return the existing user."""
        from nexus.bricks.auth.oauth.user_auth import OAuthUserAuth

        user = _make_user(email="existing@example.com", email_verified=1)
        db_session.add(user)
        db_session.flush()

        oauth = UserOAuthAccountModel(
            oauth_account_id=str(uuid.uuid4()),
            user_id=user.user_id,
            provider="google",
            provider_user_id="google-existing",
            provider_email="existing@example.com",
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        db_session.add(oauth)
        db_session.commit()

        factory = sessionmaker(bind=db_session.bind)
        auth = OAuthUserAuth(factory, {"google": MagicMock()})

        result_user, is_new = asyncio.run(
            auth._get_or_create_oauth_user(
                session=factory(),
                provider="google",
                provider_user_id="google-existing",
                provider_email="existing@example.com",
                email_verified=True,
                name="Existing",
                picture=None,
                oauth_credential=_mock_credential(),
            )
        )
        assert result_user.user_id == user.user_id
        assert is_new is False


class TestRetryOAuthRaceHelper:
    """Test the extracted _retry_oauth_race helper."""

    def test_retry_returns_user_on_existing_oauth(self, db_session: Session) -> None:
        """When a competing transaction created the OAuth row, retry finds it."""
        from nexus.bricks.auth.oauth.user_auth import OAuthUserAuth

        user = _make_user(email="race@example.com", email_verified=1)
        db_session.add(user)
        db_session.flush()

        oauth = UserOAuthAccountModel(
            oauth_account_id=str(uuid.uuid4()),
            user_id=user.user_id,
            provider="google",
            provider_user_id="google-race",
            provider_email="race@example.com",
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        db_session.add(oauth)
        db_session.commit()

        factory = sessionmaker(bind=db_session.bind)
        new_session = factory()

        stmt = select(UserOAuthAccountModel).where(
            UserOAuthAccountModel.provider == "google",
            UserOAuthAccountModel.provider_user_id == "google-race",
        )

        result_user, is_new = OAuthUserAuth._retry_oauth_race(new_session, stmt)
        assert result_user.user_id == user.user_id
        assert is_new is False
