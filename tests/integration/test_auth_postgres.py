"""Integration tests for authentication with PostgreSQL.

These tests verify PostgreSQL's race condition handling for concurrent API key creation.
Unlike other integration tests, this one tests the database layer directly to verify
PostgreSQL's row-level locking behavior.

Requirements:
    - PostgreSQL running at postgresql://postgres:nexus@localhost:5432/nexus
    - Start with: docker compose -f docker-compose.demo.yml up postgres -d

Run tests with:
    pytest tests/integration/test_auth_postgres.py -v

These tests use direct database access to test race conditions, avoiding nexus module imports.
"""

import contextlib
import hashlib
import os
import threading
import uuid
from datetime import datetime

import pytest
from sqlalchemy import Column, DateTime, Integer, String, create_engine, func, select, text
from sqlalchemy.orm import declarative_base, sessionmaker

# Define minimal SQLAlchemy models without importing nexus
Base = declarative_base()


class UserModel(Base):
    """Minimal User model for testing."""

    __tablename__ = "users"

    user_id = Column(String, primary_key=True)
    email = Column(String, unique=True, nullable=True)
    username = Column(String, unique=True, nullable=True)
    display_name = Column(String)
    api_key = Column(String)
    tenant_id = Column(String)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    password_hash = Column(String)
    primary_auth_method = Column(String(50), nullable=False, default="oauth")
    is_global_admin = Column(Integer, nullable=False, default=0)
    is_active = Column(Integer, nullable=False, default=1)
    email_verified = Column(Integer, nullable=False, default=0)


class APIKeyModel(Base):
    """Minimal API Key model for testing."""

    __tablename__ = "api_keys"

    key_id = Column(String(36), primary_key=True)
    key_hash = Column(String(64), unique=True, nullable=False)
    user_id = Column(String(255), nullable=False)
    name = Column(String(255), nullable=False)
    subject_type = Column(String(50), nullable=True)
    subject_id = Column(String(255), nullable=True)
    tenant_id = Column(String(255), nullable=True)
    is_admin = Column(Integer, nullable=False, default=0)  # 0 = False, 1 = True
    inherit_permissions = Column(Integer, nullable=False, default=1)  # 0 = False, 1 = True
    created_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=True)
    revoked = Column(Integer, nullable=False, default=0)
    revoked_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)


@pytest.fixture
def postgres_engine():
    """Create PostgreSQL engine for testing.

    Requires PostgreSQL running at:
        postgresql://postgres:nexus@localhost:5432/nexus

    Start with: docker compose -f docker-compose.demo.yml up postgres -d
    """
    database_url = "postgresql://postgres:nexus@localhost:5432/nexus"

    try:
        engine = create_engine(database_url, echo=False)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        pytest.skip(f"PostgreSQL not available: {e}")

    # Tables should already exist from the running server
    # If not, create them
    with contextlib.suppress(Exception):
        Base.metadata.create_all(engine)

    yield engine

    # Cleanup is handled per-test
    engine.dispose()


@pytest.fixture
def postgres_session(postgres_engine):
    """Create a PostgreSQL session factory for testing."""
    SessionLocal = sessionmaker(bind=postgres_engine)
    yield SessionLocal


