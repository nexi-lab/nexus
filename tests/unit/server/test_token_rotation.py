"""Tests for OAuth token rotation (Issue #997).

Covers:
- Token family creation on first store
- Rotation counter increment
- Old token hash stored in history
- Reuse detection invalidates family
- Rate limit rejects rapid refresh
- Cache returns valid token without DB hit
- Cache invalidated on rotation
- History pruning
"""

import asyncio
import gc
import platform
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.core.exceptions import AuthenticationError
from nexus.server.auth.oauth_provider import OAuthCredential, OAuthError
from nexus.server.auth.token_manager import TokenManager, _hash_token


class TestTokenRotation:
    """Token rotation tests — TDD (Issue #997)."""

    @pytest.fixture
    def temp_db(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        yield db_path
        gc.collect()
        if platform.system() == "Windows":
            time.sleep(0.2)
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
        manager = TokenManager(db_path=temp_db)
        yield manager
        manager.close()
        gc.collect()
        if platform.system() == "Windows":
            time.sleep(0.1)

    @pytest.fixture
    def valid_credential(self):
        return OAuthCredential(
            access_token="ya29.test_access_token",
            refresh_token="1//test_refresh_token",
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
        return OAuthCredential(
            access_token="ya29.expired_access_token",
            refresh_token="1//test_refresh_token",
            token_type="Bearer",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
            scopes=["https://www.googleapis.com/auth/drive"],
            provider="google",
            user_email="alice@example.com",
            client_id="test_client_id",
            token_uri="https://oauth2.googleapis.com/token",
        )

    @pytest.mark.asyncio
    async def test_store_creates_token_family(self, manager, valid_credential):
        """Storing a credential should create a token family ID."""
        cred_id = await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=valid_credential,
        )
        assert cred_id

        # Verify token family was created
        creds = await manager.list_credentials()
        assert len(creds) == 1
        assert creds[0]["token_family_id"] is not None
        assert creds[0]["rotation_counter"] == 0

    @pytest.mark.asyncio
    async def test_store_creates_refresh_token_hash(self, manager, valid_credential):
        """Store should compute and save refresh_token_hash."""
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=valid_credential,
        )

        # Verify via direct DB query
        from sqlalchemy import select
        from nexus.storage.models import OAuthCredentialModel

        with manager.SessionLocal() as session:
            stmt = select(OAuthCredentialModel).where(
                OAuthCredentialModel.user_email == "alice@example.com"
            )
            model = session.execute(stmt).scalar_one()
            assert model.refresh_token_hash is not None
            assert model.refresh_token_hash == _hash_token("1//test_refresh_token")

    @pytest.mark.asyncio
    async def test_rotation_increments_counter(self, manager, expired_credential):
        """When provider returns new refresh token, rotation counter increments."""
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=expired_credential,
        )

        # Mock provider that returns a NEW refresh token
        mock_provider = AsyncMock()
        new_cred = OAuthCredential(
            access_token="ya29.new_access_token",
            refresh_token="1//NEW_refresh_token",  # Different!
            token_type="Bearer",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        mock_provider.refresh_token = AsyncMock(return_value=new_cred)
        manager.register_provider("google", mock_provider)

        token = await manager.get_valid_token("google", "alice@example.com")
        assert token == "ya29.new_access_token"

        creds = await manager.list_credentials()
        assert creds[0]["rotation_counter"] == 1

    @pytest.mark.asyncio
    async def test_rotation_stores_old_hash_in_history(self, manager, expired_credential):
        """Rotation should store the old refresh token hash in history."""
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=expired_credential,
        )

        # Mock provider returning new refresh token
        mock_provider = AsyncMock()
        new_cred = OAuthCredential(
            access_token="ya29.new_access",
            refresh_token="1//NEW_refresh",
            token_type="Bearer",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        mock_provider.refresh_token = AsyncMock(return_value=new_cred)
        manager.register_provider("google", mock_provider)

        await manager.get_valid_token("google", "alice@example.com")

        # The old refresh token hash should be in history
        old_hash = _hash_token("1//test_refresh_token")
        from sqlalchemy import select
        from nexus.storage.models.refresh_token_history import RefreshTokenHistoryModel

        with manager.SessionLocal() as session:
            stmt = select(RefreshTokenHistoryModel).where(
                RefreshTokenHistoryModel.refresh_token_hash == old_hash
            )
            history = session.execute(stmt).scalar_one_or_none()
            assert history is not None
            assert history.rotation_counter == 0  # Was generation 0

    @pytest.mark.asyncio
    async def test_reuse_detection(self, manager, expired_credential):
        """detect_reuse returns True if a hash exists in history."""
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=expired_credential,
        )

        # Get the token family ID
        creds = await manager.list_credentials()
        family_id = creds[0]["token_family_id"]

        # Do rotation
        mock_provider = AsyncMock()
        new_cred = OAuthCredential(
            access_token="ya29.new",
            refresh_token="1//rotated",
            token_type="Bearer",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        mock_provider.refresh_token = AsyncMock(return_value=new_cred)
        manager.register_provider("google", mock_provider)

        await manager.get_valid_token("google", "alice@example.com")

        # Old token should be detected as reuse
        old_hash = _hash_token("1//test_refresh_token")
        assert manager.detect_reuse(family_id, old_hash) is True

        # Current token should NOT be detected as reuse
        current_hash = _hash_token("1//rotated")
        assert manager.detect_reuse(family_id, current_hash) is False

    @pytest.mark.asyncio
    async def test_family_invalidation(self, manager, valid_credential):
        """invalidate_family revokes all credentials in the family."""
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=valid_credential,
        )

        creds = await manager.list_credentials()
        family_id = creds[0]["token_family_id"]

        revoked_count = manager.invalidate_family(family_id)
        assert revoked_count == 1

        # Credential should now be revoked
        creds_after = await manager.list_credentials()
        assert len(creds_after) == 0  # list_credentials filters revoked

    @pytest.mark.asyncio
    async def test_cache_returns_valid_token(self, manager, valid_credential):
        """Second call should return from cache without DB hit."""
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=valid_credential,
        )

        # First call populates cache
        token1 = await manager.get_valid_token("google", "alice@example.com")

        # Verify cache is populated
        cache_key = ("google", "alice@example.com", "default")
        assert cache_key in manager._token_cache

        # Second call should use cache
        token2 = await manager.get_valid_token("google", "alice@example.com")
        assert token1 == token2

    @pytest.mark.asyncio
    async def test_cache_invalidated_on_revoke(self, manager, valid_credential):
        """Revoking a credential should clear the cache."""
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=valid_credential,
        )

        # Populate cache
        await manager.get_valid_token("google", "alice@example.com")
        cache_key = ("google", "alice@example.com", "default")
        assert cache_key in manager._token_cache

        # Revoke should clear cache
        await manager.revoke_credential("google", "alice@example.com")
        assert cache_key not in manager._token_cache

    @pytest.mark.asyncio
    async def test_no_rotation_when_same_refresh_token(self, manager, expired_credential):
        """When provider returns same refresh token, rotation counter stays 0."""
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=expired_credential,
        )

        # Mock provider returning SAME refresh token
        mock_provider = AsyncMock()
        same_cred = OAuthCredential(
            access_token="ya29.new_access",
            refresh_token="1//test_refresh_token",  # Same as original
            token_type="Bearer",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        mock_provider.refresh_token = AsyncMock(return_value=same_cred)
        manager.register_provider("google", mock_provider)

        await manager.get_valid_token("google", "alice@example.com")

        creds = await manager.list_credentials()
        assert creds[0]["rotation_counter"] == 0

    @pytest.mark.asyncio
    async def test_rate_limit_skips_refresh(self, manager, expired_credential):
        """Rapid refresh attempts should be rate limited."""
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=expired_credential,
        )

        # Set last_refreshed_at to NOW (simulating recent refresh)
        from sqlalchemy import select
        from nexus.storage.models import OAuthCredentialModel

        with manager.SessionLocal() as session:
            stmt = select(OAuthCredentialModel).where(
                OAuthCredentialModel.user_email == "alice@example.com"
            )
            model = session.execute(stmt).scalar_one()
            model.last_refreshed_at = datetime.now(UTC)
            session.commit()

        # Mock provider (should NOT be called due to rate limit)
        mock_provider = AsyncMock()
        mock_provider.refresh_token = AsyncMock()
        manager.register_provider("google", mock_provider)

        # Should return the current (expired) token due to rate limit
        token = await manager.get_valid_token("google", "alice@example.com")
        assert token is not None
        mock_provider.refresh_token.assert_not_called()

    @pytest.mark.asyncio
    async def test_audit_logger_called_on_store(self, temp_db, valid_credential):
        """Audit logger should be called when storing credentials."""
        mock_audit = MagicMock()
        manager = TokenManager(db_path=temp_db, audit_logger=mock_audit)
        try:
            await manager.store_credential(
                provider="google",
                user_email="alice@example.com",
                credential=valid_credential,
            )
            mock_audit.log_event.assert_called_once()
            call_kwargs = mock_audit.log_event.call_args[1]
            assert call_kwargs["event_type"] == "credential_created"
            assert call_kwargs["actor_id"] == "alice@example.com"
        finally:
            manager.close()
            gc.collect()

    @pytest.mark.asyncio
    async def test_update_resets_token_family(self, manager, valid_credential):
        """Updating a credential should create a new token family."""
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=valid_credential,
        )

        creds1 = await manager.list_credentials()
        family1 = creds1[0]["token_family_id"]

        # Store again (upsert)
        new_cred = OAuthCredential(
            access_token="ya29.updated",
            refresh_token="1//updated_refresh",
            token_type="Bearer",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=new_cred,
        )

        creds2 = await manager.list_credentials()
        family2 = creds2[0]["token_family_id"]

        assert family1 != family2
        assert creds2[0]["rotation_counter"] == 0


