"""Test OAuth check and confirm flow improvements.

This tests the new OAuth check/confirm flow that handles both existing
and new users without double OAuth code exchange.

Run with: python -m pytest tests/unit/server/test_oauth_check_confirm.py -v
"""

import os
import sys
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from nexus.server.auth.database_local import DatabaseLocalAuth
from nexus.server.auth.oauth_crypto import OAuthCrypto
from nexus.storage.models import Base, UserModel, UserOAuthAccountModel


# ==============================================================================
# Test Fixtures
# ==============================================================================


@pytest.fixture
def test_db():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:", echo=False)

    # Drop all tables first to avoid conflicts
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
def oauth_crypto():
    """Create OAuthCrypto for testing."""
    return OAuthCrypto(encryption_key="test-encryption-key-32-bytes!!")


# ==============================================================================
# Test get_user_info_for_jwt
# ==============================================================================


def test_get_user_info_for_jwt_success(auth_provider, test_db):
    """Test getting user info formatted for JWT token creation."""
    # Create a test user
    user_id = str(uuid.uuid4())
    tenant_id = "test-tenant"

    with test_db() as session:
        user = UserModel(
            user_id=user_id,
            email="test@example.com",
            username="testuser",
            display_name="Test User",
            password_hash="fake-hash",
            primary_auth_method="password",
            is_global_admin=0,
            email_verified=1,
            tenant_id=tenant_id,
            api_key="test-api-key",
            created_at=datetime.now(UTC),
        )
        session.add(user)
        session.commit()

    # Get user info for JWT
    user_info = auth_provider.get_user_info_for_jwt(user_id)

    # Verify
    assert user_info is not None
    assert user_info["subject_type"] == "user"
    assert user_info["subject_id"] == user_id
    assert user_info["tenant_id"] == tenant_id
    assert user_info["is_admin"] is False
    assert user_info["name"] == "Test User"
    assert user_info["api_key"] == "test-api-key"


def test_get_user_info_for_jwt_no_display_name(auth_provider, test_db):
    """Test getting user info when display_name is None."""
    # Create a test user without display_name
    user_id = str(uuid.uuid4())
    tenant_id = "test-tenant"

    with test_db() as session:
        user = UserModel(
            user_id=user_id,
            email="test@example.com",
            username="testuser",
            display_name=None,
            password_hash="fake-hash",
            primary_auth_method="password",
            is_global_admin=0,
            email_verified=1,
            tenant_id=tenant_id,
            api_key="test-api-key",
            created_at=datetime.now(UTC),
        )
        session.add(user)
        session.commit()

    # Get user info for JWT
    user_info = auth_provider.get_user_info_for_jwt(user_id)

    # Verify name falls back to username
    assert user_info is not None
    assert user_info["name"] == "testuser"


def test_get_user_info_for_jwt_no_username(auth_provider, test_db):
    """Test getting user info when username is also None."""
    # Create a test user without display_name or username
    user_id = str(uuid.uuid4())
    tenant_id = "test-tenant"

    with test_db() as session:
        user = UserModel(
            user_id=user_id,
            email="test@example.com",
            username=None,
            display_name=None,
            password_hash="fake-hash",
            primary_auth_method="password",
            is_global_admin=0,
            email_verified=1,
            tenant_id=tenant_id,
            api_key="test-api-key",
            created_at=datetime.now(UTC),
        )
        session.add(user)
        session.commit()

    # Get user info for JWT
    user_info = auth_provider.get_user_info_for_jwt(user_id)

    # Verify name falls back to email
    assert user_info is not None
    assert user_info["name"] == "test@example.com"


