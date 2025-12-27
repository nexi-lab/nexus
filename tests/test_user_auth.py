"""Test user authentication system.

This script tests both password and OAuth authentication flows.
Run with: python -m pytest tests/test_user_auth.py -v
"""

import os
import sys
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from nexus.server.auth.database_local import DatabaseLocalAuth
from nexus.server.auth.oauth_crypto import OAuthCrypto
from nexus.server.auth.oauth_user_auth import OAuthUserAuth
from nexus.server.auth.user_helpers import (
    check_email_available,
    check_username_available,
    get_user_by_email,
    get_user_by_username,
)
from nexus.storage.models import Base, UserModel, UserOAuthAccountModel

# ==============================================================================
# Test Fixtures
# ==============================================================================


@pytest.fixture
def test_db():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:", echo=False)

    # Drop all tables first to avoid conflicts (in case of re-imports)
    Base.metadata.drop_all(engine)

    # Create all tables
    Base.metadata.create_all(engine)

    SessionFactory = sessionmaker(bind=engine)
    yield SessionFactory

    # Cleanup
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def auth_provider(test_db):
    """Create DatabaseLocalAuth provider for testing."""
    return DatabaseLocalAuth(
        session_factory=test_db,
        jwt_secret="test-secret-key",
        token_expiry=3600,
    )


@pytest.fixture
def oauth_provider(test_db):
    """Create OAuthUserAuth provider for testing."""
    oauth_crypto = OAuthCrypto()
    return OAuthUserAuth(
        session_factory=test_db,
        google_client_id="test-client-id.apps.googleusercontent.com",
        google_client_secret="test-client-secret",
        google_redirect_uri="http://localhost:2026/auth/oauth/callback",
        jwt_secret="test-secret-key",
        oauth_crypto=oauth_crypto,
    )


# ==============================================================================
# Password Authentication Tests
# ==============================================================================


def test_user_registration(auth_provider, test_db):
    """Test user registration with email/password."""
    # Register user
    user = auth_provider.register_user(
        email="alice@example.com",
        password="securepassword123",
        username="alice",
        display_name="Alice Smith",
    )

    assert user.user_id is not None
    assert user.email == "alice@example.com"
    assert user.username == "alice"
    assert user.display_name == "Alice Smith"
    assert user.password_hash is not None
    assert user.primary_auth_method == "password"
    assert user.is_active == 1
    assert user.email_verified == 0  # Not verified yet

    # Verify user in database
    with test_db() as session:
        db_user = get_user_by_email(session, "alice@example.com")
        assert db_user is not None
        assert db_user.user_id == user.user_id


def test_duplicate_email_registration(auth_provider):
    """Test that duplicate email registration fails."""
    # Register first user
    auth_provider.register_user(
        email="bob@example.com",
        password="password123",
        username="bob",
    )

    # Try to register with same email
    with pytest.raises(ValueError, match="Email .* already exists"):
        auth_provider.register_user(
            email="bob@example.com",
            password="different-password",
            username="bob2",
        )


def test_duplicate_username_registration(auth_provider):
    """Test that duplicate username registration fails."""
    # Register first user
    auth_provider.register_user(
        email="charlie@example.com",
        password="password123",
        username="charlie",
    )

    # Try to register with same username
    with pytest.raises(ValueError, match="Username .* already exists"):
        auth_provider.register_user(
            email="charlie2@example.com",
            password="different-password",
            username="charlie",
        )


def test_user_login(auth_provider):
    """Test user login with email/password."""
    # Register user
    auth_provider.register_user(
        email="dave@example.com",
        password="securepassword123",
        username="dave",
    )

    # Login with email
    token = auth_provider.login("dave@example.com", "securepassword123")
    assert token is not None
    assert isinstance(token, str)
    assert len(token) > 0

    # Verify token
    claims = auth_provider.verify_token(token)
    assert claims["email"] == "dave@example.com"
    assert claims["subject_type"] == "user"

    # Login with username
    token2 = auth_provider.login("dave", "securepassword123")
    assert token2 is not None


def test_invalid_password_login(auth_provider):
    """Test that login fails with wrong password."""
    # Register user
    auth_provider.register_user(
        email="eve@example.com",
        password="correct-password",
        username="eve",
    )

    # Try wrong password
    token = auth_provider.login("eve@example.com", "wrong-password")
    assert token is None