class TestConcurrentRefresh:
    """Tests for per-credential asyncio lock (Fix 1)."""

    @pytest.fixture
    def temp_db(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        yield db_path
        gc.collect()
        db_path_obj = Path(db_path)
        db_path_obj.unlink(missing_ok=True)

    @pytest.fixture
    def manager(self, temp_db):
        manager = TokenManager(db_path=temp_db)
        yield manager
        manager.close()
        gc.collect()

    @pytest.mark.asyncio
    async def test_concurrent_refresh_uses_lock(self, manager):
        """3 concurrent get_valid_token calls should only call refresh once."""
        expired_cred = OAuthCredential(
            access_token="ya29.expired",
            refresh_token="1//old_refresh",
            token_type="Bearer",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=expired_cred,
        )

        mock_provider = AsyncMock()
        new_cred = OAuthCredential(
            access_token="ya29.fresh",
            refresh_token="1//new_refresh",
            token_type="Bearer",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        mock_provider.refresh_token = AsyncMock(return_value=new_cred)
        manager.register_provider("google", mock_provider)

        # Launch 3 concurrent calls
        results = await asyncio.gather(
            manager.get_valid_token("google", "alice@example.com"),
            manager.get_valid_token("google", "alice@example.com"),
            manager.get_valid_token("google", "alice@example.com"),
        )

        # All should return the fresh token
        assert all(r == "ya29.fresh" for r in results)

        # Provider refresh should be called at most once (lock + cache)
        assert mock_provider.refresh_token.call_count == 1


class TestReuseDetectionDuringRefresh:
    """Tests for reuse detection integrated into refresh flow (Fix 2)."""

    @pytest.fixture
    def temp_db(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        yield db_path
        gc.collect()
        db_path_obj = Path(db_path)
        db_path_obj.unlink(missing_ok=True)

    @pytest.fixture
    def manager(self, temp_db):
        manager = TokenManager(db_path=temp_db)
        yield manager
        manager.close()
        gc.collect()

    @pytest.mark.asyncio
    async def test_reuse_detection_during_refresh_invalidates_family(self, manager):
        """Pre-inserted old hash in history should trigger AuthenticationError."""
        expired_cred = OAuthCredential(
            access_token="ya29.expired",
            refresh_token="1//stale_refresh",
            token_type="Bearer",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=expired_cred,
        )

        # Get token family ID and pre-seed the old refresh hash in history
        # (simulates another caller already having rotated this token)
        from sqlalchemy import select
        from nexus.storage.models import OAuthCredentialModel
        from nexus.storage.models.refresh_token_history import RefreshTokenHistoryModel

        with manager.SessionLocal() as session:
            model = session.execute(
                select(OAuthCredentialModel).where(
                    OAuthCredentialModel.user_email == "alice@example.com"
                )
            ).scalar_one()
            family_id = model.token_family_id

            # Pre-insert the hash of the CURRENT refresh token into history
            # (as if another caller already rotated it)
            history_entry = RefreshTokenHistoryModel(
                token_family_id=family_id,
                credential_id=model.credential_id,
                refresh_token_hash=_hash_token("1//stale_refresh"),
                rotation_counter=0,
                zone_id="default",
                rotated_at=datetime.now(UTC),
            )
            session.add(history_entry)
            session.commit()

        # Mock provider returning a new refresh token (rotation attempt)
        mock_provider = AsyncMock()
        new_cred = OAuthCredential(
            access_token="ya29.attacker",
            refresh_token="1//attacker_new",
            token_type="Bearer",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        mock_provider.refresh_token = AsyncMock(return_value=new_cred)
        manager.register_provider("google", mock_provider)

        # Should raise AuthenticationError due to reuse detection
        with pytest.raises(AuthenticationError, match="reuse detected"):
            await manager.get_valid_token("google", "alice@example.com")

        # Credential should be revoked (family invalidated)
        creds = await manager.list_credentials()
        assert len(creds) == 0

    @pytest.mark.asyncio
    async def test_first_rotation_does_not_trigger_reuse(self, manager):
        """Normal first rotation should succeed without false positive."""
        expired_cred = OAuthCredential(
            access_token="ya29.expired",
            refresh_token="1//original_refresh",
            token_type="Bearer",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=expired_cred,
        )

        # Mock provider returning a new refresh token
        mock_provider = AsyncMock()
        new_cred = OAuthCredential(
            access_token="ya29.fresh",
            refresh_token="1//rotated_refresh",
            token_type="Bearer",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        mock_provider.refresh_token = AsyncMock(return_value=new_cred)
        manager.register_provider("google", mock_provider)

        # Should NOT raise — first rotation is legitimate
        token = await manager.get_valid_token("google", "alice@example.com")
        assert token == "ya29.fresh"

        # Verify rotation happened
        creds = await manager.list_credentials()
        assert creds[0]["rotation_counter"] == 1


class TestAuditIPAddress:
    """Tests for IP address capture in audit events (Fix 3)."""

    @pytest.fixture
    def temp_db(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        yield db_path
        gc.collect()
        db_path_obj = Path(db_path)
        db_path_obj.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_audit_logger_receives_ip_address(self, temp_db):
        """ip_address kwarg should be passed through to audit_logger.log_event."""
        mock_audit = MagicMock()
        manager = TokenManager(db_path=temp_db, audit_logger=mock_audit)
        try:
            cred = OAuthCredential(
                access_token="ya29.test",
                refresh_token="1//test",
                token_type="Bearer",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            await manager.store_credential(
                provider="google",
                user_email="alice@example.com",
                credential=cred,
                ip_address="192.168.1.42",
            )

            mock_audit.log_event.assert_called_once()
            call_kwargs = mock_audit.log_event.call_args[1]
            assert call_kwargs["ip_address"] == "192.168.1.42"
        finally:
            manager.close()
            gc.collect()

    @pytest.mark.asyncio
    async def test_audit_ip_address_defaults_to_none(self, temp_db):
        """ip_address should default to None when not provided."""
        mock_audit = MagicMock()
        manager = TokenManager(db_path=temp_db, audit_logger=mock_audit)
        try:
            cred = OAuthCredential(
                access_token="ya29.test",
                refresh_token="1//test",
                token_type="Bearer",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            await manager.store_credential(
                provider="google",
                user_email="alice@example.com",
                credential=cred,
            )

            call_kwargs = mock_audit.log_event.call_args[1]
            assert call_kwargs["ip_address"] is None
        finally:
            manager.close()
            gc.collect()


class TestHashToken:
    """Unit tests for _hash_token helper."""

    def test_hash_is_deterministic(self):
        assert _hash_token("test") == _hash_token("test")

    def test_hash_is_sha256(self):
        import hashlib
        expected = hashlib.sha256(b"test").hexdigest()
        assert _hash_token("test") == expected

    def test_different_tokens_different_hashes(self):
        assert _hash_token("token_a") != _hash_token("token_b")
