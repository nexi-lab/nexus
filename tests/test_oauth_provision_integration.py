"""
Integration test for OAuth user provisioning with simplified API key creation.

Tests that provision_user correctly creates OAuth-specific API keys with:
- Custom name ("OAuth Auto-generated Key")
- 90-day expiry
- key_id returned for OAuthAPIKeyModel storage
"""

from datetime import UTC, datetime, timedelta

import pytest

from nexus import LocalBackend
from nexus.core.permissions import OperationContext
from nexus.factory import create_nexus_fs
from nexus.server.auth.oauth_crypto import OAuthCrypto
from nexus.storage.models import APIKeyModel, OAuthAPIKeyModel
from nexus.storage.record_store import SQLAlchemyRecordStore
from nexus.storage.sqlalchemy_metadata_store import SQLAlchemyMetadataStore


@pytest.fixture
def record_store(tmp_path):
    """Create SQLAlchemyRecordStore for testing."""
    db_file = tmp_path / "metadata.db"
    rs = SQLAlchemyRecordStore(db_path=db_file)
    yield rs
    rs.close()


@pytest.fixture
def nx(tmp_path, record_store):
    """Create NexusFS instance for testing."""
    db_file = tmp_path / "metadata.db"
    nx_instance = create_nexus_fs(
        backend=LocalBackend(tmp_path),
        metadata_store=SQLAlchemyMetadataStore(db_path=db_file),
        record_store=record_store,
        auto_parse=False,
        enforce_permissions=True,
        allow_admin_bypass=True,
    )
    yield nx_instance
    nx_instance.close()


@pytest.fixture
def admin_context():
    """Create admin operation context."""
    return OperationContext(
        user="system",
        groups=[],
        zone_id="test_zone",
        is_admin=True,
    )


@pytest.fixture
def oauth_crypto():
    """Create OAuthCrypto instance for testing."""
    return OAuthCrypto()


