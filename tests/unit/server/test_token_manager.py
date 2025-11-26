"""Tests for OAuth TokenManager."""

import gc
import platform
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.core.exceptions import AuthenticationError
from nexus.server.auth import GoogleOAuthProvider, OAuthCredential, TokenManager


class TestTokenManager:
    """Test suite for TokenManager."""

    @pytest.fixture
    def temp_db(self):
        """Create temporary database for testing."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        yield db_path
        # Cleanup with Windows-specific handling
        gc.collect()  # Force garbage collection to release connections
        if platform.system() == "Windows":
            time.sleep(0.2)  # Give Windows time to release file handles

        # Retry deletion on Windows if it fails
        db_path_obj = Path(db_path)
        if platform.system() == "Windows":
            for attempt in range(5):
                try:
                    if db_path_obj.exists():
                        db_path_obj.unlink(missing_ok=True)
                    break
                except PermissionError:
                    if attempt < 4:
                        time.sleep(0.2 * (attempt + 1))
                        gc.collect()
                    else:
                        raise
        else:
            db_path_obj.unlink(missing_ok=True)

    @pytest.fixture
    def manager(self, temp_db):
        """Create TokenManager instance."""
        manager = TokenManager(db_path=temp_db)
        yield manager
        manager.close()
        # Force cleanup on Windows
        gc.collect()
        if platform.system() == "Windows":
            time.sleep(0.1)  # Small delay for Windows to release file handles

    @pytest.fixture
    def valid_credential(self):
        """Create a valid OAuth credential for testing."""
        return OAuthCredential(
            access_token="ya29.a0ARrdaM_test_token",
            refresh_token="1//0e_test_refresh",
            token_type="Bearer",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes=["https://www.googleapis.com/auth/drive"],
            provider="google",
            user_email="alice@example.com",
            client_id="test_client_id",
            token_uri="https://oauth2.googleapis.com/token",
        )

    @pytest.fixture
    def expired_credential(self):
        """Create an expired OAuth credential for testing."""
        return OAuthCredential(
            access_token="ya29.a0ARrdaM_expired_token",
            refresh_token="1//0e_test_refresh",
            token_type="Bearer",
            expires_at=datetime.now(UTC) - timedelta(hours=1),  # Expired
            scopes=["https://www.googleapis.com/auth/drive"],
            provider="google",
            user_email="bob@example.com",
            client_id="test_client_id",
            token_uri="https://oauth2.googleapis.com/token",
        )

    def test_register_provider(self, manager):
        """Test provider registration."""
        provider = MagicMock(spec=GoogleOAuthProvider)

        manager.register_provider("google", provider)

        assert "google" in manager.providers
        assert manager.providers["google"] == provider

    @pytest.mark.asyncio
    async def test_store_credential(self, manager, valid_credential):
        """Test storing a credential."""
        cred_id = await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=valid_credential,
            tenant_id="org_acme",
            created_by="admin",
        )

        assert cred_id is not None
        assert isinstance(cred_id, str)

    @pytest.mark.asyncio
    async def test_store_credential_twice_updates(self, manager, valid_credential):
        """Test that storing same credential twice updates it."""
        # Store first time
        cred_id_1 = await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=valid_credential,
        )

        # Store second time (should update)
        new_credential = OAuthCredential(
            access_token="new_access_token",
            refresh_token="new_refresh_token",
            expires_at=datetime.now(UTC) + timedelta(hours=2),
            scopes=["https://www.googleapis.com/auth/drive"],
            provider="google",
            user_email="alice@example.com",
        )

        cred_id_2 = await manager.store_credential(
            provider="google", user_email="alice@example.com", credential=new_credential
        )

        # Should return same credential_id (updated, not created new)
        assert cred_id_1 == cred_id_2

        # Verify it was updated
        retrieved = await manager.get_credential("google", "alice@example.com")
        assert retrieved.access_token == "new_access_token"

    @pytest.mark.asyncio
    async def test_get_credential(self, manager, valid_credential):
        """Test retrieving a credential."""
        # Store credential
        await manager.store_credential(
            provider="google", user_email="alice@example.com", credential=valid_credential
        )

        # Retrieve credential
        retrieved = await manager.get_credential("google", "alice@example.com")

        assert retrieved is not None
        assert retrieved.access_token == valid_credential.access_token
        assert retrieved.refresh_token == valid_credential.refresh_token
        assert retrieved.user_email == "alice@example.com"

    @pytest.mark.asyncio
    async def test_get_credential_not_found(self, manager):
        """Test retrieving non-existent credential returns None."""
        retrieved = await manager.get_credential("google", "nonexistent@example.com")
        assert retrieved is None

    @pytest.mark.asyncio
    async def test_get_valid_token_not_expired(self, manager, valid_credential):
        """Test getting valid token when not expired."""
        # Store credential
        await manager.store_credential(
            provider="google", user_email="alice@example.com", credential=valid_credential
        )

        # Get valid token (should not refresh)
        token = await manager.get_valid_token("google", "alice@example.com")

        assert token == valid_credential.access_token

    @pytest.mark.asyncio
    async def test_get_valid_token_auto_refresh(self, manager, expired_credential):
        """Test automatic token refresh when expired."""
        # Store expired credential
        await manager.store_credential(
            provider="google", user_email="bob@example.com", credential=expired_credential
        )

        # Mock provider with refresh capability
        mock_provider = AsyncMock(spec=GoogleOAuthProvider)
        refreshed_credential = OAuthCredential(
            access_token="new_access_token",
            refresh_token="1//0e_test_refresh",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes=["https://www.googleapis.com/auth/drive"],
            provider="google",
            user_email="bob@example.com",
        )
        mock_provider.refresh_token.return_value = refreshed_credential

        manager.register_provider("google", mock_provider)

        # Get valid token (should trigger refresh)
        token = await manager.get_valid_token("google", "bob@example.com")

        # Should return refreshed token
        assert token == "new_access_token"

        # Verify refresh was called
        mock_provider.refresh_token.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_valid_token_not_found_raises(self, manager):
        """Test that getting token for non-existent credential raises error."""
        with pytest.raises(
            AuthenticationError,
            match="No OAuth credential found for google:nonexistent@example.com",
        ):
            await manager.get_valid_token("google", "nonexistent@example.com")

    @pytest.mark.asyncio
    async def test_revoke_credential(self, manager, valid_credential):
        """Test revoking a credential."""
        # Store credential
        await manager.store_credential(
            provider="google", user_email="alice@example.com", credential=valid_credential
        )

        # Mock provider with revoke capability
        mock_provider = AsyncMock(spec=GoogleOAuthProvider)
        mock_provider.revoke_token.return_value = True
        manager.register_provider("google", mock_provider)

        # Revoke credential
        success = await manager.revoke_credential("google", "alice@example.com")

        assert success is True

        # Verify provider revoke was called
        mock_provider.revoke_token.assert_called_once()

        # Credential should still exist but be marked as revoked
        # (so get_credential should return None since it filters revoked=0)
        retrieved = await manager.get_credential("google", "alice@example.com")
        assert retrieved is None

    @pytest.mark.asyncio
    async def test_revoke_credential_not_found(self, manager):
        """Test revoking non-existent credential returns False."""
        success = await manager.revoke_credential("google", "nonexistent@example.com")
        assert success is False

    @pytest.mark.asyncio
    async def test_list_credentials(self, manager, valid_credential):
        """Test listing credentials."""
        # Store multiple credentials
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=valid_credential,
            tenant_id="org_acme",
        )

        cred2 = OAuthCredential(
            access_token="token2",
            refresh_token="refresh2",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes=["Files.ReadWrite.All"],
            provider="microsoft",
            user_email="bob@example.com",
        )
        await manager.store_credential(
            provider="microsoft",
            user_email="bob@example.com",
            credential=cred2,
            tenant_id="org_acme",
        )

        # List all credentials
        credentials = await manager.list_credentials()

        assert len(credentials) == 2
        assert any(
            c["provider"] == "google" and c["user_email"] == "alice@example.com"
            for c in credentials
        )
        assert any(
            c["provider"] == "microsoft" and c["user_email"] == "bob@example.com"
            for c in credentials
        )

    @pytest.mark.asyncio
    async def test_list_credentials_filtered_by_tenant(self, manager, valid_credential):
        """Test listing credentials filtered by tenant."""
        # Store credentials for different tenants
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=valid_credential,
            tenant_id="org_acme",
        )

        cred2 = OAuthCredential(
            access_token="token2",
            refresh_token="refresh2",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes=["Files.ReadWrite.All"],
            provider="microsoft",
            user_email="bob@example.com",
        )
        await manager.store_credential(
            provider="microsoft",
            user_email="bob@example.com",
            credential=cred2,
            tenant_id="org_other",
        )

        # List credentials for org_acme only
        credentials = await manager.list_credentials(tenant_id="org_acme")

        assert len(credentials) == 1
        assert credentials[0]["provider"] == "google"
        assert credentials[0]["user_email"] == "alice@example.com"
        assert credentials[0]["tenant_id"] == "org_acme"

    @pytest.mark.asyncio
    async def test_list_credentials_empty(self, manager):
        """Test listing credentials when none exist."""
        credentials = await manager.list_credentials()
        assert len(credentials) == 0

    @pytest.mark.asyncio
    async def test_credential_encryption_at_rest(self, manager, valid_credential):
        """Test that tokens are encrypted in database."""
        # Store credential
        await manager.store_credential(
            provider="google", user_email="alice@example.com", credential=valid_credential
        )

        # Query database directly to verify encryption
        from sqlalchemy import select

        from nexus.storage.models import OAuthCredentialModel

        with manager.SessionLocal() as session:
            stmt = select(OAuthCredentialModel).where(
                OAuthCredentialModel.provider == "google",
                OAuthCredentialModel.user_email == "alice@example.com",
            )
            model = session.execute(stmt).scalar_one()

            # Encrypted tokens should NOT match plaintext
            assert model.encrypted_access_token != valid_credential.access_token
            assert model.encrypted_refresh_token != valid_credential.refresh_token

            # Encrypted tokens should be non-empty
            assert len(model.encrypted_access_token) > 0
            assert len(model.encrypted_refresh_token) > 0

    @pytest.mark.asyncio
    async def test_unsupported_provider_raises(self, manager, valid_credential):
        """Test that using credential for unsupported provider raises error."""
        from datetime import UTC, datetime, timedelta

        from nexus.core.exceptions import AuthenticationError
        from nexus.server.auth.oauth_provider import OAuthCredential

        # Create an expired credential so it tries to refresh
        expired_credential = OAuthCredential(
            access_token="ya29.expired_token",
            refresh_token="1//0e_test_refresh",
            token_type="Bearer",
            expires_at=datetime.now(UTC) - timedelta(hours=1),  # Expired
            scopes=["https://www.googleapis.com/auth/drive"],
            provider="unknown",
            user_email="alice@example.com",
            client_id="test_client_id",
        )

        # Store credential (this should succeed - no validation at store time)
        cred_id = await manager.store_credential(
            provider="unknown",  # Unsupported
            user_email="alice@example.com",
            credential=expired_credential,
        )
        assert cred_id is not None

        # Getting a token should fail because provider is not registered
        # The error occurs when trying to refresh an expired token
        with pytest.raises(AuthenticationError, match="Provider not registered"):
            await manager.get_valid_token(
                provider="unknown",
                user_email="alice@example.com",
            )

    @pytest.mark.asyncio
    async def test_tenant_isolation(self, manager, valid_credential):
        """Test that credentials are isolated by tenant."""
        # Store same user email in different tenants
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=valid_credential,
            tenant_id="org_acme",
        )

        cred2 = OAuthCredential(
            access_token="different_token",
            refresh_token="different_refresh",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes=["https://www.googleapis.com/auth/drive"],
            provider="google",
            user_email="alice@example.com",
        )
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=cred2,
            tenant_id="org_other",
        )

        # Retrieve from org_acme
        retrieved_acme = await manager.get_credential("google", "alice@example.com", "org_acme")
        assert retrieved_acme.access_token == valid_credential.access_token

        # Retrieve from org_other
        retrieved_other = await manager.get_credential("google", "alice@example.com", "org_other")
        assert retrieved_other.access_token == "different_token"

        # Should be different credentials
        assert retrieved_acme.access_token != retrieved_other.access_token
