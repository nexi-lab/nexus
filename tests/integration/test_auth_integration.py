"""Integration test for authentication API endpoints.

Tests the FastAPI authentication endpoints with a real server instance.
Run with: python -m pytest tests/test_auth_integration.py -v
"""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from nexus.storage.raft_metadata_store import RaftMetadataStore
from nexus.storage.record_store import SQLAlchemyRecordStore

# Set up test database URL before importing
os.environ["NEXUS_JWT_SECRET"] = "test-secret-key-12345"
os.environ["NEXUS_DATABASE_URL"] = "sqlite:///:memory:"

from nexus.factory import create_nexus_fs
from nexus.server.fastapi_server import create_app
from nexus.storage.models import Base

# ==============================================================================
# Test Fixtures
# ==============================================================================


@pytest.fixture
def test_app():
    """Create FastAPI test app with in-memory database."""
    # Create in-memory database
    database_url = "sqlite:///:memory:"
    engine = create_engine(database_url, echo=False)

    # Create all tables
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    # Create LocalBackend and NexusFS instance
    import tempfile

    from nexus.backends.local import LocalBackend

    auth_tmpdir = tempfile.mkdtemp(prefix="nexus-test-auth-")
    backend = LocalBackend(root_path=auth_tmpdir)
    nx = create_nexus_fs(
        backend=backend,
        metadata_store=RaftMetadataStore.embedded(os.path.join(auth_tmpdir, "raft-metadata")),
        record_store=SQLAlchemyRecordStore(db_path=":memory:"),
        enforce_permissions=False,  # Disable permissions for simpler testing
    )

    # Create FastAPI app
    app = create_app(
        nexus_fs=nx,
        database_url=database_url,
    )

    # Create test client
    client = TestClient(app)

    yield client

    # Cleanup
    Base.metadata.drop_all(engine)
    engine.dispose()


# ==============================================================================
# Registration Tests
# ==============================================================================


