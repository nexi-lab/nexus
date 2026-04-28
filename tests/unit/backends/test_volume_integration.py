"""Integration tests for CAS volume packing with feature interactions.

Tests feature combinations:
  - CDC chunked write → volume seal → read chunks from sealed volume
  - Batch read spanning multiple sealed volumes
  - Bloom filter seeded from volume index
  - Streaming write → volume append
  - CASLocalBackend end-to-end with volume transport

Issue #3403: CAS volume packing — feature integration.
"""

from __future__ import annotations

import pytest

try:
    from nexus_kernel import BlobPackEngine

    HAS_VOLUME_ENGINE = True
except ImportError:
    HAS_VOLUME_ENGINE = False

pytestmark = pytest.mark.skipif(
    not HAS_VOLUME_ENGINE, reason="nexus_kernel.BlobPackEngine not available"
)


def make_hash(seed: int) -> str:
    return f"{seed:064x}"


# ─── BlobPackEngine Core Integration ──────────────────────────────────────────


class TestVolumeEngineIntegration:
    """Direct BlobPackEngine tests — read/write/seal/compact end-to-end."""

    def test_write_seal_read_roundtrip(self, tmp_path):
        engine = BlobPackEngine(str(tmp_path / "vol"), target_volume_size=1024 * 1024)

        data_map = {}
        for i in range(10):
            h = make_hash(i)
            data = f"content_{i}".encode()
            engine.put(h, data)
            data_map[h] = data

        engine.seal_active()

        for h, expected in data_map.items():
            actual = bytes(engine.get(h))
            assert actual == expected

        engine.close()

    def test_batch_get_across_volumes(self, tmp_path):
        engine = BlobPackEngine(str(tmp_path / "vol"), target_volume_size=256)

        hashes = []
        for i in range(20):
            h = make_hash(i)
            engine.put(h, bytes([i] * 50))
            hashes.append(h)

        engine.seal_active()

        # Batch read all 20 hashes
        results = engine.batch_get(hashes)
        assert len(results) == 20
        for i, h in enumerate(hashes):
            assert results[h] == bytes([i] * 50)

        engine.close()

    def test_list_content_hashes(self, tmp_path):
        engine = BlobPackEngine(str(tmp_path / "vol"), target_volume_size=1024 * 1024)

        written = set()
        for i in range(5):
            h = make_hash(i)
            engine.put(h, b"data")
            written.add(h)

        engine.seal_active()

        listed = {h for h, _ts in engine.list_content_hashes()}
        assert listed == written

        engine.close()


# ─── BlobPackLocalTransport Integration ────────────────────────────────────────


class TestBlobPackLocalTransportIntegration:
    """BlobPackLocalTransport wrapping BlobPackEngine."""

    def _make_transport(self, tmp_path):
        from nexus.backends.transports.blob_pack_local_transport import BlobPackLocalTransport

        return BlobPackLocalTransport(root_path=tmp_path, fsync=False)

    def test_cas_and_dir_operations(self, tmp_path):
        """CAS keys go to volumes, dir keys go to filesystem."""
        transport = self._make_transport(tmp_path)

        # CAS write
        cas_key = "cas/ab/cd/abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        transport.store(cas_key, b"cas data")

        # Dir operation
        transport.create_dir("dirs/test/")
        assert transport.exists("dirs/test/")

        # CAS read (seal first for volume transport)
        transport.seal_active_volume()
        data, _ = transport.fetch(cas_key)
        assert data == b"cas data"

    def test_meta_sidecar_goes_to_delegate(self, tmp_path):
        """CDC .meta files should NOT go to volume engine."""
        transport = self._make_transport(tmp_path)

        meta_key = "cas/ab/cd/abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890.meta"
        transport.store(meta_key, b'{"is_chunked_manifest": true}')

        data, _ = transport.fetch(meta_key)
        assert b"is_chunked_manifest" in data

    def test_volume_stats(self, tmp_path):
        transport = self._make_transport(tmp_path)
        stats = transport.volume_stats()
        assert "total_blobs" in stats
        assert "sealed_volume_count" in stats

    def test_batch_read_mixed_keys(self, tmp_path):
        """Batch read with both CAS and non-CAS keys."""
        transport = self._make_transport(tmp_path)

        cas_key = "cas/ab/cd/abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        transport.store(cas_key, b"cas blob")

        meta_key = "cas/ab/cd/abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890.meta"
        transport.store(meta_key, b"meta blob")

        transport.seal_active_volume()

        result = transport.batch_fetch([cas_key, meta_key])
        assert result[cas_key] == b"cas blob"
        assert result[meta_key] == b"meta blob"


