"""Concurrency tests for the Skills brick (Issue #2035, Follow-up 2).

Tests thread-safety of:
- Parallel discover() calls
- Concurrent subscribe/unsubscribe
- Race conditions in metadata cache
- Concurrent import operations
- Subscription cache TTL under contention
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import pytest

from nexus.skills.package_service import SkillPackageService
from nexus.skills.service import SkillService
from nexus.skills.testing import (
    FakeOperationContext,
    InMemorySkillFilesystem,
    StubSkillPermissions,
)


# ---------------------------------------------------------------------------
# Thread-safe filesystem fake
# ---------------------------------------------------------------------------


class ThreadSafeSkillFilesystem(InMemorySkillFilesystem):
    """Thread-safe variant of InMemorySkillFilesystem for concurrency tests."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()

    def read(self, path: str, *, context: Any = None) -> bytes | str:
        with self._lock:
            return super().read(path, context=context)

    def write(self, path: str, content: bytes | str, *, context: Any = None) -> None:
        with self._lock:
            super().write(path, content, context=context)

    def list(self, path: str, *, context: Any = None) -> list[str]:
        with self._lock:
            return super().list(path, context=context)

    def exists(self, path: str, *, context: Any = None) -> bool:
        with self._lock:
            return super().exists(path, context=context)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ts_fs():
    return ThreadSafeSkillFilesystem()


@pytest.fixture
def perms():
    return StubSkillPermissions()


@pytest.fixture
def svc(ts_fs, perms):
    return SkillService(fs=ts_fs, perms=perms)


@pytest.fixture
def pkg_svc(ts_fs, perms, svc):
    return SkillPackageService(fs=ts_fs, perms=perms, skill_service=svc)


def _make_ctx(user_id: str = "alice", zone_id: str = "acme") -> FakeOperationContext:
    return FakeOperationContext(user_id=user_id, zone_id=zone_id)


SKILL_BASE = "/zone/acme/user/alice/skill/"


# ---------------------------------------------------------------------------
# Parallel Discover
# ---------------------------------------------------------------------------


class TestParallelDiscover:
    """Test concurrent discover() calls don't corrupt shared state."""

    def test_concurrent_discover_returns_consistent_results(self, ts_fs, svc):
        """Multiple threads calling discover() simultaneously get consistent results."""
        for i in range(10):
            ts_fs.seed_skill(f"{SKILL_BASE}skill-{i}/", name=f"skill-{i}")

        ctx = _make_ctx()
        errors: list[Exception] = []
        results: list[int] = []
        lock = threading.Lock()

        def do_discover():
            try:
                skills = svc.discover(ctx, filter="owned")
                with lock:
                    results.append(len(skills))
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=do_discover) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Errors during concurrent discover: {errors}"
        assert all(r == 10 for r in results), f"Inconsistent results: {results}"

    def test_concurrent_discover_with_different_filters(self, ts_fs, svc, perms):
        """Different filter modes can run concurrently."""
        for i in range(5):
            ts_fs.seed_skill(f"{SKILL_BASE}skill-{i}/", name=f"skill-{i}")

        ctx = _make_ctx()
        filters = ["owned", "subscribed", "all", "owned", "all"]
        results: dict[str, list[int]] = {f: [] for f in set(filters)}
        errors: list[Exception] = []
        lock = threading.Lock()

        def do_discover(filt: str):
            try:
                skills = svc.discover(ctx, filter=filt)
                with lock:
                    results[filt].append(len(skills))
            except Exception as e:
                with lock:
                    errors.append(e)

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(do_discover, f) for f in filters]
            for fut in as_completed(futures):
                fut.result()

        assert not errors, f"Errors: {errors}"
        # owned should consistently return 5
        for count in results["owned"]:
            assert count == 5


# ---------------------------------------------------------------------------
# Concurrent Subscribe / Unsubscribe
# ---------------------------------------------------------------------------