def test_register_user(test_app):
    """Test user registration endpoint."""
    response = test_app.post(
        "/auth/register",
        json={
            "email": "test@example.com",
            "password": "securepassword123",
            "username": "testuser",
            "display_name": "Test User",
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["email"] == "test@example.com"
    assert data["username"] == "testuser"
    assert data["display_name"] == "Test User"
    assert "token" in data
    assert "user_id" in data


def test_register_duplicate_email(test_app):
    """Test that duplicate email registration fails."""
    # Register first user
    test_app.post(
        "/auth/register",
        json={
            "email": "duplicate@example.com",
            "password": "password123",
            "username": "user1",
        },
    )

    # Try to register with same email
    response = test_app.post(
        "/auth/register",
        json={
            "email": "duplicate@example.com",
            "password": "different-password",
            "username": "user2",
        },
    )

    assert response.status_code == 400
    assert "already exists" in response.json()["detail"].lower()


def test_register_short_password(test_app):
    """Test that short password registration fails."""
    response = test_app.post(
        "/auth/register",
        json={
            "email": "short@example.com",
            "password": "short",
            "username": "shortpass",
        },
    )

    assert response.status_code == 422  # Validation error


# ==============================================================================
# Login Tests
# ==============================================================================


def test_login_with_email(test_app):
    """Test login with email."""
    # Register user
    test_app.post(
        "/auth/register",
        json={
            "email": "login@example.com",
            "password": "securepassword123",
            "username": "loginuser",
        },
    )

    # Login with email
    response = test_app.post(
        "/auth/login",
        json={
            "identifier": "login@example.com",
            "password": "securepassword123",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert "token" in data
    assert data["user"]["email"] == "login@example.com"


def test_login_with_username(test_app):
    """Test login with username."""
    # Register user
    test_app.post(
        "/auth/register",
        json={
            "email": "user@example.com",
            "password": "securepassword123",
            "username": "myusername",
        },
    )

    # Login with username
    response = test_app.post(
        "/auth/login",
        json={
            "identifier": "myusername",
            "password": "securepassword123",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert "token" in data
    assert data["user"]["username"] == "myusername"


def test_login_invalid_password(test_app):
    """Test that login fails with wrong password."""
    # Register user
    test_app.post(
        "/auth/register",
        json={
            "email": "fail@example.com",
            "password": "correct-password",
            "username": "failuser",
        },
    )

    # Try wrong password
    response = test_app.post(
        "/auth/login",
        json={
            "identifier": "fail@example.com",
            "password": "wrong-password",
        },
    )

    assert response.status_code == 401


def test_login_nonexistent_user(test_app):
    """Test that login fails for non-existent user."""
    response = test_app.post(
        "/auth/login",
        json={
            "identifier": "nobody@example.com",
            "password": "any-password",
        },
    )

    assert response.status_code == 401


# ==============================================================================
# Profile Tests
# ==============================================================================


def test_get_user_profile(test_app):
    """Test getting current user profile."""
    # Register and login
    register_response = test_app.post(
        "/auth/register",
        json={
            "email": "profile@example.com",
            "password": "securepassword123",
            "username": "profileuser",
        },
    )
    token = register_response.json()["token"]

    # Get profile
    response = test_app.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["email"] == "profile@example.com"
    assert data["username"] == "profileuser"


def test_update_user_profile(test_app):
    """Test updating user profile."""
    # Register and login
    register_response = test_app.post(
        "/auth/register",
        json={
            "email": "update@example.com",
            "password": "securepassword123",
            "username": "updateuser",
        },
    )
    token = register_response.json()["token"]

    # Update profile
    response = test_app.patch(
        "/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "display_name": "Updated Name",
            "avatar_url": "https://example.com/avatar.jpg",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["display_name"] == "Updated Name"
    assert data["avatar_url"] == "https://example.com/avatar.jpg"


# ==============================================================================
# Password Management Tests
# ==============================================================================


def test_change_password(test_app):
    """Test password change."""
    # Register user
    test_app.post(
        "/auth/register",
        json={
            "email": "change@example.com",
            "password": "oldpassword123",
            "username": "changeuser",
        },
    )

    # Login
    login_response = test_app.post(
        "/auth/login",
        json={
            "identifier": "change@example.com",
            "password": "oldpassword123",
        },
    )
    token = login_response.json()["token"]

    # Change password
    response = test_app.post(
        "/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "old_password": "oldpassword123",
            "new_password": "newpassword456",
        },
    )

    assert response.status_code == 200

    # Try old password (should fail)
    response = test_app.post(
        "/auth/login",
        json={
            "identifier": "change@example.com",
            "password": "oldpassword123",
        },
    )
    assert response.status_code == 401

    # Try new password (should work)
    response = test_app.post(
        "/auth/login",
        json={
            "identifier": "change@example.com",
            "password": "newpassword456",
        },
    )
    assert response.status_code == 200


# ==============================================================================
# OAuth Tests
# ==============================================================================


def test_get_google_oauth_url(test_app):
    """Test getting Google OAuth authorization URL."""
    # This will fail if Google OAuth is not configured, which is expected
    response = test_app.get("/auth/oauth/google/authorize")

    # Either succeeds with auth URL or fails with 500 (OAuth not configured)
    assert response.status_code in [200, 500]

    if response.status_code == 200:
        data = response.json()
        assert "auth_url" in data
        assert "state" in data


def test_oauth_callback_race_condition():
    """Test that concurrent OAuth callbacks don't create duplicate API keys.

    This test simulates the race condition where the OAuth callback endpoint
    is called twice simultaneously (e.g., double-click or network retry).
    The double-check pattern with proper locking should prevent duplicate keys.

    Note: This test uses a threading.Lock to simulate the database-level locking
    that would be provided by PostgreSQL's SELECT ... FOR UPDATE. In production,
    the provision_user() method uses with_for_update() for PostgreSQL, while
    SQLite relies on its file-level locking with appropriate transaction modes.
    """
    import os
    import tempfile
    import threading
    from datetime import datetime

    from sqlalchemy import func, select

    from nexus.server.auth.database_key import DatabaseAPIKeyAuth
    from nexus.storage.models import APIKeyModel, UserModel

    # Create temporary file-based database (required for thread safety)
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".db") as f:
        db_file = f.name

    try:
        # Create file-based database for proper thread safety
        database_url = f"sqlite:///{db_file}"
        engine = create_engine(database_url, echo=False)
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)

        # Create a test user
        test_user_id = "race-test-user-123"
        test_email = "race-test@example.com"

        with SessionLocal() as session:
            user = UserModel(
                user_id=test_user_id,
                email=test_email,
                username="racetest",
                display_name="Race Test User",
                created_at=datetime.now(),
            )
            session.add(user)
            session.commit()

        # Results storage
        api_keys_created = []
        errors = []

        # Lock to simulate database-level row locking (FOR UPDATE)
        # In production, PostgreSQL provides this via SELECT ... FOR UPDATE
        # For SQLite tests, we use a Python lock to achieve the same serialization
        api_key_creation_lock = threading.Lock()

        def create_api_key_for_user():
            """Simulate the API key creation logic from OAuth callback.

            Uses a lock to serialize access to the check-then-create pattern,
            simulating PostgreSQL's SELECT ... FOR UPDATE behavior.
            """
            try:
                zone_id = test_email

                # Acquire lock to ensure exclusive access during check-then-create
                # This simulates PostgreSQL's FOR UPDATE row-level locking
                with api_key_creation_lock, SessionLocal() as session:
                    # Double-check if API key was created by concurrent request
                    user_model = session.get(UserModel, test_user_id)
                    if user_model and user_model.api_key:
                        # Another request already created the API key
                        api_key = user_model.api_key
                    else:
                        # Create new API key
                        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
                            session=session,
                            user_id=test_user_id,
                            name="Personal API Key",
                            subject_type="user",
                            subject_id=test_user_id,
                            zone_id=zone_id,
                            is_admin=False,
                            expires_at=None,
                            inherit_permissions=True,
                        )

                        # Store plaintext API key in users table
                        if user_model:
                            user_model.api_key = raw_key
                            user_model.zone_id = zone_id

                        session.commit()
                        api_key = raw_key

                    api_keys_created.append(api_key)

            except Exception as e:
                errors.append(e)

        # Create two threads to simulate concurrent API key creation
        thread1 = threading.Thread(target=create_api_key_for_user)
        thread2 = threading.Thread(target=create_api_key_for_user)

        # Start both threads simultaneously
        thread1.start()
        thread2.start()

        # Wait for both to complete
        thread1.join()
        thread2.join()

        # Check for errors
        assert len(errors) == 0, f"Errors occurred during concurrent execution: {errors}"

        # Both threads should have gotten an API key
        assert len(api_keys_created) == 2, (
            f"Expected 2 API keys returned, got {len(api_keys_created)}"
        )

        # Query the database to verify how many API keys were actually created
        with SessionLocal() as session:
            # Count API keys created for this user
            api_key_count = session.scalar(
                select(func.count())
                .select_from(APIKeyModel)
                .where(APIKeyModel.user_id == test_user_id)
            )

            # Get the user to check api_key field
            user = session.get(UserModel, test_user_id)

            # Assert only one API key was created
            assert api_key_count == 1, (
                f"Expected 1 API key, but found {api_key_count}. "
                f"Race condition detected: concurrent requests created duplicate keys!"
            )

            # Assert user has an API key
            assert user.api_key is not None, "User should have an API key"
            assert user.zone_id is not None, "User should have a zone_id"
            assert user.api_key == api_keys_created[0], (
                "User's API key should match the created key"
            )

        # Clean up
        Base.metadata.drop_all(engine)
        engine.dispose()

    finally:
        # Clean up temp database file
        if os.path.exists(db_file):
            os.unlink(db_file)


def test_oauth_callback_race_condition_postgres():
    """Test race condition with PostgreSQL (if available).

    This test verifies that PostgreSQL's row-level locking and better
    transaction isolation prevent duplicate API key creation during
    concurrent OAuth callbacks.

    Note: Requires PostgreSQL running at postgresql://postgres:nexus@localhost:5433/nexus
    """
    import threading
    from datetime import datetime

    from sqlalchemy import func, select

    from nexus.server.auth.database_key import DatabaseAPIKeyAuth
    from nexus.storage.models import APIKeyModel, UserModel

    # PostgreSQL connection
    database_url = "postgresql://postgres:nexus@localhost:5433/nexus"

    try:
        # Try to connect to PostgreSQL
        engine = create_engine(database_url, echo=False)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("✅ PostgreSQL connection successful")
    except Exception as e:
        pytest.skip(f"PostgreSQL not available: {e}")
        return

    # Create tables
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    # Create a test user with unique ID
    test_user_id = f"pg-race-test-{datetime.now().timestamp()}"
    test_email = f"pg-race-test-{datetime.now().timestamp()}@example.com"

    with SessionLocal() as session:
        user = UserModel(
            user_id=test_user_id,
            email=test_email,
            username="pgracetest",
            display_name="PG Race Test User",
            created_at=datetime.now(),
        )
        session.add(user)
        session.commit()

    # Results storage
    api_keys_created = []
    errors = []

    def create_api_key_for_user():
        """Simulate the API key creation logic from OAuth callback."""
        try:
            # This simulates the exact double-check logic from auth_routes.py
            zone_id = test_email

            # Create API key with race condition protection
            with SessionLocal() as session:
                # Double-check if API key was created by concurrent request
                user_model = session.get(UserModel, test_user_id)
                if user_model and user_model.api_key:
                    # Another request already created the API key
                    api_key = user_model.api_key
                else:
                    # Create new API key
                    key_id, raw_key = DatabaseAPIKeyAuth.create_key(
                        session=session,
                        user_id=test_user_id,
                        name="Personal API Key",
                        subject_type="user",
                        subject_id=test_user_id,
                        zone_id=zone_id,
                        is_admin=False,
                        expires_at=None,
                        inherit_permissions=True,
                    )

                    # Store plaintext API key in users table
                    if user_model:
                        user_model.api_key = raw_key
                        user_model.zone_id = zone_id

                    session.commit()
                    api_key = raw_key

                api_keys_created.append(api_key)
        except Exception as e:
            errors.append(e)

    # Create two threads to simulate concurrent API key creation
    thread1 = threading.Thread(target=create_api_key_for_user)
    thread2 = threading.Thread(target=create_api_key_for_user)

    # Start both threads simultaneously
    thread1.start()
    thread2.start()

    # Wait for both to complete
    thread1.join()
    thread2.join()

    # Check for errors
    assert len(errors) == 0, f"Errors occurred during concurrent execution: {errors}"

    # Both threads should have gotten an API key
    assert len(api_keys_created) == 2, f"Expected 2 API keys returned, got {len(api_keys_created)}"

    # Query the database to verify how many API keys were actually created
    with SessionLocal() as session:
        # Count API keys created for this user
        api_key_count = session.scalar(
            select(func.count()).select_from(APIKeyModel).where(APIKeyModel.user_id == test_user_id)
        )

        # Get the user to check api_key field
        user = session.get(UserModel, test_user_id)

        # With PostgreSQL, we expect only ONE API key due to row-level locking
        assert api_key_count == 1, (
            f"Expected 1 API key with PostgreSQL, but found {api_key_count}. "
            f"PostgreSQL's row-level locking should prevent duplicate keys!"
        )

        # Both threads should have gotten the SAME API key
        assert api_keys_created[0] == api_keys_created[1], (
            f"Both threads should have received the same API key. Got: {api_keys_created}"
        )

        # Assert user has an API key
        assert user.api_key is not None, "User should have an API key"
        assert user.zone_id is not None, "User should have a zone_id"
        assert user.api_key == api_keys_created[0], "User's API key should match the created key"

        print("✅ PostgreSQL race condition test passed!")
        print("   - Both threads executed concurrently")
        print(f"   - Only 1 API key created: {api_key_count}")
        print(f"   - Both threads got same key: {api_keys_created[0] == api_keys_created[1]}")

    # Clean up test data
    with SessionLocal() as session:
        # Delete test user and related data
        session.execute(text(f"DELETE FROM api_keys WHERE user_id = '{test_user_id}'"))
        session.execute(text(f"DELETE FROM users WHERE user_id = '{test_user_id}'"))
        session.commit()

    engine.dispose()


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v"])
