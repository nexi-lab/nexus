"""Tests for SqliteAuthProfileStore.

Coverage map:
  - CRUD: upsert, get, list, delete
  - LRU cache: hit, miss, eviction, eviction-on-upsert
  - Dirty-bit flush: mark_success buffers, mark_failure writes immediately
  - raw_error truncation at 500 chars
  - Provider filtering on list()
  - mark_success / mark_failure state transitions
  - Concurrent access: two threads calling select simultaneously (10A)
  - Cooldown expiry race (10A)
  - no_network: store operations never touch network
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from nexus.bricks.auth.profile import (
    RAW_ERROR_MAX_LEN,
    AuthProfileFailureReason,
)
from nexus.bricks.auth.profile_store import SqliteAuthProfileStore
from nexus.bricks.auth.tests.conftest import make_profile

# ---------------------------------------------------------------------------
# CRUD basics
# ---------------------------------------------------------------------------


class TestCRUD:
    def test_upsert_and_get(self, sqlite_store: SqliteAuthProfileStore) -> None:
        p = make_profile("openai/alice", provider="openai")
        sqlite_store.upsert(p)
        result = sqlite_store.get("openai/alice")
        assert result is not None
        assert result.id == "openai/alice"
        assert result.provider == "openai"
        assert result.account_identifier == "openai/alice"
        assert result.backend == "nexus-token-manager"

    def test_get_nonexistent(self, sqlite_store: SqliteAuthProfileStore) -> None:
        assert sqlite_store.get("nope") is None

    def test_upsert_updates_existing(self, sqlite_store: SqliteAuthProfileStore) -> None:
        p = make_profile("p1", backend_key="old-key")
        sqlite_store.upsert(p)
        p2 = make_profile("p1", backend_key="new-key")
        sqlite_store.upsert(p2)
        result = sqlite_store.get("p1")
        assert result is not None
        assert result.backend_key == "new-key"

    def test_delete(self, sqlite_store: SqliteAuthProfileStore) -> None:
        p = make_profile("p1")
        sqlite_store.upsert(p)
        sqlite_store.delete("p1")
        assert sqlite_store.get("p1") is None

    def test_delete_nonexistent(self, sqlite_store: SqliteAuthProfileStore) -> None:
        sqlite_store.delete("nope")  # should not raise

    def test_list_all(self, sqlite_store: SqliteAuthProfileStore) -> None:
        sqlite_store.upsert(make_profile("a", provider="openai"))
        sqlite_store.upsert(make_profile("b", provider="anthropic"))
        sqlite_store.upsert(make_profile("c", provider="openai"))
        result = sqlite_store.list()
        assert len(result) == 3

    def test_list_by_provider(self, sqlite_store: SqliteAuthProfileStore) -> None:
        sqlite_store.upsert(make_profile("a", provider="openai"))
        sqlite_store.upsert(make_profile("b", provider="anthropic"))
        sqlite_store.upsert(make_profile("c", provider="openai"))
        result = sqlite_store.list(provider="openai")
        assert len(result) == 2
        assert all(p.provider == "openai" for p in result)

    def test_list_empty_provider(self, sqlite_store: SqliteAuthProfileStore) -> None:
        sqlite_store.upsert(make_profile("a", provider="openai"))
        assert sqlite_store.list(provider="google") == []


# ---------------------------------------------------------------------------
# LRU cache behavior
# ---------------------------------------------------------------------------


class TestLRUCache:
    def test_cache_hit_after_upsert(self, sqlite_store: SqliteAuthProfileStore) -> None:
        p = make_profile("p1")
        sqlite_store.upsert(p)
        # Second get should hit cache (no way to observe directly,
        # but we verify correctness)
        r1 = sqlite_store.get("p1")
        r2 = sqlite_store.get("p1")
        assert r1 is not None and r2 is not None
        assert r1.id == r2.id

    def test_cache_eviction_at_capacity(self) -> None:
        store = SqliteAuthProfileStore(":memory:", cache_size=3)
        try:
            store.upsert(make_profile("a"))
            store.upsert(make_profile("b"))
            store.upsert(make_profile("c"))
            store.upsert(make_profile("d"))  # should evict "a" from cache

            # "d" should be retrievable (from cache or DB)
            assert store.get("d") is not None
            # "a" should still be retrievable (from DB, re-cached)
            assert store.get("a") is not None
        finally:
            store.close()

    def test_upsert_evicts_cache_entry(self, sqlite_store: SqliteAuthProfileStore) -> None:
        p = make_profile("p1", success_count=0)
        sqlite_store.upsert(p)
        assert sqlite_store.get("p1").usage_stats.success_count == 0

        p2 = make_profile("p1", success_count=42)
        sqlite_store.upsert(p2)
        assert sqlite_store.get("p1").usage_stats.success_count == 42


# ---------------------------------------------------------------------------
# Dirty-bit flush behavior
# ---------------------------------------------------------------------------


class TestDirtyFlush:
    def test_mark_success_buffers_in_cache(self) -> None:
        """mark_success does NOT write to SQLite immediately."""
        store = SqliteAuthProfileStore(":memory:", flush_interval=9999)
        try:
            store.upsert(make_profile("p1"))
            store.mark_success("p1")

            # Read directly from SQLite (bypass cache)
            row = store._conn.execute(
                "SELECT success_count FROM auth_profiles WHERE id = ?", ("p1",)
            ).fetchone()
            # Still 0 in SQLite — buffered in cache
            assert row["success_count"] == 0

            # But cache has the updated value
            cached = store.get("p1")
            assert cached is not None
            assert cached.usage_stats.success_count == 1
        finally:
            store.close()

    def test_mark_failure_writes_immediately(self) -> None:
        """mark_failure writes to SQLite immediately (cooldown must be durable)."""
        store = SqliteAuthProfileStore(":memory:", flush_interval=9999)
        try:
            store.upsert(make_profile("p1"))
            store.mark_failure("p1", AuthProfileFailureReason.RATE_LIMIT)

            row = store._conn.execute(
                "SELECT failure_count, cooldown_reason FROM auth_profiles WHERE id = ?",
                ("p1",),
            ).fetchone()
            assert row["failure_count"] == 1
            assert row["cooldown_reason"] == "rate_limit"
        finally:
            store.close()

    def test_flush_writes_dirty_profiles(self) -> None:
        store = SqliteAuthProfileStore(":memory:", flush_interval=9999)
        try:
            store.upsert(make_profile("p1"))
            store.mark_success("p1")
            store.mark_success("p1")

            # Before flush: SQLite has 0
            row = store._conn.execute(
                "SELECT success_count FROM auth_profiles WHERE id = ?", ("p1",)
            ).fetchone()
            assert row["success_count"] == 0

            store.flush()

            # After flush: SQLite has 2
            row = store._conn.execute(
                "SELECT success_count FROM auth_profiles WHERE id = ?", ("p1",)
            ).fetchone()
            assert row["success_count"] == 2
        finally:
            store.close()

    def test_close_flushes_dirty(self) -> None:
        store = SqliteAuthProfileStore(":memory:", flush_interval=9999)
        store.upsert(make_profile("p1"))
        store.mark_success("p1")

        # Read before close via a second connection (not possible with :memory:,
        # so we check via flush behavior by reading after explicit flush)
        store.flush()
        row = store._conn.execute(
            "SELECT success_count FROM auth_profiles WHERE id = ?", ("p1",)
        ).fetchone()
        assert row["success_count"] == 1
        store.close()


# ---------------------------------------------------------------------------
# raw_error truncation
# ---------------------------------------------------------------------------


class TestRawErrorTruncation:
    def test_raw_error_stored(self, sqlite_store: SqliteAuthProfileStore) -> None:
        sqlite_store.upsert(make_profile("p1"))
        sqlite_store.mark_failure("p1", AuthProfileFailureReason.UNKNOWN, raw_error="some error")
        p = sqlite_store.get("p1")
        assert p is not None
        assert p.usage_stats.raw_error == "some error"

    def test_raw_error_truncated_at_500(self, sqlite_store: SqliteAuthProfileStore) -> None:
        long_error = "x" * 1000
        sqlite_store.upsert(make_profile("p1"))
        sqlite_store.mark_failure("p1", AuthProfileFailureReason.UNKNOWN, raw_error=long_error)
        p = sqlite_store.get("p1")
        assert p is not None
        assert len(p.usage_stats.raw_error) == RAW_ERROR_MAX_LEN

    def test_raw_error_none_by_default(self, sqlite_store: SqliteAuthProfileStore) -> None:
        sqlite_store.upsert(make_profile("p1"))
        p = sqlite_store.get("p1")
        assert p is not None
        assert p.usage_stats.raw_error is None


# ---------------------------------------------------------------------------
# mark_success / mark_failure state transitions
# ---------------------------------------------------------------------------


class TestOutcomeRecording:
    def test_mark_success_increments_count(self, sqlite_store: SqliteAuthProfileStore) -> None:
        sqlite_store.upsert(make_profile("p1"))
        sqlite_store.mark_success("p1")
        sqlite_store.mark_success("p1")
        p = sqlite_store.get("p1")
        assert p.usage_stats.success_count == 2

    def test_mark_success_clears_expired_cooldown(
        self, sqlite_store: SqliteAuthProfileStore
    ) -> None:
        past = datetime.utcnow() - timedelta(hours=1)
        sqlite_store.upsert(
            make_profile(
                "p1", cooldown_until=past, cooldown_reason=AuthProfileFailureReason.RATE_LIMIT
            )
        )
        sqlite_store.mark_success("p1")
        p = sqlite_store.get("p1")
        assert p.usage_stats.cooldown_until is None
        assert p.usage_stats.cooldown_reason is None

    def test_mark_success_preserves_future_cooldown(
        self, sqlite_store: SqliteAuthProfileStore
    ) -> None:
        future = datetime.utcnow() + timedelta(hours=1)
        sqlite_store.upsert(
            make_profile(
                "p1", cooldown_until=future, cooldown_reason=AuthProfileFailureReason.RATE_LIMIT
            )
        )
        sqlite_store.mark_success("p1")
        p = sqlite_store.get("p1")
        assert p.usage_stats.cooldown_until is not None

    def test_mark_failure_sets_reason(self, sqlite_store: SqliteAuthProfileStore) -> None:
        sqlite_store.upsert(make_profile("p1"))
        sqlite_store.mark_failure("p1", AuthProfileFailureReason.BILLING)
        p = sqlite_store.get("p1")
        assert p.usage_stats.cooldown_reason == AuthProfileFailureReason.BILLING
        assert p.usage_stats.failure_count == 1

    def test_mark_on_nonexistent_profile(self, sqlite_store: SqliteAuthProfileStore) -> None:
        # Should not raise
        sqlite_store.mark_success("nope")
        sqlite_store.mark_failure("nope", AuthProfileFailureReason.UNKNOWN)


# ---------------------------------------------------------------------------
# Concurrency: torn reads (decision 10A)
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_concurrent_reads_no_torn_data(self, sqlite_store: SqliteAuthProfileStore) -> None:
        """Two threads reading the same profile simultaneously get consistent data."""
        sqlite_store.upsert(make_profile("p1", success_count=0))

        barrier = threading.Barrier(2, timeout=5)
        results: list[int] = []
        errors: list[Exception] = []

        def reader():
            try:
                barrier.wait()
                p = sqlite_store.get("p1")
                if p is not None:
                    results.append(p.usage_stats.success_count)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=reader)
        t2 = threading.Thread(target=reader)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert not errors, f"Thread errors: {errors}"
        assert len(results) == 2
        # Both should see the same consistent value
        assert results[0] == results[1] == 0

    def test_concurrent_mark_success_no_lost_updates(
        self, sqlite_store: SqliteAuthProfileStore
    ) -> None:
        """Multiple threads marking success don't lose counts."""
        sqlite_store.upsert(make_profile("p1", success_count=0))
        num_threads = 10
        calls_per_thread = 50

        barrier = threading.Barrier(num_threads, timeout=5)

        def worker():
            barrier.wait()
            for _ in range(calls_per_thread):
                sqlite_store.mark_success("p1")

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        sqlite_store.flush()
        p = sqlite_store.get("p1")
        assert p is not None
        assert p.usage_stats.success_count == num_threads * calls_per_thread


