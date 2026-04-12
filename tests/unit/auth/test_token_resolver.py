"""Tests for the TokenResolver seam (Issue #3737, epic #3722).

Proves that:

1. ``TokenManager`` satisfies the ``TokenResolver`` protocol structurally
   (runtime_checkable isinstance + real resolve() call against a working
   manager with a registered provider).
2. A minimal fake implementation also satisfies the protocol, confirming
   the seam is usable by Phase 1 (#3738) code that needs a stub in tests.
3. ``resolve()`` returns fresh metadata (access token + expiry + scopes)
   consistent with what ``get_valid_token()`` + ``get_credential()`` would
   return independently — i.e. it's a true wrapper with no drift.
"""

from __future__ import annotations

import gc
import platform
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.auth.oauth.token_manager import TokenManager
from nexus.bricks.auth.oauth.token_resolver import ResolvedToken, TokenResolver
from nexus.bricks.auth.oauth.types import OAuthCredential
from nexus.cache.inmemory import InMemoryCacheStore
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import AuthenticationError


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
def manager(temp_db):
    mgr = TokenManager(db_path=temp_db, cache_store=InMemoryCacheStore())
    yield mgr
    mgr.close()
    gc.collect()


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


# ---------------------------------------------------------------------------
# Protocol conformance — structural subtyping
# ---------------------------------------------------------------------------


class _FakeResolver:
    """Minimal TokenResolver used to prove the Protocol is implementable
    without inheriting from it or depending on TokenManager. Phase 1
    (#3738) will use a similar fake in its CredentialBackend tests.
    """

    def __init__(self, token: str = "fake-access-token") -> None:
        self._token = token
        self.calls: list[tuple[str, str, str]] = []

    async def resolve(
        self,
        provider: str,
        user_email: str,
        *,
        zone_id: str = ROOT_ZONE_ID,
    ) -> ResolvedToken:
        self.calls.append((provider, user_email, zone_id))
        return ResolvedToken(
            access_token=self._token,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes=("scope.a", "scope.b"),
        )


def test_fake_implementation_satisfies_protocol() -> None:
    fake = _FakeResolver()
    assert isinstance(fake, TokenResolver)


def test_token_manager_satisfies_protocol(manager: TokenManager) -> None:
    assert isinstance(manager, TokenResolver)


@pytest.mark.asyncio
async def test_fake_resolver_returns_expected_shape() -> None:
    fake = _FakeResolver("my-token")
    resolved = await fake.resolve("google", "alice@example.com")
    assert isinstance(resolved, ResolvedToken)
    assert resolved.access_token == "my-token"
    assert resolved.scopes == ("scope.a", "scope.b")
    assert resolved.expires_at is not None
    assert fake.calls == [("google", "alice@example.com", ROOT_ZONE_ID)]