def test_oauth_race_condition_postgres(postgres_session):
    """Test that PostgreSQL prevents duplicate API keys during concurrent OAuth callbacks.

    This test verifies PostgreSQL's row-level locking and transaction isolation
    prevent the race condition where two concurrent requests could create
    duplicate API keys for the same user.

    The race condition can occur when:
    1. User completes OAuth login
    2. Browser makes callback request
    3. Network retry or double-click triggers second concurrent callback
    4. Both requests check if user has API key at nearly the same time
    5. Without proper locking, both could create API keys

    With PostgreSQL:
    - Row-level locking ensures only one transaction can modify the user row
    - Second transaction waits for first to commit before checking api_key
    - Result: Only ONE API key is created
    """
    # Create a test user with unique ID to avoid conflicts
    test_timestamp = datetime.now().timestamp()
    test_user_id = f"pg-race-test-{test_timestamp}"
    test_email = f"pg-race-test-{test_timestamp}@example.com"

    with postgres_session() as session:
        now = datetime.now()
        user = UserModel(
            user_id=test_user_id,
            email=test_email,
            username=f"pgracetest{int(test_timestamp)}",
            display_name="PG Race Test User",
            created_at=now,
            updated_at=now,
            primary_auth_method="oauth",
            is_global_admin=0,
            is_active=1,
            email_verified=0,
        )
        session.add(user)
        session.commit()

    # Results storage
    api_keys_created = []
    errors = []

    def create_api_key_for_user():
        """Simulate the API key creation logic from OAuth callback.

        This replicates the exact double-check pattern from the OAuth callback handler.
        """
        try:
            tenant_id = test_email

            # Create API key with race condition protection
            with postgres_session() as session:
                # Double-check if API key was created by concurrent request
                # Use SELECT ... FOR UPDATE to lock the row and prevent race conditions
                user_model = session.execute(
                    select(UserModel).where(UserModel.user_id == test_user_id).with_for_update()
                ).scalar_one_or_none()
                if user_model and user_model.api_key:
                    # Another request already created the API key
                    api_key = user_model.api_key
                else:
                    # Generate API key (simplified version of DatabaseAPIKeyAuth.create_key)
                    key_id = str(uuid.uuid4())
                    raw_key = f"test-key-{os.urandom(16).hex()}"
                    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

                    # Create API key record
                    api_key_record = APIKeyModel(
                        key_id=key_id,
                        key_hash=key_hash,
                        user_id=test_user_id,
                        name="Personal API Key",
                        subject_type="user",
                        subject_id=test_user_id,
                        tenant_id=tenant_id,
                        is_admin=0,  # Integer: 0 = False, 1 = True
                        created_at=datetime.now(),
                        expires_at=None,
                        inherit_permissions=1,  # Integer: 0 = False, 1 = True
                        revoked=0,
                    )
                    session.add(api_key_record)

                    # Store plaintext API key in users table
                    if user_model:
                        user_model.api_key = raw_key
                        user_model.tenant_id = tenant_id

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

    # Verify results
    assert len(errors) == 0, f"Errors occurred during concurrent execution: {errors}"
    assert len(api_keys_created) == 2, f"Expected 2 API keys returned, got {len(api_keys_created)}"

    # Query the database to verify only ONE API key was created
    with postgres_session() as session:
        # Count API keys created for this user
        api_key_count = session.scalar(
            select(func.count()).select_from(APIKeyModel).where(APIKeyModel.user_id == test_user_id)
        )

        # Get the user to check api_key field
        user = session.get(UserModel, test_user_id)

        # PostgreSQL should prevent duplicate keys via row-level locking
        assert api_key_count == 1, (
            f"Expected 1 API key with PostgreSQL, but found {api_key_count}. "
            f"Race condition detected! PostgreSQL's row-level locking should prevent this."
        )

        # Both threads should have gotten the SAME API key
        assert api_keys_created[0] == api_keys_created[1], (
            f"Both threads should have received the same API key. "
            f"Got different keys: {api_keys_created[0]} vs {api_keys_created[1]}"
        )

        # Verify user has the API key
        assert user.api_key is not None, "User should have an API key"
        assert user.tenant_id is not None, "User should have a tenant_id"
        assert user.api_key == api_keys_created[0], "User's API key should match the created key"

    # Cleanup test data
    with postgres_session() as session:
        session.execute(
            text("DELETE FROM api_keys WHERE user_id = :user_id"), {"user_id": test_user_id}
        )
        session.execute(
            text("DELETE FROM users WHERE user_id = :user_id"), {"user_id": test_user_id}
        )
        session.commit()


def test_user_registration_postgres(postgres_session):
    """Test basic user registration with PostgreSQL.

    This is a simple smoke test to verify the PostgreSQL connection
    and basic user operations work correctly.
    """
    test_timestamp = datetime.now().timestamp()
    test_user_id = f"test-user-{test_timestamp}"
    test_email = f"test-{test_timestamp}@example.com"

    with postgres_session() as session:
        now = datetime.now()
        user = UserModel(
            user_id=test_user_id,
            email=test_email,
            username=f"testuser{int(test_timestamp)}",
            display_name="Test User",
            created_at=now,
            updated_at=now,
            primary_auth_method="oauth",
            is_global_admin=0,
            is_active=1,
            email_verified=0,
        )
        session.add(user)
        session.commit()

        # Verify user was created
        retrieved_user = session.get(UserModel, test_user_id)
        assert retrieved_user is not None
        assert retrieved_user.email == test_email
        assert retrieved_user.username == f"testuser{int(test_timestamp)}"

    # Cleanup
    with postgres_session() as session:
        session.execute(
            text("DELETE FROM users WHERE user_id = :user_id"), {"user_id": test_user_id}
        )
        session.commit()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
