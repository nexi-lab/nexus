"""Unit tests for setup_admin_api_key script.

Tests cover the setup_admin_api_key function:
- Creating admin API key successfully
- Verifying existing admin API key
- User registration in entity registry
- Error handling for invalid inputs
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from nexus.server.auth.database_key import DatabaseAPIKeyAuth
from nexus.storage.models import APIKeyModel, Base

# Add scripts directory to path for imports
script_dir = Path(__file__).parent.parent.parent.parent / "scripts"
sys.path.insert(0, str(script_dir))

from setup_admin_api_key import setup_admin_api_key  # noqa: E402


@pytest.fixture
def temp_db_file():
    """Create a temporary database file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    # Cleanup
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def engine(temp_db_file):
    """Create SQLite database for testing."""
    database_url = f"sqlite:///{temp_db_file}"
    engine = create_engine(database_url)
    # Create all tables including api_keys
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(engine):
    """Create session factory."""
    return sessionmaker(bind=engine)


@pytest.fixture
def database_url(temp_db_file):
    """Return database URL for the temporary file."""
    return f"sqlite:///{temp_db_file}"


class TestSetupAdminAPIKey:
    """Test suite for setup_admin_api_key function."""

    def test_setup_admin_api_key_creates_new_key(self, engine, session_factory, database_url):
        """Test that setup_admin_api_key creates a new admin API key."""
        admin_key = "sk-admin_test_key_12345"

        result = setup_admin_api_key(database_url, admin_key)

        assert result is True

        # Verify key was created in database
        with session_factory() as session:
            key_hash = DatabaseAPIKeyAuth._hash_key(admin_key)
            existing = session.execute(
                select(APIKeyModel).where(APIKeyModel.key_hash == key_hash)
            ).scalar_one_or_none()

            assert existing is not None
            assert existing.user_id == "admin"
            assert existing.subject_type == "user"
            assert existing.subject_id == "admin"
            assert existing.tenant_id == "default"
            assert existing.is_admin == 1
            assert existing.name == "Admin Bootstrap Key"
            assert existing.revoked == 0

    def test_setup_admin_api_key_verifies_existing_key(self, engine, session_factory, database_url):
        """Test that setup_admin_api_key verifies existing key without error."""
        admin_key = "sk-admin_existing_key_12345"

        # Create key first time
        result1 = setup_admin_api_key(database_url, admin_key)
        assert result1 is True

        # Call again - should verify existing key
        result2 = setup_admin_api_key(database_url, admin_key)
        assert result2 is True

        # Verify only one key exists
        with session_factory() as session:
            key_hash = DatabaseAPIKeyAuth._hash_key(admin_key)
            keys = session.execute(
                select(APIKeyModel).where(APIKeyModel.key_hash == key_hash)
            ).all()
            assert len(keys) == 1

    def test_setup_admin_api_key_registers_user(self, engine, session_factory, database_url):
        """Test that setup_admin_api_key registers user in entity registry."""
        from nexus.core.entity_registry import EntityRegistry

        admin_key = "sk-admin_register_test"

        result = setup_admin_api_key(database_url, admin_key)

        assert result is True

        # Verify user was registered
        registry = EntityRegistry(session_factory)
        entity = registry.get_entity("user", "admin")
        assert entity is not None
        assert entity.entity_id == "admin"
        assert entity.parent_type == "tenant"
        assert entity.parent_id == "default"

    def test_setup_admin_api_key_with_custom_tenant_id(self, engine, session_factory, database_url):
        """Test setup_admin_api_key with custom tenant_id."""
        admin_key = "sk-admin_custom_tenant"
        tenant_id = "acme_corp"

        result = setup_admin_api_key(database_url, admin_key, tenant_id=tenant_id, user_id="admin")

        assert result is True

        # Verify key was created with custom tenant_id
        with session_factory() as session:
            key_hash = DatabaseAPIKeyAuth._hash_key(admin_key)
            existing = session.execute(
                select(APIKeyModel).where(APIKeyModel.key_hash == key_hash)
            ).scalar_one_or_none()

            assert existing is not None
            assert existing.tenant_id == tenant_id

    def test_setup_admin_api_key_with_custom_user_id(self, engine, session_factory, database_url):
        """Test setup_admin_api_key with custom user_id."""
        admin_key = "sk-custom_user_key"
        user_id = "superadmin"

        result = setup_admin_api_key(database_url, admin_key, tenant_id="default", user_id=user_id)

        assert result is True

        # Verify key was created with custom user_id
        with session_factory() as session:
            key_hash = DatabaseAPIKeyAuth._hash_key(admin_key)
            existing = session.execute(
                select(APIKeyModel).where(APIKeyModel.key_hash == key_hash)
            ).scalar_one_or_none()

            assert existing is not None
            assert existing.user_id == user_id
            assert existing.subject_id == user_id
            assert existing.name == "Superadmin Bootstrap Key"

    def test_setup_admin_api_key_handles_existing_user(self, engine, session_factory, database_url):
        """Test that setup_admin_api_key handles existing user gracefully."""
        from nexus.core.entity_registry import EntityRegistry

        admin_key = "sk-existing_user_test"

        # Register user first
        registry = EntityRegistry(session_factory)
        registry.register_entity(
            entity_type="user",
            entity_id="admin",
            parent_type="tenant",
            parent_id="default",
        )

        # Setup API key - should not fail
        result = setup_admin_api_key(database_url, admin_key)
        assert result is True

    def test_setup_admin_api_key_with_postgresql_url(self):
        """Test that setup_admin_api_key works with PostgreSQL URL format."""
        # This test mocks the database connection since we can't easily set up PostgreSQL
        admin_key = "sk-postgres_test"

        with (
            patch("setup_admin_api_key.create_engine") as mock_engine,
            patch("setup_admin_api_key.sessionmaker") as mock_sessionmaker,
            patch("setup_admin_api_key.EntityRegistry") as mock_registry,
            patch("setup_admin_api_key.DatabaseAPIKeyAuth") as mock_auth,
        ):
            # Setup mocks
            mock_session = Mock()
            mock_session_factory = Mock(return_value=mock_session)
            mock_sessionmaker.return_value = mock_session_factory

            mock_registry_instance = Mock()
            mock_registry.return_value = mock_registry_instance

            mock_session.execute.return_value.scalar_one_or_none.return_value = None
            mock_session.__enter__ = Mock(return_value=mock_session)
            mock_session.__exit__ = Mock(return_value=False)

            # Mock hash function
            mock_auth._hash_key.return_value = "hashed_key"

            postgres_url = "postgresql://user:pass@localhost/nexus"

            result = setup_admin_api_key(postgres_url, admin_key)

            # Should attempt to create engine with postgres URL
            mock_engine.assert_called_once_with(postgres_url)
            assert result is True

    def test_setup_admin_api_key_handles_database_error(self, database_url):
        """Test that setup_admin_api_key handles database connection errors."""
        admin_key = "sk-error_test"

        # Use invalid database URL
        invalid_url = "invalid://database/url"

        result = setup_admin_api_key(invalid_url, admin_key)

        assert result is False

    def test_setup_admin_api_key_handles_key_creation_error(
        self, engine, session_factory, database_url
    ):
        """Test that setup_admin_api_key handles key creation errors gracefully."""
        admin_key = "sk-creation_error_test"

        # Patch sessionmaker to return a session that raises on commit
        with patch("setup_admin_api_key.sessionmaker") as mock_sessionmaker:
            mock_session = Mock()
            mock_session.__enter__ = Mock(return_value=mock_session)
            mock_session.__exit__ = Mock(return_value=False)
            mock_session.execute.return_value.scalar_one_or_none.return_value = None
            mock_session.add.side_effect = Exception("Database error")
            mock_session_factory = Mock(return_value=mock_session)
            mock_sessionmaker.return_value = mock_session_factory

            result = setup_admin_api_key(database_url, admin_key)

            assert result is False