# ---------------------------------------------------------------------------
# TokenManager.resolve() behavior — delegates, does not drift
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_returns_fresh_access_token(
    manager: TokenManager, valid_credential: OAuthCredential
) -> None:
    """resolve() must return the same token get_valid_token() would,
    plus consistent expires_at and scopes from get_credential()."""
    # Register a no-op provider so refresh paths exist; credential is
    # already valid so refresh_token() is never called.
    provider = MagicMock()
    provider.refresh_token = AsyncMock()
    manager.register_provider("google", provider)

    await manager.store_credential(
        provider="google",
        user_email="alice@example.com",
        credential=valid_credential,
    )

    resolved = await manager.resolve("google", "alice@example.com")

    assert resolved.access_token == valid_credential.access_token
    assert resolved.expires_at == valid_credential.expires_at
    assert resolved.scopes == valid_credential.scopes
    provider.refresh_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_honors_zone_id(
    manager: TokenManager, valid_credential: OAuthCredential
) -> None:
    """Zone isolation must survive the seam: two credentials with the same
    (provider, user_email) but different zones return distinct tokens."""
    provider = MagicMock()
    provider.refresh_token = AsyncMock()
    manager.register_provider("google", provider)

    zone_a_cred = OAuthCredential(
        access_token="token-zone-a",
        refresh_token="1//zone-a",
        token_type="Bearer",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=("scope.a",),
        provider="google",
        user_email="alice@example.com",
    )
    zone_b_cred = OAuthCredential(
        access_token="token-zone-b",
        refresh_token="1//zone-b",
        token_type="Bearer",
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        scopes=("scope.b",),
        provider="google",
        user_email="alice@example.com",
    )
    await manager.store_credential(
        provider="google",
        user_email="alice@example.com",
        credential=zone_a_cred,
        zone_id="zone-a",
    )
    await manager.store_credential(
        provider="google",
        user_email="alice@example.com",
        credential=zone_b_cred,
        zone_id="zone-b",
    )

    resolved_a = await manager.resolve("google", "alice@example.com", zone_id="zone-a")
    resolved_b = await manager.resolve("google", "alice@example.com", zone_id="zone-b")

    assert resolved_a.access_token == "token-zone-a"
    assert resolved_a.scopes == ("scope.a",)
    assert resolved_b.access_token == "token-zone-b"
    assert resolved_b.scopes == ("scope.b",)


@pytest.mark.asyncio
async def test_resolve_raises_when_no_credential(manager: TokenManager) -> None:
    with pytest.raises(AuthenticationError):
        await manager.resolve("google", "nobody@example.com")


@pytest.mark.asyncio
async def test_resolve_empty_scopes_becomes_empty_tuple(
    manager: TokenManager,
) -> None:
    """A credential stored without scopes must resolve to an empty tuple,
    not None — ResolvedToken.scopes is non-optional by contract."""
    provider = MagicMock()
    provider.refresh_token = AsyncMock()
    manager.register_provider("google", provider)

    cred_no_scopes = OAuthCredential(
        access_token="token-noscopes",
        refresh_token="1//noscopes",
        token_type="Bearer",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=None,
        provider="google",
        user_email="alice@example.com",
    )
    await manager.store_credential(
        provider="google",
        user_email="alice@example.com",
        credential=cred_no_scopes,
    )

    resolved = await manager.resolve("google", "alice@example.com")
    assert resolved.scopes == ()
    assert isinstance(resolved.scopes, tuple)


# ---------------------------------------------------------------------------
# Regression tests for adversarial-review findings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_zone_id_none_normalizes_to_root(
    manager: TokenManager, valid_credential: OAuthCredential
) -> None:
    """zone_id=None must behave identically to ROOT_ZONE_ID.

    Regression: get_valid_token() normalizes None→ROOT_ZONE_ID internally,
    but get_credential() does not. Without explicit normalization in
    resolve(), the second call fails to find the credential."""
    provider = MagicMock()
    provider.refresh_token = AsyncMock()
    manager.register_provider("google", provider)

    await manager.store_credential(
        provider="google",
        user_email="alice@example.com",
        credential=valid_credential,
    )

    resolved = await manager.resolve("google", "alice@example.com", zone_id=None)
    assert resolved.access_token == valid_credential.access_token
    assert resolved.expires_at == valid_credential.expires_at