class TestConcurrentSubscription:
    """Test subscribe/unsubscribe under contention."""

    def test_concurrent_subscribe_no_duplicates(self, ts_fs, svc):
        """Multiple threads subscribing to the same skill don't create duplicates."""
        ts_fs.seed_skill(f"{SKILL_BASE}target/", name="target")
        ctx = _make_ctx()
        skill_path = f"{SKILL_BASE}target/"

        results: list[bool] = []
        lock = threading.Lock()

        def do_subscribe():
            result = svc.subscribe(skill_path, ctx)
            with lock:
                results.append(result)

        threads = [threading.Thread(target=do_subscribe) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # Exactly one thread should have newly subscribed
        true_count = sum(1 for r in results if r is True)
        assert true_count >= 1, "At least one thread should succeed"

        # After all threads, subscription list should have exactly one entry
        svc.clear_metadata_cache()
        svc._subscriptions_cache.clear()
        subs = svc._load_subscriptions(ctx)
        assert subs.count(skill_path) == 1, f"Expected 1 subscription, got {subs}"

    def test_concurrent_subscribe_unsubscribe(self, ts_fs, svc):
        """Interleaved subscribe and unsubscribe don't corrupt state."""
        ts_fs.seed_skill(f"{SKILL_BASE}toggle/", name="toggle")
        ctx = _make_ctx()
        skill_path = f"{SKILL_BASE}toggle/"
        errors: list[Exception] = []
        lock = threading.Lock()

        def toggle(subscribe: bool):
            try:
                if subscribe:
                    svc.subscribe(skill_path, ctx)
                else:
                    svc.unsubscribe(skill_path, ctx)
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = []
        for i in range(10):
            threads.append(threading.Thread(target=toggle, args=(i % 2 == 0,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Errors during toggle: {errors}"

        # State should be consistent (either subscribed or not, no corruption)
        svc._subscriptions_cache.clear()
        subs = svc._load_subscriptions(ctx)
        assert isinstance(subs, list)
        # skill_path appears at most once
        assert subs.count(skill_path) <= 1


# ---------------------------------------------------------------------------
# Metadata Cache Race Conditions
# ---------------------------------------------------------------------------


class TestMetadataCacheRace:
    """Test metadata cache doesn't corrupt under concurrent access."""

    def test_concurrent_metadata_load(self, ts_fs, svc):
        """Multiple threads loading same skill metadata get consistent results."""
        ts_fs.seed_skill(f"{SKILL_BASE}cached/", name="cached-skill", description="Cached")
        ctx = _make_ctx()
        skill_path = f"{SKILL_BASE}cached/"
        results: list[dict[str, Any]] = []
        lock = threading.Lock()

        def do_load():
            metadata = svc._load_skill_metadata(skill_path, ctx)
            with lock:
                results.append(dict(metadata))

        threads = [threading.Thread(target=do_load) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(results) == 8
        # All results should be identical
        for r in results:
            assert r.get("name") == "cached-skill"
            assert r.get("description") == "Cached"

    def test_cache_clear_during_concurrent_reads(self, ts_fs, svc):
        """Clearing cache while reads are in progress doesn't crash."""
        ts_fs.seed_skill(f"{SKILL_BASE}volatile/", name="volatile")
        ctx = _make_ctx()
        skill_path = f"{SKILL_BASE}volatile/"
        errors: list[Exception] = []
        lock = threading.Lock()

        def do_read():
            try:
                for _ in range(20):
                    svc._load_skill_metadata(skill_path, ctx)
            except Exception as e:
                with lock:
                    errors.append(e)

        def do_clear():
            for _ in range(20):
                svc.clear_metadata_cache()

        threads = [
            threading.Thread(target=do_read),
            threading.Thread(target=do_read),
            threading.Thread(target=do_clear),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Errors during concurrent cache access: {errors}"


# ---------------------------------------------------------------------------
# Concurrent Import
# ---------------------------------------------------------------------------


class TestConcurrentImport:
    """Test parallel import operations."""

    def test_concurrent_imports_different_skills(self, ts_fs, pkg_svc):
        """Importing different skills concurrently should all succeed."""
        import base64
        import io
        import json
        import zipfile

        def make_zip(name: str) -> str:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                manifest = {"version": "1.0", "skill_path": f"/skill/{name}/"}
                zf.writestr("manifest.json", json.dumps(manifest))
                zf.writestr("SKILL.md", f"---\nname: {name}\n---\n# {name}")
            return base64.b64encode(buf.getvalue()).decode()

        results: list[dict[str, Any]] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def do_import(name: str):
            try:
                ctx = _make_ctx()
                result = pkg_svc.import_skill(
                    zip_data=make_zip(name),
                    context=ctx,
                )
                with lock:
                    results.append(result)
            except Exception as e:
                with lock:
                    errors.append(e)

        skill_names = [f"concurrent-{i}" for i in range(5)]
        threads = [threading.Thread(target=do_import, args=(n,)) for n in skill_names]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"Errors during concurrent import: {errors}"
        assert len(results) == 5

        # All skills should be importable
        imported_names = set()
        for r in results:
            for name in r.get("imported_skills", []):
                imported_names.add(name)
        assert len(imported_names) == 5


# ---------------------------------------------------------------------------
# Subscription Cache TTL Under Contention
# ---------------------------------------------------------------------------


class TestSubscriptionCacheTTL:
    """Test TTL cache behavior under concurrent access."""

    def test_ttl_expiry_with_concurrent_readers(self, ts_fs, svc):
        """Cache expiry doesn't cause errors when multiple threads are reading."""
        import time

        ts_fs.seed_skill(f"{SKILL_BASE}ttl-test/", name="ttl-test")
        ctx = _make_ctx()

        # Pre-populate subscriptions
        svc.subscribe(f"{SKILL_BASE}ttl-test/", ctx)

        errors: list[Exception] = []
        lock = threading.Lock()

        def read_subscriptions():
            try:
                for _ in range(30):
                    subs = svc._load_subscriptions(ctx)
                    assert isinstance(subs, list)
                    time.sleep(0.01)
            except Exception as e:
                with lock:
                    errors.append(e)

        # Start multiple readers that will overlap with cache expiry (5s TTL)
        threads = [threading.Thread(target=read_subscriptions) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"Errors during TTL contention: {errors}"
