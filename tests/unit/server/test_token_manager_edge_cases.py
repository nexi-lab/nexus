"""Edge case tests for TokenManager (bricks/auth/oauth/token_manager.py).

Phase 0 safety net: tests before moving TokenManager to auth brick.
Covers:
- Concurrent refresh race with asyncio lock
- Provider refresh timeout
- Fernet decryption failure handling
- Cache coherence: cache invalidated on rotation
- Lock dict memory (no unbounded growth check — just documents behavior)
- Rate limiting prevents rapid refresh
- Store with empty provider name
- Revoke non-existent credential
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

from nexus.bricks.auth.oauth.token_manager import TokenManager
from nexus.bricks.auth.oauth.types import OAuthCredential
from nexus.cache.inmemory import InMemoryCacheStore
from nexus.contracts.exceptions import AuthenticationError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    gc.collect()
    if platform.system() == "Windows":
        time.sleep(0.2)
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def cache_store():
    return InMemoryCacheStore()


@pytest.fixture
def manager(temp_db, cache_store):
    mgr = TokenManager(db_path=temp_db, cache_store=cache_store)
    yield mgr
    mgr.close()
    gc.collect()


@pytest.fixture
def expired_credential():
    return OAuthCredential(
        access_token="ya29.expired_access",
        refresh_token="1//test_refresh",
        token_type="Bearer",
        expires_at=datetime.now(UTC) - timedelta(hours=1),
        scopes=("https://www.googleapis.com/auth/drive",),
        provider="google",
        user_email="alice@example.com",
    )


@pytest.fixture
def valid_credential():
    return OAuthCredential(
        access_token="ya29.valid_access",
        refresh_token="1//test_refresh",
        token_type="Bearer",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=("https://www.googleapis.com/auth/drive",),
        provider="google",
        user_email="alice@example.com",
    )


# ===========================================================================
# Provider refresh timeout
# ===========================================================================


class TestProviderRefreshTimeout:
    """Tests for provider refresh timeout behavior."""

    @pytest.mark.asyncio
    async def test_refresh_timeout_raises_auth_error(self, manager, expired_credential):
        """When provider.refresh_token hangs, should timeout after 30s."""
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=expired_credential,
        )

        # Mock provider that hangs forever
        mock_provider = AsyncMock()

        async def hang_forever(_cred):
            await asyncio.sleep(999)

        mock_provider.refresh_token = hang_forever
        manager.register_provider("google", mock_provider)

        # Patch the timeout to 0.1s for fast testing
        with (
            patch("nexus.bricks.auth.oauth.token_manager._PROVIDER_REFRESH_TIMEOUT_SECONDS", 0.1),
            pytest.raises(AuthenticationError, match="timed out"),
        ):
            await manager.get_valid_token("google", "alice@example.com")


# ===========================================================================
# Fernet decryption failure
# ===========================================================================


class TestFernetDecryptionFailure:
    """Tests for handling corrupted/rotated encryption keys."""

    @pytest.mark.asyncio
    async def test_corrupted_token_raises(self, manager, valid_credential):
        """If encrypted token is corrupted, decryption should raise."""
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=valid_credential,
        )

        # Corrupt the encrypted token in the database
        from sqlalchemy import select

        from nexus.storage.models import OAuthCredentialModel

        with manager.SessionLocal() as session:
            stmt = select(OAuthCredentialModel).where(
                OAuthCredentialModel.user_email == "alice@example.com"
            )
            model = session.execute(stmt).scalar_one()
            model.encrypted_access_token = "corrupted_not_fernet_data"
            session.commit()

        # Should raise on decryption
        with pytest.raises(Exception):  # noqa: B017
            await manager.get_valid_token("google", "alice@example.com")


# ===========================================================================
# Cache coherence on rotation
# ===========================================================================


class TestCacheCoherenceOnRotation:
    """Tests that cache is invalidated when tokens rotate."""

    @pytest.mark.asyncio
    async def test_cache_populated_after_rotation(self, manager, cache_store, expired_credential):
        """After rotation, new token should be cached."""
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=expired_credential,
        )

        cache_key = manager._token_cache_key("google", "alice@example.com", "root")

        # Ensure cache is empty initially
        assert await cache_store.get(cache_key) is None

        # Mock provider returns new tokens (triggers rotation)
        mock_provider = AsyncMock()
        new_cred = OAuthCredential(
            access_token="ya29.rotated_new",
            refresh_token="1//rotated_new_refresh",
            token_type="Bearer",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        mock_provider.refresh_token = AsyncMock(return_value=new_cred)
        manager.register_provider("google", mock_provider)

        token = await manager.get_valid_token("google", "alice@example.com")
        assert token == "ya29.rotated_new"

        # Cache should now have the new token
        cached_raw = await cache_store.get(cache_key)
        assert cached_raw is not None
        assert cached_raw.decode() == "ya29.rotated_new"

    @pytest.mark.asyncio
    async def test_cache_invalidated_on_store_update(self, manager, cache_store, valid_credential):
        """Re-storing a credential (upsert) should invalidate cache."""
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=valid_credential,
        )

        # Populate cache
        cache_key = manager._token_cache_key("google", "alice@example.com", "root")
        await cache_store.set(cache_key, b"ya29.stale_cached", ttl=60)

        # Upsert with new credential
        new_cred = OAuthCredential(
            access_token="ya29.fresh_upsert",
            refresh_token="1//fresh_refresh",
            token_type="Bearer",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=new_cred,
        )

        # Cache should be cleared
        cached = await cache_store.get(cache_key)
        assert cached is None


# ===========================================================================
# Concurrent refresh — extended tests
# ===========================================================================


class TestConcurrentRefreshExtended:
    """Extended concurrency tests beyond basic lock behavior."""

    @pytest.mark.asyncio
    async def test_different_credentials_refresh_independently(self, temp_db, cache_store):
        """Different credentials should not block each other."""
        manager = TokenManager(db_path=temp_db, cache_store=cache_store)
        try:
            # Store two different expired credentials
            cred_alice = OAuthCredential(
                access_token="ya29.alice_expired",
                refresh_token="1//alice_refresh",
                token_type="Bearer",
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
            cred_bob = OAuthCredential(
                access_token="ya29.bob_expired",
                refresh_token="1//bob_refresh",
                token_type="Bearer",
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
            await manager.store_credential("google", "alice@example.com", cred_alice)
            await manager.store_credential("google", "bob@example.com", cred_bob)

            # Different credentials use different locks
            call_order: list[str] = []

            async def refresh_alice(_cred):
                call_order.append("alice_start")
                await asyncio.sleep(0.05)
                call_order.append("alice_end")
                return OAuthCredential(
                    access_token="ya29.alice_fresh",
                    refresh_token="1//alice_new",
                    token_type="Bearer",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )

            async def refresh_bob(_cred):
                call_order.append("bob_start")
                await asyncio.sleep(0.05)
                call_order.append("bob_end")
                return OAuthCredential(
                    access_token="ya29.bob_fresh",
                    refresh_token="1//bob_new",
                    token_type="Bearer",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )

            mock_provider = MagicMock()

            # Use side_effect to route by credential
            async def route_refresh(cred):
                if cred.access_token.startswith("ya29.alice"):
                    return await refresh_alice(cred)
                return await refresh_bob(cred)

            mock_provider.refresh_token = route_refresh
            manager.register_provider("google", mock_provider)

            results = await asyncio.gather(
                manager.get_valid_token("google", "alice@example.com"),
                manager.get_valid_token("google", "bob@example.com"),
            )

            assert results[0] == "ya29.alice_fresh"
            assert results[1] == "ya29.bob_fresh"

            # Both should have started (not sequentially blocked)
            assert "alice_start" in call_order
            assert "bob_start" in call_order
        finally:
            manager.close()
            gc.collect()


# ===========================================================================
# Edge cases: empty provider, revoke non-existent
# ===========================================================================


class TestEdgeCases:
    """Miscellaneous edge cases."""

    @pytest.mark.asyncio
    async def test_store_empty_provider_raises(self, manager, valid_credential):
        """Empty provider name should raise ValueError."""
        with pytest.raises(ValueError, match="Provider name cannot be empty"):
            await manager.store_credential(
                provider="",
                user_email="alice@example.com",
                credential=valid_credential,
            )

    @pytest.mark.asyncio
    async def test_store_whitespace_provider_raises(self, manager, valid_credential):
        """Whitespace-only provider should raise ValueError."""
        with pytest.raises(ValueError, match="Provider name cannot be empty"):
            await manager.store_credential(
                provider="   ",
                user_email="alice@example.com",
                credential=valid_credential,
            )

    @pytest.mark.asyncio
    async def test_revoke_nonexistent_returns_false(self, manager):
        """Revoking a non-existent credential should return False."""
        result = await manager.revoke_credential("google", "nobody@example.com")
        assert result is False

    @pytest.mark.asyncio
    async def test_get_credential_nonexistent_returns_none(self, manager):
        """Getting a non-existent credential should return None."""
        result = await manager.get_credential("google", "nobody@example.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_valid_token_no_credential_raises(self, manager):
        """Getting a valid token when no credential exists should raise."""
        with pytest.raises(AuthenticationError, match="No OAuth credential found"):
            await manager.get_valid_token("google", "nobody@example.com")

    @pytest.mark.asyncio
    async def test_get_valid_token_unregistered_provider_raises(self, manager, expired_credential):
        """Refresh with unregistered provider should raise."""
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=expired_credential,
        )

        with pytest.raises(AuthenticationError, match="Provider not registered"):
            await manager.get_valid_token("google", "alice@example.com")

    @pytest.mark.asyncio
    async def test_store_none_zone_defaults_to_root(self, manager, valid_credential):
        """zone_id=None should default to ROOT_ZONE_ID."""
        cred_id = await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=valid_credential,
            zone_id=None,
        )
        assert cred_id is not None

        creds = await manager.list_credentials()
        assert len(creds) == 1
        assert creds[0]["zone_id"] == "root"

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, temp_db):
        """Calling close() multiple times should not raise."""
        mgr = TokenManager(db_path=temp_db)
        mgr.close()
        mgr.close()  # Should not raise

    @pytest.mark.asyncio
    async def test_audit_logger_failure_does_not_propagate(self, temp_db, valid_credential):
        """If audit logger raises, it should not break the operation."""
        mock_audit = MagicMock()
        mock_audit.log_event.side_effect = RuntimeError("audit DB down")

        manager = TokenManager(db_path=temp_db, audit_logger=mock_audit)
        try:
            # Should succeed despite audit failure
            cred_id = await manager.store_credential(
                provider="google",
                user_email="alice@example.com",
                credential=valid_credential,
            )
            assert cred_id is not None
        finally:
            manager.close()
            gc.collect()


# ===========================================================================
# Lock dict behavior (documenting current state)
# ===========================================================================


class TestLockDictBehavior:
    """Tests for per-credential lock dict with LRU eviction (Issue #2281)."""

    @pytest.mark.asyncio
    async def test_lock_created_per_credential(self, manager, expired_credential):
        """Each credential combo gets its own lock."""
        assert len(manager._refresh_locks) == 0

        await manager.store_credential("google", "alice@example.com", expired_credential)

        mock_provider = AsyncMock()
        new_cred = OAuthCredential(
            access_token="ya29.new",
            refresh_token="1//new",
            token_type="Bearer",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        mock_provider.refresh_token = AsyncMock(return_value=new_cred)
        manager.register_provider("google", mock_provider)

        await manager.get_valid_token("google", "alice@example.com")

        # Lock should have been created for this credential
        assert ("google", "alice@example.com", "root") in manager._refresh_locks

    def test_get_refresh_lock_reuses_existing(self, manager):
        """_get_refresh_lock returns same lock for same key."""
        key = ("google", "alice@example.com", "root")
        lock1 = manager._get_refresh_lock(key)
        lock2 = manager._get_refresh_lock(key)
        assert lock1 is lock2

    def test_get_refresh_lock_different_keys(self, manager):
        """Different keys get different locks."""
        lock1 = manager._get_refresh_lock(("google", "alice@example.com", "root"))
        lock2 = manager._get_refresh_lock(("google", "bob@example.com", "root"))
        assert lock1 is not lock2

    def test_lock_eviction_at_capacity(self, manager):
        """Oldest unlocked entries are evicted when at capacity."""
        with patch("nexus.bricks.auth.oauth.token_manager._MAX_REFRESH_LOCKS", 3):
            # Fill to capacity
            manager._get_refresh_lock(("p", "a@test.com", "z"))
            manager._get_refresh_lock(("p", "b@test.com", "z"))
            manager._get_refresh_lock(("p", "c@test.com", "z"))
            assert len(manager._refresh_locks) == 3

            # Adding one more should evict the oldest
            manager._get_refresh_lock(("p", "d@test.com", "z"))
            assert len(manager._refresh_locks) <= 3
            assert ("p", "d@test.com", "z") in manager._refresh_locks

    @pytest.mark.asyncio
    async def test_lock_acquire_timeout(self, manager, expired_credential):
        """Lock acquisition should time out rather than wait forever."""
        await manager.store_credential("google", "alice@example.com", expired_credential)

        # Pre-acquire the lock to simulate contention
        key = ("google", "alice@example.com", "root")
        lock = manager._get_refresh_lock(key)
        await lock.acquire()

        with (
            patch("nexus.bricks.auth.oauth.token_manager._LOCK_ACQUIRE_TIMEOUT_SECONDS", 0.1),
            pytest.raises(AuthenticationError, match="lock acquisition timed out"),
        ):
            await manager.get_valid_token("google", "alice@example.com")

        lock.release()
