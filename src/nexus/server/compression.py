"""HTTP response compression utilities with zstd support.

Provides compression for HTTP responses with automatic algorithm selection:
- zstd (Python 3.14+): 3-5x faster than gzip with similar compression ratios
- gzip: Universal fallback for older Python or clients

Usage:
    from nexus.server.compression import compress_response

    body, encoding = compress_response(data, accept_encoding="zstd, gzip")
    if encoding:
        headers["Content-Encoding"] = encoding
"""

import gzip
import logging
from typing import Literal

logger = logging.getLogger(__name__)

# Try to import zstd from Python 3.14+ compression module
_ZSTD_AVAILABLE = False
_zstd_module: object | None = None

try:
    from compression import zstd as _zstd_mod  # Python 3.14+

    _zstd_module = _zstd_mod
    _ZSTD_AVAILABLE = True
    logger.debug("[COMPRESSION] Using Python 3.14+ native zstd compression")
except ImportError:
    # Try third-party zstandard package as fallback
    try:
        import zstandard as _zstd_mod_fallback

        _zstd_module = _zstd_mod_fallback
        _ZSTD_AVAILABLE = True
        logger.debug("[COMPRESSION] Using zstandard package for zstd compression")
    except ImportError:
        logger.debug("[COMPRESSION] zstd not available, using gzip only")


def is_zstd_available() -> bool:
    """Check if zstd compression is available."""
    return _ZSTD_AVAILABLE


def compress_zstd(data: bytes, level: int = 3) -> bytes:
    """Compress data using zstd.

    Args:
        data: Data to compress
        level: Compression level (1-22, default 3 for speed)

    Returns:
        Compressed data

    Raises:
        RuntimeError: If zstd is not available
    """
    if not _ZSTD_AVAILABLE or _zstd_module is None:
        raise RuntimeError("zstd compression not available")

    # Handle both Python 3.14 compression.zstd and zstandard package
    if hasattr(_zstd_module, "compress"):
        # Python 3.14+ compression.zstd or zstandard with simple API
        return bytes(_zstd_module.compress(data, level=level))
    elif hasattr(_zstd_module, "ZstdCompressor"):
        # zstandard package
        compressor = _zstd_module.ZstdCompressor(level=level)
        return bytes(compressor.compress(data))
    else:
        raise RuntimeError("Unknown zstd module interface")


def compress_gzip(data: bytes, level: int = 6) -> bytes:
    """Compress data using gzip.

    Args:
        data: Data to compress
        level: Compression level (1-9, default 6)

    Returns:
        Compressed data
    """
    return gzip.compress(data, compresslevel=level)


def compress_response(
    data: bytes,
    accept_encoding: str,
    min_size: int = 1024,
) -> tuple[bytes, Literal["zstd", "gzip"] | None]:
    """Compress HTTP response data based on Accept-Encoding header.

    Prefers zstd over gzip for better performance (3-5x faster).

    Args:
        data: Response data to compress
        accept_encoding: Value of Accept-Encoding header
        min_size: Minimum size to compress (default 1KB)

    Returns:
        Tuple of (compressed_data, content_encoding)
        If no compression applied, returns (data, None)

    Example:
        >>> body, encoding = compress_response(data, "zstd, gzip, deflate")
        >>> if encoding:
        ...     headers["Content-Encoding"] = encoding
    """
    original_size = len(data)

    # Skip compression for small payloads
    if original_size <= min_size:
        return data, None

    # Prefer zstd if available and client supports it (3-5x faster than gzip)
    if _ZSTD_AVAILABLE and "zstd" in accept_encoding:
        try:
            compressed = compress_zstd(data, level=3)  # Level 3 for speed
            logger.debug(
                f"[COMPRESSION] zstd: {original_size} → {len(compressed)} bytes "
                f"({len(compressed) / original_size * 100:.1f}%)"
            )
            return compressed, "zstd"
        except Exception as e:
            logger.warning(f"[COMPRESSION] zstd failed, falling back to gzip: {e}")

    # Fall back to gzip
    if "gzip" in accept_encoding:
        compressed = compress_gzip(data, level=6)
        logger.debug(
            f"[COMPRESSION] gzip: {original_size} → {len(compressed)} bytes "
            f"({len(compressed) / original_size * 100:.1f}%)"
        )
        return compressed, "gzip"

    # No compression
    if original_size > min_size:
        logger.debug(
            f"[COMPRESSION] No compression: {original_size} bytes, "
            f"Accept-Encoding: {accept_encoding}"
        )

    return data, None
