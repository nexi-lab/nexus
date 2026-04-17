"""CompressedStorage — transparent compression decorator for any Backend (#1705).

Wraps an inner Backend and compresses content before writing, decompresses
on read. Uses zstd (Zstandard) for excellent compression ratio with fast
decompression.

zstd compression is deterministic: same content + same level = same
compressed output = same CAS hash, preserving deduplication.

Follows LEGO Architecture PART 16 (Recursive Wrapping, Mechanism 2).
All non-content Backend operations pass through transparently.

Recommended chain ordering (compress BEFORE encrypt for at-rest storage):

    storage = CompressedStorage(
        inner=EncryptedStorage(
            inner=S3Storage(bucket="data"),
            config=EncryptedStorageConfig(key=key),
        ),
        config=CompressedStorageConfig(level=3),
    )
    # storage.describe() → "compress(zstd) → encrypt(AES-256-GCM-SIV) → s3"

Memory overhead: ~2x content size per operation (original + compressed).
CAS blobs are content-addressed chunks, not multi-GB monoliths.

Usage:
    from nexus.backends.wrappers.compressed import CompressedStorage, CompressedStorageConfig

    config = CompressedStorageConfig(level=3)
    wrapper = CompressedStorage(inner=local_backend, config=config)
    # Use wrapper exactly like any Backend

Design reference:
    - NEXUS-LEGO-ARCHITECTURE.md PART 16 — Recursive Wrapping (Mechanism 2)
    - PART 6 §6.3 (zstd for bundle compression)
    - Issue #1705: EncryptedStorage + CompressedStorage recursive wrappers
    - Issue #2077: Deduplicate backend wrapper boilerplate
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from compression import zstd
from compression.zstd import ZstdCompressor

from nexus.backends.storage.delegating import DelegatingBackend
from nexus.backends.wrappers.headers import COMPRESSED_HEADER as _COMPRESSED_HEADER
from nexus.backends.wrappers.metrics import WrapperMetrics

if TYPE_CHECKING:
    from nexus.backends.base.backend import Backend

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# zstd availability detection (Python 3.14+ stdlib)
# ---------------------------------------------------------------------------

_zstd_compress = zstd.compress
_zstd_decompress = zstd.decompress
_ZSTD_AVAILABLE = True


def is_zstd_available() -> bool:
    """Check if zstd compression is available."""
    return _ZSTD_AVAILABLE


_HEADER_LEN = len(_COMPRESSED_HEADER)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompressedStorageConfig:
    """Immutable configuration for CompressedStorage.

    Attributes:
        level: zstd compression level (1-22). Default 3 for speed.
            1-3: maximum throughput. 4-10: good speed/size tradeoff.
            11-22: maximum compression, slow.
        min_size: Minimum content size in bytes to attempt compression.
            Content below this threshold is stored uncompressed.
        metrics_enabled: Enable OTel compress/decompress/error counters.
    """

    level: int = 3
    min_size: int = 64
    metrics_enabled: bool = True

    def __post_init__(self) -> None:
        if not 1 <= self.level <= 22:
            raise ValueError(f"CompressedStorageConfig level must be 1-22, got {self.level}")
        if self.min_size < 0:
            raise ValueError(f"CompressedStorageConfig min_size must be >= 0, got {self.min_size}")


# ---------------------------------------------------------------------------
# CompressedStorage
# ---------------------------------------------------------------------------


class CompressedStorage(DelegatingBackend):
    """Transparent compression decorator for any Backend implementation.

    Uses zstd (Zstandard) for fast compression with good ratios.
    Compression is deterministic: same content + same level = same
    compressed output, preserving CAS deduplication.

    Compressed content format:
        NEXZ\\x01 (5 bytes header) || zstd-compressed data

    Content below ``min_size`` bytes or where compression doesn't reduce
    size is stored uncompressed (no header prefix).

    Inherits property delegation, ``__getattr__``, and hook-based
    ``read_content`` / ``write_content`` / ``batch_read_content`` from
    DelegatingBackend. Overrides ``_transform_on_write`` and
    ``_transform_on_read`` to add compression/decompression.
    """

    def __init__(self, inner: "Backend", config: CompressedStorageConfig | None = None) -> None:
        super().__init__(inner)
        self._config = config or CompressedStorageConfig()
        self._metrics = WrapperMetrics(
            meter_name="nexus.compressed_storage",
            counter_names=[
                "compress_ops",
                "decompress_ops",
                "passthrough_ops",
                "errors",
                "bytes_saved",
            ],
            enabled=self._config.metrics_enabled,
        )

        # Cache compressor instance for reuse.
        self._cached_compressor: ZstdCompressor = ZstdCompressor(level=self._config.level)

        logger.info(
            "CompressedStorage initialized (zstd, level=%d, min_size=%d)",
            self._config.level,
            self._config.min_size,
        )

    # === Chain Introspection ===

    def describe(self) -> str:
        """Return chain description: ``"compress(zstd) → {inner}"``."""
        return f"compress(zstd) → {self._inner.describe()}"

    @property
    def name(self) -> str:
        return f"compressed({self._inner.name})"

    # === Transform Hooks ===

    def _transform_on_write(self, content: bytes) -> bytes:
        """Compress content with magic header.

        Returns the content with NEXZ header if compressed, or the
        original content without header if compression was skipped.

        Compression is skipped when:
        - Content is smaller than min_size
        - Content is empty
        - Compressed output is not smaller than original

        This is a soft-fallback transform: never raises, always returns
        valid content (compressed or original).
        """
        if len(content) < self._config.min_size:
            self._metrics.increment("passthrough_ops")
            return content

        try:
            compressed = self._cached_compressor.compress(content) + self._cached_compressor.flush(
                self._cached_compressor.FLUSH_FRAME
            )

            # Only use compressed version if it actually saves space
            if len(compressed) >= len(content):
                self._metrics.increment("passthrough_ops")
                return content

            self._metrics.increment("compress_ops")
            savings = len(content) - len(compressed) - _HEADER_LEN
            if savings > 0:
                self._metrics.increment("bytes_saved", savings)

            return _COMPRESSED_HEADER + compressed

        except Exception as e:
            self._metrics.increment("errors")
            logger.warning("Compression failed, storing uncompressed: %s", e)
            return content

    def _transform_on_read(self, data: bytes) -> bytes:
        """Decompress data with magic header detection.

        Content without the NEXZ header is returned as-is (was stored
        uncompressed due to threshold or negative ratio).

        Raises on decompression failure (DelegatingBackend catches and
        returns error response).
        """
        if not data.startswith(_COMPRESSED_HEADER):
            # Not compressed (below threshold, negative ratio, or empty)
            return data

        compressed = data[_HEADER_LEN:]
        try:
            result = bytes(_zstd_decompress(compressed))
            self._metrics.increment("decompress_ops")
            return result
        except Exception as e:
            self._metrics.increment("errors")
            raise RuntimeError(f"Decompression failed: {e}") from e

    # === Stats ===

    def get_compression_stats(self) -> dict[str, int]:
        """Return compression/decompression counters."""
        return self._metrics.get_stats()
