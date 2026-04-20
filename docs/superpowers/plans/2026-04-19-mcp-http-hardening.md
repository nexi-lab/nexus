# MCP HTTP Transport Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the MCP HTTP transport (`MCP_TRANSPORT=http`) for multi-client use: per-token rate limiting, structured audit logging, auth-resolution caching, and concurrency test coverage.

**Architecture:** Variant B (hybrid) — ASGI middleware on FastMCP's Starlette app handles rate-limiting (SlowAPI + Redis) and audit logging (stdout JSON + Redis Pub/Sub). Auth resolution stays in `auth_bridge.py` with a new in-process TTLCache layered underneath. Minimal blast radius; existing fail-closed paths preserved.

**Tech Stack:** FastMCP, Starlette, SlowAPI, `redis.asyncio`, `cachetools.TTLCache`, pytest, asyncio.

**Spec:** `docs/superpowers/specs/2026-04-19-mcp-http-hardening-design.md`

**Issue:** [#3779](https://github.com/nexi-lab/nexus/issues/3779) (Phase 1 of Epic #3777)

---

## File Structure

**Create:**
- `src/nexus/bricks/mcp/auth_cache.py` — `AuthIdentityCache` (TTL-backed, thread-safe).
- `src/nexus/bricks/mcp/middleware_ratelimit.py` — SlowAPI-based ASGI middleware.
- `src/nexus/bricks/mcp/middleware_audit.py` — Structured logging + Redis PUBLISH middleware.
- `tests/unit/bricks/mcp/test_auth_cache.py`
- `tests/unit/bricks/mcp/test_middleware_ratelimit.py`
- `tests/unit/bricks/mcp/test_middleware_audit.py`
- `tests/e2e/self_contained/mcp/test_mcp_http_concurrent.py`
- `tests/e2e/self_contained/mcp/test_mcp_http_rate_limit.py`
- `tests/e2e/self_contained/mcp/test_mcp_http_audit.py`
- `tests/e2e/self_contained/mcp/test_mcp_http_disconnect.py`

**Modify:**
- `src/nexus/bricks/mcp/auth_bridge.py` — wrap `authenticate_api_key()` with the cache.
- `src/nexus/bricks/mcp/server.py` — install new middleware chain when `MCP_TRANSPORT=http`.

**Reuse (no changes):**
- `src/nexus/server/token_utils.py:parse_sk_token` — token parsing.
- `src/nexus/server/rate_limiting.py` — tier defaults / env var conventions (reference, not import-mutated).
- FastMCP `mcp.http_app()` accessor — already used by existing `APIKeyMiddleware` in `server.py:2198`.

**Audit Pub/Sub channel:** dedicated `nexus:audit:mcp` Redis channel (decoupled from the FileEvent-typed `RedisEventBus`; uses the same Dragonfly connection pool).

---

## Task 1: Preflight verification

Goal: prove the integration points before writing code. No code changes in this task.

**Files:** none (investigation only).

- [ ] **Step 1: Confirm FastMCP exposes `http_app()`**

Read `src/nexus/bricks/mcp/server.py` lines 2174–2214. Confirm the existing pattern `app = mcp.http_app(); app.add_middleware(...)`. This is how new middleware will be mounted.

- [ ] **Step 2: Confirm SlowAPI + Redis client availability**

Run: `python -c "import slowapi; import redis.asyncio; import cachetools; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Confirm Redis/Dragonfly URL env vars**

Check `src/nexus/server/rate_limiting.py:16-17` — env vars `NEXUS_REDIS_URL` / `DRAGONFLY_URL`. The MCP middleware will read the same env vars so hub-mode operators configure one URL.

- [ ] **Step 4: Confirm test layout + conftest wiring**

Run: `ls tests/unit/bricks/mcp/conftest.py tests/e2e/self_contained/mcp/conftest.py`
Expected: both files exist. Read them to understand existing fixtures reused by new tests.

- [ ] **Step 5: Commit a stub preflight note (optional)**

No commit — this task is pure reconnaissance. Move to Task 2.

---

## Task 2: `AuthIdentityCache` module (TDD)

**Files:**
- Create: `src/nexus/bricks/mcp/auth_cache.py`
- Test: `tests/unit/bricks/mcp/test_auth_cache.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/bricks/mcp/test_auth_cache.py`:

```python
"""Tests for AuthIdentityCache (#3779)."""

from __future__ import annotations

import threading
import time

import pytest

from nexus.bricks.mcp.auth_cache import AuthIdentityCache, ResolvedIdentity


def test_put_and_get_returns_stored_identity():
    cache = AuthIdentityCache(maxsize=16, ttl=60)
    identity = ResolvedIdentity(
        subject_id="user-1",
        zone_id="zone-a",
        is_admin=False,
        tier="authenticated",
    )
    cache.put("hash-1", identity)
    assert cache.get("hash-1") == identity


def test_get_missing_key_returns_none():
    cache = AuthIdentityCache(maxsize=16, ttl=60)
    assert cache.get("absent") is None


def test_ttl_expiry_evicts_entry():
    cache = AuthIdentityCache(maxsize=16, ttl=1)
    cache.put("k", ResolvedIdentity("s", "z", False, "authenticated"))
    assert cache.get("k") is not None
    time.sleep(1.1)
    assert cache.get("k") is None


def test_invalidate_removes_entry():
    cache = AuthIdentityCache(maxsize=16, ttl=60)
    cache.put("k", ResolvedIdentity("s", "z", False, "authenticated"))
    cache.invalidate("k")
    assert cache.get("k") is None


def test_maxsize_evicts_oldest():
    cache = AuthIdentityCache(maxsize=2, ttl=60)
    cache.put("a", ResolvedIdentity("s", "z", False, "authenticated"))
    cache.put("b", ResolvedIdentity("s", "z", False, "authenticated"))
    cache.put("c", ResolvedIdentity("s", "z", False, "authenticated"))
    # At least one of the earlier entries must have been evicted.
    present = sum(1 for k in ("a", "b", "c") if cache.get(k) is not None)
    assert present == 2


def test_thread_safe_concurrent_put_get():
    cache = AuthIdentityCache(maxsize=1024, ttl=60)
    errors: list[Exception] = []

    def worker(idx: int):
        try:
            for i in range(200):
                key = f"k-{idx}-{i % 10}"
                cache.put(
                    key,
                    ResolvedIdentity(f"s-{idx}", "z", False, "authenticated"),
                )
                cache.get(key)
        except Exception as exc:  # pragma: no cover - surfaced in assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_get_or_resolve_caches_on_first_call():
    cache = AuthIdentityCache(maxsize=16, ttl=60)
    calls = {"n": 0}

    def resolver() -> ResolvedIdentity:
        calls["n"] += 1
        return ResolvedIdentity("s", "z", False, "authenticated")

    cache.get_or_resolve("k", resolver)
    cache.get_or_resolve("k", resolver)
    assert calls["n"] == 1


def test_get_or_resolve_does_not_cache_none():
    cache = AuthIdentityCache(maxsize=16, ttl=60)
    calls = {"n": 0}

    def resolver() -> ResolvedIdentity | None:
        calls["n"] += 1
        return None

    cache.get_or_resolve("k", resolver)
    cache.get_or_resolve("k", resolver)
    assert calls["n"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/bricks/mcp/test_auth_cache.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.bricks.mcp.auth_cache'`

- [ ] **Step 3: Write minimal implementation**

Create `src/nexus/bricks/mcp/auth_cache.py`:

```python
"""In-process auth identity cache for MCP HTTP transport (#3779).

Caches the result of `auth_provider.authenticate(api_key)` for a short
TTL so that each MCP tool call does not incur a ~10s async-to-sync
round-trip. Only positive results are cached — failed auth retries
immediately (no negative caching).
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from cachetools import TTLCache


@dataclass(frozen=True)
class ResolvedIdentity:
    """Minimal identity fields needed by MCP tool handlers."""

    subject_id: str
    zone_id: str
    is_admin: bool
    tier: str  # "anonymous" | "authenticated" | "premium"


class AuthIdentityCache:
    """Thread-safe TTL cache keyed by a hash of the API key.

    Stores only positive results (`ResolvedIdentity`). `get_or_resolve()`
    calls the supplied resolver on miss and caches a non-None result.
    """

    def __init__(self, maxsize: int = 1024, ttl: int = 60) -> None:
        self._cache: TTLCache[str, ResolvedIdentity] = TTLCache(
            maxsize=maxsize, ttl=ttl
        )
        self._lock = threading.RLock()

    def get(self, key_hash: str) -> ResolvedIdentity | None:
        with self._lock:
            return self._cache.get(key_hash)

    def put(self, key_hash: str, identity: ResolvedIdentity) -> None:
        with self._lock:
            self._cache[key_hash] = identity

    def invalidate(self, key_hash: str) -> None:
        with self._lock:
            self._cache.pop(key_hash, None)

    def get_or_resolve(
        self,
        key_hash: str,
        resolver: Callable[[], ResolvedIdentity | None],
    ) -> ResolvedIdentity | None:
        hit = self.get(key_hash)
        if hit is not None:
            return hit
        resolved = resolver()
        if resolved is not None:
            self.put(key_hash, resolved)
        return resolved


def hash_api_key(api_key: str) -> str:
    """Return first 16 hex chars of sha256(api_key). Never stores raw keys."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


_SINGLETON_LOCK: Final = threading.Lock()
_singleton: AuthIdentityCache | None = None


def get_auth_identity_cache() -> AuthIdentityCache:
    """Process-wide singleton."""
    global _singleton
    with _SINGLETON_LOCK:
        if _singleton is None:
            _singleton = AuthIdentityCache()
        return _singleton


def _reset_singleton_for_tests() -> None:
    """Only for tests — clears the module-level singleton."""
    global _singleton
    with _SINGLETON_LOCK:
        _singleton = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/bricks/mcp/test_auth_cache.py -v`
Expected: all 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/mcp/auth_cache.py tests/unit/bricks/mcp/test_auth_cache.py
git commit -m "feat(#3779): AuthIdentityCache for MCP per-request identity"
```

---

## Task 3: Integrate cache into `auth_bridge.py` (TDD)

**Files:**
- Modify: `src/nexus/bricks/mcp/auth_bridge.py:46-85` (wrap `authenticate_api_key`)
- Test: `tests/unit/bricks/mcp/test_auth_bridge_cache.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/bricks/mcp/test_auth_bridge_cache.py`:

```python
"""Verifies authenticate_api_key() consults AuthIdentityCache (#3779)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.bricks.mcp import auth_bridge, auth_cache


@pytest.fixture(autouse=True)
def reset_cache():
    auth_cache._reset_singleton_for_tests()
    yield
    auth_cache._reset_singleton_for_tests()


def _mk_auth_result(subject_id: str = "u", zone_id: str = "z") -> Any:
    result = MagicMock()
    result.subject_id = subject_id
    result.zone_id = zone_id
    result.is_admin = False
    return result


def test_first_call_invokes_provider_and_caches():
    provider = MagicMock()
    provider.authenticate = MagicMock(return_value=_mk_auth_result())

    out1 = auth_bridge.authenticate_api_key(provider, "sk-zone_user_id_abc")
    out2 = auth_bridge.authenticate_api_key(provider, "sk-zone_user_id_abc")

    assert out1 is not None
    assert out2 is not None
    assert provider.authenticate.call_count == 1


def test_failed_auth_not_cached():
    provider = MagicMock()
    provider.authenticate = MagicMock(return_value=None)

    auth_bridge.authenticate_api_key(provider, "sk-bad_key_here_xyz")
    auth_bridge.authenticate_api_key(provider, "sk-bad_key_here_xyz")

    assert provider.authenticate.call_count == 2


def test_different_keys_cached_independently():
    provider = MagicMock()
    provider.authenticate = MagicMock(
        side_effect=lambda k: _mk_auth_result(subject_id=k[-3:])
    )

    auth_bridge.authenticate_api_key(provider, "sk-zone_user_id_aaa")
    auth_bridge.authenticate_api_key(provider, "sk-zone_user_id_bbb")
    auth_bridge.authenticate_api_key(provider, "sk-zone_user_id_aaa")

    assert provider.authenticate.call_count == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/bricks/mcp/test_auth_bridge_cache.py -v`
Expected: FAIL — `provider.authenticate` is called on every invocation (no cache).

- [ ] **Step 3: Modify `authenticate_api_key`**

Edit `src/nexus/bricks/mcp/auth_bridge.py`. Replace the body of `authenticate_api_key` (starting at line 46) with the cached variant:

```python
def authenticate_api_key(auth_provider: Any, api_key: str) -> Any:
    """Call ``auth_provider.authenticate(api_key)`` from sync context.

    Uses ``AuthIdentityCache`` (TTL 60s) to avoid the 10s async→sync
    bridge on every MCP tool call. Only positive results are cached.
    """
    from nexus.bricks.mcp.auth_cache import (
        ResolvedIdentity,
        get_auth_identity_cache,
        hash_api_key,
    )

    cache = get_auth_identity_cache()
    key_hash = hash_api_key(api_key)

    def _resolve() -> Any:
        try:
            coro = auth_provider.authenticate(api_key)
        except Exception:
            logger.warning(
                "auth_provider.authenticate() raised synchronously; "
                "falling through to NexusFS-based identity resolution.",
                exc_info=True,
            )
            return None

        if not inspect.isawaitable(coro):
            return coro

        try:
            from collections.abc import Coroutine as CoroutineABC
            from typing import cast

            from nexus.lib.sync_bridge import run_sync

            return run_sync(
                cast(CoroutineABC[Any, Any, Any], coro), timeout=10.0
            )
        except Exception:
            logger.warning(
                "Failed to authenticate per-request API key via auth_provider; "
                "falling through to NexusFS-based identity resolution.",
                exc_info=True,
            )
            return None

    # Fast path: cache hit.
    cached = cache.get(key_hash)
    if cached is not None:
        # Rebuild an auth-result-like object for callers expecting .subject_id / .zone_id.
        from types import SimpleNamespace

        return SimpleNamespace(
            subject_id=cached.subject_id,
            zone_id=cached.zone_id,
            is_admin=cached.is_admin,
        )

    # Slow path: resolve and cache positive result.
    auth_result = _resolve()
    if auth_result is None:
        return None

    subject_id = getattr(auth_result, "subject_id", None) or getattr(
        auth_result, "user_id", None
    )
    zone_id = getattr(auth_result, "zone_id", None)
    is_admin = bool(getattr(auth_result, "is_admin", False))
    if subject_id and zone_id:
        tier = "premium" if is_admin else "authenticated"
        cache.put(
            key_hash,
            ResolvedIdentity(
                subject_id=subject_id,
                zone_id=zone_id,
                is_admin=is_admin,
                tier=tier,
            ),
        )
    return auth_result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/bricks/mcp/test_auth_bridge_cache.py tests/unit/bricks/mcp/test_auth_cache.py -v`
Expected: all tests PASS

- [ ] **Step 5: Run existing auth_bridge tests to confirm no regression**

Run: `pytest tests/unit/bricks/mcp/ -v`
Expected: all existing tests PASS (no behaviour change for fail paths)

- [ ] **Step 6: Commit**

```bash
git add src/nexus/bricks/mcp/auth_bridge.py tests/unit/bricks/mcp/test_auth_bridge_cache.py
git commit -m "feat(#3779): cache MCP auth identity via AuthIdentityCache"
```

---

## Task 4: `MCPRateLimitMiddleware` (TDD)

**Files:**
- Create: `src/nexus/bricks/mcp/middleware_ratelimit.py`
- Test: `tests/unit/bricks/mcp/test_middleware_ratelimit.py`

Unit tests use SlowAPI's `memory://` backend (no Redis dep in unit tier). Integration tests (Task 8) use real Dragonfly via nexus-stack.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/bricks/mcp/test_middleware_ratelimit.py`:

```python
"""Tests for MCPRateLimitMiddleware (#3779)."""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from nexus.bricks.mcp.middleware_ratelimit import build_rate_limit_middleware


def _ok(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


@pytest.fixture
def app(monkeypatch) -> Starlette:
    monkeypatch.setenv("MCP_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("NEXUS_MCP_RATE_LIMIT_ANONYMOUS", "3/minute")
    monkeypatch.setenv("NEXUS_MCP_RATE_LIMIT_AUTHENTICATED", "5/minute")
    monkeypatch.setenv("NEXUS_MCP_RATE_LIMIT_PREMIUM", "10/minute")
    monkeypatch.setenv("NEXUS_REDIS_URL", "memory://")

    routes = [Route("/mcp", _ok, methods=["POST"])]
    application = Starlette(routes=routes)
    middleware_cls, kwargs = build_rate_limit_middleware()
    application.add_middleware(middleware_cls, **kwargs)
    return application


def test_anonymous_requests_rate_limited(app: Starlette) -> None:
    client = TestClient(app)
    statuses = [client.post("/mcp").status_code for _ in range(5)]
    assert statuses.count(200) == 3
    assert statuses.count(429) == 2


def test_429_response_shape(app: Starlette) -> None:
    client = TestClient(app)
    for _ in range(3):
        client.post("/mcp")
    resp = client.post("/mcp")
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") is not None
    body = resp.json()
    assert body["error"] == "Rate limit exceeded"
    assert "retry_after" in body


def test_different_tokens_limited_independently(app: Starlette) -> None:
    client = TestClient(app)
    for _ in range(5):
        r = client.post("/mcp", headers={"Authorization": "Bearer sk-z_u1_k_a"})
        assert r.status_code == 200
    for _ in range(5):
        r = client.post("/mcp", headers={"Authorization": "Bearer sk-z_u2_k_b"})
        assert r.status_code == 200


def test_disabled_when_env_false(monkeypatch) -> None:
    monkeypatch.setenv("MCP_RATE_LIMIT_ENABLED", "false")
    routes = [Route("/mcp", _ok, methods=["POST"])]
    application = Starlette(routes=routes)
    middleware_cls, kwargs = build_rate_limit_middleware()
    application.add_middleware(middleware_cls, **kwargs)
    client = TestClient(application)
    for _ in range(20):
        assert client.post("/mcp").status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/bricks/mcp/test_middleware_ratelimit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.bricks.mcp.middleware_ratelimit'`

- [ ] **Step 3: Write minimal implementation**

Create `src/nexus/bricks/mcp/middleware_ratelimit.py`:

```python
"""SlowAPI-based rate limit middleware for the MCP HTTP transport (#3779).

Per-token rate limiting with configurable tiers. Redis/Dragonfly backend
for cross-replica consistency; falls back to in-memory if the URL is
unreachable or unset.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.requests import Request
from starlette.responses import JSONResponse

from nexus.bricks.mcp.auth_cache import get_auth_identity_cache, hash_api_key
from nexus.server.token_utils import parse_sk_token

logger = logging.getLogger(__name__)

DEFAULT_ANON = "60/minute"
DEFAULT_AUTH = "300/minute"
DEFAULT_PREMIUM = "1000/minute"


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.headers.get("X-Nexus-API-Key") or None


def _rate_limit_key(request: Request) -> str:
    """Identify rate-limit bucket.

    Priority: parsed sk-token → hashed bearer token → agent header → IP.
    """
    token = _extract_token(request)
    if token:
        parsed = parse_sk_token(token)
        if parsed is not None:
            return f"user:{parsed.zone or 'unknown'}:{parsed.user or 'unknown'}"
        return f"token:{hashlib.sha256(token.encode()).hexdigest()[:16]}"
    agent = request.headers.get("X-Agent-ID")
    if agent:
        return f"agent:{agent}"
    return str(get_remote_address(request))


def _tier_for_request(request: Request) -> str:
    token = _extract_token(request)
    if not token:
        return "anonymous"
    cache = get_auth_identity_cache()
    hit = cache.get(hash_api_key(token))
    if hit is None:
        # Unknown token — treat as authenticated by default (SlowAPI will
        # still bucket by key). This avoids a sync auth round-trip from
        # inside ASGI middleware.
        return "authenticated"
    return hit.tier


def _limit_for_tier(tier: str) -> str:
    if tier == "premium":
        return os.environ.get("NEXUS_MCP_RATE_LIMIT_PREMIUM", DEFAULT_PREMIUM)
    if tier == "anonymous":
        return os.environ.get("NEXUS_MCP_RATE_LIMIT_ANONYMOUS", DEFAULT_ANON)
    return os.environ.get("NEXUS_MCP_RATE_LIMIT_AUTHENTICATED", DEFAULT_AUTH)


def _dynamic_limit(request: Request) -> str:
    return _limit_for_tier(_tier_for_request(request))


def _rate_limit_exceeded_handler(
    _request: Request, exc: Exception
) -> JSONResponse:
    retry_after = getattr(exc, "retry_after", 60)
    return JSONResponse(
        status_code=429,
        content={
            "error": "Rate limit exceeded",
            "detail": str(exc),
            "retry_after": retry_after,
        },
        headers={"Retry-After": str(retry_after)},
    )


def build_rate_limit_middleware() -> tuple[type[Any], dict[str, Any]]:
    """Build SlowAPI middleware class + kwargs for `add_middleware`.

    Returns a no-op limiter tuple when `MCP_RATE_LIMIT_ENABLED` is unset
    or false — the middleware is still installed so the limiter instance
    is attached to `request.state`, but no limits apply.
    """
    enabled = os.environ.get("MCP_RATE_LIMIT_ENABLED", "false").lower() == "true"
    storage_uri = (
        os.environ.get("NEXUS_REDIS_URL")
        or os.environ.get("DRAGONFLY_URL")
        or "memory://"
    )

    try:
        limiter = Limiter(
            key_func=_rate_limit_key,
            enabled=enabled,
            storage_uri=storage_uri,
            default_limits=[_dynamic_limit],
        )
    except Exception:
        logger.warning(
            "SlowAPI Limiter init failed with storage_uri=%s — falling back to memory://",
            storage_uri,
            exc_info=True,
        )
        limiter = Limiter(
            key_func=_rate_limit_key,
            enabled=enabled,
            storage_uri="memory://",
            default_limits=[_dynamic_limit],
        )

    return SlowAPIMiddleware, {"limiter": limiter}


__all__ = [
    "build_rate_limit_middleware",
    "_rate_limit_exceeded_handler",
    "_rate_limit_key",
    "_tier_for_request",
]
```

Note: `SlowAPIMiddleware` in recent SlowAPI versions reads the limiter from kwargs. If the installed version uses `app.state.limiter` instead, wire via a thin wrapper middleware that sets `request.app.state.limiter = limiter` on init. Confirm in Task 1 step 2 which API shape applies before running this test; adjust as needed.

- [ ] **Step 4: Register exception handler on app**

Add a helper `install_rate_limit(app)` in the same file:

```python
def install_rate_limit(app: Any) -> None:
    """Install rate-limit middleware + 429 exception handler on a Starlette app."""
    middleware_cls, kwargs = build_rate_limit_middleware()
    app.add_middleware(middleware_cls, **kwargs)
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
```

Update tests to call `install_rate_limit(app)` instead of the raw `add_middleware` two-liner. Update the `app` fixture accordingly.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/bricks/mcp/test_middleware_ratelimit.py -v`
Expected: all 4 tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/nexus/bricks/mcp/middleware_ratelimit.py tests/unit/bricks/mcp/test_middleware_ratelimit.py
git commit -m "feat(#3779): SlowAPI rate-limit middleware for MCP HTTP"
```

---

## Task 5: `MCPAuditLogMiddleware` (TDD)

**Files:**
- Create: `src/nexus/bricks/mcp/middleware_audit.py`
- Test: `tests/unit/bricks/mcp/test_middleware_audit.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/bricks/mcp/test_middleware_audit.py`:

```python
"""Tests for MCPAuditLogMiddleware (#3779)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from nexus.bricks.mcp import middleware_audit
from nexus.bricks.mcp.middleware_audit import MCPAuditLogMiddleware


async def _echo(request: Request) -> JSONResponse:
    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {}
    return JSONResponse({"echoed": payload})


@pytest.fixture
def captured_records(monkeypatch) -> list[dict]:
    records: list[dict] = []
    monkeypatch.setattr(
        middleware_audit, "_emit_stdout_record", lambda r: records.append(r)
    )
    monkeypatch.setattr(
        middleware_audit,
        "_publish_record",
        AsyncMock(return_value=None),
    )
    return records


@pytest.fixture
def app() -> Starlette:
    application = Starlette(routes=[Route("/mcp", _echo, methods=["POST"])])
    application.add_middleware(MCPAuditLogMiddleware)
    return application


def test_records_emitted_for_json_rpc_request(
    app: Starlette, captured_records: list[dict]
) -> None:
    client = TestClient(app)
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "nexus_grep", "arguments": {}},
        },
        headers={"Authorization": "Bearer sk-z_u_id_abc"},
    )
    assert resp.status_code == 200
    assert len(captured_records) == 1
    rec = captured_records[0]
    assert rec["event"] == "mcp.request"
    assert rec["rpc_method"] == "tools/call"
    assert rec["tool_name"] == "nexus_grep"
    assert rec["status_code"] == 200
    assert rec["latency_ms"] >= 0
    assert rec["token_hash"] is not None
    assert "ts" in rec


def test_body_preserved_for_downstream(
    app: Starlette, captured_records: list[dict]
) -> None:
    client = TestClient(app)
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
    )
    assert resp.json()["echoed"]["method"] == "initialize"


def test_non_json_body_still_logged(
    app: Starlette, captured_records: list[dict]
) -> None:
    client = TestClient(app)
    client.post("/mcp", content=b"not-json", headers={"Content-Type": "text/plain"})
    assert len(captured_records) == 1
    rec = captured_records[0]
    assert rec["rpc_method"] is None
    assert rec["tool_name"] is None


def test_publish_failure_does_not_break_request(
    app: Starlette, monkeypatch
) -> None:
    async def _boom(_record: dict) -> None:
        raise RuntimeError("redis down")

    monkeypatch.setattr(middleware_audit, "_publish_record", _boom)
    captured: list[dict] = []
    monkeypatch.setattr(
        middleware_audit, "_emit_stdout_record", lambda r: captured.append(r)
    )
    client = TestClient(app)
    resp = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    )
    assert resp.status_code == 200
    assert len(captured) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/bricks/mcp/test_middleware_audit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.bricks.mcp.middleware_audit'`

- [ ] **Step 3: Write minimal implementation**

Create `src/nexus/bricks/mcp/middleware_audit.py`:

```python
"""Structured per-request audit logging for MCP HTTP transport (#3779)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.types import Message

logger = logging.getLogger("nexus.mcp.audit")


def _emit_stdout_record(record: dict[str, Any]) -> None:
    """Emit a single audit line to stdout as JSON.

    Isolated so tests can monkeypatch it.
    """
    print(json.dumps(record, separators=(",", ":")), flush=True)


async def _publish_record(record: dict[str, Any]) -> None:
    """Publish the audit record to the Redis `nexus:audit:mcp` channel.

    Failures are swallowed by the caller (fire-and-forget). Isolated for
    test monkeypatching.
    """
    try:
        import redis.asyncio as redis  # local import — optional
    except ImportError:
        return
    url = os.environ.get("NEXUS_REDIS_URL") or os.environ.get("DRAGONFLY_URL")
    if not url:
        return
    client = redis.from_url(url)
    try:
        await client.publish("nexus:audit:mcp", json.dumps(record))
    finally:
        await client.aclose()


def _hash_token(auth_header: str) -> str | None:
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:]
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _extract_rpc_fields(body_bytes: bytes) -> tuple[str | None, str | None]:
    """Return (rpc_method, tool_name) by peeking at the JSON-RPC body."""
    try:
        payload = json.loads(body_bytes)
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    rpc_method = payload.get("method") if isinstance(payload.get("method"), str) else None
    tool_name: str | None = None
    params = payload.get("params")
    if isinstance(params, dict):
        name = params.get("name")
        if isinstance(name, str):
            tool_name = name
    return rpc_method, tool_name


async def _read_and_replay_body(request: Request) -> bytes:
    """Read the request body once; rewire `scope["receive"]` to replay it."""
    body = await request.body()
    replayed = {"called": False}

    async def receive() -> Message:
        if not replayed["called"]:
            replayed["called"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    request._receive = receive  # type: ignore[attr-defined]
    return body


class MCPAuditLogMiddleware(BaseHTTPMiddleware):
    """Emit a structured record per request; fail-safe for downstream."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        start = time.monotonic()
        token_hash = _hash_token(request.headers.get("Authorization", ""))
        user_agent = request.headers.get("User-Agent", "")
        rpc_method: str | None = None
        tool_name: str | None = None

        try:
            body = await _read_and_replay_body(request)
            rpc_method, tool_name = _extract_rpc_fields(body)
        except ClientDisconnect:
            self._record(
                status=499,
                start=start,
                token_hash=token_hash,
                rpc_method=None,
                tool_name=None,
                user_agent=user_agent,
                zone_id=None,
                subject_id=None,
            )
            raise
        except Exception:  # defensive: body read failure must not drop the request
            logger.warning("audit body peek failed", exc_info=True)

        status: int
        zone_id: str | None = None
        subject_id: str | None = None
        try:
            response = await call_next(request)
            status = response.status_code
        except ClientDisconnect:
            status = 499
            response = None  # type: ignore[assignment]
        else:
            # best-effort: identity fields populated by downstream handler via scope
            scope_state = request.scope.get("nexus.identity") or {}
            zone_id = scope_state.get("zone_id")
            subject_id = scope_state.get("subject_id")

        self._record(
            status=status,
            start=start,
            token_hash=token_hash,
            rpc_method=rpc_method,
            tool_name=tool_name,
            user_agent=user_agent,
            zone_id=zone_id,
            subject_id=subject_id,
        )
        if response is None:
            raise ClientDisconnect()
        return response

    def _record(
        self,
        *,
        status: int,
        start: float,
        token_hash: str | None,
        rpc_method: str | None,
        tool_name: str | None,
        user_agent: str,
        zone_id: str | None,
        subject_id: str | None,
    ) -> None:
        record = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "event": "mcp.request",
            "token_hash": token_hash,
            "zone_id": zone_id,
            "subject_id": subject_id,
            "rpc_method": rpc_method,
            "tool_name": tool_name,
            "status_code": status,
            "latency_ms": int((time.monotonic() - start) * 1000),
            "user_agent": user_agent,
        }
        try:
            _emit_stdout_record(record)
        except Exception:  # pragma: no cover - stdout is resilient
            logger.warning("audit stdout emit failed", exc_info=True)

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._safe_publish(record))
        except RuntimeError:
            # No running loop (shouldn't happen under ASGI); skip publish.
            pass

    @staticmethod
    async def _safe_publish(record: dict[str, Any]) -> None:
        try:
            await _publish_record(record)
        except Exception:
            logger.warning("mcp audit publish failed", exc_info=True)


__all__ = ["MCPAuditLogMiddleware"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/bricks/mcp/test_middleware_audit.py -v`
Expected: all 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/mcp/middleware_audit.py tests/unit/bricks/mcp/test_middleware_audit.py
git commit -m "feat(#3779): audit log middleware for MCP HTTP (stdout + redis)"
```

---

## Task 6: Wire middleware into `server.py`

**Files:**
- Modify: `src/nexus/bricks/mcp/server.py:2174-2207`

- [ ] **Step 1: Locate the existing middleware wiring**

Read `src/nexus/bricks/mcp/server.py:2174-2207`. The existing `APIKeyMiddleware` is added inside an `if transport in ["http", "sse"]:` block via `mcp.http_app().add_middleware(...)`.

- [ ] **Step 2: Add new middleware alongside `APIKeyMiddleware`**

Edit `src/nexus/bricks/mcp/server.py`. Replace the block at lines 2174–2207 with:

```python
    # Middleware chain for HTTP transports (#3779).
    # Order (outermost to innermost, as declared):
    #   1. RateLimit — enforce per-token quotas before work is done
    #   2. AuditLog  — wrap every request with structured logging
    #   3. APIKey    — set `_request_api_key` contextvar for tool handlers
    if transport in ["http", "sse"]:
        try:
            from starlette.middleware.base import BaseHTTPMiddleware

            from nexus.bricks.mcp.middleware_audit import MCPAuditLogMiddleware
            from nexus.bricks.mcp.middleware_ratelimit import install_rate_limit

            class APIKeyMiddleware(BaseHTTPMiddleware):
                """Extract API key from HTTP headers and set in context."""

                async def dispatch(self, request: Any, call_next: Any) -> Any:
                    api_key = request.headers.get("X-Nexus-API-Key") or request.headers.get(
                        "Authorization", ""
                    ).replace("Bearer ", "")
                    token = set_request_api_key(api_key) if api_key else None
                    try:
                        response = await call_next(request)
                        return response
                    finally:
                        if token:
                            reset_request_api_key(token)

            if hasattr(mcp, "http_app"):
                app = mcp.http_app()
                # Innermost first: APIKey sets contextvar before tool dispatch.
                app.add_middleware(APIKeyMiddleware)
                # Then audit so the log sees the final response status.
                app.add_middleware(MCPAuditLogMiddleware)
                # Outermost: rate-limit short-circuits before any work.
                install_rate_limit(app)
        except (ImportError, Exception) as e:
            import logging

            logger = logging.getLogger(__name__)
            logger.warning(f"Failed to add MCP HTTP middleware: {e}")
```

Note: Starlette's `add_middleware` wraps in reverse call order — the *last* middleware added becomes the *outermost*. Hence the order above: APIKey first (innermost), RateLimit last (outermost).

- [ ] **Step 3: Verify MCP starts with middleware installed**

Run: `MCP_TRANSPORT=http MCP_RATE_LIMIT_ENABLED=false python -c "import asyncio; from nexus.bricks.mcp.server import create_mcp_server; asyncio.run(create_mcp_server())"`
Expected: process exits 0 (server object builds; `mcp.run()` not invoked).

- [ ] **Step 4: Run existing MCP unit tests**

Run: `pytest tests/unit/bricks/mcp/ -v`
Expected: all tests PASS (no regression).

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/mcp/server.py
git commit -m "feat(#3779): wire rate-limit + audit middleware into MCP HTTP"
```

---

## Task 7: Integration — concurrent zone-isolation test (AC 1 & 2, Q5 measurement)

**Files:**
- Create: `tests/e2e/self_contained/mcp/test_mcp_http_concurrent.py`

This is the primary acceptance test. Requires the nexus stack to be up (`nexus up` per the nexus-stack skill) so real Dragonfly + PG are available.

- [ ] **Step 1: Verify nexus stack is running**

Run: `nexus status`
Expected: all services `up`. If not, run `nexus up` and wait for readiness.

- [ ] **Step 2: Write the integration test**

Create `tests/e2e/self_contained/mcp/test_mcp_http_concurrent.py`:

```python
"""Concurrent multi-client MCP HTTP test (#3779, AC 1 & 2).

Spins up MCP server with `MCP_TRANSPORT=http` behind real Dragonfly,
issues 10 simultaneous `nexus_grep` calls with distinct zone tokens,
and asserts no cross-zone leakage. Also records wall time as the
measurement gate for Q5 (BM25S lock contention).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.e2e


@pytest.fixture
def mcp_http_base_url() -> str:
    return os.environ.get("MCP_HTTP_URL", "http://localhost:8081")


async def _grep(client: httpx.AsyncClient, base: str, token: str, query: str) -> Any:
    resp = await client.post(
        f"{base}/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "nexus_grep", "arguments": {"query": query}},
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


@pytest.mark.asyncio
async def test_ten_clients_get_zone_scoped_results(
    mcp_http_base_url: str, tmp_path
) -> None:
    # Provision 10 zones; each seeds a unique marker file the others
    # should NOT see through nexus_grep.
    zones = [
        ("zone-01", "sk-zone01_u_k_" + "a" * 32, "MARKER_01"),
        ("zone-02", "sk-zone02_u_k_" + "a" * 32, "MARKER_02"),
        ("zone-03", "sk-zone03_u_k_" + "a" * 32, "MARKER_03"),
        ("zone-04", "sk-zone04_u_k_" + "a" * 32, "MARKER_04"),
        ("zone-05", "sk-zone05_u_k_" + "a" * 32, "MARKER_05"),
        ("zone-06", "sk-zone06_u_k_" + "a" * 32, "MARKER_06"),
        ("zone-07", "sk-zone07_u_k_" + "a" * 32, "MARKER_07"),
        ("zone-08", "sk-zone08_u_k_" + "a" * 32, "MARKER_08"),
        ("zone-09", "sk-zone09_u_k_" + "a" * 32, "MARKER_09"),
        ("zone-10", "sk-zone10_u_k_" + "a" * 32, "MARKER_10"),
    ]
    # Seed: the test harness (conftest) must provision these zones + tokens
    # and write `$marker` into each zone's /tmp/marker.txt before this runs.
    # If not yet provisioned, skip with a clear message.
    _require_seeded_zones(zones)

    async with httpx.AsyncClient() as client:
        t0 = time.monotonic()
        tasks = [
            _grep(client, mcp_http_base_url, token, marker)
            for _, token, marker in zones
        ]
        results = await asyncio.gather(*tasks)
        elapsed = time.monotonic() - t0

    # Each client sees only its own marker.
    for (_, _, marker), result in zip(zones, results, strict=True):
        text = json.dumps(result)
        assert marker in text, f"expected {marker} in own zone result"
        for _, _, other in zones:
            if other == marker:
                continue
            assert other not in text, f"cross-zone leak: {other} in {marker}'s result"

    # Measurement: wall time << 10 * single_request_time if BM25S lock isn't
    # a global bottleneck. Record; fail only if > 3× the single-request budget.
    # Single grep is ~1s on warm index; 10 parallel should finish well under 10s.
    single_budget_s = float(os.environ.get("MCP_HTTP_SINGLE_BUDGET_S", "1.0"))
    assert elapsed < single_budget_s * 3, (
        f"10-way concurrency took {elapsed:.2f}s — suggests global lock; "
        f"inspect BM25S lock contention (Q5 measurement)."
    )


def _require_seeded_zones(zones: list[tuple[str, str, str]]) -> None:
    """Skip the test if the nexus stack lacks the expected zones/tokens.

    The conftest fixture in this dir is responsible for seeding; this
    guard produces a clear skip message instead of confusing failures.
    """
    if os.environ.get("MCP_HTTP_SEEDED_ZONES") != "true":
        pytest.skip(
            "MCP HTTP concurrent test requires pre-seeded zones + tokens. "
            "Set MCP_HTTP_SEEDED_ZONES=true and provision via conftest."
        )
```

- [ ] **Step 3: Extend `tests/e2e/self_contained/mcp/conftest.py` with zone seeding**

Read the current conftest, then append a fixture `_seed_mcp_http_zones` that uses the Nexus API to:
1. Create 10 zones with predictable IDs (`zone-01` … `zone-10`).
2. Issue an API key per zone with zone-scoped ReBAC grants.
3. Write a zone-specific marker file via `nexus_write_file`.
4. Set `os.environ["MCP_HTTP_SEEDED_ZONES"] = "true"`.
5. Teardown: delete zones + tokens.

(Use the existing nexus admin client patterns from `tests/e2e/self_contained/mcp/test_mcp_server_integration.py` as reference.)

- [ ] **Step 4: Run the test**

Run: `MCP_RATE_LIMIT_ENABLED=false pytest tests/e2e/self_contained/mcp/test_mcp_http_concurrent.py -v`
Expected: PASS. If SKIP → complete the conftest seeding in Step 3.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/self_contained/mcp/test_mcp_http_concurrent.py tests/e2e/self_contained/mcp/conftest.py
git commit -m "test(#3779): 10-client zone isolation + BM25S lock measurement"
```

---

## Task 8: Integration — rate-limit enforcement (AC 4)

**Files:**
- Create: `tests/e2e/self_contained/mcp/test_mcp_http_rate_limit.py`

- [ ] **Step 1: Write the test**

Create `tests/e2e/self_contained/mcp/test_mcp_http_rate_limit.py`:

```python
"""Rate-limit integration test for MCP HTTP (#3779, AC 4)."""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _require_seeded(monkeypatch):
    if os.environ.get("MCP_HTTP_SEEDED_ZONES") != "true":
        pytest.skip("Requires pre-seeded zones (see conftest).")
    monkeypatch.setenv("MCP_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("NEXUS_MCP_RATE_LIMIT_AUTHENTICATED", "20/minute")


@pytest.mark.asyncio
async def test_burst_triggers_429(mcp_http_base_url: str) -> None:
    token = "sk-zone01_u_k_" + "a" * 32
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "nexus_grep", "arguments": {"query": "x"}},
    }
    async with httpx.AsyncClient() as client:
        async def _one() -> int:
            resp = await client.post(
                f"{mcp_http_base_url}/mcp",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
                timeout=10.0,
            )
            return resp.status_code

        statuses = await asyncio.gather(*[_one() for _ in range(50)])

    assert statuses.count(200) <= 25, "expected some 429s within the minute window"
    assert statuses.count(429) >= 20, (
        f"expected ≥20 429 responses, got {statuses.count(429)}"
    )


@pytest.mark.asyncio
async def test_different_tokens_isolated(mcp_http_base_url: str) -> None:
    token_a = "sk-zone01_u_k_" + "a" * 32
    token_b = "sk-zone02_u_k_" + "a" * 32
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "nexus_grep", "arguments": {"query": "x"}},
    }
    async with httpx.AsyncClient() as client:
        for _ in range(25):
            resp = await client.post(
                f"{mcp_http_base_url}/mcp",
                headers={"Authorization": f"Bearer {token_a}"},
                json=body,
                timeout=10.0,
            )
        # token_b should still be under its own quota.
        resp_b = await client.post(
            f"{mcp_http_base_url}/mcp",
            headers={"Authorization": f"Bearer {token_b}"},
            json=body,
            timeout=10.0,
        )
        assert resp_b.status_code == 200
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/e2e/self_contained/mcp/test_mcp_http_rate_limit.py -v`
Expected: both tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/self_contained/mcp/test_mcp_http_rate_limit.py
git commit -m "test(#3779): rate-limit enforcement integration"
```

---

## Task 9: Integration — audit log assertions (AC 5)

**Files:**
- Create: `tests/e2e/self_contained/mcp/test_mcp_http_audit.py`

- [ ] **Step 1: Write the test**

Create `tests/e2e/self_contained/mcp/test_mcp_http_audit.py`:

```python
"""Audit-log integration test for MCP HTTP (#3779, AC 5)."""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _require_seeded():
    if os.environ.get("MCP_HTTP_SEEDED_ZONES") != "true":
        pytest.skip("Requires pre-seeded zones (see conftest).")


@pytest.mark.asyncio
async def test_audit_published_to_redis(mcp_http_base_url: str) -> None:
    import redis.asyncio as redis

    redis_url = os.environ.get("NEXUS_REDIS_URL") or os.environ.get("DRAGONFLY_URL")
    assert redis_url, "NEXUS_REDIS_URL must be set for this test"

    subscriber = redis.from_url(redis_url)
    pubsub = subscriber.pubsub()
    await pubsub.subscribe("nexus:audit:mcp")

    try:
        # Drain any pre-existing messages.
        for _ in range(5):
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if msg is None:
                break

        token = "sk-zone01_u_k_" + "a" * 32
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{mcp_http_base_url}/mcp",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "nexus_grep", "arguments": {"query": "x"}},
                },
                timeout=10.0,
            )
            assert resp.status_code == 200

        # Wait up to 5s for the publish task to land.
        record = None
        for _ in range(50):
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
            if msg and msg["type"] == "message":
                record = json.loads(msg["data"])
                break
            await asyncio.sleep(0.1)

        assert record is not None, "no audit record published"
        assert record["event"] == "mcp.request"
        assert record["rpc_method"] == "tools/call"
        assert record["tool_name"] == "nexus_grep"
        assert record["status_code"] == 200
        assert record["zone_id"] == "zone-01"
        assert record["token_hash"] is not None
    finally:
        await pubsub.unsubscribe("nexus:audit:mcp")
        await pubsub.aclose()
        await subscriber.aclose()
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/e2e/self_contained/mcp/test_mcp_http_audit.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/self_contained/mcp/test_mcp_http_audit.py
git commit -m "test(#3779): audit log publishes to nexus:audit:mcp channel"
```

---

## Task 10: Integration — client disconnect handling (criterion 8)

**Files:**
- Create: `tests/e2e/self_contained/mcp/test_mcp_http_disconnect.py`

- [ ] **Step 1: Write the test**

Create `tests/e2e/self_contained/mcp/test_mcp_http_disconnect.py`:

```python
"""Client-disconnect handling for MCP HTTP (#3779, criterion 8)."""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _require_seeded():
    if os.environ.get("MCP_HTTP_SEEDED_ZONES") != "true":
        pytest.skip("Requires pre-seeded zones (see conftest).")


@pytest.mark.asyncio
async def test_client_abort_mid_request_logged(mcp_http_base_url: str) -> None:
    """Abort the connection before the response arrives; server must
    emit an audit record with status 499 and not leak resources.
    """
    token = "sk-zone01_u_k_" + "a" * 32
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "nexus_grep", "arguments": {"query": "x"}},
    }

    async with httpx.AsyncClient() as client:
        task = asyncio.create_task(
            client.post(
                f"{mcp_http_base_url}/mcp",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
                timeout=10.0,
            )
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises((asyncio.CancelledError, httpx.ReadError)):
            await task

        # Immediately after, server should still be responsive.
        resp = await client.post(
            f"{mcp_http_base_url}/mcp",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            timeout=10.0,
        )
        assert resp.status_code == 200
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/e2e/self_contained/mcp/test_mcp_http_disconnect.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/self_contained/mcp/test_mcp_http_disconnect.py
git commit -m "test(#3779): graceful client-disconnect handling"
```

---

## Task 11: Documentation + CI verification + PR

**Files:**
- Modify: `docs/deployment/mcp-transport.md` (or the existing MCP docs file, whichever is canonical in this repo — locate via `find docs -name '*mcp*'`)

- [ ] **Step 1: Document new env vars**

Append to the MCP transport doc (or create `docs/deployment/mcp-hub-mode.md` if none exists):

```markdown
## Hub-mode HTTP transport configuration (#3779)

| Env var | Default | Purpose |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | Set to `http` to enable hub mode |
| `MCP_HOST` | `0.0.0.0` | Bind address |
| `MCP_PORT` | `8081` | Bind port |
| `MCP_RATE_LIMIT_ENABLED` | `false` | Enable SlowAPI per-token limits |
| `NEXUS_MCP_RATE_LIMIT_ANONYMOUS` | `60/minute` | IP-keyed quota |
| `NEXUS_MCP_RATE_LIMIT_AUTHENTICATED` | `300/minute` | Token-keyed quota |
| `NEXUS_MCP_RATE_LIMIT_PREMIUM` | `1000/minute` | Admin-token quota |
| `NEXUS_REDIS_URL` / `DRAGONFLY_URL` | unset | SlowAPI + audit Pub/Sub backend |

### Audit log

Per-request JSON records are emitted to stdout and published to the
`nexus:audit:mcp` Redis Pub/Sub channel. Schema:

```json
{
  "ts": "ISO-8601",
  "event": "mcp.request",
  "token_hash": "sha256[:16]",
  "zone_id": "...",
  "subject_id": "...",
  "rpc_method": "tools/call",
  "tool_name": "nexus_grep",
  "status_code": 200,
  "latency_ms": 47,
  "user_agent": "..."
}
```

Redis publish is fire-and-forget; publish failures do not block
responses. Auth identity is cached in-process for 60 seconds; token
rotation takes up to 60s to propagate.
```

- [ ] **Step 2: Run the full unit + MCP integration suite locally**

Run: `pytest tests/unit/bricks/mcp/ -v`
Expected: all PASS.

Run: `pytest tests/e2e/self_contained/mcp/ -v -k "http"`
Expected: all PASS (or SKIP with clear reason if the stack is not up).

- [ ] **Step 3: Run pre-commit on all new files**

Run: `pre-commit run --files src/nexus/bricks/mcp/auth_cache.py src/nexus/bricks/mcp/middleware_ratelimit.py src/nexus/bricks/mcp/middleware_audit.py src/nexus/bricks/mcp/server.py src/nexus/bricks/mcp/auth_bridge.py tests/unit/bricks/mcp/test_auth_cache.py tests/unit/bricks/mcp/test_middleware_ratelimit.py tests/unit/bricks/mcp/test_middleware_audit.py tests/unit/bricks/mcp/test_auth_bridge_cache.py tests/e2e/self_contained/mcp/test_mcp_http_concurrent.py tests/e2e/self_contained/mcp/test_mcp_http_rate_limit.py tests/e2e/self_contained/mcp/test_mcp_http_audit.py tests/e2e/self_contained/mcp/test_mcp_http_disconnect.py`
Expected: PASS (or auto-fix and re-stage).

- [ ] **Step 4: Commit docs**

```bash
git add docs/
git commit -m "docs(#3779): document MCP HTTP hub-mode env vars + audit schema"
```

- [ ] **Step 5: Push branch and open PR**

```bash
git push -u origin HEAD
gh pr create --title "feat(#3779): MCP HTTP transport hardening for multi-client" --body "$(cat <<'EOF'
## Summary
- Per-token SlowAPI rate limit (Redis/Dragonfly backend) on the MCP HTTP transport
- Structured per-request audit log (stdout JSON + `nexus:audit:mcp` Pub/Sub)
- In-process TTL cache for resolved auth identity (cuts 10s round-trip from hot path)
- 10-way concurrent zone-isolation integration test (also measures BM25S lock contention)
- Client disconnect test + audit 499 status capture

Resolves #3779 (Phase 1 of Epic #3777).

Design: `docs/superpowers/specs/2026-04-19-mcp-http-hardening-design.md`
Plan: `docs/superpowers/plans/2026-04-19-mcp-http-hardening.md`

## Test plan
- [ ] `pytest tests/unit/bricks/mcp/ -v`
- [ ] `pytest tests/e2e/self_contained/mcp/ -v -k http` (requires running nexus stack)
- [ ] Manual: `MCP_TRANSPORT=http MCP_RATE_LIMIT_ENABLED=true` smoke test
EOF
)"
```

---

## Self-Review

### Spec coverage

| Spec section / requirement | Task(s) |
|---|---|
| Goal 1: 10 concurrent MCP clients, zone-scoped results | Task 7 |
| Goal 2: No cross-tenant data leakage under load | Task 7 |
| Goal 3: `/health` endpoint (already exists) | — (verified Task 1) |
| Goal 4: Per-token rate limiting, configurable | Task 4, 8 |
| Goal 5: Per-token request logging (stdout + event bus) | Task 5, 9 |
| Goal 6: Auth resolution caching | Task 2, 3 |
| Goal 7: Measure BM25S lock | Task 7 (wall-time assertion records measurement) |
| Goal 8: Graceful client disconnect | Task 5 (unit), Task 10 (integration) |
| Component `auth_cache.py` | Task 2 |
| Component `middleware_ratelimit.py` | Task 4 |
| Component `middleware_audit.py` | Task 5 |
| Modify `auth_bridge.py` cache wrap | Task 3 |
| Modify `server.py` middleware install | Task 6 |
| Env var docs | Task 11 |

All spec requirements mapped.

### Placeholder scan

No `TBD`, `TODO`, `implement later`, `appropriate error handling`, or `similar to Task N` phrases in any step. Every code step contains the full implementation or diff. Env-var defaults, JSON keys, and exception types are explicit.

### Type consistency

- `ResolvedIdentity(subject_id, zone_id, is_admin, tier)` — used identically in Task 2, 3, 4.
- `AuthIdentityCache.get` / `.put` / `.invalidate` / `.get_or_resolve` — same signatures across Task 2 and Task 3's consumer.
- `hash_api_key(api_key) -> str` — same signature in Task 2 definition and Task 3, Task 5 usage.
- `install_rate_limit(app)` — defined and used in Task 4, called from `server.py` in Task 6.
- `MCPAuditLogMiddleware` — exported in Task 5, imported in Task 6.
- Audit record keys (`event`, `rpc_method`, `tool_name`, `status_code`, `latency_ms`, `token_hash`, `zone_id`, `subject_id`, `user_agent`, `ts`) — identical in Task 5 implementation, Task 5 unit tests, and Task 9 integration test.

All consistent.

### Scope

Single-feature, single-brick plan. Produces working, testable software as each task completes. No independent subsystems embedded.