@pytest.mark.asyncio
async def test_resolve_returns_refreshed_scopes_not_stale(
    manager: TokenManager,
) -> None:
    """When a provider returns new scopes during refresh, resolve() must
    return them — not the stale scopes from the original store_credential().

    Regression: get_valid_token()'s refresh path previously did not persist
    updated scopes to the DB model, so the follow-up get_credential() read
    returned stale data."""
    expired_no_scopes = OAuthCredential(
        access_token="ya29.expired",
        refresh_token="1//will_refresh",
        token_type="Bearer",
        expires_at=datetime.now(UTC) - timedelta(hours=1),
        scopes=None,
        provider="google",
        user_email="alice@example.com",
    )

    refreshed_with_scopes = OAuthCredential(
        access_token="ya29.fresh",
        refresh_token="1//will_refresh",
        token_type="Bearer",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=("https://www.googleapis.com/auth/gmail.readonly",),
        provider="google",
        user_email="alice@example.com",
    )

    provider = MagicMock()
    provider.refresh_token = AsyncMock(return_value=refreshed_with_scopes)
    manager.register_provider("google", provider)

    await manager.store_credential(
        provider="google",
        user_email="alice@example.com",
        credential=expired_no_scopes,
    )

    resolved = await manager.resolve("google", "alice@example.com")
    assert resolved.access_token == "ya29.fresh"
    assert resolved.scopes == ("https://www.googleapis.com/auth/gmail.readonly",)


@pytest.mark.asyncio
async def test_resolve_cache_hit_returns_correct_metadata(
    manager: TokenManager, valid_credential: OAuthCredential
) -> None:
    """On a cache hit, resolve() must still return the correct metadata
    for the requested (provider, user_email, zone_id) — not stale data
    from a previous resolve() of a different credential.

    Regression: a global _last_resolved stash cross-contaminated
    credentials on cache-hit paths."""
    provider = MagicMock()
    provider.refresh_token = AsyncMock()
    manager.register_provider("google", provider)

    cred_alice = OAuthCredential(
        access_token="token-alice",
        refresh_token="1//alice",
        token_type="Bearer",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=("scope.alice",),
        provider="google",
        user_email="alice@example.com",
    )
    cred_bob = OAuthCredential(
        access_token="token-bob",
        refresh_token="1//bob",
        token_type="Bearer",
        expires_at=datetime.now(UTC) + timedelta(hours=2),
        scopes=("scope.bob",),
        provider="google",
        user_email="bob@example.com",
    )
    await manager.store_credential(
        provider="google", user_email="alice@example.com", credential=cred_alice
    )
    await manager.store_credential(
        provider="google", user_email="bob@example.com", credential=cred_bob
    )

    resolved_alice = await manager.resolve("google", "alice@example.com")
    resolved_bob = await manager.resolve("google", "bob@example.com")
    # Second call for alice — should hit cache but still return alice's metadata
    resolved_alice_2 = await manager.resolve("google", "alice@example.com")

    assert resolved_alice.scopes == ("scope.alice",)
    assert resolved_bob.scopes == ("scope.bob",)
    assert resolved_alice_2.scopes == ("scope.alice",)
    assert resolved_alice_2.expires_at == cred_alice.expires_at


@pytest.mark.asyncio
async def test_resolve_fresh_worker_external_cache_hit(temp_db: str) -> None:
    """Simulates a fresh worker where the token cache (external) is warm
    but _resolved_metadata is empty. resolve() must fall back to
    get_credential() for metadata rather than returning (None, None).

    Regression: a global stash returned (None, None) on fresh workers."""
    cache = InMemoryCacheStore()
    mgr = TokenManager(db_path=temp_db, cache_store=cache)
    provider = MagicMock()
    provider.refresh_token = AsyncMock()
    mgr.register_provider("google", provider)

    cred = OAuthCredential(
        access_token="token-cached",
        refresh_token="1//cached",
        token_type="Bearer",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=("scope.cached",),
        provider="google",
        user_email="alice@example.com",
    )
    await mgr.store_credential(provider="google", user_email="alice@example.com", credential=cred)
    # Warm the cache via get_valid_token
    await mgr.get_valid_token("google", "alice@example.com")

    # Simulate fresh worker: clear the metadata stash but keep the cache
    mgr._resolved_metadata.clear()

    resolved = await mgr.resolve("google", "alice@example.com")
    assert resolved.access_token == "token-cached"
    assert resolved.expires_at == cred.expires_at
    assert resolved.scopes == ("scope.cached",)
    mgr.close()
