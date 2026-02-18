"""LoggingBackendWrapper — structured logging decorator for any Backend (#1449).

Wraps an inner Backend and logs every operation at DEBUG level with:
- Operation name
- Content hash or path (where applicable)
- Latency in milliseconds
- Success/failure status

Lifecycle events (connect, disconnect) are logged at INFO level.

Usage:
    from nexus.backends.logging_wrapper import LoggingBackendWrapper

    logged = LoggingBackendWrapper(inner=s3_backend)
    # or as part of a chain:
    cached_logged = CachingBackendWrapper(
        inner=LoggingBackendWrapper(inner=s3_backend),
        config=config,
    )
    cached_logged.describe()  # "cache → logging → s3"

Design reference:
    - NEXUS-LEGO-ARCHITECTURE.md PART 16 — Recursive Wrapping (Mechanism 2)
    - Issue #1449: Recursive Protocol wrapping + describe() for composition chains
"""


import logging
import time
from typing import TYPE_CHECKING

from nexus.backends.delegating import DelegatingBackend

if TYPE_CHECKING:
    from nexus.backends.backend import Backend, HandlerStatusResponse
    from nexus.core.permissions import OperationContext
    from nexus.core.response import HandlerResponse

logger = logging.getLogger(__name__)


class LoggingBackendWrapper(DelegatingBackend):
    """Transparent logging decorator for any Backend implementation.

    Inherits property delegation and ``__getattr__`` from ``DelegatingBackend``.
    Overrides content, directory, and connection operations to add structured
    debug logging with latency measurement.

    Log levels:
        - DEBUG: per-operation logs (read, write, delete, mkdir, etc.)
        - INFO: lifecycle events (connect, disconnect)
    """

    def __init__(self, inner: Backend) -> None:
        super().__init__(inner)

    # === Chain Introspection ===

    def describe(self) -> str:
        """Return chain description: ``"logging → {inner.describe()}"``."""
        return f"logging → {self._inner.describe()}"

    # === Content Operations (with logging) ===

    def read_content(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[bytes]:
        start = time.perf_counter()
        response = self._inner.read_content(content_hash, context=context)
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.debug(
            "read_content hash=%s success=%s latency_ms=%.2f",
            content_hash[:12],
            response.success,
            elapsed_ms,
        )
        return response

    def write_content(
        self, content: bytes, context: OperationContext | None = None
    ) -> HandlerResponse[str]:
        start = time.perf_counter()
        response = self._inner.write_content(content, context=context)
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.debug(
            "write_content size=%d success=%s hash=%s latency_ms=%.2f",
            len(content),
            response.success,
            response.data[:12] if response.data else "N/A",
            elapsed_ms,
        )
        return response

    def delete_content(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[None]:
        start = time.perf_counter()
        response = self._inner.delete_content(content_hash, context=context)
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.debug(
            "delete_content hash=%s success=%s latency_ms=%.2f",
            content_hash[:12],
            response.success,
            elapsed_ms,
        )
        return response

    def content_exists(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[bool]:
        start = time.perf_counter()
        response = self._inner.content_exists(content_hash, context=context)
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.debug(
            "content_exists hash=%s exists=%s latency_ms=%.2f",
            content_hash[:12],
            response.data,
            elapsed_ms,
        )
        return response

    # === Directory Operations (with logging) ===

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: OperationContext | None = None,
    ) -> HandlerResponse[None]:
        start = time.perf_counter()
        response = self._inner.mkdir(path, parents=parents, exist_ok=exist_ok, context=context)
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.debug(
            "mkdir path=%s success=%s latency_ms=%.2f",
            path,
            response.success,
            elapsed_ms,
        )
        return response

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: OperationContext | None = None,
    ) -> HandlerResponse[None]:
        start = time.perf_counter()
        response = self._inner.rmdir(path, recursive=recursive, context=context)
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.debug(
            "rmdir path=%s recursive=%s success=%s latency_ms=%.2f",
            path,
            recursive,
            response.success,
            elapsed_ms,
        )
        return response

    # === Connection Lifecycle (INFO level) ===

    def connect(self, context: OperationContext | None = None) -> HandlerStatusResponse:
        start = time.perf_counter()
        response = self._inner.connect(context=context)
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "connect backend=%s success=%s latency_ms=%.2f",
            self._inner.name,
            response.success,
            elapsed_ms,
        )
        return response

    def disconnect(self, context: OperationContext | None = None) -> None:
        start = time.perf_counter()
        self._inner.disconnect(context=context)
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "disconnect backend=%s latency_ms=%.2f",
            self._inner.name,
            elapsed_ms,
        )