def test_nonexistent_user_login(auth_provider):
    """Test that login fails for non-existent user."""
    token = auth_provider.login("nobody@example.com", "any-password")
    assert token is None


def test_change_password(auth_provider):
    """Test password change."""
    # Register user
    user = auth_provider.register_user(
        email="frank@example.com",
        password="oldpassword123",
        username="frank",
    )

    # Change password
    success = auth_provider.change_password(
        user_id=user.user_id,
        old_password="oldpassword123",
        new_password="newpassword456",
    )
    assert success is True

    # Login with new password
    token = auth_provider.login("frank@example.com", "newpassword456")
    assert token is not None

    # Old password should not work
    token = auth_provider.login("frank@example.com", "oldpassword123")
    assert token is None


def test_change_password_wrong_old_password(auth_provider):
    """Test that password change fails with wrong old password."""
    # Register user
    user = auth_provider.register_user(
        email="grace@example.com",
        password="password123",
        username="grace",
    )

    # Try to change with wrong old password
    with pytest.raises(ValueError, match="Incorrect current password"):
        auth_provider.change_password(
            user_id=user.user_id,
            old_password="wrong-password",
            new_password="newpassword456",
        )


def test_update_profile(auth_provider):
    """Test user profile update."""
    # Register user
    user = auth_provider.register_user(
        email="henry@example.com",
        password="password123",
        username="henry",
        display_name="Henry",
    )

    # Update profile
    updated_user = auth_provider.update_profile(
        user_id=user.user_id,
        display_name="Henry Smith",
        avatar_url="https://example.com/avatar.jpg",
    )

    assert updated_user is not None
    assert updated_user.display_name == "Henry Smith"
    assert updated_user.avatar_url == "https://example.com/avatar.jpg"


def test_get_user_info(auth_provider):
    """Test getting user information."""
    # Register user
    user = auth_provider.register_user(
        email="iris@example.com",
        password="password123",
        username="iris",
        display_name="Iris Johnson",
    )

    # Get user info
    user_info = auth_provider.get_user_info(user.user_id)
    assert user_info is not None
    assert user_info["user_id"] == user.user_id
    assert user_info["email"] == "iris@example.com"
    assert user_info["username"] == "iris"
    assert user_info["display_name"] == "Iris Johnson"
    assert user_info["primary_auth_method"] == "password"
    assert user_info["is_global_admin"] is False
    assert user_info["email_verified"] is False


# ==============================================================================
# User Helper Function Tests
# ==============================================================================


def test_check_email_available(auth_provider, test_db):
    """Test email availability checking."""
    # Initially available
    with test_db() as session:
        assert check_email_available(session, "test@example.com") is True

    # Register user
    auth_provider.register_user(
        email="test@example.com",
        password="password123",
    )

    # No longer available
    with test_db() as session:
        assert check_email_available(session, "test@example.com") is False


def test_check_username_available(auth_provider, test_db):
    """Test username availability checking."""
    # Initially available
    with test_db() as session:
        assert check_username_available(session, "testuser") is True

    # Register user
    auth_provider.register_user(
        email="user@example.com",
        password="password123",
        username="testuser",
    )

    # No longer available
    with test_db() as session:
        assert check_username_available(session, "testuser") is False


def test_get_user_by_email(auth_provider, test_db):
    """Test getting user by email."""
    # Register user
    registered_user = auth_provider.register_user(
        email="lookup@example.com",
        password="password123",
    )

    # Lookup by email
    with test_db() as session:
        user = get_user_by_email(session, "lookup@example.com")
        assert user is not None
        assert user.user_id == registered_user.user_id


def test_get_user_by_username(auth_provider, test_db):
    """Test getting user by username."""
    # Register user
    registered_user = auth_provider.register_user(
        email="user@example.com",
        password="password123",
        username="lookupuser",
    )

    # Lookup by username
    with test_db() as session:
        user = get_user_by_username(session, "lookupuser")
        assert user is not None
        assert user.user_id == registered_user.user_id


# ==============================================================================
# OAuth Authentication Tests
# ==============================================================================


def test_get_google_auth_url(oauth_provider):
    """Test getting Google OAuth authorization URL."""
    auth_url, state = oauth_provider.get_google_auth_url()

    assert auth_url is not None
    assert "accounts.google.com" in auth_url
    assert "client_id=test-client-id" in auth_url
    assert "scope=openid" in auth_url
    assert state is not None
    assert len(state) > 0


