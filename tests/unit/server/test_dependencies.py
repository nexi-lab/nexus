"""Unit tests for server authentication dependencies.

Tests cover:
- TTLCache hit/miss and eviction
- Shallow copy behavior (mutation safety)
- _reset_auth_cache for test isolation
- get_auth_result: open access, auth provider, static API key, token formats
- require_auth: authenticated vs. unauthenticated
- get_operation_context: subject mapping, admin capabilities, agent handling
"""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.server.dependencies import (
    _AUTH_CACHE,
    _get_cached_auth,
    _reset_auth_cache,
    _set_cached_auth,
    get_auth_result,
    get_operation_context,
    require_auth,
)

# The _app_state object lives in nexus.server.fastapi_server and is imported
# lazily inside get_auth_result / get_operation_context.  We must patch it at
# its canonical location so the lazy import picks up the mock.
_APP_STATE_PATH = "nexus.server.fastapi_server._app_state"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _call_get_auth_result(
    *,
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
    return await get_auth_result(
        authorization=authorization,
        x_agent_id=x_agent_id,
        x_nexus_subject=x_nexus_subject,
        x_nexus_zone_id=x_nexus_zone_id,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_auth_cache():
    """Ensure every test starts with an empty auth cache."""
    _reset_auth_cache()
    yield
    _reset_auth_cache()


def _make_app_state(
    *,
    api_key: str | None = None,
    auth_provider: Any = None,
    agent_registry: Any = None,
) -> MagicMock:
    """Build a minimal mock _app_state."""
    state = MagicMock()
    state.api_key = api_key
    state.auth_provider = auth_provider
    state.agent_registry = agent_registry
    return state


# ===========================================================================
# Cache layer
# ===========================================================================


class TestAuthCacheOperations:
    """Tests for _get_cached_auth / _set_cached_auth / _reset_auth_cache."""

    def test_cache_miss_returns_none(self):
        """A never-seen token should return None."""
        assert _get_cached_auth("never-seen-token") is None

    def test_cache_hit_returns_data(self):
        """After caching, the same token should be returned."""
        _set_cached_auth("token-1", {"authenticated": True, "subject_id": "alice"})

        result = _get_cached_auth("token-1")
        assert result is not None
        assert result["subject_id"] == "alice"

    def test_cache_returns_shallow_copy(self):
        """Returned dict must be a copy so callers cannot mutate the cached entry."""
        _set_cached_auth("token-copy", {"key": "original"})

        first = _get_cached_auth("token-copy")
        assert first is not None
        first["key"] = "mutated"

        second = _get_cached_auth("token-copy")
        assert second is not None
        assert second["key"] == "original", "Mutation leaked into cache"

    def test_reset_clears_cache(self):
        """_reset_auth_cache should evict all entries."""
        _set_cached_auth("token-reset", {"authenticated": True})
        assert _get_cached_auth("token-reset") is not None

        _reset_auth_cache()
        assert _get_cached_auth("token-reset") is None

    def test_different_tokens_have_different_entries(self):
        """Two different tokens must not collide."""
        _set_cached_auth("token-a", {"user": "alice"})
        _set_cached_auth("token-b", {"user": "bob"})

        assert _get_cached_auth("token-a")["user"] == "alice"
        assert _get_cached_auth("token-b")["user"] == "bob"

    def test_cache_key_is_sha256_prefix(self):
        """Cache key should be the first 32 chars of the SHA-256 hex digest."""
        token = "my-secret-token"
        expected_hash = hashlib.sha256(token.encode()).hexdigest()[:32]

        _set_cached_auth(token, {"ok": True})
        assert expected_hash in _AUTH_CACHE


# ===========================================================================
# get_auth_result
# ===========================================================================


class TestGetAuthResultOpenAccess:
    """Tests for open-access mode (no api_key, no auth_provider)."""

    @patch(_APP_STATE_PATH, _make_app_state())
    async def test_open_access_returns_authenticated(self):
        """Open access should always return authenticated=True."""
        result = await _call_get_auth_result()
        assert result is not None
        assert result["authenticated"] is True
        assert result["metadata"]["open_access"] is True
        assert result["inherit_permissions"] is True

    @patch(_APP_STATE_PATH, _make_app_state())
    async def test_open_access_with_subject_header(self):
        """X-Nexus-Subject header should populate subject_type/subject_id."""
        result = await _call_get_auth_result(x_nexus_subject="user:alice")
        assert result["subject_type"] == "user"
        assert result["subject_id"] == "alice"

    @patch(_APP_STATE_PATH, _make_app_state())
    async def test_open_access_bad_subject_header(self):
        """Malformed subject header should result in None values."""
        result = await _call_get_auth_result(x_nexus_subject="no-colon")
        assert result["subject_type"] is None
        assert result["subject_id"] is None

    @patch(_APP_STATE_PATH, _make_app_state())
    async def test_open_access_empty_parts_subject_header(self):
        """Subject header with empty parts should result in None values."""
        result = await _call_get_auth_result(x_nexus_subject=":missing_type")
        assert result["subject_type"] is None
        assert result["subject_id"] is None

    @patch(_APP_STATE_PATH, _make_app_state())
    async def test_open_access_sk_token_infers_identity(self):
        """sk- token in open access infers zone and user from token fields."""
        result = await _call_get_auth_result(
            authorization="Bearer sk-myzone_alice_k1_random",
        )
        assert result["subject_type"] == "user"
        assert result["subject_id"] == "alice"
        assert result["zone_id"] == "myzone"

    @patch(_APP_STATE_PATH, _make_app_state())
    async def test_open_access_zone_header_takes_precedence(self):
        """X-Nexus-Zone-ID header should override token-parsed zone."""
        result = await _call_get_auth_result(
            authorization="Bearer sk-myzone_alice_k1_random",
            x_nexus_zone_id="explicit-zone",
        )
        assert result["zone_id"] == "explicit-zone"

    @patch(_APP_STATE_PATH, _make_app_state())
    async def test_open_access_agent_id_header(self):
        """X-Agent-ID should be passed through."""
        result = await _call_get_auth_result(x_agent_id="agent-42")
        assert result["x_agent_id"] == "agent-42"

    @patch(_APP_STATE_PATH, _make_app_state())
    async def test_open_access_non_sk_bearer_token(self):
        """Non-sk- bearer token in open access should not infer identity."""
        result = await _call_get_auth_result(
            authorization="Bearer random-jwt-token",
        )
        assert result["subject_type"] is None
        assert result["subject_id"] is None


class TestGetAuthResultStaticKey:
    """Tests for static API key authentication."""

    @patch(_APP_STATE_PATH, _make_app_state(api_key="secret-key-123"))
    async def test_no_authorization_returns_none(self):
        """Missing Authorization header should return None."""
        result = await _call_get_auth_result()
        assert result is None

    @patch(_APP_STATE_PATH, _make_app_state(api_key="secret-key-123"))
    async def test_valid_bearer_key(self):
        """Valid Bearer key should authenticate as admin."""
        result = await _call_get_auth_result(authorization="Bearer secret-key-123")
        assert result is not None
        assert result["authenticated"] is True
        assert result["is_admin"] is True
        assert result["subject_id"] == "admin"

    @patch(_APP_STATE_PATH, _make_app_state(api_key="sk-mykey"))
    async def test_raw_sk_token_without_bearer(self):
        """Raw sk- token without 'Bearer' prefix should still authenticate."""
        result = await _call_get_auth_result(authorization="sk-mykey")
        assert result is not None
        assert result["authenticated"] is True

    @patch(_APP_STATE_PATH, _make_app_state(api_key="secret-key-123"))
    async def test_wrong_key_returns_none(self):
        """Incorrect API key should return None."""
        result = await _call_get_auth_result(authorization="Bearer wrong-key")
        assert result is None

    @patch(_APP_STATE_PATH, _make_app_state(api_key="secret-key-123"))
    async def test_garbage_authorization_returns_none(self):
        """Non-Bearer, non-sk- authorization should return None."""
        result = await _call_get_auth_result(authorization="Basic dXNlcjpwYXNz")
        assert result is None

    @patch(_APP_STATE_PATH, _make_app_state(api_key="secret-key-123"))
    async def test_static_key_has_inherit_permissions(self):
        """Static API key auth should set inherit_permissions=True."""
        result = await _call_get_auth_result(authorization="Bearer secret-key-123")
        assert result["inherit_permissions"] is True


class TestGetAuthResultAuthProvider:
    """Tests for external auth provider authentication."""

    def _make_auth_result_obj(self, **overrides):
        """Build a minimal AuthResult mock."""
        result = MagicMock()
        result.authenticated = overrides.get("authenticated", True)
        result.is_admin = overrides.get("is_admin", False)
        result.subject_type = overrides.get("subject_type", "user")
        result.subject_id = overrides.get("subject_id", "alice")
        result.zone_id = overrides.get("zone_id", "default")
        result.inherit_permissions = overrides.get("inherit_permissions", True)
        result.metadata = overrides.get("metadata", {})
        return result

    async def test_provider_success_returns_result(self):
        """Successful provider auth should return structured result."""
        provider = AsyncMock()
        provider.authenticate = AsyncMock(return_value=self._make_auth_result_obj())
        state = _make_app_state(auth_provider=provider)

        with patch(_APP_STATE_PATH, state):
            result = await _call_get_auth_result(authorization="Bearer valid-token")
        assert result is not None
        assert result["authenticated"] is True
        assert result["subject_id"] == "alice"
        assert result["_auth_cached"] is False

    async def test_provider_caches_result(self):
        """Second call with same token should hit cache."""
        provider = AsyncMock()
        provider.authenticate = AsyncMock(return_value=self._make_auth_result_obj())
        state = _make_app_state(auth_provider=provider)

        with patch(_APP_STATE_PATH, state):
            # First call: cache miss
            result1 = await _call_get_auth_result(authorization="Bearer cache-test-token")
            assert result1["_auth_cached"] is False

            # Second call: cache hit
            result2 = await _call_get_auth_result(authorization="Bearer cache-test-token")
            assert result2 is not None
            assert result2["_auth_cached"] is True
            assert result2["_auth_time_ms"] == 0.0

        # Provider should only have been called once
        assert provider.authenticate.call_count == 1

    async def test_provider_returns_none_on_failure(self):
        """Provider returning None means auth failed."""
        provider = AsyncMock()
        provider.authenticate = AsyncMock(return_value=None)
        state = _make_app_state(auth_provider=provider)

        with patch(_APP_STATE_PATH, state):
            result = await _call_get_auth_result(authorization="Bearer bad-token")
        assert result is None

    async def test_cached_result_gets_fresh_agent_id(self):
        """Cached result should have per-request x_agent_id updated."""
        provider = AsyncMock()
        provider.authenticate = AsyncMock(return_value=self._make_auth_result_obj())
        state = _make_app_state(auth_provider=provider)

        with patch(_APP_STATE_PATH, state):
            await _call_get_auth_result(authorization="Bearer agent-test", x_agent_id="agent-1")
            result = await _call_get_auth_result(
                authorization="Bearer agent-test", x_agent_id="agent-2"
            )
        assert result["x_agent_id"] == "agent-2"

    async def test_cache_entry_excludes_per_request_fields(self):
        """Cache should not store x_agent_id or timing fields."""
        provider = AsyncMock()
        provider.authenticate = AsyncMock(return_value=self._make_auth_result_obj())
        state = _make_app_state(auth_provider=provider)

        with patch(_APP_STATE_PATH, state):
            await _call_get_auth_result(
                authorization="Bearer exclusion-test", x_agent_id="should-not-cache"
            )

        # Read raw cache entry
        cached = _get_cached_auth("exclusion-test")
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

    @patch(_APP_STATE_PATH, _make_app_state())
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
        assert ctx.user == "alice"
        assert ctx.subject_type == "user"
        assert ctx.subject_id == "alice"
        assert ctx.zone_id == "z1"
        assert ctx.is_admin is False

    @patch(_APP_STATE_PATH, _make_app_state())
    def test_admin_capabilities(self):
        """Admin should get full admin capabilities set."""
        ctx = get_operation_context(
            {
                "subject_type": "user",
                "subject_id": "root",
                "zone_id": "default",
                "is_admin": True,
            }
        )
        assert ctx.is_admin is True
        assert len(ctx.admin_capabilities) == 4

    @patch(_APP_STATE_PATH, _make_app_state())
    def test_agent_subject_type(self):
        """Agent subject_type should set agent_id from subject_id."""
        ctx = get_operation_context(
            {
                "subject_type": "agent",
                "subject_id": "agent-001",
                "zone_id": "default",
                "is_admin": False,
                "metadata": {"legacy_user_id": "alice"},
            }
        )
        assert ctx.agent_id == "agent-001"
        assert ctx.user == "alice"  # From legacy_user_id

    @patch(_APP_STATE_PATH, _make_app_state())
    def test_x_agent_id_upgrades_user_to_agent(self):
        """X-Agent-ID header should upgrade user subject to agent."""
        ctx = get_operation_context(
            {
                "subject_type": "user",
                "subject_id": "alice",
                "zone_id": "default",
                "is_admin": False,
                "x_agent_id": "my-agent",
            }
        )
        assert ctx.subject_type == "agent"
        assert ctx.subject_id == "my-agent"
        assert ctx.agent_id == "my-agent"

    @patch(_APP_STATE_PATH, _make_app_state())
    def test_defaults_for_missing_fields(self):
        """Missing fields should get sensible defaults."""
        ctx = get_operation_context({})
        assert ctx.user == "anonymous"
        assert ctx.subject_type == "user"
        assert ctx.zone_id == "default"
        assert ctx.is_admin is False

    def test_agent_generation_from_registry(self):
        """Agent generation should be looked up from agent registry."""
        registry = MagicMock()
        record = MagicMock()
        record.generation = 42
        registry.get.return_value = record
        state = _make_app_state(agent_registry=registry)

        with patch(_APP_STATE_PATH, state):
            ctx = get_operation_context(
                {
                    "subject_type": "agent",
                    "subject_id": "agent-001",
                    "zone_id": "default",
                    "is_admin": False,
                    "metadata": {},
                }
            )
        assert ctx.agent_generation == 42

    def test_agent_generation_none_when_not_found(self):
        """Missing agent record should result in None generation."""
        registry = MagicMock()
        registry.get.return_value = None
        state = _make_app_state(agent_registry=registry)

        with patch(_APP_STATE_PATH, state):
            ctx = get_operation_context(
                {
                    "subject_type": "agent",
                    "subject_id": "unknown-agent",
                    "zone_id": "default",
                    "is_admin": False,
                    "metadata": {},
                }
            )
        assert ctx.agent_generation is None

    def test_agent_generation_exception_handled(self):
        """Registry exception should be caught and generation set to None."""
        registry = MagicMock()
        registry.get.side_effect = RuntimeError("DB down")
        state = _make_app_state(agent_registry=registry)

        with patch(_APP_STATE_PATH, state):
            ctx = get_operation_context(
                {
                    "subject_type": "agent",
                    "subject_id": "agent-001",
                    "zone_id": "default",
                    "is_admin": False,
                    "metadata": {},
                }
            )
        assert ctx.agent_generation is None
