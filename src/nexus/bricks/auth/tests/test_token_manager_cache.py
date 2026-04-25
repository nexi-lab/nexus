"""Regression tests for TokenManager cache expiry handling."""

from __future__ import annotations

import asyncio
import base64
import gc
import platform
import tempfile
import time
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.auth.oauth.token_manager import TokenManager
from nexus.bricks.auth.oauth.types import OAuthCredential
from nexus.cache.inmemory import InMemoryCacheStore
from nexus.contracts.exceptions import AuthenticationError

_TEST_FERNET_KEY = base64.urlsafe_b64encode(b"token-manager-cache-test-key-123").decode()


class BlockingSetCacheStore(InMemoryCacheStore):
    def __init__(self) -> None:
        super().__init__()
        self.set_started = asyncio.Event()
        self.allow_set = asyncio.Event()

    async def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        self.set_started.set()
        await self.allow_set.wait()
        await super().set(key, value, ttl=ttl)


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
def manager(temp_db: str) -> Generator[TokenManager, None, None]:
    mgr = TokenManager(
        db_path=temp_db,
        encryption_key=_TEST_FERNET_KEY,
        cache_store=InMemoryCacheStore(),
    )
    yield mgr
    mgr.close()
    gc.collect()


@pytest.mark.asyncio
async def test_expired_nonrefreshable_token_is_not_cached(manager: TokenManager) -> None:
    credential = OAuthCredential(
        access_token="expired-token",
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
        provider="google",
        user_email="alice@example.com",
    )
    await manager.store_credential(
        provider="google",
        user_email="alice@example.com",
        credential=credential,
    )

    cache_key = manager._token_cache_key("google", "alice@example.com", "root")
    with pytest.raises(AuthenticationError, match="Token expired"):
        await manager.get_valid_token("google", "alice@example.com")

    assert manager._cache_store is not None
    assert await manager._cache_store.get(cache_key) is None


@pytest.mark.asyncio
async def test_expired_cache_entry_is_ignored_and_replaced(manager: TokenManager) -> None:
    credential = OAuthCredential(
        access_token="fresh-db-token",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=("scope.a",),
        provider="google",
        user_email="alice@example.com",
    )
    await manager.store_credential(
        provider="google",
        user_email="alice@example.com",
        credential=credential,
    )

    cache_key = manager._token_cache_key("google", "alice@example.com", "root")
    expired_cached = OAuthCredential(
        access_token="stale-cache-token",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        scopes=("scope.old",),
        provider="google",
        user_email="alice@example.com",
    )
    assert manager._cache_store is not None
    await manager._cache_store.set(cache_key, manager._encode_cached_token(expired_cached), ttl=60)

    token = await manager.get_valid_token("google", "alice@example.com")

    assert token == "fresh-db-token"
    refreshed_raw = await manager._cache_store.get(cache_key)
    assert refreshed_raw is not None
    assert refreshed_raw.decode() == "fresh-db-token"


@pytest.mark.asyncio
async def test_cache_ttl_is_capped_by_safe_remaining_lifetime(manager: TokenManager) -> None:
    credential = OAuthCredential(
        access_token="short-lived-token",
        expires_at=datetime.now(UTC) + timedelta(seconds=75),
        provider="google",
        user_email="alice@example.com",
    )
    await manager.store_credential(
        provider="google",
        user_email="alice@example.com",
        credential=credential,
    )

    cache_key = manager._token_cache_key("google", "alice@example.com", "root")
    token = await manager.get_valid_token("google", "alice@example.com")

    assert token == "short-lived-token"
    cache_store = manager._cache_store
    assert isinstance(cache_store, InMemoryCacheStore)
    cached_entry = cache_store._store.get(cache_key)
    assert cached_entry is not None
    _, expire_at = cached_entry
    assert expire_at is not None
    remaining = expire_at - time.monotonic()
    assert 0 < remaining < 20


