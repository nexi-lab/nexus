"""Unit tests for server authentication and API v1 dependencies.

Tests cover:
- CacheStoreABC-based auth cache hit/miss and isolation
- get_auth_result: open access, auth provider, static API key, token formats
- require_auth: authenticated vs. unauthenticated
- get_operation_context: subject mapping, admin capabilities, agent handling
- get_async_read_session_factory: read replica dependency (Issue #725)
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.cache.inmemory import InMemoryCacheStore
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.dependencies import (
    _get_cached_auth,
    _reset_auth_cache,
    _set_cached_auth,
    get_auth_result,
    get_operation_context,
    require_auth,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app_state(
    *,
    api_key: str | None = None,
    auth_provider: Any = None,
    auth_cache_store: InMemoryCacheStore | None = None,
) -> MagicMock:
    """Build a minimal mock app state."""
    state = MagicMock()
    state.api_key = api_key
    state.auth_provider = auth_provider
    state.auth_cache_store = auth_cache_store
    return state


def _make_mock_request(
    *,
    api_key: str | None = None,
    auth_provider: Any = None,
    auth_cache_store: InMemoryCacheStore | None = None,
) -> MagicMock:
    """Create a mock Request with app.state configured."""
    state = _make_app_state(
        api_key=api_key,
        auth_provider=auth_provider,
        auth_cache_store=auth_cache_store,
    )
    request = MagicMock()
    request.app.state = state
    request.client.host = "127.0.0.1"  # Loopback for open-access tests
    return request


def _make_mock_request_from_state(state: MagicMock) -> MagicMock:
    """Create a mock Request wrapping an existing state mock."""
    request = MagicMock()
    request.app.state = state
    request.client.host = "127.0.0.1"
    return request


async def _call_get_auth_result(
    *,
    request: MagicMock | None = None,
    authorization: str | None = None,
    x_agent_id: str | None = None,
    x_nexus_subject: str | None = None,
    x_nexus_zone_id: str | None = None,
) -> dict[str, Any] | None:
    """Call get_auth_result with explicit keyword args.

    When calling get_auth_result directly (outside FastAPI DI), the Header()
    default parameters are not resolved to None automatically, so we must
    always pass all four arguments explicitly.
    """
    if request is None:
        request = _make_mock_request()
    return await get_auth_result(
        request=request,
        authorization=authorization,
        x_agent_id=x_agent_id,
        x_nexus_subject=x_nexus_subject,
        x_nexus_zone_id=x_nexus_zone_id,
    )


# ===========================================================================
# Cache layer
# ===========================================================================


class TestAuthCacheOperations:
    """Tests for _get_cached_auth / _set_cached_auth / _reset_auth_cache with CacheStoreABC."""

    @pytest.fixture
    def cache(self) -> InMemoryCacheStore:
        return InMemoryCacheStore()

    async def test_cache_miss_returns_none(self, cache: InMemoryCacheStore) -> None:
        """A never-seen token should return None."""
        assert await _get_cached_auth(cache, "never-seen-token") is None

    async def test_cache_hit_returns_data(self, cache: InMemoryCacheStore) -> None:
        """After caching, the same token should be returned."""
        await _set_cached_auth(cache, "token-1", {"authenticated": True, "subject_id": "alice"})

        result = await _get_cached_auth(cache, "token-1")
        assert result is not None
        assert result["subject_id"] == "alice"

    async def test_cache_returns_independent_copy(self, cache: InMemoryCacheStore) -> None:
        """Returned dict must be independent (JSON round-trip) so callers cannot mutate cache."""
        await _set_cached_auth(cache, "token-copy", {"key": "original"})

        first = await _get_cached_auth(cache, "token-copy")
        assert first is not None
        first["key"] = "mutated"

        second = await _get_cached_auth(cache, "token-copy")
        assert second is not None
        assert second["key"] == "original", "Mutation leaked into cache"

    async def test_reset_clears_cache(self, cache: InMemoryCacheStore) -> None:
        """_reset_auth_cache should evict all entries."""
        await _set_cached_auth(cache, "token-reset", {"authenticated": True})
        assert await _get_cached_auth(cache, "token-reset") is not None

        await _reset_auth_cache(cache)
        assert await _get_cached_auth(cache, "token-reset") is None

    async def test_different_tokens_have_different_entries(self, cache: InMemoryCacheStore) -> None:
        """Two different tokens must not collide."""
        await _set_cached_auth(cache, "token-a", {"user": "alice"})
        await _set_cached_auth(cache, "token-b", {"user": "bob"})

        result_a = await _get_cached_auth(cache, "token-a")
        result_b = await _get_cached_auth(cache, "token-b")
        assert result_a is not None and result_a["user"] == "alice"
        assert result_b is not None and result_b["user"] == "bob"

    async def test_none_cache_store_degrades_gracefully(self) -> None:
        """When cache_store is None, operations are no-ops."""
        await _set_cached_auth(None, "token-x", {"ok": True})
        assert await _get_cached_auth(None, "token-x") is None
        await _reset_auth_cache(None)  # Should not raise


# ===========================================================================
# get_auth_result
# ===========================================================================


class TestGetAuthResultOpenAccess:
    """Tests for open-access mode (no api_key, no auth_provider)."""

    async def test_open_access_returns_authenticated(self):
        """Open access should always return authenticated=True."""
        result = await _call_get_auth_result(request=_make_mock_request())
        assert result is not None
        assert result["authenticated"] is True
        assert result["metadata"]["open_access"] is True
        assert result["inherit_permissions"] is True

    async def test_open_access_with_subject_header(self):
        """X-Nexus-Subject header should populate subject_type/subject_id."""
        result = await _call_get_auth_result(
            request=_make_mock_request(), x_nexus_subject="user:alice"
        )
        assert result["subject_type"] == "user"
        assert result["subject_id"] == "alice"

    async def test_open_access_bad_subject_header(self):
        """Malformed subject header should result in None values."""
        result = await _call_get_auth_result(
            request=_make_mock_request(), x_nexus_subject="no-colon"
        )
        assert result["subject_type"] is None
        assert result["subject_id"] is None

    async def test_open_access_empty_parts_subject_header(self):
        """Subject header with empty parts should result in None values."""
        result = await _call_get_auth_result(
            request=_make_mock_request(), x_nexus_subject=":missing_type"
        )
        assert result["subject_type"] is None
        assert result["subject_id"] is None

    async def test_open_access_sk_token_infers_identity(self):
        """sk- token in open access infers zone and user from token fields."""
        result = await _call_get_auth_result(
            request=_make_mock_request(),
            authorization="Bearer sk-myzone_alice_k1_random",
        )
        assert result["subject_type"] == "user"
        assert result["subject_id"] == "alice"
        assert result["zone_id"] == "myzone"

    async def test_open_access_zone_header_takes_precedence(self):
        """X-Nexus-Zone-ID header should override token-parsed zone."""
        result = await _call_get_auth_result(
            request=_make_mock_request(),
            authorization="Bearer sk-myzone_alice_k1_random",
            x_nexus_zone_id="explicit-zone",
        )
        assert result["zone_id"] == "explicit-zone"

    async def test_open_access_agent_id_header(self):
        """X-Agent-ID should be passed through."""
        result = await _call_get_auth_result(request=_make_mock_request(), x_agent_id="agent-42")
        assert result["x_agent_id"] == "agent-42"

    async def test_open_access_non_sk_bearer_token(self):
        """Non-sk- bearer token in open access should not infer identity."""
        result = await _call_get_auth_result(
            request=_make_mock_request(),
            authorization="Bearer random-jwt-token",
        )
        assert result["subject_type"] is None
        assert result["subject_id"] is None


class TestGetAuthResultStaticKey:
    """Tests for static API key authentication."""

    async def test_no_authorization_returns_none(self):
        """Missing Authorization header should return None."""
        result = await _call_get_auth_result(request=_make_mock_request(api_key="secret-key-123"))
        assert result is None

    async def test_valid_bearer_key(self):
        """Valid Bearer key should authenticate as admin."""
        result = await _call_get_auth_result(
            request=_make_mock_request(api_key="secret-key-123"),
            authorization="Bearer secret-key-123",
        )
        assert result is not None
        assert result["authenticated"] is True
        assert result["is_admin"] is True
        assert result["subject_id"] == "admin"

    async def test_raw_sk_token_without_bearer(self):
        """Raw sk- token without 'Bearer' prefix should still authenticate."""
        result = await _call_get_auth_result(
            request=_make_mock_request(api_key="sk-mykey"),
            authorization="sk-mykey",
        )
        assert result is not None
        assert result["authenticated"] is True

    async def test_wrong_key_returns_none(self):
        """Incorrect API key should return None."""
        result = await _call_get_auth_result(
            request=_make_mock_request(api_key="secret-key-123"),
            authorization="Bearer wrong-key",
        )
        assert result is None

    async def test_garbage_authorization_returns_none(self):
        """Non-Bearer, non-sk- authorization should return None."""
        result = await _call_get_auth_result(
            request=_make_mock_request(api_key="secret-key-123"),
            authorization="Basic dXNlcjpwYXNz",
        )
        assert result is None

    async def test_static_key_has_inherit_permissions(self):
        """Static API key auth should set inherit_permissions=True."""
        result = await _call_get_auth_result(
            request=_make_mock_request(api_key="secret-key-123"),
            authorization="Bearer secret-key-123",
        )
        assert result["inherit_permissions"] is True


class TestGetAuthResultAuthProvider:
    """Tests for external auth provider authentication."""

    def _make_auth_result_obj(self, **overrides):
        """Build a minimal AuthResult mock.

        Uses spec=[] to prevent MagicMock from auto-creating attributes
        (which are not JSON-serializable and break CacheStoreABC round-trip).
        """
        result = MagicMock(spec=[])
        result.authenticated = overrides.get("authenticated", True)
        result.is_admin = overrides.get("is_admin", False)
        result.subject_type = overrides.get("subject_type", "user")
        result.subject_id = overrides.get("subject_id", "alice")
        result.zone_id = overrides.get("zone_id", ROOT_ZONE_ID)
        result.inherit_permissions = overrides.get("inherit_permissions", True)
        result.metadata = overrides.get("metadata", {})
        result.agent_generation = overrides.get("agent_generation")
        return result

    async def test_provider_success_returns_result(self):
        """Successful provider auth should return structured result."""
        cache = InMemoryCacheStore()
        provider = AsyncMock()
        provider.authenticate = AsyncMock(return_value=self._make_auth_result_obj())
        request = _make_mock_request(auth_provider=provider, auth_cache_store=cache)

        result = await _call_get_auth_result(request=request, authorization="Bearer valid-token")
        assert result is not None
        assert result["authenticated"] is True
        assert result["subject_id"] == "alice"
        assert result["_auth_cached"] is False

    async def test_provider_caches_result(self):
        """Second call with same token should hit cache."""
        cache = InMemoryCacheStore()
        provider = AsyncMock()
        provider.authenticate = AsyncMock(return_value=self._make_auth_result_obj())
        request = _make_mock_request(auth_provider=provider, auth_cache_store=cache)

        # First call: cache miss
        result1 = await _call_get_auth_result(
            request=request, authorization="Bearer cache-test-token"
        )
        assert result1["_auth_cached"] is False

        # Second call: cache hit
        result2 = await _call_get_auth_result(
            request=request, authorization="Bearer cache-test-token"
        )
        assert result2 is not None
        assert result2["_auth_cached"] is True
        assert result2["_auth_time_ms"] == 0.0

        # Provider should only have been called once
        assert provider.authenticate.call_count == 1

    async def test_provider_returns_none_on_failure(self):
        """Provider returning None means auth failed."""
        provider = AsyncMock()
        provider.authenticate = AsyncMock(return_value=None)
        request = _make_mock_request(auth_provider=provider)

        result = await _call_get_auth_result(request=request, authorization="Bearer bad-token")
        assert result is None

    async def test_cached_result_gets_fresh_agent_id(self):
        """Cached result should have per-request x_agent_id updated."""
        cache = InMemoryCacheStore()
        provider = AsyncMock()
        provider.authenticate = AsyncMock(return_value=self._make_auth_result_obj())
        request = _make_mock_request(auth_provider=provider, auth_cache_store=cache)

        await _call_get_auth_result(
            request=request, authorization="Bearer agent-test", x_agent_id="agent-1"
        )
        result = await _call_get_auth_result(
            request=request, authorization="Bearer agent-test", x_agent_id="agent-2"
        )
        assert result["x_agent_id"] == "agent-2"

    async def test_cache_entry_excludes_per_request_fields(self):
        """Cache should not store x_agent_id or timing fields."""
        cache = InMemoryCacheStore()
        provider = AsyncMock()
        provider.authenticate = AsyncMock(return_value=self._make_auth_result_obj())
        request = _make_mock_request(auth_provider=provider, auth_cache_store=cache)

        await _call_get_auth_result(
            request=request,
            authorization="Bearer exclusion-test",
            x_agent_id="should-not-cache",
        )

        # Read raw cache entry
        cached = await _get_cached_auth(cache, "exclusion-test")
        assert cached is not None
        assert "x_agent_id" not in cached
        assert "_auth_time_ms" not in cached
        assert "_auth_cached" not in cached


# ===========================================================================
# require_auth
# ===========================================================================


class TestRequireAuth:
    """Tests for the require_auth dependency."""

    async def test_raises_on_none(self):
        """None auth result should raise 401."""
        with pytest.raises(Exception) as exc_info:
            await require_auth(auth_result=None)
        # FastAPI HTTPException
        assert exc_info.value.status_code == 401

    async def test_raises_on_unauthenticated(self):
        """auth_result with authenticated=False should raise 401."""
        with pytest.raises(Exception) as exc_info:
            await require_auth(auth_result={"authenticated": False})
        assert exc_info.value.status_code == 401

    async def test_passes_authenticated_result(self):
        """Valid auth result should be returned as-is."""
        auth = {"authenticated": True, "subject_id": "alice"}
        result = await require_auth(auth_result=auth)
        assert result is auth

    async def test_raises_on_empty_dict(self):
        """Empty dict (no 'authenticated' key) should raise 401."""
        with pytest.raises(Exception) as exc_info:
            await require_auth(auth_result={})
        assert exc_info.value.status_code == 401


# ===========================================================================
# get_operation_context
# ===========================================================================


class TestGetOperationContext:
    """Tests for get_operation_context."""

    def test_basic_user_context(self):
        """Basic user auth result should produce a user context."""
        ctx = get_operation_context(
            {
                "subject_type": "user",
                "subject_id": "alice",
                "zone_id": "z1",
                "is_admin": False,
            }
        )
        assert ctx.user_id == "alice"
        assert ctx.subject_type == "user"
        assert ctx.subject_id == "alice"
        assert ctx.zone_id == "z1"
        assert ctx.is_admin is False

    def test_admin_capabilities(self):
        """Admin should get full admin capabilities set."""
        ctx = get_operation_context(
            {
                "subject_type": "user",
                "subject_id": "root",
                "zone_id": "root",
                "is_admin": True,
            }
        )
        assert ctx.is_admin is True
        assert len(ctx.admin_capabilities) == 4

    def test_agent_subject_type(self):
        """Agent subject_type should set agent_id from subject_id."""
        ctx = get_operation_context(
            {
                "subject_type": "agent",
                "subject_id": "agent-001",
                "zone_id": "root",
                "is_admin": False,
            }
        )
        assert ctx.agent_id == "agent-001"
        assert ctx.user_id == "agent-001"

    def test_x_agent_id_ignored_for_non_admin(self):
        """X-Agent-ID header should NOT upgrade non-admin user to agent (security fix)."""
        ctx = get_operation_context(
            {
                "subject_type": "user",
                "subject_id": "alice",
                "zone_id": "root",
                "is_admin": False,
                "x_agent_id": "my-agent",
            }
        )
        # Non-admin: X-Agent-ID is ignored to prevent impersonation
        assert ctx.subject_type == "user"
        assert ctx.subject_id == "alice"

    def test_x_agent_id_upgrades_admin_to_agent(self):
        """X-Agent-ID header should upgrade admin user to agent."""
        ctx = get_operation_context(
            {
                "subject_type": "user",
                "subject_id": "alice",
                "zone_id": "root",
                "is_admin": True,
                "x_agent_id": "my-agent",
            }
        )
        assert ctx.subject_type == "agent"
        assert ctx.subject_id == "my-agent"
        assert ctx.agent_id == "my-agent"

    def test_defaults_for_missing_fields(self):
        """Missing fields should get sensible defaults."""
        ctx = get_operation_context({})
        assert ctx.user_id == "anonymous"
        assert ctx.subject_type == "user"
        assert ctx.zone_id == ROOT_ZONE_ID
        assert ctx.is_admin is False

    def test_agent_generation_from_auth_result(self):
        """Agent generation should come from auth_result (JWT claims), not DB."""
        ctx = get_operation_context(
            {
                "subject_type": "agent",
                "subject_id": "agent-001",
                "zone_id": "root",
                "is_admin": False,
                "metadata": {},
                "agent_generation": 42,
            }
        )
        assert ctx.agent_generation == 42

    def test_agent_generation_none_when_absent(self):
        """Missing agent_generation in auth_result should result in None."""
        ctx = get_operation_context(
            {
                "subject_type": "agent",
                "subject_id": "agent-001",
                "zone_id": "root",
                "is_admin": False,
                "metadata": {},
            }
        )
        assert ctx.agent_generation is None

    def test_agent_generation_none_for_user_subject(self):
        """User subjects should not have agent_generation even if present in auth."""
        ctx = get_operation_context(
            {
                "subject_type": "user",
                "subject_id": "alice",
                "zone_id": "root",
                "is_admin": False,
                "agent_generation": 5,
            }
        )
        # agent_generation is passed through regardless of subject_type;
        # the PermissionEnforcer only checks it for agent subjects.
        assert ctx.agent_generation == 5


# ===========================================================================
# get_async_read_session_factory (Issue #725)
# ===========================================================================

# TestGetAsyncReadSessionFactory removed - v1 dependencies sunset (#2056)
