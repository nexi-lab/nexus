"""Regression tests for TokenManager credential listing filters."""

from __future__ import annotations

import base64
import gc
import platform
import tempfile
import time
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nexus.bricks.auth.oauth.token_manager import TokenManager
from nexus.bricks.auth.oauth.types import OAuthCredential
from nexus.cache.inmemory import InMemoryCacheStore

_TEST_FERNET_KEY = base64.urlsafe_b64encode(b"token-manager-cache-test-key-123").decode()


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
async def test_list_credentials_include_revoked_surfaces_revoked_rows(
    manager: TokenManager,
) -> None:
    credential = OAuthCredential(
        access_token="ya29.revocable",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        provider="google",
        user_email="alice@example.com",
    )
    await manager.store_credential(
        provider="google",
        user_email="alice@example.com",
        credential=credential,
    )
    await manager.revoke_credential("google", "alice@example.com")

    visible_default = await manager.list_credentials(user_email="alice@example.com")
    visible_with_revoked = await manager.list_credentials(
        user_email="alice@example.com",
        include_revoked=True,
    )

    assert visible_default == []
    assert len(visible_with_revoked) == 1
    assert visible_with_revoked[0]["revoked"] is True
    assert visible_with_revoked[0]["revoked_at"] is not None


@pytest.mark.asyncio
async def test_reactivated_credential_clears_revoked_at(manager: TokenManager) -> None:
    initial = OAuthCredential(
        access_token="ya29.initial",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        provider="google",
        user_email="alice@example.com",
    )
    refreshed = OAuthCredential(
        access_token="ya29.reactivated",
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        provider="google",
        user_email="alice@example.com",
    )
    await manager.store_credential(
        provider="google",
        user_email="alice@example.com",
        credential=initial,
    )
    await manager.revoke_credential("google", "alice@example.com")
    await manager.store_credential(
        provider="google",
        user_email="alice@example.com",
        credential=refreshed,
    )

    visible = await manager.list_credentials(
        user_email="alice@example.com",
        include_revoked=True,
    )

    assert len(visible) == 1
    assert visible[0]["revoked"] is False
    assert visible[0]["revoked_at"] is None


@pytest.mark.asyncio
async def test_revoke_without_registered_provider_returns_success(manager: TokenManager) -> None:
    credential = OAuthCredential(
        access_token="ya29.noprovider",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        provider="google",
        user_email="alice@example.com",
    )
    await manager.store_credential(
        provider="google",
        user_email="alice@example.com",
        credential=credential,
    )

    revoked = await manager.revoke_credential("google", "alice@example.com")
    visible = await manager.list_credentials(
        user_email="alice@example.com",
        include_revoked=True,
    )

    assert revoked is True
    assert len(visible) == 1
    assert visible[0]["revoked"] is True
