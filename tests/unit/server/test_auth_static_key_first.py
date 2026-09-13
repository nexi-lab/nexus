"""The static ``NEXUS_API_KEY`` is checked before the auth provider (#4777).

Every request carrying the operator key used to pay a provider (database)
round-trip that could never be cached — static-key results are not cached —
and showed up as a permanent 100 % "(cache miss)" rate.  Now the constant-time
compare runs first; provider keys still go through the provider + cache.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from nexus.contracts.cache_store import InMemoryCacheStore
from nexus.server import dependencies as deps

STATIC = "sk-static-operator-key-0123456789"


class _Provider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def authenticate(self, token: str) -> Any:
        self.calls.append(token)
        if token == "sk-db-user-key":
            return SimpleNamespace(
                authenticated=True,
                is_admin=False,
                subject_type="user",
                subject_id="alice",
                zone_id="root",
                zone_set=(),
                zone_perms=(),
                inherit_permissions=True,
                metadata={},
                agent_generation=None,
            )
        return None


def _state(provider: _Provider, cache: Any = None) -> Any:
    return SimpleNamespace(api_key=STATIC, auth_provider=provider, auth_cache_store=cache)


def _auth(state: Any, token: str) -> dict[str, Any] | None:
    return asyncio.run(
        deps.resolve_auth(
            state,
            authorization=f"Bearer {token}",
            x_agent_id=None,
            x_nexus_subject=None,
            x_nexus_zone_id=None,
            client_host="203.0.113.7",
        )
    )


def test_static_key_never_hits_the_provider() -> None:
    provider = _Provider()
    result = _auth(_state(provider), STATIC)
    assert result is not None and result["authenticated"] and result["is_admin"]
    assert result["subject_id"] == "admin"
    assert provider.calls == [], "static key must short-circuit before the provider"


def test_provider_key_goes_through_provider_and_cache() -> None:
    provider = _Provider()
    cache = InMemoryCacheStore()
    state = _state(provider, cache)
    first = _auth(state, "sk-db-user-key")
    second = _auth(state, "sk-db-user-key")
    assert first is not None and first["subject_id"] == "alice" and first["_auth_cached"] is False
    assert second is not None and second["subject_id"] == "alice" and second["_auth_cached"] is True
    assert provider.calls == ["sk-db-user-key"], "second call must be served from the cache"


def test_unknown_key_is_rejected_after_both_checks() -> None:
    provider = _Provider()
    assert _auth(_state(provider), "sk-nope") is None
    assert provider.calls == ["sk-nope"]


def test_static_key_without_provider_still_works() -> None:
    state = SimpleNamespace(api_key=STATIC, auth_provider=None, auth_cache_store=None)
    result = _auth(state, STATIC)
    assert result is not None and result["is_admin"]
    assert _auth(state, "sk-nope") is None
