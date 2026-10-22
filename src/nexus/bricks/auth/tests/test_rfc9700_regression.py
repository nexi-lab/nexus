"""RFC 9700 regression test: token rotation through NexusTokenManagerBackend.

This is the single highest-risk regression vector in the auth unification
epic (#3722). It verifies that:

1. A token rotated through TokenManager propagates token_family_id,
   rotation_counter, and refresh_token_hash correctly.
2. NexusTokenManagerBackend.resolve() returns the refreshed token.
3. The rotation metadata survives the backend abstraction layer.

Uses a real TokenManager with SQLite in-memory + a fake OAuth provider
that returns rotated refresh tokens (simulating Google's behavior).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet

from nexus.bricks.auth.credential_backend import NexusTokenManagerBackend
from nexus.bricks.auth.oauth.token_manager import TokenManager
from nexus.bricks.auth.oauth.types import OAuthCredential
from nexus.cache.inmemory import InMemoryCacheStore


@pytest.fixture
def manager(tmp_path: Path) -> Iterator[TokenManager]:
    mgr = TokenManager(
        db_path=tmp_path / "rotation.db",
        encryption_key=Fernet.generate_key().decode(),
        cache_store=InMemoryCacheStore(),
    )
    try:
        yield mgr
    finally:
        mgr.close()


# ---------------------------------------------------------------------------
# RFC 9700 rotation through NexusTokenManagerBackend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rotation_fields_propagate_through_backend(manager: TokenManager) -> None:
    """Store a credential, force a refresh that rotates the refresh token,
    then verify rotation metadata is correct in the DB.

    This exercises the full chain:
      NexusTokenManagerBackend.resolve()
        → TokenResolver.resolve() (= TokenManager.resolve())
          → get_valid_token() (detects expired, calls provider.refresh_token())
            → rotation detection (new refresh_token != old)
              → record_rotation() + update model
    """
    # Initial credential — already expired to trigger refresh on resolve()
    initial_cred = OAuthCredential(
        access_token="ya29.initial_expired",
        refresh_token="1//initial_refresh_token",
        token_type="Bearer",
        expires_at=datetime.now(UTC) - timedelta(hours=1),  # expired
        scopes=("https://www.googleapis.com/auth/drive",),
        provider="google",
        user_email="alice@example.com",
    )

    # Provider returns a ROTATED refresh token (different from the initial one)
    rotated_cred = OAuthCredential(
        access_token="ya29.fresh_after_rotation",
        refresh_token="1//rotated_refresh_token",  # <-- different!
        token_type="Bearer",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=("https://www.googleapis.com/auth/drive",),
        provider="google",
        user_email="alice@example.com",
    )

    fake_provider = MagicMock()
    fake_provider.refresh_token = AsyncMock(return_value=rotated_cred)
    manager.register_provider("google", fake_provider)

    # Store the initial (expired) credential
    await manager.store_credential(
        provider="google",
        user_email="alice@example.com",
        credential=initial_cred,
    )

    # Verify initial state: rotation_counter=0, token_family_id set
    from sqlalchemy import select as sa_select

    from nexus.storage.models import OAuthCredentialModel

    with manager.SessionLocal() as session:
        model = session.execute(
            sa_select(OAuthCredentialModel).where(
                OAuthCredentialModel.provider == "google",
                OAuthCredentialModel.user_email == "alice@example.com",
            )
        ).scalar_one()
        initial_family_id = model.token_family_id
        assert initial_family_id is not None
        assert model.rotation_counter == 0
        initial_refresh_hash = model.refresh_token_hash
        assert initial_refresh_hash == hashlib.sha256(b"1//initial_refresh_token").hexdigest()

    # Now resolve through NexusTokenManagerBackend — this triggers refresh + rotation
    backend = NexusTokenManagerBackend(manager)
    resolved = await backend.resolve("google/alice@example.com")

    # Verify the resolved credential has the fresh (rotated) access token
    assert resolved.kind == "bearer_token"
    assert resolved.access_token == "ya29.fresh_after_rotation"
    assert resolved.scopes == ("https://www.googleapis.com/auth/drive",)

    # Verify rotation metadata in the DB
    with manager.SessionLocal() as session:
        model = session.execute(
            sa_select(OAuthCredentialModel).where(
                OAuthCredentialModel.provider == "google",
                OAuthCredentialModel.user_email == "alice@example.com",
            )
        ).scalar_one()

        # token_family_id preserved (same family)
        assert model.token_family_id == initial_family_id

        # rotation_counter incremented
        assert model.rotation_counter == 1

        # refresh_token_hash updated to the NEW token's hash
        expected_new_hash = hashlib.sha256(b"1//rotated_refresh_token").hexdigest()
        assert model.refresh_token_hash == expected_new_hash

        # Old refresh token hash should NOT be the current one
        assert model.refresh_token_hash != initial_refresh_hash

    # Provider's refresh was called exactly once
    fake_provider.refresh_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_backend_resolve_returns_correct_scopes_after_rotation(
    manager: TokenManager,
) -> None:
    """Scopes must survive the rotation path and appear in ResolvedCredential."""
    expired = OAuthCredential(
        access_token="ya29.old",
        refresh_token="1//old",
        token_type="Bearer",
        expires_at=datetime.now(UTC) - timedelta(hours=1),
        scopes=("scope.a", "scope.b"),
        provider="google",
        user_email="bob@example.com",
    )
    refreshed = OAuthCredential(
        access_token="ya29.new",
        refresh_token="1//new",
        token_type="Bearer",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=("scope.a", "scope.b", "scope.c"),  # scopes expanded
        provider="google",
        user_email="bob@example.com",
    )

    fake_provider = MagicMock()
    fake_provider.refresh_token = AsyncMock(return_value=refreshed)
    manager.register_provider("google", fake_provider)

    await manager.store_credential(
        provider="google",
        user_email="bob@example.com",
        credential=expired,
    )

    backend = NexusTokenManagerBackend(manager)
    resolved = await backend.resolve("google/bob@example.com")

    assert resolved.scopes == ("scope.a", "scope.b", "scope.c")
    assert resolved.expires_at is not None
    assert resolved.expires_at > datetime.now(UTC)
