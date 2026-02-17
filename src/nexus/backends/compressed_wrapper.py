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
    from nexus.backends.compressed_wrapper import CompressedStorage, CompressedStorageConfig

    config = CompressedStorageConfig(level=3)
    wrapper = CompressedStorage(inner=local_backend, config=config)
    # Use wrapper exactly like any Backend

Design reference:
    - NEXUS-LEGO-ARCHITECTURE.md PART 16 — Recursive Wrapping (Mechanism 2)
    - PART 6 §6.3 (zstd for bundle compression)
    - Issue #1705: EncryptedStorage + CompressedStorage recursive wrappers
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nexus.backends.delegating import DelegatingBackend
from nexus.backends.wrapper_metrics import WrapperMetrics
from nexus.core.response import HandlerResponse

if TYPE_CHECKING:
    from nexus.backends.backend import Backend
    from nexus.core.permissions import OperationContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# zstd availability detection (Python 3.14+ stdlib or zstandard package)
# ---------------------------------------------------------------------------

_ZSTD_AVAILABLE = False
_zstd_compress: object | None = None
_zstd_decompress: object | None = None

try:
    from compression import zstd  # Python 3.14+

    _zstd_compress = zstd.compress
    _zstd_decompress = zstd.decompress
    _ZSTD_AVAILABLE = True
    logger.debug("[COMPRESSED_WRAPPER] Using Python 3.14+ native zstd")
except ImportError:
    try:
        import zstandard

        def _compat_compress(data: bytes, *, level: int = 3) -> bytes:
            compressor = zstandard.ZstdCompressor(level=level)
            return bytes(compressor.compress(data))

        def _compat_decompress(data: bytes) -> bytes:
            decompressor = zstandard.ZstdDecompressor()
            return bytes(decompressor.decompress(data))

        _zstd_compress = _compat_compress
        _zstd_decompress = _compat_decompress
        _ZSTD_AVAILABLE = True
        logger.debug("[COMPRESSED_WRAPPER] Using zstandard package")
    except ImportError:
        logger.debug("[COMPRESSED_WRAPPER] zstd not available")


def is_zstd_available() -> bool:
    """Check if zstd compression is available."""
    return _ZSTD_AVAILABLE


# Magic header to identify compressed content.
# "NEXZ" + version byte (1).
_COMPRESSED_HEADER = b"NEXZ\x01"
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

    Inherits property delegation and ``__getattr__`` from DelegatingBackend.
    Overrides content read/write operations to add compression/decompression.

    Raises:
        RuntimeError: If zstd is not available at construction time.
    """

    def __init__(self, inner: Backend, config: CompressedStorageConfig | None = None) -> None:
        super().__init__(inner)
        if not _ZSTD_AVAILABLE:
            raise RuntimeError(
                "zstd compression not available. Install the 'zstandard' package "
                "or upgrade to Python 3.14+ for native compression.zstd support."
            )
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

    # === Compressed Content Operations ===

    def write_content(
        self, content: bytes, context: OperationContext | None = None
    ) -> HandlerResponse[str]:
        """Compress content and write to inner backend.

        Skips compression if content is below min_size or if compressed
        output is not smaller than the original.
        """
        data_to_write = self._compress(content)
        return self._inner.write_content(data_to_write, context=context)

    def read_content(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[bytes]:
        """Read from inner backend and decompress content."""
        response = self._inner.read_content(content_hash, context=context)
        if not response.success or response.data is None:
            return response

        return self._decompress_response(response.data)

    def batch_read_content(
        self,
        content_hashes: list[str],
        context: OperationContext | None = None,
        *,
        contexts: dict[str, OperationContext] | None = None,
    ) -> dict[str, bytes | None]:
        """Read batch from inner backend and decompress each item."""
        raw_results = self._inner.batch_read_content(
            content_hashes, context=context, contexts=contexts
        )

        decompressed: dict[str, bytes | None] = {}
        for content_hash, data in raw_results.items():
            if data is None:
                decompressed[content_hash] = None
                continue

            resp = self._decompress_response(data)
            decompressed[content_hash] = resp.data if resp.success else None

        return decompressed

    # === Compression / Decompression Internals ===

    def _compress(self, content: bytes) -> bytes:
        """Compress content with magic header.

        Returns the content with NEXZ header if compressed, or the
        original content without header if compression was skipped.

        Compression is skipped when:
        - Content is smaller than min_size
        - Content is empty
        - Compressed output is not smaller than original
        """
        if len(content) < self._config.min_size:
            self._metrics.increment("passthrough_ops")
            return content

        try:
            compressed = _zstd_compress(content, level=self._config.level)  # type: ignore[misc]

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

    def _decompress(self, data: bytes) -> bytes:
        """Decompress data with magic header detection.

        Content without the NEXZ header is returned as-is (was stored
        uncompressed due to threshold or negative ratio).
        """
        if not data.startswith(_COMPRESSED_HEADER):
            # Not compressed (below threshold, negative ratio, or empty)
            return data

        compressed = data[_HEADER_LEN:]
        return bytes(_zstd_decompress(compressed))  # type: ignore[misc]

    def _decompress_response(self, data: bytes) -> HandlerResponse[bytes]:
        """Decompress and return as HandlerResponse."""
        try:
            content = self._decompress(data)
            if data.startswith(_COMPRESSED_HEADER):
                self._metrics.increment("decompress_ops")
            return HandlerResponse.ok(data=content, backend_name=self.name)
        except Exception as e:
            self._metrics.increment("errors")
            logger.warning("Decompression failed: %s", e)
            return HandlerResponse.error(message=f"Decompression failed: {e}")

    # === Stats ===

    def get_compression_stats(self) -> dict[str, int]:
        """Return compression/decompression counters."""
        return self._metrics.get_stats()
