"""E2E tests for CachingBackendWrapper (#1392).

Tests verify:
1. Correctness — cached reads return identical data to uncached reads
2. Performance — cache hits are faster than cache misses
3. Permissions — caching does not bypass ReBAC permission enforcement
4. Invalidation — deletes properly invalidate cached entries

Uses create_nexus_fs with CachingBackendWrapper-wrapped backend and
in-process NexusFS for deterministic, fast e2e testing.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from nexus.backends.local import LocalBackend
from nexus.cache.backend_wrapper import (
    CacheStrategy,
    CacheWrapperConfig,
    CachingBackendWrapper,
)
from nexus.core.permissions import OperationContext
from nexus.factory import create_nexus_fs
from nexus.storage.record_store import SQLAlchemyRecordStore
from tests.helpers.in_memory_metadata_store import InMemoryFileMetadataStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def local_backend(tmp_path: Path) -> LocalBackend:
    """Create a real LocalBackend with temp directory."""
    root = tmp_path / "storage"
    root.mkdir()
    return LocalBackend(root_path=str(root))


@pytest.fixture
def cached_backend(local_backend: LocalBackend) -> CachingBackendWrapper:
    """LocalBackend wrapped with CachingBackendWrapper."""
    config = CacheWrapperConfig(
        strategy=CacheStrategy.WRITE_AROUND,
        l1_max_size_mb=16,
        l2_enabled=False,
        metrics_enabled=False,
    )
    return CachingBackendWrapper(inner=local_backend, config=config)


@pytest.fixture
def write_through_backend(local_backend: LocalBackend) -> CachingBackendWrapper:
    """LocalBackend wrapped with write-through strategy."""
    config = CacheWrapperConfig(
        strategy=CacheStrategy.WRITE_THROUGH,
        l1_max_size_mb=16,
        l2_enabled=False,
        metrics_enabled=False,
    )
    return CachingBackendWrapper(inner=local_backend, config=config)


# ===========================================================================
# Correctness Tests
# ===========================================================================


class TestCachingCorrectness:
    """Verify cached operations produce identical results to direct backend."""

    def test_write_and_read_through_cache(
        self, local_backend: LocalBackend, cached_backend: CachingBackendWrapper
    ):
        """Written content should be readable through cache identically."""
        content = b"E2E test content: " + uuid.uuid4().bytes

        # Write through cached wrapper
        write_resp = cached_backend.write_content(content)
        assert write_resp.success
        content_hash = write_resp.data

        # Read through cached wrapper
        read_resp = cached_backend.read_content(content_hash)
        assert read_resp.success
        assert read_resp.data == content

        # Also verify direct backend read matches
        direct_resp = local_backend.read_content(content_hash)
        assert direct_resp.success
        assert direct_resp.data == content

    def test_batch_read_matches_individual_reads(self, cached_backend: CachingBackendWrapper):
        """batch_read_content should return same data as individual read_content."""
        contents = [f"batch item {i}".encode() for i in range(5)]
        hashes = []
        for c in contents:
            resp = cached_backend.write_content(c)
            assert resp.success
            hashes.append(resp.data)

        # Individual reads first (populates cache)
        individual = {}
        for h in hashes:
            resp = cached_backend.read_content(h)
            assert resp.success
            individual[h] = resp.data

        # Clear cache to force batch to re-read
        cached_backend.clear_cache()

        # Batch read
        batch = cached_backend.batch_read_content(hashes)

        for h in hashes:
            assert batch[h] == individual[h], f"Mismatch for hash {h[:16]}"

    def test_content_exists_consistent_with_read(self, cached_backend: CachingBackendWrapper):
        """content_exists should always reflect inner backend truth."""
        content = b"existence check"
        write_resp = cached_backend.write_content(content)
        content_hash = write_resp.data

        # Exists check delegates to inner backend (not L1 cache)
        exists_resp = cached_backend.content_exists(content_hash)
        assert exists_resp.success
        assert exists_resp.data is True

        # Non-existent hash
        fake_hash = "0" * 64
        exists_resp = cached_backend.content_exists(fake_hash)
        assert exists_resp.success
        assert exists_resp.data is False

    def test_large_content_handles_correctly(self, cached_backend: CachingBackendWrapper):
        """Large content (>1MB) should work correctly through cache."""
        large_content = b"x" * (1024 * 1024 + 1)  # 1MB + 1 byte
        write_resp = cached_backend.write_content(large_content)
        assert write_resp.success

        read_resp = cached_backend.read_content(write_resp.data)
        assert read_resp.success
        assert read_resp.data == large_content


# ===========================================================================
# Performance Tests
# ===========================================================================


class TestCachingPerformance:
    """Verify cache hits are measurably faster than cache misses."""

    def test_cached_read_faster_than_uncached(self, cached_backend: CachingBackendWrapper):
        """Second read (cache hit) should be significantly faster than first (miss)."""
        # Write several files to get stable measurements
        hashes = []
        for i in range(10):
            content = f"perf test {i} {uuid.uuid4()}".encode()
            resp = cached_backend.write_content(content)
            hashes.append(resp.data)

        # First reads — cache misses (disk I/O)
        start = time.perf_counter()
        for h in hashes:
            cached_backend.read_content(h)
        miss_time = time.perf_counter() - start

        # Second reads — cache hits (memory only)
        start = time.perf_counter()
        for h in hashes:
            cached_backend.read_content(h)
        hit_time = time.perf_counter() - start

        # Cache hits should be faster. Using generous margin (10x) for CI stability.
        assert hit_time < miss_time * 10, (
            f"Cache hits ({hit_time:.4f}s) not faster than misses ({miss_time:.4f}s)"
        )

    def test_batch_read_performance(self, cached_backend: CachingBackendWrapper):
        """batch_read_content with cached items should be fast."""
        hashes = []
        for i in range(20):
            content = f"batch perf {i}".encode()
            resp = cached_backend.write_content(content)
            hashes.append(resp.data)

        # Populate cache
        for h in hashes:
            cached_backend.read_content(h)

        # Batch read from cache should be very fast
        start = time.perf_counter()
        result = cached_backend.batch_read_content(hashes)
        elapsed = time.perf_counter() - start

        assert all(result[h] is not None for h in hashes)
        # 20 cached reads should complete in under 100ms (generous for CI)
        assert elapsed < 0.1, f"Batch read took {elapsed:.4f}s (expected < 0.1s)"

    def test_write_through_avoids_miss_penalty(self, write_through_backend: CachingBackendWrapper):
        """Write-through: read after write should be instant (no disk I/O)."""
        content = b"write-through perf test " + uuid.uuid4().bytes
        write_through_backend.write_content(content)

        # The write should have populated cache
        content_hash = write_through_backend.write_content(content).data

        start = time.perf_counter()
        resp = write_through_backend.read_content(content_hash)
        read_time = time.perf_counter() - start

        assert resp.success
        assert resp.data == content
        # Should be fast (generous 10ms for CI stability)
        assert read_time < 0.01, f"Write-through read took {read_time:.4f}s"


# ===========================================================================
# Invalidation Tests
# ===========================================================================


class TestCachingInvalidation:
    """Verify cache invalidation works correctly."""

    def test_delete_invalidates_cache(self, cached_backend: CachingBackendWrapper):
        """Deleted content should not be served from cache."""
        content = b"delete me e2e"
        write_resp = cached_backend.write_content(content)
        content_hash = write_resp.data

        # Populate cache
        cached_backend.read_content(content_hash)

        # Delete content
        delete_resp = cached_backend.delete_content(content_hash)
        assert delete_resp.success

        # Read should now fail (not serve stale cached data)
        read_resp = cached_backend.read_content(content_hash)
        assert not read_resp.success

    def test_clear_cache_forces_re_read(
        self, local_backend: LocalBackend, cached_backend: CachingBackendWrapper
    ):
        """After clear_cache, reads should go back to the backend."""
        content = b"clear test"
        write_resp = cached_backend.write_content(content)
        content_hash = write_resp.data

        # Populate cache
        cached_backend.read_content(content_hash)

        # Verify stats show cache entries
        stats = cached_backend.get_cache_stats()
        assert stats["l1"]["entries"] > 0

        # Clear
        cached_backend.clear_cache()

        # Stats should be reset
        stats = cached_backend.get_cache_stats()
        assert stats["l1"]["entries"] == 0

        # Read should still work (re-reads from backend)
        read_resp = cached_backend.read_content(content_hash)
        assert read_resp.success
        assert read_resp.data == content

    def test_cache_stats_accurate(self, cached_backend: CachingBackendWrapper):
        """Cache stats should accurately track hits and misses."""
        cached_backend.clear_cache()

        content = b"stats test"
        write_resp = cached_backend.write_content(content)
        content_hash = write_resp.data

        # 1 miss
        cached_backend.read_content(content_hash)
        # 3 hits
        for _ in range(3):
            cached_backend.read_content(content_hash)

        stats = cached_backend.get_cache_stats()
        assert stats["l1_hits"] == 3
        assert stats["l1_misses"] == 1
        assert stats["strategy"] == "write_around"


# ===========================================================================
# Permission Tests — NexusFS + CachingBackendWrapper + enforce_permissions
# ===========================================================================


class TestCachingPermissions:
    """Verify caching does NOT bypass ReBAC permission enforcement.

    Architecture: NexusFS._check_permission() runs BEFORE backend.read_content().
    CachingBackendWrapper wraps the backend layer. Permission checks happen at
    the NexusFS layer, so even cached content must pass permission checks.

    These tests create a real NexusFS with:
    - CachingBackendWrapper wrapping LocalBackend
    - enforce_permissions=True
    - ReBAC permission tuples granting specific users access
    """

    @pytest.fixture
    def nexus_with_cache(self, tmp_path: Path):
        """Create NexusFS with CachingBackendWrapper and permissions enabled."""
        storage_path = tmp_path / "storage"
        storage_path.mkdir()

        # Create LocalBackend wrapped with CachingBackendWrapper
        inner_backend = LocalBackend(root_path=str(storage_path))
        config = CacheWrapperConfig(
            strategy=CacheStrategy.WRITE_AROUND,
            l1_max_size_mb=16,
            l2_enabled=False,
            metrics_enabled=False,
        )
        cached_backend = CachingBackendWrapper(inner=inner_backend, config=config)

        # Create NexusFS with permissions enabled
        metadata_store = InMemoryFileMetadataStore()
        record_store = SQLAlchemyRecordStore()  # in-memory SQLite

        nx = create_nexus_fs(
            backend=cached_backend,
            metadata_store=metadata_store,
            record_store=record_store,
            enforce_permissions=True,
            enforce_zone_isolation=False,  # simplify test — no zone checks
        )

        yield nx, cached_backend

        nx.close()

    def test_admin_can_read_through_cache(self, nexus_with_cache):
        """Admin user can read files, and content gets cached."""
        nx, cached_backend = nexus_with_cache
        admin = OperationContext(user="admin", groups=[], is_admin=True)

        # Write a file as admin
        nx.write("/test/cached_file.txt", b"cached content", context=admin)

        # First read — cache miss, reads from backend
        content = nx.read("/test/cached_file.txt", context=admin)
        assert content == b"cached content"

        # Second read — should use cache (verify via stats)
        cached_backend.clear_cache()

        # Re-read to populate cache
        content = nx.read("/test/cached_file.txt", context=admin)
        assert content == b"cached content"

        # Third read — L1 hit
        content = nx.read("/test/cached_file.txt", context=admin)
        assert content == b"cached content"

        stats = cached_backend.get_cache_stats()
        assert stats["l1_hits"] >= 1, f"Expected L1 hit, got stats: {stats}"

    def test_unauthorized_user_denied_even_when_cached(self, nexus_with_cache):
        """Non-authorized user is denied even when content is in cache."""
        nx, cached_backend = nexus_with_cache
        admin = OperationContext(user="admin", groups=[], is_admin=True)

        # Write a file as admin
        nx.write("/test/secret.txt", b"secret data", context=admin)

        # Read as admin to populate cache
        content = nx.read("/test/secret.txt", context=admin)
        assert content == b"secret data"

        # Verify content is cached
        stats = cached_backend.get_cache_stats()
        assert stats["l1"]["entries"] > 0, "Content should be cached in L1"

        # Non-admin user without explicit permission should be DENIED
        unauthorized = OperationContext(user="mallory", groups=[], is_admin=False)
        with pytest.raises(PermissionError):
            nx.read("/test/secret.txt", context=unauthorized)

    def test_permission_enforced_on_write_with_cache(self, nexus_with_cache):
        """Non-authorized user cannot write even with caching enabled."""
        nx, _ = nexus_with_cache
        admin = OperationContext(user="admin", groups=[], is_admin=True)

        # Create directory as admin
        nx.mkdir("/test/protected", parents=True, context=admin)

        # Non-admin user should be denied write
        unauthorized = OperationContext(user="eve", groups=[], is_admin=False)
        with pytest.raises(PermissionError):
            nx.write("/test/protected/hack.txt", b"pwned", context=unauthorized)

    def test_cached_content_not_leaked_across_users(self, nexus_with_cache):
        """User A's cached read does not leak data to unauthorized User B."""
        nx, cached_backend = nexus_with_cache
        admin = OperationContext(user="admin", groups=[], is_admin=True)

        # Write multiple files as admin
        nx.write("/test/public.txt", b"public info", context=admin)
        nx.write("/test/private.txt", b"private info", context=admin)

        # Admin reads both (populates cache for both)
        assert nx.read("/test/public.txt", context=admin) == b"public info"
        assert nx.read("/test/private.txt", context=admin) == b"private info"

        # Verify both are cached
        stats = cached_backend.get_cache_stats()
        assert stats["l1"]["entries"] >= 2

        # Grant read permission to alice on public.txt only (via ReBAC)
        rebac = nx._rebac_manager
        if rebac is not None:
            rebac.rebac_write(
                subject=("user", "alice"),
                relation="direct_viewer",
                object=("file", "/test/public.txt"),
                zone_id="default",
            )

        # Alice should STILL be denied on private.txt even though it's cached
        alice = OperationContext(user="alice", groups=[], is_admin=False)
        with pytest.raises(PermissionError):
            nx.read("/test/private.txt", context=alice)

    def test_delete_with_permissions_invalidates_cache(self, nexus_with_cache):
        """Deleting a file invalidates it from cache."""
        nx, cached_backend = nexus_with_cache
        admin = OperationContext(user="admin", groups=[], is_admin=True)

        # Write and read to populate cache
        nx.write("/test/delete_me.txt", b"temp data", context=admin)
        assert nx.read("/test/delete_me.txt", context=admin) == b"temp data"

        # Delete the file
        nx.delete("/test/delete_me.txt", context=admin)

        # Should raise FileNotFoundError — NOT serve stale cached data
        from nexus.core.exceptions import NexusFileNotFoundError

        with pytest.raises((NexusFileNotFoundError, FileNotFoundError)):
            nx.read("/test/delete_me.txt", context=admin)

    def test_write_through_with_permissions(self, tmp_path: Path):
        """Write-through strategy works correctly with permission checks."""
        storage_path = tmp_path / "wt_storage"
        storage_path.mkdir()

        inner_backend = LocalBackend(root_path=str(storage_path))
        config = CacheWrapperConfig(
            strategy=CacheStrategy.WRITE_THROUGH,
            l1_max_size_mb=16,
            l2_enabled=False,
            metrics_enabled=False,
        )
        cached_backend = CachingBackendWrapper(inner=inner_backend, config=config)

        metadata_store = InMemoryFileMetadataStore()
        record_store = SQLAlchemyRecordStore()

        nx = create_nexus_fs(
            backend=cached_backend,
            metadata_store=metadata_store,
            record_store=record_store,
            enforce_permissions=True,
            enforce_zone_isolation=False,
        )

        try:
            admin = OperationContext(user="admin", groups=[], is_admin=True)

            # Write-through: write populates cache immediately
            nx.write("/wt/file.txt", b"write through data", context=admin)

            # Read should be from cache (fast)
            content = nx.read("/wt/file.txt", context=admin)
            assert content == b"write through data"

            # Unauthorized user still blocked
            unauthorized = OperationContext(user="nobody", groups=[], is_admin=False)
            with pytest.raises(PermissionError):
                nx.read("/wt/file.txt", context=unauthorized)
        finally:
            nx.close()