# ─── store_batch Integration (Issue #3409) ───────────────────────────────────


class TestStoreBatchIntegration:
    """BlobPackLocalTransport.store_batch end-to-end."""

    def _make_transport(self, tmp_path):
        from nexus.backends.transports.blob_pack_local_transport import BlobPackLocalTransport

        return BlobPackLocalTransport(root_path=tmp_path, fsync=False)

    def test_store_batch_roundtrip(self, tmp_path):
        """store_batch writes, seal, fetch reads back correctly."""
        import hashlib

        transport = self._make_transport(tmp_path)

        items = []
        expected = {}
        for i in range(20):
            data = f"batch_content_{i}".encode()
            h = hashlib.sha256(data).hexdigest()
            cas_key = f"cas/{h[:2]}/{h[2:4]}/{h}"
            items.append((cas_key, data))
            expected[cas_key] = data

        written = transport.store_batch(items)
        assert written == 20, f"Expected 20 written, got {written}"

        transport.seal_active_volume()

        for key, data in expected.items():
            fetched, _ = transport.fetch(key)
            assert fetched == data, f"Content mismatch for {key}"

    def test_store_batch_dedup(self, tmp_path):
        """store_batch skips blobs already written by prior put()."""
        import hashlib

        transport = self._make_transport(tmp_path)

        data = b"already_exists"
        h = hashlib.sha256(data).hexdigest()
        cas_key = f"cas/{h[:2]}/{h[2:4]}/{h}"

        # Write via normal put
        transport.store(cas_key, data)

        # store_batch with same hash should skip it
        written = transport.store_batch([(cas_key, data)])
        assert written == 0, f"Expected 0 new writes (dedup), got {written}"

    def test_store_batch_mixed_with_single_writes(self, tmp_path):
        """store_batch and single store() produce consistent state."""
        import hashlib

        transport = self._make_transport(tmp_path)

        # Single writes
        single_keys = {}
        for i in range(5):
            data = f"single_{i}".encode()
            h = hashlib.sha256(data).hexdigest()
            key = f"cas/{h[:2]}/{h[2:4]}/{h}"
            transport.store(key, data)
            single_keys[key] = data

        # Batch writes
        batch_items = []
        batch_keys = {}
        for i in range(5, 15):
            data = f"batch_{i}".encode()
            h = hashlib.sha256(data).hexdigest()
            key = f"cas/{h[:2]}/{h[2:4]}/{h}"
            batch_items.append((key, data))
            batch_keys[key] = data

        written = transport.store_batch(batch_items)
        assert written == 10

        transport.seal_active_volume()

        # All 15 items readable
        for key, data in {**single_keys, **batch_keys}.items():
            fetched, _ = transport.fetch(key)
            assert fetched == data

    def test_store_batch_empty(self, tmp_path):
        """store_batch with empty list returns 0."""
        transport = self._make_transport(tmp_path)
        assert transport.store_batch([]) == 0

    def test_store_batch_large_batch(self, tmp_path):
        """store_batch with 1000 items — verifies no data corruption at scale."""
        import hashlib

        transport = self._make_transport(tmp_path)

        items = []
        expected = {}
        for i in range(1000):
            data = f"large_batch_item_{i:05d}_{i * 7}".encode()
            h = hashlib.sha256(data).hexdigest()
            key = f"cas/{h[:2]}/{h[2:4]}/{h}"
            items.append((key, data))
            expected[key] = data

        written = transport.store_batch(items)
        assert written == 1000

        transport.seal_active_volume()

        # Verify a sample (every 100th item)
        keys_list = list(expected.keys())
        for idx in range(0, 1000, 100):
            key = keys_list[idx]
            fetched, _ = transport.fetch(key)
            assert fetched == expected[key], f"Mismatch at index {idx}"


