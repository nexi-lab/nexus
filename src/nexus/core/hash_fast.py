"""Fast content hashing using BLAKE3 (Rust-accelerated).

This module provides BLAKE3 hashing for content-addressable storage,
with ~3x speedup over SHA-256.

Usage:
    from nexus.core.hash_fast import hash_content, hash_content_smart

    # Full BLAKE3 hash (for all files)
    content_hash = hash_content(b"file content")

    # Smart hash with sampling for large files (>256KB)
    content_hash = hash_content_smart(large_content)

When the Rust extension is not available, falls back to SHA-256.
"""

from __future__ import annotations

import hashlib
import logging

logger = logging.getLogger(__name__)

# Try to import Rust-accelerated BLAKE3
_RUST_AVAILABLE = False
try:
    from nexus._nexus_fast import hash_content as _rust_hash_content
    from nexus._nexus_fast import hash_content_smart as _rust_hash_content_smart

    _RUST_AVAILABLE = True
except ImportError:
    _rust_hash_content = None
    _rust_hash_content_smart = None
    logger.debug("Rust extension not available, using Python SHA-256 fallback")


def hash_content(content: bytes) -> str:
    """Compute content hash using BLAKE3 (Rust) or SHA-256 (fallback).

    Args:
        content: Binary content to hash

    Returns:
        64-character hex string (256-bit hash)
    """
    if _RUST_AVAILABLE and _rust_hash_content is not None:
        return _rust_hash_content(content)
    # Fallback to SHA-256
    return hashlib.sha256(content).hexdigest()


def hash_content_smart(content: bytes) -> str:
    """Compute content hash with strategic sampling for large files.

    For files < 256KB: full hash (same as hash_content)
    For files >= 256KB: samples first 64KB + middle 64KB + last 64KB

    This provides ~10x speedup for large files while maintaining
    good collision resistance for deduplication purposes.

    NOTE: This is NOT suitable for cryptographic integrity verification,
    only for content-addressable storage fingerprinting.

    Args:
        content: Binary content to hash

    Returns:
        64-character hex string (256-bit hash)
    """
    if _RUST_AVAILABLE and _rust_hash_content_smart is not None:
        return _rust_hash_content_smart(content)

    # Fallback to Python implementation
    threshold = 256 * 1024  # 256KB
    sample_size = 64 * 1024  # 64KB per sample

    if len(content) < threshold:
        return hashlib.sha256(content).hexdigest()

    # Strategic sampling
    hasher = hashlib.sha256()

    # First 64KB
    hasher.update(content[:sample_size])

    # Middle 64KB
    mid_start = len(content) // 2 - sample_size // 2
    hasher.update(content[mid_start : mid_start + sample_size])

    # Last 64KB
    hasher.update(content[-sample_size:])

    # Include file size
    hasher.update(len(content).to_bytes(8, byteorder="little"))

    return hasher.hexdigest()


def is_rust_available() -> bool:
    """Check if Rust-accelerated hashing is available."""
    return _RUST_AVAILABLE
