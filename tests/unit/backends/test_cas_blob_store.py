"""Unit tests for CASBlobStore (Issue #925)."""

import json

import pytest

from nexus.backends.cas_blob_store import CASBlobStore, CASMeta, cas_retry
from nexus.core.hash_fast import hash_content

# === CASMeta Tests ===


class TestCASMeta:
    """Tests for CASMeta frozen dataclass."""

    def test_default_values(self):
        meta = CASMeta()
        assert meta.ref_count == 0
        assert meta.size == 0
        assert meta.extra == ()

    def test_immutability(self):
        meta = CASMeta(ref_count=1, size=100)
        with pytest.raises(AttributeError):
            meta.ref_count = 2  # type: ignore[misc]

    def test_to_dict_basic(self):
        meta = CASMeta(ref_count=3, size=1024)
        d = meta.to_dict()
        assert d == {"ref_count": 3, "size": 1024}

    def test_to_dict_with_extra(self):
        meta = CASMeta(ref_count=1, size=500, extra=(("is_chunk", True), ("custom", "val")))
        d = meta.to_dict()
        assert d == {"ref_count": 1, "size": 500, "is_chunk": True, "custom": "val"}

    def test_from_dict_basic(self):
        meta = CASMeta.from_dict({"ref_count": 5, "size": 2048})
        assert meta.ref_count == 5
        assert meta.size == 2048
        assert meta.extra == ()

    def test_from_dict_missing_keys(self):
        meta = CASMeta.from_dict({})
        assert meta.ref_count == 0
        assert meta.size == 0

    def test_from_dict_extra_fields(self):
        meta = CASMeta.from_dict(
            {"ref_count": 1, "size": 100, "is_chunk": True, "is_chunked_manifest": True}
        )
        assert meta.ref_count == 1
        assert meta.size == 100
        assert ("is_chunk", True) in meta.extra
        assert ("is_chunked_manifest", True) in meta.extra

    def test_roundtrip_backward_compat(self):
        """Verify CASMeta.from_dict(old).to_dict() == old for all formats."""
        old_simple = {"ref_count": 2, "size": 512}
        assert CASMeta.from_dict(old_simple).to_dict() == old_simple

        old_chunk = {"ref_count": 1, "size": 100, "is_chunk": True}
        assert CASMeta.from_dict(old_chunk).to_dict() == old_chunk

        old_manifest = {
            "ref_count": 1,
            "size": 50_000_000,
            "is_chunked_manifest": True,
            "chunk_count": 50,
        }
        assert CASMeta.from_dict(old_manifest).to_dict() == old_manifest

    def test_inc_ref(self):
        meta = CASMeta(ref_count=2, size=100, extra=(("is_chunk", True),))
        incremented = meta.inc_ref()
        assert incremented.ref_count == 3
        assert incremented.size == 100
        assert incremented.extra == (("is_chunk", True),)
        # Original unchanged
        assert meta.ref_count == 2

    def test_dec_ref(self):
        meta = CASMeta(ref_count=3, size=100)
        decremented = meta.dec_ref()
        assert decremented.ref_count == 2
        assert meta.ref_count == 3  # original unchanged

    def test_dec_ref_floor_at_zero(self):
        meta = CASMeta(ref_count=0, size=100)
        assert meta.dec_ref().ref_count == 0


# === cas_retry Tests ===


class TestCasRetry:
    """Tests for the cas_retry utility."""

    def test_success_first_attempt(self):
        result = cas_retry(lambda: 42)
        assert result == 42

    def test_retries_on_failure(self):
        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise OSError("transient")
            return "ok"

        result = cas_retry(flaky, base_delay=0.001)
        assert result == "ok"
        assert call_count == 3

    def test_exhausted_retries_raises(self):
        def always_fail():
            raise OSError("permanent")

        with pytest.raises(OSError, match="permanent"):
            cas_retry(always_fail, max_attempts=3, base_delay=0.001)

    def test_non_retryable_exception_propagates(self):
        def bad():
            raise ValueError("not retryable")

        with pytest.raises(ValueError, match="not retryable"):
            cas_retry(bad, retryable=(OSError,))

    def test_custom_retryable_types(self):
        call_count = 0

        def flaky_json():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise json.JSONDecodeError("bad", "", 0)
            return "parsed"

        result = cas_retry(
            flaky_json,
            retryable=(json.JSONDecodeError,),
            base_delay=0.001,
        )
        assert result == "parsed"


