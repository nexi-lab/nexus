"""Fast content hashing using BLAKE3 (Rust-accelerated).

This module provides BLAKE3 hashing for content-addressable storage,
with ~3x speedup over SHA-256.

Usage:
    from nexus.core.hash_fast import hash_content, hash_content_smart

    # Full BLAKE3 hash (for all files)
    content_hash = hash_content(b"file content")

    # Smart hash with sampling for large files (>256KB)
    content_hash = hash_content_smart(large_content)

Fallback chain (Issue #582):
    1. Rust BLAKE3 (fastest, ~3x faster than SHA-256)
    2. Python blake3 package (consistent hashes with Rust)
    3. SHA-256 (last resort, WARNING: incompatible hashes!)
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Try to import Rust-accelerated BLAKE3
_RUST_AVAILABLE = False
_PYTHON_BLAKE3_AVAILABLE = False

try:
    from nexus._nexus_fast import hash_content as _rust_hash_content
    from nexus._nexus_fast import hash_content_smart as _rust_hash_content_smart

    _RUST_AVAILABLE = True
    logger.debug("Using Rust BLAKE3 acceleration")
except ImportError:
    _rust_hash_content = None
    _rust_hash_content_smart = None

# Always try to import Python blake3 as fallback (Issue #582)
# This ensures consistent hashes even if Rust becomes unavailable at runtime
try:
    import blake3 as _python_blake3

    _PYTHON_BLAKE3_AVAILABLE = True
    if not _RUST_AVAILABLE:
        logger.debug("Using Python blake3 package (consistent with Rust)")
except ImportError:
    _python_blake3 = None
    if not _RUST_AVAILABLE:
        logger.warning(
            "Neither Rust extension nor blake3 package available. "
            "Using SHA-256 fallback - hashes will be INCOMPATIBLE with BLAKE3! "
            "Install blake3: pip install blake3"
        )


def hash_content(content: bytes) -> str:
    """Compute content hash using BLAKE3.

    Fallback chain:
        1. Rust BLAKE3 (fastest)
        2. Python blake3 package (consistent hashes)
        3. SHA-256 (last resort, incompatible!)

    Args:
        content: Binary content to hash

    Returns:
        64-character hex string (256-bit hash)
    """
    # Priority 1: Rust BLAKE3 (fastest)
    if _RUST_AVAILABLE and _rust_hash_content is not None:
        return _rust_hash_content(content)  # type: ignore[no-any-return]

    # Priority 2: Python blake3 (consistent with Rust)
    if _PYTHON_BLAKE3_AVAILABLE and _python_blake3 is not None:
        return _python_blake3.blake3(content).hexdigest()  # type: ignore[no-any-return]

    # Priority 3: SHA-256 fallback (WARNING: incompatible hashes!)
    return hashlib.sha256(content).hexdigest()


def hash_content_smart(content: bytes) -> str:
    """Compute content hash with strategic sampling for large files.

    For files < 256KB: full hash (same as hash_content)
    For files >= 256KB: samples first 64KB + middle 64KB + last 64KB

    This provides ~10x speedup for large files while maintaining
    good collision resistance for deduplication purposes.

    NOTE: This is NOT suitable for cryptographic integrity verification,
    only for content-addressable storage fingerprinting.

    Fallback chain:
        1. Rust BLAKE3 with sampling (fastest)
        2. Python blake3 with sampling (consistent hashes)
        3. SHA-256 with sampling (last resort, incompatible!)

    Args:
        content: Binary content to hash

    Returns:
        64-character hex string (256-bit hash)
    """
    # Priority 1: Rust BLAKE3 with smart sampling
    if _RUST_AVAILABLE and _rust_hash_content_smart is not None:
        return _rust_hash_content_smart(content)  # type: ignore[no-any-return]

    # Fallback to Python implementation with same sampling strategy
    threshold = 256 * 1024  # 256KB
    sample_size = 64 * 1024  # 64KB per sample

    # Priority 2: Python blake3 (consistent with Rust)
    if _PYTHON_BLAKE3_AVAILABLE and _python_blake3 is not None:
        if len(content) < threshold:
            return _python_blake3.blake3(content).hexdigest()  # type: ignore[no-any-return]

        # Strategic sampling with blake3
        hasher = _python_blake3.blake3()
        hasher.update(content[:sample_size])  # First 64KB
        mid_start = len(content) // 2 - sample_size // 2
        hasher.update(content[mid_start : mid_start + sample_size])  # Middle 64KB
        hasher.update(content[-sample_size:])  # Last 64KB
        hasher.update(len(content).to_bytes(8, byteorder="little"))  # File size
        return hasher.hexdigest()  # type: ignore[no-any-return]

    # Priority 3: SHA-256 fallback (WARNING: incompatible hashes!)
    if len(content) < threshold:
        return hashlib.sha256(content).hexdigest()

    # Strategic sampling with SHA-256
    hasher = hashlib.sha256()
    hasher.update(content[:sample_size])  # First 64KB
    mid_start = len(content) // 2 - sample_size // 2
    hasher.update(content[mid_start : mid_start + sample_size])  # Middle 64KB
    hasher.update(content[-sample_size:])  # Last 64KB
    hasher.update(len(content).to_bytes(8, byteorder="little"))  # File size
    return str(hasher.hexdigest())


def is_rust_available() -> bool:
    """Check if Rust-accelerated hashing is available."""
    return _RUST_AVAILABLE


def is_blake3_available() -> bool:
    """Check if BLAKE3 hashing is available (Rust or Python)."""
    return _RUST_AVAILABLE or _PYTHON_BLAKE3_AVAILABLE


def get_hash_backend() -> str:
    """Get the current hash backend being used.

    Returns:
        One of: "rust-blake3", "python-blake3", "sha256"
    """
    if _RUST_AVAILABLE:
        return "rust-blake3"
    elif _PYTHON_BLAKE3_AVAILABLE:
        return "python-blake3"
    else:
        return "sha256"


def create_hasher() -> Any:
    """Create an incremental hasher for streaming content.

    Returns a hasher object with .update(chunk) and .hexdigest() methods.
    Uses BLAKE3 if available, otherwise SHA-256.

    Example:
        >>> hasher = create_hasher()
        >>> for chunk in file_chunks:
        ...     hasher.update(chunk)
        >>> content_hash = hasher.hexdigest()
    """
    if _PYTHON_BLAKE3_AVAILABLE and _python_blake3 is not None:
        return _python_blake3.blake3()
    else:
        return hashlib.sha256()
