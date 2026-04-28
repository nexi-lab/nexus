"""Transport conformance test suite — parametrized across transports.

Verifies that LocalTransport and BlobPackLocalTransport both implement
the Transport protocol identically from the engine's perspective.

Issue #3403: CAS volume packing — transport conformance.
"""

from __future__ import annotations

import pytest

from nexus.backends.transports.local_transport import LocalTransport


def _make_local_transport(tmp_path):
    return LocalTransport(root_path=tmp_path, fsync=False)


def _make_volume_transport(tmp_path):
    try:
        from nexus.backends.transports.blob_pack_local_transport import BlobPackLocalTransport

        return BlobPackLocalTransport(root_path=tmp_path, fsync=False)
    except Exception:
        pytest.skip("BlobPackLocalTransport not available (nexus_kernel not built)")


@pytest.fixture(params=["local", "volume"], ids=["LocalTransport", "BlobPackLocalTransport"])
def transport(request, tmp_path):
    if request.param == "local":
        return _make_local_transport(tmp_path)
    else:
        return _make_volume_transport(tmp_path)


@pytest.fixture
def local_transport(tmp_path):
    return _make_local_transport(tmp_path)


@pytest.fixture
def volume_transport(tmp_path):
    return _make_volume_transport(tmp_path)


# ─── Transport Protocol Conformance ──────────────────────────────────────


class TestPutGetRoundtrip:
    def test_put_get_basic(self, transport):
        key = "cas/ab/cd/abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        data = b"hello world"
        transport.store(key, data)
        result, version = transport.fetch(key)
        assert result == data

    def test_put_get_empty(self, transport):
        key = "cas/00/00/0000000000000000000000000000000000000000000000000000000000000000"
        data = b""
        transport.store(key, data)
        result, _ = transport.fetch(key)
        assert result == data

    def test_put_get_large(self, transport):
        key = "cas/ff/ff/ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        data = b"x" * (1024 * 1024)  # 1MB
        transport.store(key, data)
        result, _ = transport.fetch(key)
        assert result == data

    def test_put_overwrites(self, transport):
        key = "cas/ab/cd/abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        transport.store(key, b"first")
        transport.store(key, b"second")
        result, _ = transport.fetch(key)
        # Both transports should have the data (CAS is idempotent)
        assert result in (b"first", b"second")


class TestBlobExists:
    def test_exists_true(self, transport):
        key = "cas/ab/cd/abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        transport.store(key, b"data")
        assert transport.exists(key) is True

    def test_exists_false(self, transport):
        key = "cas/ab/cd/abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        assert transport.exists(key) is False


class TestGetBlobSize:
    def test_size(self, transport):
        key = "cas/ab/cd/abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        data = b"hello"
        transport.store(key, data)
        assert transport.get_size(key) == 5

    def test_size_not_found(self, transport):
        from nexus.contracts.exceptions import NexusFileNotFoundError

        key = "cas/ab/cd/abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        with pytest.raises(NexusFileNotFoundError):
            transport.get_size(key)


class TestDeleteBlob:
    def test_delete(self, transport):
        key = "cas/ab/cd/abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        transport.store(key, b"to delete")
        assert transport.exists(key) is True
        transport.remove(key)
        assert transport.exists(key) is False

    def test_delete_not_found(self, transport):
        from nexus.contracts.exceptions import NexusFileNotFoundError

        key = "cas/ab/cd/abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        with pytest.raises(NexusFileNotFoundError):
            transport.remove(key)


class TestGetBlobNotFound:
    def test_get_not_found(self, transport):
        from nexus.contracts.exceptions import NexusFileNotFoundError

        key = "cas/ab/cd/abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        with pytest.raises(NexusFileNotFoundError):
            transport.fetch(key)


class TestStreamBlob:
    def test_stream(self, transport):
        key = "cas/ab/cd/abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        data = b"streaming data here"
        transport.store(key, data)
        chunks = list(transport.stream(key, chunk_size=5))
        assert b"".join(chunks) == data

    def test_stream_not_found(self, transport):
        from nexus.contracts.exceptions import NexusFileNotFoundError

        key = "cas/de/ad/dead1234567890abcdef1234567890abcdef1234567890abcdef12345678dead"
        with pytest.raises(NexusFileNotFoundError):
            list(transport.stream(key))


class TestDirectoryMarker:
    def test_create_dir_marker(self, transport):
        key = "dirs/test/subdir/"
        transport.create_dir(key)
        assert transport.exists(key) is True


class TestCopyBlob:
    def test_copy(self, transport):
        src = "cas/ab/cd/abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        dst = "cas/ef/01/ef011234567890abcdef1234567890abcdef1234567890abcdef12345678ef01"
        data = b"copy me"
        transport.store(src, data)
        transport.copy_key(src, dst)
        result, _ = transport.fetch(dst)
        assert result == data


# ─── Extended Methods ────────────────────────────────────────────────────────


class TestListContentHashes:
    def test_empty(self, transport):
        if not hasattr(transport, "list_content_hashes"):
            pytest.skip("Transport does not support list_content_hashes")
        result = transport.list_content_hashes()
        assert result == []

    def test_after_put(self, transport):
        if not hasattr(transport, "list_content_hashes"):
            pytest.skip("Transport does not support list_content_hashes")

        hash_hex = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        key = f"cas/{hash_hex[:2]}/{hash_hex[2:4]}/{hash_hex}"
        transport.store(key, b"test data")

        # For volume transport, seal first to make entries visible
        if hasattr(transport, "seal_active_volume"):
            transport.seal_active_volume()

        result = transport.list_content_hashes()
        hashes = [h for h, _ts in result]
        assert hash_hex in hashes


class TestBatchGetBlobs:
    def test_batch_get(self, transport):
        if not hasattr(transport, "batch_fetch"):
            pytest.skip("Transport does not support batch_fetch")

        keys = []
        for i in range(5):
            h = f"{i:064x}"
            key = f"cas/{h[:2]}/{h[2:4]}/{h}"
            transport.store(key, f"data_{i}".encode())
            keys.append(key)

        # Seal for volume transport
        if hasattr(transport, "seal_active_volume"):
            transport.seal_active_volume()

        result = transport.batch_fetch(keys)
        assert len(result) == 5
        for i, key in enumerate(keys):
            assert result[key] == f"data_{i}".encode()

    def test_batch_get_missing(self, transport):
        if not hasattr(transport, "batch_fetch"):
            pytest.skip("Transport does not support batch_fetch")

        key = "cas/ff/ff/ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
        result = transport.batch_fetch([key])
        assert result[key] is None


class TestGetBlobMtime:
    def test_mtime(self, transport):
        if not hasattr(transport, "get_mtime"):
            pytest.skip("Transport does not support get_mtime")

        key = "cas/ab/cd/abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        transport.store(key, b"data")
        mtime = transport.get_mtime(key)
        assert isinstance(mtime, float)
        assert mtime > 0


class TestPutBlobNosync:
    def test_nosync(self, transport):
        if not hasattr(transport, "store_nosync"):
            pytest.skip("Transport does not support store_nosync")

        key = "cas/ab/cd/abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        transport.store_nosync(key, b"nosync data")
        result, _ = transport.fetch(key)
        assert result == b"nosync data"