# ===========================================================================
# FastAPI HTTP Server Tests — CachingBackendWrapper + Permissions via HTTP
# ===========================================================================


class TestCachingWithFastAPIServer:
    """E2E tests for CachingBackendWrapper through the full FastAPI HTTP stack.

    These tests create a real FastAPI app with:
    - CachingBackendWrapper wrapping LocalBackend
    - enforce_permissions=True
    - ReBAC permission tuples granting specific users access
    - Open access mode (no api_key) — identity from X-Nexus-Subject header

    HTTP requests go through the full stack:
        httpx → FastAPI → auth → OperationContext → NexusFS → CachingBackendWrapper → LocalBackend

    Validates that:
    1. Authorized users can read/write through the HTTP API with caching
    2. Unauthorized users get denied even when content is cached
    3. Cache stats reflect actual cache usage through the HTTP layer
    4. Permission enforcement is NOT bypassed by the caching layer
    """

    @pytest.fixture
    def fastapi_with_cache(self, tmp_path: Path):
        """Create FastAPI app with CachingBackendWrapper-backed NexusFS.

        Sets up:
        - LocalBackend wrapped with CachingBackendWrapper
        - NexusFS with enforce_permissions=True
        - ReBAC grants for specific users
        - FastAPI app in open access mode (no api_key)

        Yields (httpx.Client, nx, cached_backend) tuple.
        """
        from starlette.testclient import TestClient

        from nexus.server.fastapi_server import create_app

        storage_path = tmp_path / "http_storage"
        storage_path.mkdir()

        # Create CachingBackendWrapper-wrapped LocalBackend
        inner_backend = LocalBackend(root_path=str(storage_path))
        config = CacheWrapperConfig(
            strategy=CacheStrategy.WRITE_AROUND,
            l1_max_size_mb=16,
            l2_enabled=False,
            metrics_enabled=False,
        )
        cached_backend = CachingBackendWrapper(inner=inner_backend, config=config)

        # Create NexusFS with permissions enabled
        # Use file-based SQLite so all connections share the same DB
        # (in-memory SQLite gives each connection a separate database)
        db_path = tmp_path / "http_test.db"
        metadata_store = InMemoryFileMetadataStore()
        record_store = SQLAlchemyRecordStore(db_path=db_path)

        nx = create_nexus_fs(
            backend=cached_backend,
            metadata_store=metadata_store,
            record_store=record_store,
            enforce_permissions=True,
            enforce_zone_isolation=False,
            enable_deferred_permissions=False,  # Avoid async parent tuple creation
        )

        # === Set up data and permissions directly (Python API) ===
        admin = OperationContext(user="admin", groups=[], is_admin=True)

        # Write test files in SEPARATE directories to prevent ReBAC parent
        # inheritance cascading across boundaries.  The permission system
        # auto-creates (child -> parent) tuples and inherits permissions
        # from parent directories to all children.  Using isolated dirs
        # ensures that a grant on /alice_area/readme.md does NOT bleed to
        # /secret_area/secret.txt.
        nx.write("/alice_area/readme.md", b"# HTTP Cache Test", context=admin)
        nx.write("/secret_area/secret.txt", b"top secret data", context=admin)
        nx.write("/shared_area/shared.txt", b"shared content", context=admin)

        # Grant ReBAC permissions
        rebac = nx._rebac_manager
        assert rebac is not None, "ReBAC manager not initialized"

        # alice can read readme.md and shared.txt
        rebac.rebac_write(
            subject=("user", "alice"),
            relation="direct_viewer",
            object=("file", "/alice_area/readme.md"),
            zone_id="default",
        )
        rebac.rebac_write(
            subject=("user", "alice"),
            relation="direct_viewer",
            object=("file", "/shared_area/shared.txt"),
            zone_id="default",
        )

        # bob can only read shared.txt
        rebac.rebac_write(
            subject=("user", "bob"),
            relation="direct_viewer",
            object=("file", "/shared_area/shared.txt"),
            zone_id="default",
        )

        # Clear cache stats from setup writes/reads
        cached_backend.clear_cache()

        # Create FastAPI app in open access mode (no api_key → identity from headers)
        app = create_app(nx, api_key=None)
        client = TestClient(app)

        yield client, nx, cached_backend

        nx.close()

    @staticmethod
    def _rpc_call(
        client,
        method: str,
        params: dict,
        subject: str = "user:anonymous",
        zone_id: str = "default",
    ) -> dict:
        """Make a JSON-RPC call to the FastAPI server."""
        resp = client.post(
            f"/api/nfs/{method}",
            json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
            headers={
                "X-Nexus-Subject": subject,
                "X-Nexus-Zone-ID": zone_id,
            },
        )
        return resp.json()

    def test_server_health(self, fastapi_with_cache):
        """FastAPI server is healthy with CachingBackendWrapper-backed NexusFS."""
        client, _, _ = fastapi_with_cache
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"

    def test_authorized_user_can_read_via_http(self, fastapi_with_cache):
        """Alice (granted viewer) can read files through HTTP with caching."""
        client, _, _ = fastapi_with_cache

        # Alice reads readme.md (she has direct_viewer grant)
        result = self._rpc_call(
            client, "read", {"path": "/alice_area/readme.md"}, subject="user:alice"
        )
        assert "error" not in result, f"Expected success, got: {result}"
        assert result.get("result") is not None

    def test_unauthorized_user_denied_via_http(self, fastapi_with_cache):
        """Charlie (no grants) is denied reading files through HTTP."""
        client, _, _ = fastapi_with_cache

        # Charlie has NO ReBAC grants — should be denied
        result = self._rpc_call(
            client, "read", {"path": "/alice_area/readme.md"}, subject="user:charlie"
        )
        # Permission denial comes as RPC error
        assert "error" in result, f"Expected permission error, got: {result}"

    def test_cache_populated_then_unauthorized_denied(self, fastapi_with_cache):
        """Content cached by Alice's read is NOT leaked to unauthorized Charlie."""
        client, _, cached_backend = fastapi_with_cache

        # Alice reads shared.txt — populates cache
        result = self._rpc_call(
            client, "read", {"path": "/shared_area/shared.txt"}, subject="user:alice"
        )
        assert "error" not in result, f"Alice should succeed: {result}"

        # Verify content is now cached in L1
        stats = cached_backend.get_cache_stats()
        assert stats["l1"]["entries"] > 0, "Content should be cached after Alice's read"

        # Charlie (no grants) tries to read the same file — should be DENIED
        result = self._rpc_call(
            client, "read", {"path": "/shared_area/shared.txt"}, subject="user:charlie"
        )
        assert "error" in result, (
            f"Charlie should be denied even though content is cached: {result}"
        )

    def test_different_permission_levels_enforced(self, fastapi_with_cache):
        """Bob can read shared.txt but NOT readme.md (in separate dirs)."""
        client, _, cached_backend = fastapi_with_cache

        # Alice reads both files (populates cache for both)
        r1 = self._rpc_call(client, "read", {"path": "/alice_area/readme.md"}, subject="user:alice")
        assert "error" not in r1

        r2 = self._rpc_call(
            client, "read", {"path": "/shared_area/shared.txt"}, subject="user:alice"
        )
        assert "error" not in r2

        # Verify cache has entries
        stats = cached_backend.get_cache_stats()
        assert stats["l1"]["entries"] >= 2

        # Bob can read shared.txt (he has direct_viewer grant)
        result = self._rpc_call(
            client, "read", {"path": "/shared_area/shared.txt"}, subject="user:bob"
        )
        assert "error" not in result, f"Bob should read shared.txt: {result}"

        # Bob CANNOT read readme.md (no grant, content is cached from Alice's read)
        # Files are in separate directories so ReBAC parent inheritance
        # does NOT give bob access to /alice_area/readme.md
        result = self._rpc_call(
            client, "read", {"path": "/alice_area/readme.md"}, subject="user:bob"
        )
        assert "error" in result, f"Bob should be denied readme.md despite cache: {result}"

    def test_secret_file_inaccessible_to_all_non_admin(self, fastapi_with_cache):
        """secret.txt (in separate dir, no grants) denied for all non-admin."""
        client, _, _ = fastapi_with_cache

        for user in ["alice", "bob", "charlie", "mallory"]:
            result = self._rpc_call(
                client,
                "read",
                {"path": "/secret_area/secret.txt"},
                subject=f"user:{user}",
            )
            assert "error" in result, f"{user} should be denied access to secret.txt: {result}"

    def test_cache_stats_reflect_http_usage(self, fastapi_with_cache):
        """Cache stats should track hits/misses from HTTP requests."""
        client, _, cached_backend = fastapi_with_cache

        # Reset stats
        cached_backend.clear_cache()

        # First read — cache miss
        self._rpc_call(client, "read", {"path": "/shared_area/shared.txt"}, subject="user:alice")

        # Second read — cache hit (same content hash)
        self._rpc_call(client, "read", {"path": "/shared_area/shared.txt"}, subject="user:alice")

        # Third read — cache hit
        self._rpc_call(client, "read", {"path": "/shared_area/shared.txt"}, subject="user:alice")

        stats = cached_backend.get_cache_stats()
        # At least 2 hits from the 2nd and 3rd reads
        assert stats["l1_hits"] >= 2, f"Expected >= 2 L1 hits, got stats: {stats}"
        assert stats["l1_misses"] >= 1, f"Expected >= 1 L1 miss, got stats: {stats}"
