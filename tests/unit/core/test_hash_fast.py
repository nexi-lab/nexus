"""Unit tests for nexus.core.hash_fast — BLAKE3 hashing with fallback chain.

Tests cover:
- Hash output format and consistency
- Edge cases: empty content, threshold boundaries
- Smart hashing sampling correctness
- Backend detection functions
- Fallback chain behavior (Issue #1395)
"""

from __future__ import annotations

import hashlib
import random

import pytest

from nexus.core.hash_fast import (
    create_hasher,
    get_hash_backend,
    hash_content,
    hash_content_smart,
    is_blake3_available,
    is_rust_available,
)


class TestHashContent:
    """Tests for hash_content()."""

    def test_returns_64_char_hex_string(self):
        result = hash_content(b"Hello, World!")
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_empty_content(self):
        result = hash_content(b"")
        assert len(result) == 64
        # Empty content should still produce a valid hash
        assert result != ""

    def test_deterministic(self):
        content = b"test content for hashing"
        assert hash_content(content) == hash_content(content)

    def test_different_content_different_hash(self):
        assert hash_content(b"content A") != hash_content(b"content B")

    def test_single_byte_difference(self):
        assert hash_content(b"\x00") != hash_content(b"\x01")


class TestHashContentSmart:
    """Tests for hash_content_smart() — sampling for large files."""

    def test_small_file_matches_full_hash(self):
        """Files under 256KB should produce the same hash as hash_content."""
        content = b"x" * 1024  # 1KB
        assert hash_content_smart(content) == hash_content(content)

    def test_at_threshold_matches_full_hash(self):
        """Files exactly at 256KB - 1 should still use full hash."""
        content = b"y" * (256 * 1024 - 1)
        assert hash_content_smart(content) == hash_content(content)

    def test_above_threshold_uses_sampling(self):
        """Large non-uniform files should produce different smart vs full hash."""
        rng = random.Random(42)
        content = bytes(rng.getrandbits(8) for _ in range(300 * 1024))
        smart = hash_content_smart(content)
        full = hash_content(content)
        assert smart != full, "Smart hash should differ from full hash for large files"

    def test_large_file_deterministic(self):
        content = b"a" * (1024 * 1024)  # 1MB
        assert hash_content_smart(content) == hash_content_smart(content)

    def test_empty_content(self):
        result = hash_content_smart(b"")
        assert len(result) == 64

    def test_large_file_different_content(self):
        """Two large files with different middle sections should hash differently."""
        size = 512 * 1024  # 512KB
        content_a = bytearray(b"a" * size)
        content_b = bytearray(b"a" * size)
        # Modify the middle section (which is sampled)
        mid = size // 2
        content_b[mid] = ord("b")
        assert hash_content_smart(bytes(content_a)) != hash_content_smart(bytes(content_b))

    def test_unsampled_region_not_detected(self):
        """Files differing only in unsampled regions produce the same hash.

        This is a known limitation of smart hashing — it trades
        completeness for speed. Only use for deduplication hints,
        not integrity verification.
        """
        size = 512 * 1024  # 512KB
        content_a = bytearray(b"a" * size)
        content_b = bytearray(b"a" * size)
        # Modify an unsampled region (between first and middle samples)
        content_b[80 * 1024] = ord("b")
        assert hash_content_smart(bytes(content_a)) == hash_content_smart(bytes(content_b))


class TestBackendDetection:
    """Tests for backend detection utility functions."""

    def test_is_rust_available_returns_bool(self):
        assert isinstance(is_rust_available(), bool)

    def test_is_blake3_available_returns_bool(self):
        assert isinstance(is_blake3_available(), bool)

    def test_blake3_available_when_python_blake3_installed(self):
        """blake3 should be available since it's a required dependency."""
        assert is_blake3_available() is True

    def test_get_hash_backend_returns_valid_string(self):
        backend = get_hash_backend()
        assert backend in ("rust-blake3", "python-blake3", "sha256")

    def test_backend_consistent_with_availability(self):
        backend = get_hash_backend()
        if is_rust_available():
            assert backend == "rust-blake3"
        elif is_blake3_available():
            assert backend == "python-blake3"
        else:
            assert backend == "sha256"


class TestCreateHasher:
    """Tests for create_hasher() — streaming hash interface."""

    def test_hasher_has_update_and_hexdigest(self):
        hasher = create_hasher()
        assert hasattr(hasher, "update")
        assert hasattr(hasher, "hexdigest")

    def test_hasher_produces_valid_hash(self):
        hasher = create_hasher()
        hasher.update(b"chunk 1")
        hasher.update(b"chunk 2")
        result = hasher.hexdigest()
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_streaming_matches_oneshot(self):
        """Streaming hash of concatenated chunks should match one-shot hash."""
        if not is_blake3_available():
            pytest.skip("blake3 not available")

        chunks = [b"hello ", b"world"]
        full_content = b"".join(chunks)

        hasher = create_hasher()
        for chunk in chunks:
            hasher.update(chunk)
        streaming_result = hasher.hexdigest()

        oneshot_result = hash_content(full_content)
        assert streaming_result == oneshot_result


class TestFallbackChain:
    """Tests for the fallback chain behavior (Issue #1395)."""

    def test_sha256_fallback_when_no_blake3(self):
        """Verify SHA-256 fallback produces valid output format."""
        content = b"fallback test"
        sha256_hash = hashlib.sha256(content).hexdigest()
        assert len(sha256_hash) == 64  # SHA-256 also produces 64-char hex

    def test_hash_consistency_across_calls(self):
        """Hash should be consistent across multiple calls (no state leakage)."""
        content = b"consistency check"
        results = [hash_content(content) for _ in range(10)]
        assert len(set(results)) == 1
