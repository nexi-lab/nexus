"""Integration test: full write → read → expire → 404 path (Issue #3405).

Tests the complete TTL lifecycle through CASAddressingEngine → VolumeLocalTransport
→ BlobPackEngine with real Rust engines (no mocks).
"""

from __future__ import annotations

import time

import pytest


def _vol_engine_available() -> bool:
    try:
        from nexus_kernel import BlobPackEngine  # noqa: F401

        return True
    except ImportError:
        return False


needs_vol_engine = pytest.mark.skipif(
    not _vol_engine_available(), reason="nexus_kernel.BlobPackEngine not available"
)


@needs_vol_engine
class TestTTLFullPathIntegration:
    """End-to-end: CASAddressingEngine → VolumeLocalTransport → BlobPackEngine."""

    def _make_engine(self, tmp_path):
        """Create a CASAddressingEngine backed by VolumeLocalTransport."""
        from nexus.backends.base.cas_addressing_engine import CASAddressingEngine
        from nexus.backends.transports.volume_local_transport import VolumeLocalTransport

        transport = VolumeLocalTransport(str(tmp_path))
        engine = CASAddressingEngine(transport=transport, backend_name="test_cas")
        return engine, transport

    def _make_context(self, ttl_seconds: float | None = None):
        from nexus.contracts.types import OperationContext

        return OperationContext(
            user_id="test",
            groups=[],
            ttl_seconds=ttl_seconds,
        )

    def test_write_without_ttl_goes_permanent(self, tmp_path) -> None:
        engine, transport = self._make_engine(tmp_path)
        ctx = self._make_context(ttl_seconds=None)

        result = engine.write_content(b"permanent data", context=ctx)
        assert result.content_id
        assert result.size == len(b"permanent data")

        # No TTL engines created
        assert transport.ttl_engine_count == 0

        # Readable
        data = engine.read_content(result.content_id)
        assert data == b"permanent data"

    def test_write_with_ttl_goes_to_bucket(self, tmp_path) -> None:
        engine, transport = self._make_engine(tmp_path)
        ctx = self._make_context(ttl_seconds=60.0)  # → bucket "1m"

        result = engine.write_content(b"ephemeral data", context=ctx)
        assert result.content_id

        # Should have created a TTL engine
        assert transport.ttl_engine_count >= 1

        # Readable (not yet expired)
        data = engine.read_content(result.content_id)
        assert data == b"ephemeral data"

    def test_write_read_expire_404(self, tmp_path) -> None:
        """The core acceptance test: write → read → expire → 404."""
        from nexus.contracts.exceptions import NexusFileNotFoundError

        engine, transport = self._make_engine(tmp_path)

        # Write with very small TTL
        ctx = self._make_context(ttl_seconds=0.001)
        result = engine.write_content(b"short lived", context=ctx)
        content_id = result.content_id

        # Seal so the sweeper can operate
        for eng in transport._ttl_engines.values():
            eng.seal_active()

        # Wait for expiry
        time.sleep(0.02)

        # Read should raise NexusFileNotFoundError (expired at read time)
        with pytest.raises(NexusFileNotFoundError):
            engine.read_content(content_id)

        # Run the sweeper
        results = transport.expire_ttl_volumes()
        total_expired = sum(count for _, count in results)
        assert total_expired >= 1

    def test_permanent_and_ttl_coexist(self, tmp_path) -> None:
        """Permanent and TTL content coexist without interference."""
        from nexus.contracts.exceptions import NexusFileNotFoundError

        engine, transport = self._make_engine(tmp_path)

        # Write permanent
        ctx_perm = self._make_context(ttl_seconds=None)
        r_perm = engine.write_content(b"permanent", context=ctx_perm)

        # Write TTL (will expire after 2 seconds)
        ctx_ttl = self._make_context(ttl_seconds=2.0)
        r_ttl = engine.write_content(b"ephemeral", context=ctx_ttl)

        # Both readable initially (within TTL window)
        assert engine.read_content(r_perm.content_id) == b"permanent"
        assert engine.read_content(r_ttl.content_id) == b"ephemeral"

        # Wait for TTL expiry
        time.sleep(2.1)

        # Permanent still readable, TTL expired (raises NexusFileNotFoundError)
        assert engine.read_content(r_perm.content_id) == b"permanent"
        with pytest.raises(NexusFileNotFoundError):
            engine.read_content(r_ttl.content_id)

    def test_ttl_write_dedup_same_content(self, tmp_path) -> None:
        """Same content written twice should be deduplicated, even with TTL."""
        engine, transport = self._make_engine(tmp_path)

        ctx = self._make_context(ttl_seconds=3600.0)
        r1 = engine.write_content(b"dedup me", context=ctx)
        r2 = engine.write_content(b"dedup me", context=ctx)

        assert r1.content_id == r2.content_id  # same hash


@needs_vol_engine
class TestTTLGCSeparation:
    """Verify GC only operates on permanent engine (decision 7A)."""

    def test_list_content_hashes_excludes_ttl(self, tmp_path) -> None:
        """list_content_ids() only returns permanent engine hashes."""
        from nexus.backends.transports.volume_local_transport import VolumeLocalTransport

        transport = VolumeLocalTransport(str(tmp_path))

        # Write to permanent
        h_perm = f"{'a' * 64}"
        transport.store(f"cas/{h_perm[:2]}/{h_perm[2:4]}/{h_perm}", b"perm")

        # Write to TTL bucket
        h_ttl = f"{'b' * 64}"
        transport.store_ttl(f"cas/{h_ttl[:2]}/{h_ttl[2:4]}/{h_ttl}", b"ttl", ttl_seconds=60.0)

        # list_content_ids should only return permanent hashes
        hashes = transport.list_content_ids()
        hash_set = {h for h, _ in hashes}

        assert h_perm in hash_set
        assert h_ttl not in hash_set  # TTL hashes excluded from GC scope

        transport.close()