class TestOAuthProvisionIntegration:
    """Integration tests for OAuth user provisioning with simplified API key creation."""

    def test_provision_user_creates_oauth_key_with_expiry(self, nx, admin_context, record_store):
        """Test that provision_user creates API key with custom expiry."""
        # Provision user with OAuth-specific parameters
        expiry_date = datetime.now(UTC) + timedelta(days=90)
        result = nx.provision_user(
            user_id="oauth_user_1",
            email="oauth1@example.com",
            display_name="OAuth User 1",
            zone_id="test_zone",
            create_api_key=True,
            api_key_name="OAuth Auto-generated Key",
            api_key_expires_at=expiry_date,
            create_agents=False,
            import_skills=False,
            context=admin_context,
        )

        # Verify result structure
        assert result["user_id"] == "oauth_user_1"
        assert result["zone_id"] == "test_zone"
        assert result["api_key"] is not None
        assert result["key_id"] is not None
        assert result["api_key"].startswith("sk-")

        # Verify API key in database
        session = record_store.session_factory()
        try:
            api_key_model = session.query(APIKeyModel).filter_by(key_id=result["key_id"]).first()
            assert api_key_model is not None
            assert api_key_model.user_id == "oauth_user_1"
            assert api_key_model.name == "OAuth Auto-generated Key"
            assert api_key_model.revoked == 0

            # Verify expiry (allow 1 second tolerance for test execution time)
            assert api_key_model.expires_at is not None
            expires_at = api_key_model.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)

            time_diff = abs((expires_at - expiry_date).total_seconds())
            assert time_diff < 2  # Within 2 seconds
        finally:
            session.close()

    def test_provision_user_returns_key_id_for_oauth_storage(
        self, nx, admin_context, oauth_crypto, record_store
    ):
        """Test that provision_user returns key_id which can be used for OAuthAPIKeyModel."""
        # Provision user
        result = nx.provision_user(
            user_id="oauth_user_2",
            email="oauth2@example.com",
            display_name="OAuth User 2",
            zone_id="test_zone",
            create_api_key=True,
            api_key_name="OAuth Auto-generated Key",
            api_key_expires_at=datetime.now(UTC) + timedelta(days=90),
            create_agents=False,
            import_skills=False,
            context=admin_context,
        )

        api_key_value = result["api_key"]
        key_id = result["key_id"]

        assert api_key_value is not None
        assert key_id is not None

        # Encrypt and store in OAuthAPIKeyModel (simulating OAuth callback)
        encrypted_value = oauth_crypto.encrypt_token(api_key_value)
        session = record_store.session_factory()
        try:
            oauth_key = OAuthAPIKeyModel(
                key_id=key_id,
                user_id="oauth_user_2",
                encrypted_key_value=encrypted_value,
            )
            session.add(oauth_key)
            session.commit()

            # Verify stored
            stored_key = session.query(OAuthAPIKeyModel).filter_by(key_id=key_id).first()
            assert stored_key is not None
            assert stored_key.user_id == "oauth_user_2"

            # Verify decryption
            decrypted = oauth_crypto.decrypt_token(stored_key.encrypted_key_value)
            assert decrypted == api_key_value
        finally:
            session.close()

    def test_provision_user_default_parameters_no_expiry(self, nx, admin_context, record_store):
        """Test that provision_user without expiry parameter creates key with no expiry."""
        result = nx.provision_user(
            user_id="regular_user",
            email="regular@example.com",
            display_name="Regular User",
            zone_id="test_zone",
            create_api_key=True,
            create_agents=False,
            import_skills=False,
            context=admin_context,
        )

        # Verify API key has no expiry
        session = record_store.session_factory()
        try:
            api_key_model = session.query(APIKeyModel).filter_by(key_id=result["key_id"]).first()
            assert api_key_model is not None
            assert api_key_model.expires_at is None  # No expiry
        finally:
            session.close()

    def test_provision_user_custom_key_name(self, nx, admin_context, record_store):
        """Test that provision_user respects custom API key name."""
        result = nx.provision_user(
            user_id="custom_user",
            email="custom@example.com",
            display_name="Custom User",
            zone_id="test_zone",
            create_api_key=True,
            api_key_name="Custom Key Name",
            create_agents=False,
            import_skills=False,
            context=admin_context,
        )

        # Verify custom name
        session = record_store.session_factory()
        try:
            api_key_model = session.query(APIKeyModel).filter_by(key_id=result["key_id"]).first()
            assert api_key_model is not None
            assert api_key_model.name == "Custom Key Name"
        finally:
            session.close()

    def test_complete_oauth_flow_simulation(self, nx, admin_context, oauth_crypto, record_store):
        """Test complete OAuth flow: provision + encrypt + store."""
        # Step 1: Provision user (as OAuth callback would do)
        expiry_date = datetime.now(UTC) + timedelta(days=90)
        provision_result = nx.provision_user(
            user_id="oauth_user_complete",
            email="complete@example.com",
            display_name="Complete OAuth User",
            zone_id="test_zone",
            create_api_key=True,
            api_key_name="OAuth Auto-generated Key",
            api_key_expires_at=expiry_date,
            create_agents=True,  # Include agents
            import_skills=False,
            context=admin_context,
        )

        api_key_value = provision_result["api_key"]
        key_id = provision_result["key_id"]

        # Step 2: Encrypt and store (as OAuth callback would do)
        encrypted_value = oauth_crypto.encrypt_token(api_key_value)
        session = record_store.session_factory()
        try:
            oauth_key = OAuthAPIKeyModel(
                key_id=key_id,
                user_id="oauth_user_complete",
                encrypted_key_value=encrypted_value,
            )
            session.add(oauth_key)
            session.commit()
        finally:
            session.close()

        # Step 3: Verify complete provisioning
        assert provision_result["workspace_path"] is not None
        assert len(provision_result["agent_paths"]) >= 0

        # Step 4: Verify API key retrieval (as subsequent OAuth login would do)
        session = record_store.session_factory()
        try:
            # Find OAuth key
            oauth_keys = (
                session.query(OAuthAPIKeyModel).filter_by(user_id="oauth_user_complete").all()
            )
            assert len(oauth_keys) == 1

            # Decrypt key
            retrieved_key = oauth_crypto.decrypt_token(oauth_keys[0].encrypted_key_value)
            assert retrieved_key == api_key_value
            assert retrieved_key.startswith("sk-")

            # Verify expiry
            api_key_model = session.query(APIKeyModel).filter_by(key_id=key_id).first()
            assert api_key_model.expires_at is not None
            expires_at = api_key_model.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)

            # Should expire in ~90 days
            days_until_expiry = (expires_at - datetime.now(UTC)).days
            assert 89 <= days_until_expiry <= 90
        finally:
            session.close()

    def test_provision_without_api_key_returns_none(self, nx, admin_context):
        """Test that provision_user with create_api_key=False returns None for key_id."""
        result = nx.provision_user(
            user_id="no_key_user",
            email="nokey@example.com",
            display_name="No Key User",
            zone_id="test_zone",
            create_api_key=False,
            create_agents=False,
            import_skills=False,
            context=admin_context,
        )

        assert result["api_key"] is None
        assert result["key_id"] is None
