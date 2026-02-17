"""Unit tests for AuthService (Decision #7)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import nexus.auth.service as _svc_mod
from nexus.auth.cache import AuthCache
from nexus.auth.providers.base import AuthProvider, AuthResult
from nexus.auth.service import AuthService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class StubProvider(AuthProvider):
    """Stub provider returning configurable results."""

    def __init__(self, result: AuthResult | None = None) -> None:
        self._result = result or AuthResult(
            authenticated=True,
            subject_type="user",
            subject_id="alice",
            zone_id="org_acme",
            is_admin=False,
        )

    async def authenticate(self, _token: str) -> AuthResult:
        return self._result

    async def validate_token(self, _token: str) -> bool:
        return self._result.authenticated

    def close(self) -> None:
        pass

@pytest.fixture
def provider():
    return StubProvider()

@pytest.fixture
def cache():
    return AuthCache(ttl=60, max_size=100)

@pytest.fixture
def service(provider, cache):
    return AuthService(provider=provider, cache=cache)

# ---------------------------------------------------------------------------
# authenticate() tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_authenticate_success(service):
    """Successful authentication returns correct AuthResult."""
    result = await service.authenticate("sk-valid-token")
    assert result.authenticated is True
    assert result.subject_id == "alice"
    assert result.zone_id == "org_acme"

@pytest.mark.asyncio
async def test_authenticate_empty_token(service):
    """Empty token returns unauthenticated without hitting provider."""
    result = await service.authenticate("")
    assert result.authenticated is False

@pytest.mark.asyncio
async def test_authenticate_caches_result(service, cache):
    """Successful auth result is cached."""
    await service.authenticate("sk-cached-token")

    cached = cache.get("sk-cached-token")
    assert cached is not None
    assert cached["subject_id"] == "alice"
    assert cached["authenticated"] is True

@pytest.mark.asyncio
async def test_authenticate_cache_hit_skips_provider():
    """On cache hit, provider.authenticate is NOT called."""
    mock_provider = MagicMock(spec=AuthProvider)
    mock_provider.authenticate = AsyncMock()

    cache = AuthCache(ttl=60, max_size=100)
    cache.set(
        "sk-preloaded",
        {
            "authenticated": True,
            "subject_type": "user",
            "subject_id": "cached_user",
            "zone_id": "org_cached",
            "is_admin": False,
            "metadata": None,
            "agent_generation": None,
            "inherit_permissions": True,
        },
    )

    svc = AuthService(provider=mock_provider, cache=cache)
    result = await svc.authenticate("sk-preloaded")

    assert result.authenticated is True
    assert result.subject_id == "cached_user"
    mock_provider.authenticate.assert_not_called()

@pytest.mark.asyncio
async def test_authenticate_failed_not_cached():
    """Failed authentication is NOT cached."""
    provider = StubProvider(result=AuthResult(authenticated=False))
    cache = AuthCache(ttl=60, max_size=100)
    svc = AuthService(provider=provider, cache=cache)

    await svc.authenticate("sk-bad-token")

    assert cache.get("sk-bad-token") is None

# ---------------------------------------------------------------------------
# validate_token() tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_validate_token_delegates(service):
    """validate_token delegates to provider."""
    assert await service.validate_token("sk-any-token") is True

@pytest.mark.asyncio
async def test_validate_token_rejected():
    """validate_token returns False for failed auth."""
    provider = StubProvider(result=AuthResult(authenticated=False))
    svc = AuthService(provider=provider)
    assert await svc.validate_token("sk-bad") is False

# ---------------------------------------------------------------------------
# invalidate_cached_token() tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invalidate_cached_token(service, cache):
    """invalidate_cached_token removes entry from cache."""
    await service.authenticate("sk-to-revoke")
    assert cache.get("sk-to-revoke") is not None

    service.invalidate_cached_token("sk-to-revoke")
    assert cache.get("sk-to-revoke") is None

def test_invalidate_nonexistent_token(service):
    """invalidate_cached_token on missing key does not raise."""
    service.invalidate_cached_token("sk-ghost")

# ---------------------------------------------------------------------------
# setup_zone() tests
# ---------------------------------------------------------------------------

def test_setup_zone_personal_email(monkeypatch):
    """setup_zone with personal email creates a personal zone."""
    provider = StubProvider()
    svc = AuthService(provider=provider)

    mock_session = MagicMock()
    mock_zone = MagicMock()
    mock_zone.zone_id = "alice-personal"
    mock_zone.name = "Alice's Zone"

    monkeypatch.setattr(
        _svc_mod,
        "get_zone_strategy_from_email",
        lambda _email: ("alice", "Alice's Zone", "gmail.com", True),
    )
    monkeypatch.setattr(_svc_mod, "suggest_zone_id", lambda _base_slug, _session: "alice-personal")
    create_calls = []
    monkeypatch.setattr(
        _svc_mod,
        "create_zone",
        lambda **_kw: (create_calls.append(_kw), mock_zone)[1],
    )

    result = svc.setup_zone(mock_session, "alice@gmail.com")

    assert result["zone_id"] == "alice-personal"
    assert result["zone_name"] == "Alice's Zone"
    assert result["is_personal"] is True
    assert result["domain"] == "gmail.com"
    assert len(create_calls) == 1

def test_setup_zone_work_email(monkeypatch):
    """setup_zone with work email creates a company zone."""
    provider = StubProvider()
    svc = AuthService(provider=provider)

    mock_session = MagicMock()
    mock_zone = MagicMock()
    mock_zone.zone_id = "acme"
    mock_zone.name = "Acme Corp"

    monkeypatch.setattr(
        _svc_mod,
        "get_zone_strategy_from_email",
        lambda _email: ("acme", "Acme Corp", "acme.com", False),
    )
    monkeypatch.setattr(_svc_mod, "suggest_zone_id", lambda _base_slug, _session: "acme")
    monkeypatch.setattr(_svc_mod, "create_zone", lambda **_kw: mock_zone)

    result = svc.setup_zone(mock_session, "bob@acme.com")

    assert result["zone_id"] == "acme"
    assert result["zone_name"] == "Acme Corp"
    assert result["is_personal"] is False
    assert result["domain"] == "acme.com"

def test_setup_zone_with_overrides(monkeypatch):
    """setup_zone respects zone_id and zone_name overrides."""
    provider = StubProvider()
    svc = AuthService(provider=provider)

    mock_session = MagicMock()
    mock_zone = MagicMock()
    mock_zone.zone_id = "custom-zone"
    mock_zone.name = "Custom Name"

    monkeypatch.setattr(
        _svc_mod,
        "get_zone_strategy_from_email",
        lambda _email: ("auto", "Auto Name", "example.com", False),
    )
    suggest_calls = []
    monkeypatch.setattr(
        _svc_mod,
        "suggest_zone_id",
        lambda _base_slug, _session: suggest_calls.append(1) or "unused",
    )
    monkeypatch.setattr(_svc_mod, "create_zone", lambda **_kw: mock_zone)

    result = svc.setup_zone(
        mock_session,
        "user@example.com",
        zone_id_override="custom-zone",
        zone_name_override="Custom Name",
    )

    assert result["zone_id"] == "custom-zone"
    assert result["zone_name"] == "Custom Name"
    # suggest_zone_id should NOT be called when override is provided
    assert len(suggest_calls) == 0

# ---------------------------------------------------------------------------
# close() tests
# ---------------------------------------------------------------------------

def test_close_clears_cache_and_provider():
    """close() cleans up provider and cache."""
    mock_provider = MagicMock(spec=AuthProvider)
    cache = AuthCache(ttl=60, max_size=100)
    cache.set("tok", {"data": True})

    svc = AuthService(provider=mock_provider, cache=cache)
    svc.close()

    mock_provider.close.assert_called_once()
    assert cache.size == 0

def test_default_cache_created():
    """AuthService creates a default cache if none provided."""
    provider = StubProvider()
    svc = AuthService(provider=provider)
    assert svc.cache is not None
    assert isinstance(svc.cache, AuthCache)

def test_provider_property(service, provider):
    """provider property returns the underlying provider."""
    assert service.provider is provider