def test_get_user_oauth_accounts(oauth_provider, test_db):
    """Test getting user's OAuth accounts."""
    # Create a test user with OAuth account
    user_id = str(uuid.uuid4())

    with test_db() as session, session.begin():
        # Create user
        user = UserModel(
            user_id=user_id,
            email="oauth@example.com",
            primary_auth_method="oauth",
            is_active=1,
            email_verified=1,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user)
        session.flush()

        # Create OAuth account
        oauth_account = UserOAuthAccountModel(
            oauth_account_id=str(uuid.uuid4()),
            user_id=user_id,
            provider="google",
            provider_user_id="google-123456",
            provider_email="oauth@example.com",
            created_at=datetime.now(UTC),
            last_used_at=datetime.now(UTC),
        )
        session.add(oauth_account)

    # Get OAuth accounts
    accounts = oauth_provider.get_user_oauth_accounts(user_id)
    assert len(accounts) == 1
    assert accounts[0]["provider"] == "google"
    assert accounts[0]["provider_email"] == "oauth@example.com"


def test_unlink_oauth_account(oauth_provider, test_db):
    """Test unlinking OAuth account from user."""
    # Create a test user with OAuth account
    user_id = str(uuid.uuid4())
    oauth_account_id = str(uuid.uuid4())

    with test_db() as session, session.begin():
        # Create user
        user = UserModel(
            user_id=user_id,
            email="unlink@example.com",
            primary_auth_method="oauth",
            is_active=1,
            email_verified=1,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user)

        # Create OAuth account
        oauth_account = UserOAuthAccountModel(
            oauth_account_id=oauth_account_id,
            user_id=user_id,
            provider="google",
            provider_user_id="google-789",
            provider_email="unlink@example.com",
            created_at=datetime.now(UTC),
        )
        session.add(oauth_account)

    # Unlink account
    success = oauth_provider.unlink_oauth_account(user_id, oauth_account_id)
    assert success is True

    # Verify account is removed
    accounts = oauth_provider.get_user_oauth_accounts(user_id)
    assert len(accounts) == 0


def test_unlink_wrong_user_oauth_account(oauth_provider, test_db):
    """Test that unlinking fails for wrong user."""
    # Create two users with OAuth accounts
    user_id_1 = str(uuid.uuid4())
    user_id_2 = str(uuid.uuid4())
    oauth_account_id = str(uuid.uuid4())

    with test_db() as session, session.begin():
        # Create user 1 with OAuth account
        user1 = UserModel(
            user_id=user_id_1,
            email="user1@example.com",
            primary_auth_method="oauth",
            is_active=1,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user1)

        # Create user 2
        user2 = UserModel(
            user_id=user_id_2,
            email="user2@example.com",
            primary_auth_method="password",
            is_active=1,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user2)

        # Create OAuth account for user 1
        oauth_account = UserOAuthAccountModel(
            oauth_account_id=oauth_account_id,
            user_id=user_id_1,
            provider="google",
            provider_user_id="google-xyz",
            provider_email="user1@example.com",
            created_at=datetime.now(UTC),
        )
        session.add(oauth_account)

    # Try to unlink user1's account as user2
    with pytest.raises(ValueError, match="does not belong to user"):
        oauth_provider.unlink_oauth_account(user_id_2, oauth_account_id)


# ==============================================================================
# Integration Tests
# ==============================================================================


def test_full_password_auth_flow(auth_provider):
    """Test complete password authentication flow."""
    # 1. Register
    user = auth_provider.register_user(
        email="integration@example.com",
        password="password123",
        username="integration",
        display_name="Integration Test User",
    )
    assert user.user_id is not None

    # 2. Login
    token = auth_provider.login("integration@example.com", "password123")
    assert token is not None

    # 3. Verify token
    claims = auth_provider.verify_token(token)
    assert claims["subject_id"] == user.user_id

    # 4. Get user info
    user_info = auth_provider.get_user_info(user.user_id)
    assert user_info["email"] == "integration@example.com"

    # 5. Update profile
    updated_user = auth_provider.update_profile(
        user_id=user.user_id,
        display_name="Updated Name",
    )
    assert updated_user.display_name == "Updated Name"

    # 6. Change password
    auth_provider.change_password(
        user_id=user.user_id,
        old_password="password123",
        new_password="newpassword456",
    )

    # 7. Login with new password
    token2 = auth_provider.login("integration@example.com", "newpassword456")
    assert token2 is not None


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v"])