def test_get_user_info_for_jwt_admin_user(auth_provider, test_db):
    """Test getting user info for an admin user."""
    # Create an admin user
    user_id = str(uuid.uuid4())
    tenant_id = "test-tenant"

    with test_db() as session:
        user = UserModel(
            user_id=user_id,
            email="admin@example.com",
            username="admin",
            display_name="Admin User",
            password_hash="fake-hash",
            primary_auth_method="password",
            is_global_admin=1,  # Admin
            email_verified=1,
            tenant_id=tenant_id,
            api_key="admin-api-key",
            created_at=datetime.now(UTC),
        )
        session.add(user)
        session.commit()

    # Get user info for JWT
    user_info = auth_provider.get_user_info_for_jwt(user_id)

    # Verify is_admin is True
    assert user_info is not None
    assert user_info["is_admin"] is True


def test_get_user_info_for_jwt_nonexistent_user(auth_provider):
    """Test getting user info for a non-existent user."""
    fake_user_id = str(uuid.uuid4())

    # Get user info for non-existent user
    user_info = auth_provider.get_user_info_for_jwt(fake_user_id)

    # Verify returns None
    assert user_info is None


# ==============================================================================
# Test OAuth existing user flow
# ==============================================================================


def test_oauth_existing_user_no_double_exchange(auth_provider, test_db):
    """Test that existing OAuth users don't trigger double code exchange."""
    # Create an existing OAuth user
    user_id = str(uuid.uuid4())
    tenant_id = "test-tenant"
    provider_user_id = "google-12345"

    with test_db() as session:
        # Create user
        user = UserModel(
            user_id=user_id,
            email="existing@gmail.com",
            username="existing",
            display_name="Existing User",
            password_hash=None,
            primary_auth_method="oauth",
            is_global_admin=0,
            email_verified=1,
            tenant_id=tenant_id,
            api_key="existing-api-key",
            created_at=datetime.now(UTC),
        )
        session.add(user)

        # Create OAuth account link
        oauth_account = UserOAuthAccountModel(
            user_id=user_id,
            provider="google",
            provider_user_id=provider_user_id,
            provider_email="existing@gmail.com",
            encrypted_id_token="encrypted-id-token",
            token_expires_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )
        session.add(oauth_account)
        session.commit()

    # Get user info for JWT (simulating OAuth check endpoint)
    user_info_dict = auth_provider.get_user_info_for_jwt(user_id)

    # Verify user info is returned without querying UserModel directly
    assert user_info_dict is not None
    assert user_info_dict["subject_id"] == user_id
    assert user_info_dict["tenant_id"] == tenant_id
    assert user_info_dict["api_key"] == "existing-api-key"

    # Verify we can create a token with this info
    token = auth_provider.create_token("existing@gmail.com", user_info_dict)
    assert token is not None
    assert isinstance(token, str)


# ==============================================================================
# Test OAuth user info consistency
# ==============================================================================


def test_get_user_info_and_jwt_consistency(auth_provider, test_db):
    """Test that get_user_info and get_user_info_for_jwt return consistent data."""
    # Create a test user
    user_id = str(uuid.uuid4())
    tenant_id = "test-tenant"

    with test_db() as session:
        user = UserModel(
            user_id=user_id,
            email="test@example.com",
            username="testuser",
            display_name="Test User",
            password_hash="fake-hash",
            primary_auth_method="password",
            is_global_admin=0,
            email_verified=1,
            tenant_id=tenant_id,
            api_key="test-api-key",
            created_at=datetime.now(UTC),
        )
        session.add(user)
        session.commit()

    # Get both versions
    user_info = auth_provider.get_user_info(user_id)
    user_info_jwt = auth_provider.get_user_info_for_jwt(user_id)

    # Verify consistency
    assert user_info is not None
    assert user_info_jwt is not None

    # Check that JWT version maps correctly to user info version
    assert user_info_jwt["subject_id"] == user_info["user_id"]
    assert user_info_jwt["tenant_id"] == user_info["tenant_id"]
    assert user_info_jwt["is_admin"] == user_info["is_global_admin"]
    assert user_info_jwt["api_key"] == user_info["api_key"]


# ==============================================================================
# Run tests
# ==============================================================================


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