# === CASBlobStore Tests ===


class TestCASBlobStore:
    """Tests for CASBlobStore operations."""

    @pytest.fixture
    def store(self, tmp_path):
        cas_root = tmp_path / "cas"
        cas_root.mkdir()
        return CASBlobStore(cas_root)

    def test_hash_to_path(self, store):
        h = "abcdef1234567890" + "0" * 48
        path = store.hash_to_path(h)
        assert path.parent.name == "cd"
        assert path.parent.parent.name == "ab"
        assert path.name == h

    def test_hash_to_path_invalid(self, store):
        with pytest.raises(ValueError, match="Invalid hash"):
            store.hash_to_path("abc")

    def test_meta_path(self, store):
        h = "abcdef1234567890" + "0" * 48
        mp = store.meta_path(h)
        assert mp.suffix == ".meta"
        assert mp.stem == h

    def test_write_and_read_blob(self, store):
        content = b"hello world"
        h = hash_content(content)
        is_new = store.write_blob(h, content)
        assert is_new is True

        read_back = store.read_blob(h)
        assert read_back == content

    def test_write_blob_idempotent(self, store):
        content = b"idempotent content"
        h = hash_content(content)
        assert store.write_blob(h, content) is True
        assert store.write_blob(h, content) is False  # already exists

        # Content is still readable
        assert store.read_blob(h) == content

    def test_blob_exists(self, store):
        content = b"exists check"
        h = hash_content(content)
        assert store.blob_exists(h) is False
        store.write_blob(h, content)
        assert store.blob_exists(h) is True

    def test_read_meta_nonexistent(self, store):
        h = "0" * 64
        meta = store.read_meta(h)
        assert meta.ref_count == 0
        assert meta.size == 0

    def test_write_and_read_meta(self, store):
        h = "0" * 64
        meta = CASMeta(ref_count=2, size=1024, extra=(("is_chunk", True),))
        # Ensure directory exists for meta
        store.hash_to_path(h).parent.mkdir(parents=True, exist_ok=True)
        store.write_meta(h, meta)

        read_meta = store.read_meta(h)
        assert read_meta.ref_count == 2
        assert read_meta.size == 1024
        assert ("is_chunk", True) in read_meta.extra

    def test_store_new_content(self, store):
        content = b"new content"
        h = hash_content(content)
        is_new = store.store(h, content)
        assert is_new is True

        # Verify blob + meta
        assert store.blob_exists(h)
        meta = store.read_meta(h)
        assert meta.ref_count == 1
        assert meta.size == len(content)

    def test_store_duplicate_increments_ref(self, store):
        content = b"dup content"
        h = hash_content(content)

        store.store(h, content)
        store.store(h, content)
        store.store(h, content)

        meta = store.read_meta(h)
        assert meta.ref_count == 3
        assert meta.size == len(content)

    def test_store_with_extra_meta(self, store):
        content = b"chunked"
        h = hash_content(content)
        store.store(h, content, extra_meta={"is_chunk": True})

        meta = store.read_meta(h)
        assert meta.ref_count == 1
        assert ("is_chunk", True) in meta.extra

    def test_release_decrement(self, store):
        content = b"release me"
        h = hash_content(content)

        store.store(h, content)
        store.store(h, content)  # ref_count=2

        deleted = store.release(h)
        assert deleted is False

        meta = store.read_meta(h)
        assert meta.ref_count == 1
        assert store.blob_exists(h)

    def test_release_delete_at_zero(self, store):
        content = b"delete me"
        h = hash_content(content)

        store.store(h, content)  # ref_count=1
        deleted = store.release(h)
        assert deleted is True
        assert not store.blob_exists(h)

    def test_release_cleans_up_meta(self, store):
        content = b"cleanup"
        h = hash_content(content)
        store.store(h, content)
        store.release(h)

        mp = store.meta_path(h)
        assert not mp.exists()

    def test_cleanup_empty_dirs(self, store):
        content = b"dir cleanup"
        h = hash_content(content)
        store.store(h, content)

        blob_path = store.hash_to_path(h)
        parent_dir = blob_path.parent
        grandparent_dir = parent_dir.parent
        assert parent_dir.exists()

        store.release(h)

        # Empty dirs should be cleaned up
        assert not parent_dir.exists()
        assert not grandparent_dir.exists()

    def test_store_empty_content(self, store):
        content = b""
        h = hash_content(content)
        store.store(h, content)

        assert store.blob_exists(h)
        assert store.read_blob(h) == b""
        meta = store.read_meta(h)
        assert meta.ref_count == 1
        assert meta.size == 0

    # --- release() edge cases (Issue #925, Decision #9) ---

    def test_release_nonexistent_hash(self, store):
        """Releasing a never-stored hash should not raise."""
        h = "0" * 64
        deleted = store.release(h)
        assert deleted is True  # ref_count=0 → treated as last ref
        assert not store.blob_exists(h)

    def test_double_release(self, store):
        """Release twice on ref_count=1 — second release is a no-op."""
        content = b"double release"
        h = hash_content(content)
        store.store(h, content)

        deleted1 = store.release(h)
        assert deleted1 is True
        assert not store.blob_exists(h)

        # Second release on already-deleted blob
        deleted2 = store.release(h)
        assert deleted2 is True  # ref_count=0 → "deleted" again (idempotent)

    def test_release_after_full_release(self, store):
        """Release after blob + meta already cleaned up."""
        content = b"full release"
        h = hash_content(content)

        store.store(h, content)
        store.store(h, content)  # ref_count=2

        store.release(h)  # ref_count=1
        store.release(h)  # ref_count=0 → deleted

        assert not store.blob_exists(h)
        assert not store.meta_path(h).exists()

        # One more release — should not raise
        deleted = store.release(h)
        assert deleted is True

    def test_read_blob_verify_success(self, store):
        """read_blob with verify=True succeeds for valid content."""
        content = b"verify me"
        h = hash_content(content)
        store.write_blob(h, content)

        result = store.read_blob(h, verify=True)
        assert result == content

    def test_read_blob_verify_mismatch(self, store):
        """read_blob with verify=True raises on hash mismatch."""
        content = b"tampered content"
        h = hash_content(content)
        store.write_blob(h, content)

        # Overwrite the blob with different content to cause mismatch
        blob_path = store.hash_to_path(h)
        blob_path.write_bytes(b"different content")

        from nexus.core.exceptions import BackendError

        with pytest.raises(BackendError, match="Content hash mismatch"):
            store.read_blob(h, verify=True)

    def test_meta_lock_context_manager(self, store):
        """meta_lock() provides mutual exclusion for metadata operations."""
        content = b"lock test"
        h = hash_content(content)

        with store.meta_lock(h):
            # Should be able to do metadata ops inside lock
            store.hash_to_path(h).parent.mkdir(parents=True, exist_ok=True)
            store.write_meta(h, CASMeta(ref_count=1, size=len(content)))

        meta = store.read_meta(h)
        assert meta.ref_count == 1

    def test_fsync_disabled(self, tmp_path):
        """CASBlobStore with fsync_blobs=False still writes correctly."""
        cas_root = tmp_path / "cas_nofsync"
        cas_root.mkdir()
        store_nofsync = CASBlobStore(cas_root, fsync_blobs=False)

        content = b"no fsync content"
        h = hash_content(content)
        store_nofsync.write_blob(h, content)

        assert store_nofsync.read_blob(h) == content