# ─── CASLocalBackend Integration ─────────────────────────────────────────────


class TestCASLocalBackendWithVolumes:
    """CASLocalBackend end-to-end with volume transport."""

    def _make_backend(self, tmp_path):
        from nexus.backends.storage.cas_local import CASLocalBackend

        return CASLocalBackend(root_path=tmp_path, use_volume_packing=True)

    def test_write_read_roundtrip(self, tmp_path):
        backend = self._make_backend(tmp_path)
        content = b"hello from volume-packed CAS"
        result = backend.write_content(content)

        # Read-after-write should work without explicit seal
        read_back = backend.read_content(result.content_id)
        assert read_back == content

    def test_dedup(self, tmp_path):
        backend = self._make_backend(tmp_path)
        data = b"dedup test"

        r1 = backend.write_content(data)
        r2 = backend.write_content(data)
        assert r1.content_id == r2.content_id

    def test_content_exists(self, tmp_path):
        backend = self._make_backend(tmp_path)
        result = backend.write_content(b"exists test")
        assert backend.content_exists(result.content_id)

    def test_content_size(self, tmp_path):
        backend = self._make_backend(tmp_path)
        data = b"size test data"
        result = backend.write_content(data)

        # Size should be available immediately (from index)
        assert backend.get_content_size(result.content_id) == len(data)

    def test_delete_content(self, tmp_path):
        backend = self._make_backend(tmp_path)
        result = backend.write_content(b"to delete")
        backend.delete_content(result.content_id)
        assert not backend.content_exists(result.content_id)

    def test_batch_read(self, tmp_path):
        backend = self._make_backend(tmp_path)
        ids = []
        for i in range(5):
            result = backend.write_content(f"batch_{i}".encode())
            ids.append(result.content_id)

        results = backend.batch_read_content(ids)
        for i, cid in enumerate(ids):
            assert results[cid] == f"batch_{i}".encode()

    def test_stream_write(self, tmp_path):
        backend = self._make_backend(tmp_path)

        def chunks():
            yield b"chunk1"
            yield b"chunk2"
            yield b"chunk3"

        result = backend.write_stream(chunks())

        read_back = backend.read_content(result.content_id)
        assert read_back == b"chunk1chunk2chunk3"

    def test_directory_operations_unaffected(self, tmp_path):
        backend = self._make_backend(tmp_path)
        backend.mkdir("test_dir", parents=True, exist_ok=True)
        assert backend.is_directory("test_dir")
        entries = backend.list_dir("test_dir")
        assert isinstance(entries, list)

    def test_fallback_to_local_transport(self, tmp_path):
        """use_volume_packing=False should use LocalTransport."""
        from nexus.backends.storage.cas_local import CASLocalBackend
        from nexus.backends.transports.local_transport import LocalTransport

        backend = CASLocalBackend(root_path=tmp_path, use_volume_packing=False)
        assert isinstance(backend._transport, LocalTransport)

        content = b"fallback test"
        result = backend.write_content(content)
        read_back = backend.read_content(result.content_id)
        assert read_back == content


# ─── Bloom Filter + Volume Integration ───────────────────────────────────────


class TestBloomWithVolumes:
    """Bloom filter should be seeded from volume index."""

    def test_bloom_seeded_from_volumes(self, tmp_path):
        import gc

        from nexus.backends.storage.cas_local import CASLocalBackend

        # Write data and seal
        backend1 = CASLocalBackend(root_path=tmp_path, use_volume_packing=True)
        r = backend1.write_content(b"bloom seed test")
        if hasattr(backend1._transport, "seal_active_volume"):
            backend1._transport.seal_active_volume()
        if hasattr(backend1._transport, "close"):
            backend1._transport.close()
        # Release redb lock before reopening
        del backend1
        gc.collect()

        # Recreate backend — Bloom should be seeded from volume index
        backend2 = CASLocalBackend(root_path=tmp_path, use_volume_packing=True)
        # If Bloom is properly seeded, content_exists should be fast (Bloom hit)
        assert backend2.content_exists(r.content_id)
