"""Fast content hashing using BLAKE3 (Rust-accelerated).

This module provides BLAKE3 hashing for content-addressable storage,
with ~3x speedup over SHA-256.

Usage:
    from nexus.core.hash_fast import hash_content, hash_content_smart

    # Full BLAKE3 hash (for all files)
    content_hash = hash_content(b"file content")

    # Smart hash with sampling for large files (>256KB)
    content_hash = hash_content_smart(large_content)

Fallback chain (Issue #582, #1395):
    1. Rust BLAKE3 (fastest, ~3x faster than SHA-256)
    2. Python blake3 package (consistent hashes with Rust)
    3. SHA-256 (last resort, WARNING: incompatible hashes!)
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

# --- Backend availability detection ---

_RUST_AVAILABLE = False
_PYTHON_BLAKE3_AVAILABLE = False
_python_blake3: Any = None
_rust_hash_content: Any = None
_rust_hash_content_smart: Any = None

# Priority 1: Rust-accelerated BLAKE3
try:
    import nexus._nexus_fast as _nexus_fast_mod

    _rust_hash_content = _nexus_fast_mod.hash_content
    _rust_hash_content_smart = _nexus_fast_mod.hash_content_smart
    _RUST_AVAILABLE = True
    logger.debug("Using Rust BLAKE3 acceleration")
except ImportError:
    logger.warning(
        "Rust BLAKE3 extension not available — falling back to Python blake3. "
        "Install with: pip install nexus-ai-fs[rust] or maturin develop --release"
    )

# Priority 2: Python blake3 package (Issue #582, #833)
try:
    import blake3 as _blake3_mod

    _python_blake3 = _blake3_mod
    _PYTHON_BLAKE3_AVAILABLE = True
    if not _RUST_AVAILABLE:
        logger.debug("Using Python blake3 package (consistent with Rust)")
except ImportError:
    if not _RUST_AVAILABLE:
        logger.warning(
            "Neither Rust BLAKE3 nor Python blake3 available — "
            "falling back to SHA-256 (WARNING: incompatible hashes!)"
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
    if _RUST_AVAILABLE and _rust_hash_content is not None:
        result: str = _rust_hash_content(content)
        return result

    if _PYTHON_BLAKE3_AVAILABLE:
        result = _python_blake3.blake3(content).hexdigest()
        return result

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
    if _RUST_AVAILABLE and _rust_hash_content_smart is not None:
        result: str = _rust_hash_content_smart(content)
        return result

    threshold = 256 * 1024  # 256KB
    sample_size = 64 * 1024  # 64KB per sample

    if _PYTHON_BLAKE3_AVAILABLE:
        if len(content) < threshold:
            result = _python_blake3.blake3(content).hexdigest()
            return result

        blake3_hasher = _python_blake3.blake3()
        blake3_hasher.update(content[:sample_size])
        mid_start = len(content) // 2 - sample_size // 2
        blake3_hasher.update(content[mid_start : mid_start + sample_size])
        blake3_hasher.update(content[-sample_size:])
        blake3_hasher.update(len(content).to_bytes(8, byteorder="little"))
        result = blake3_hasher.hexdigest()
        return result

    # SHA-256 fallback (WARNING: incompatible hashes!)
    if len(content) < threshold:
        return hashlib.sha256(content).hexdigest()

    sha_hasher = hashlib.sha256()
    sha_hasher.update(content[:sample_size])
    mid_start = len(content) // 2 - sample_size // 2
    sha_hasher.update(content[mid_start : mid_start + sample_size])
    sha_hasher.update(content[-sample_size:])
    sha_hasher.update(len(content).to_bytes(8, byteorder="little"))
    return sha_hasher.hexdigest()


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
    Uses Python blake3 if available, otherwise SHA-256.

    NOTE: This always uses the Python blake3 or SHA-256 backend.
    The Rust backend is only available for one-shot hashing via
    hash_content() and hash_content_smart().

    Example:
        >>> hasher = create_hasher()
        >>> for chunk in file_chunks:
        ...     hasher.update(chunk)
        >>> content_hash = hasher.hexdigest()
    """
    if _PYTHON_BLAKE3_AVAILABLE:
        return _python_blake3.blake3()
    else:
        return hashlib.sha256()