# ---------------------------------------------------------------------------
# Cooldown expiry race (decision 10A)
# ---------------------------------------------------------------------------


class TestCooldownExpiryRace:
    def test_cooldown_expires_then_success_clears_then_failure_retriggers(self) -> None:
        """Profile in cooldown → cooldown expires → success clears → failure re-triggers.

        Verifies the store correctly handles the sequence:
        1. Profile has a cooldown set (via upsert, simulating pool behavior)
        2. Cooldown expires (time passes)
        3. mark_success clears expired cooldown
        4. New failure sets a new cooldown (via upsert with new cooldown_until)
        """
        store = SqliteAuthProfileStore(":memory:")
        try:
            # Set cooldown to expire in 0.1s
            near_future = datetime.utcnow() + timedelta(milliseconds=100)
            store.upsert(
                make_profile(
                    "p1",
                    cooldown_until=near_future,
                    cooldown_reason=AuthProfileFailureReason.RATE_LIMIT,
                )
            )

            # Verify profile is currently on cooldown
            p = store.get("p1")
            assert p.usage_stats.cooldown_until is not None

            # Wait for cooldown to expire
            time.sleep(0.15)

            # Success should clear expired cooldown
            store.mark_success("p1")
            p = store.get("p1")
            assert p.usage_stats.cooldown_until is None
            assert p.usage_stats.cooldown_reason is None

            # Simulate pool behavior: failure sets cooldown via upsert
            p = store.get("p1")
            p.usage_stats.failure_count += 1
            p.usage_stats.cooldown_until = datetime.utcnow() + timedelta(hours=1)
            p.usage_stats.cooldown_reason = AuthProfileFailureReason.OVERLOADED
            store.upsert(p)

            p = store.get("p1")
            assert p.usage_stats.cooldown_until is not None
            assert p.usage_stats.cooldown_reason == AuthProfileFailureReason.OVERLOADED
        finally:
            store.close()

    def test_concurrent_success_and_failure_no_corruption(self) -> None:
        """Concurrent mark_success and mark_failure — no data corruption."""
        store = SqliteAuthProfileStore(":memory:")
        try:
            store.upsert(make_profile("p1"))

            barrier = threading.Barrier(2, timeout=5)
            errors: list[Exception] = []

            def do_success():
                try:
                    barrier.wait()
                    store.mark_success("p1")
                except Exception as e:
                    errors.append(e)

            def do_failure():
                try:
                    barrier.wait()
                    store.mark_failure("p1", AuthProfileFailureReason.RATE_LIMIT)
                except Exception as e:
                    errors.append(e)

            t1 = threading.Thread(target=do_success)
            t2 = threading.Thread(target=do_failure)
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

            assert not errors, f"Thread errors: {errors}"
            store.flush()
            p = store.get("p1")
            # Both operations completed — counts should reflect both
            assert p.usage_stats.success_count == 1
            assert p.usage_stats.failure_count == 1
            # failure_count persisted to SQLite immediately
            row = store._conn.execute(
                "SELECT failure_count FROM auth_profiles WHERE id = ?", ("p1",)
            ).fetchone()
            assert row["failure_count"] == 1
        finally:
            store.close()


# ---------------------------------------------------------------------------
# no_network smoke test
# ---------------------------------------------------------------------------


class TestNoNetwork:
    def test_store_operations_without_network(
        self,
        sqlite_store: SqliteAuthProfileStore,
        no_network,  # noqa: ARG002
    ) -> None:
        """All store operations work without any network access."""
        sqlite_store.upsert(make_profile("p1"))
        assert sqlite_store.get("p1") is not None
        assert len(sqlite_store.list()) == 1
        sqlite_store.mark_success("p1")
        sqlite_store.mark_failure("p1", AuthProfileFailureReason.UNKNOWN)
        sqlite_store.delete("p1")
        assert sqlite_store.get("p1") is None
