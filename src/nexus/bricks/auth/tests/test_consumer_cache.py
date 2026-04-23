"""Tests for ResolvedCredCache — TTL = min(300, expires_at - 60) (#3818)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from nexus.bricks.auth.consumer import MaterializedCredential
from nexus.bricks.auth.consumer_cache import ResolvedCredCache, _compute_ttl_seconds


def _cred(*, expires_at: datetime | None = None) -> MaterializedCredential:
    return MaterializedCredential(
        provider="github",
        access_token="t",
        expires_at=expires_at,
        metadata={},
    )


def test_compute_ttl_uses_ceiling_when_no_expiry():
    assert _compute_ttl_seconds(now=datetime.now(UTC), expires_at=None) == 10**9


def test_compute_ttl_caps_at_expiry_minus_60():
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    exp = now + timedelta(seconds=200)
    assert _compute_ttl_seconds(now=now, expires_at=exp) == 140


def test_compute_ttl_clamps_to_zero_when_already_near_expiry():
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    exp = now + timedelta(seconds=30)
    assert _compute_ttl_seconds(now=now, expires_at=exp) == 0


def test_get_returns_cached_then_evicts_after_ttl():
    cache = ResolvedCredCache(ceiling_seconds=300)
    key = ("t1", "p1", "github")
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    cred = _cred(expires_at=now + timedelta(seconds=200))

    cache.put(key, cred, now=now)
    # Hit immediately
    assert cache.get(key, now=now) is cred
    # 139s later: still warm (ttl is 200-60 = 140)
    assert cache.get(key, now=now + timedelta(seconds=139)) is cred
    # Just after TTL boundary: expired
    assert cache.get(key, now=now + timedelta(seconds=141)) is None


def test_put_with_no_expiry_uses_ceiling():
    cache = ResolvedCredCache(ceiling_seconds=300)
    key = ("t1", "p1", "github")
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    cache.put(key, _cred(expires_at=None), now=now)
    assert cache.get(key, now=now + timedelta(seconds=299)) is not None
    assert cache.get(key, now=now + timedelta(seconds=301)) is None


def test_thread_safe_concurrent_put_get():
    cache = ResolvedCredCache(ceiling_seconds=300)
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    cred = _cred(expires_at=now + timedelta(seconds=600))

    def worker(i: int):
        for _ in range(50):
            cache.put((f"t{i}", "p", "github"), cred, now=now)
            cache.get((f"t{i}", "p", "github"), now=now)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # No exception = pass