@pytest.mark.asyncio
async def test_token_cache_uses_main_key_for_raw_token(manager: TokenManager) -> None:
    credential = OAuthCredential(
        access_token="fresh-db-token",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        provider="google",
        user_email="alice@example.com",
    )
    await manager.store_credential(
        provider="google",
        user_email="alice@example.com",
        credential=credential,
    )

    cache_key = manager._token_cache_key("google", "alice@example.com", "root")

    token = await manager.get_valid_token("google", "alice@example.com")

    assert token == "fresh-db-token"
    assert manager._cache_store is not None
    cached = await manager._cache_store.get(cache_key)
    assert cached is not None
    assert cached.decode() == "fresh-db-token"


@pytest.mark.asyncio
async def test_raw_cache_entries_are_accepted_after_db_validation(
    manager: TokenManager,
) -> None:
    credential = OAuthCredential(
        access_token="legacy-raw-token",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        provider="google",
        user_email="alice@example.com",
    )
    await manager.store_credential(
        provider="google",
        user_email="alice@example.com",
        credential=credential,
    )
    cache_key = manager._token_cache_key("google", "alice@example.com", "root")
    assert manager._cache_store is not None
    await manager._cache_store.set(cache_key, b"legacy-raw-token", ttl=60)

    token = await manager.get_valid_token("google", "alice@example.com")

    assert token == "legacy-raw-token"
    assert await manager._cache_store.get(cache_key) == b"legacy-raw-token"


@pytest.mark.asyncio
async def test_legacy_raw_cache_entry_is_rejected_when_db_credential_missing(
    manager: TokenManager,
) -> None:
    legacy_key = manager._token_cache_key("google", "alice@example.com", "root")
    assert manager._cache_store is not None
    await manager._cache_store.set(legacy_key, b"legacy-raw-token", ttl=60)

    with pytest.raises(AuthenticationError, match="No OAuth credential found"):
        await manager.get_valid_token("google", "alice@example.com")

    assert await manager._cache_store.get(legacy_key) is None


@pytest.mark.asyncio
async def test_refresh_without_scopes_preserves_cached_scope_metadata(
    manager: TokenManager,
) -> None:
    expired = OAuthCredential(
        access_token="expired-token",
        refresh_token="refresh-token",
        expires_at=datetime.now(UTC) - timedelta(hours=1),
        scopes=("scope.a", "scope.b"),
        provider="google",
        user_email="alice@example.com",
    )
    refreshed = OAuthCredential(
        access_token="fresh-token",
        refresh_token="refresh-token",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=None,
        provider="google",
        user_email="alice@example.com",
    )
    provider = MagicMock()
    provider.refresh_token = AsyncMock(return_value=refreshed)
    manager.register_provider("google", provider)
    await manager.store_credential(
        provider="google",
        user_email="alice@example.com",
        credential=expired,
    )

    first = await manager.resolve("google", "alice@example.com")
    manager._resolved_metadata.clear()
    second = await manager.resolve("google", "alice@example.com")

    assert first.access_token == "fresh-token"
    assert first.scopes == ("scope.a", "scope.b")
    assert second.access_token == "fresh-token"
    assert second.scopes == ("scope.a", "scope.b")


@pytest.mark.asyncio
async def test_revoke_during_cache_write_does_not_leave_stale_cache_entry(
    temp_db: str,
) -> None:
    cache_store = BlockingSetCacheStore()
    manager = TokenManager(
        db_path=temp_db,
        encryption_key=_TEST_FERNET_KEY,
        cache_store=cache_store,
    )
    try:
        credential = OAuthCredential(
            access_token="fresh-token",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            provider="google",
            user_email="alice@example.com",
        )
        await manager.store_credential(
            provider="google",
            user_email="alice@example.com",
            credential=credential,
        )

        token_task = asyncio.create_task(manager.get_valid_token("google", "alice@example.com"))
        await cache_store.set_started.wait()
        revoke_task = asyncio.create_task(manager.revoke_credential("google", "alice@example.com"))
        await asyncio.sleep(0)
        cache_store.allow_set.set()

        token = await token_task
        assert token == "fresh-token"
        assert await revoke_task is True

        cache_key = manager._token_cache_key("google", "alice@example.com", "root")
        assert await cache_store.get(cache_key) is None
        with pytest.raises(AuthenticationError, match="No OAuth credential found"):
            await manager.get_valid_token("google", "alice@example.com")
    finally:
        manager.close()
        gc.collect()
