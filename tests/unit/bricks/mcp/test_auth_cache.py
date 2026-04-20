"""Tests for AuthIdentityCache (#3779)."""

from __future__ import annotations

import threading
import time

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
