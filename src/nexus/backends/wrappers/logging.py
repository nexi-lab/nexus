"""LoggingBackendWrapper — structured logging decorator for any Backend (#1449).

Wraps an inner Backend and logs every operation at DEBUG level with:
- Operation name
- Content hash or path (where applicable)
- Latency in milliseconds
- Success/failure status

Lifecycle events (check_connection) are logged at DEBUG level.

Usage:
    from nexus.backends.wrappers.logging import LoggingBackendWrapper

    logged = LoggingBackendWrapper(inner=s3_backend)

Design reference:
    - NEXUS-LEGO-ARCHITECTURE.md PART 16 — Recursive Wrapping (Mechanism 2)
    - Issue #1449: Recursive Protocol wrapping + describe() for composition chains
    - Issue #2077: Deduplicate backend wrapper boilerplate
"""

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

from nexus.backends.storage.delegating import DelegatingBackend

if TYPE_CHECKING:
    from nexus.backends.base.backend import Backend, HandlerStatusResponse
    from nexus.contracts.types import OperationContext
    from nexus.core.object_store import WriteResult

logger = logging.getLogger(__name__)

T = TypeVar("T")


class LoggingBackendWrapper(DelegatingBackend):
    """Transparent logging decorator for any Backend implementation.

    Inherits property delegation and ``__getattr__`` from ``DelegatingBackend``.
    Overrides content, directory, and connection operations to add structured
    debug logging with latency measurement.

    Uses ``_timed()`` helper to eliminate timing boilerplate across all methods.

    Log levels:
        - DEBUG: per-operation logs (read, write, delete, mkdir, etc.)
        - INFO: lifecycle events (connect, disconnect)
    """

    def __init__(self, inner: "Backend") -> None:
        super().__init__(inner)

    # === Chain Introspection ===

    def describe(self) -> str:
        """Return chain description: ``"logging → {inner.describe()}"``."""
        return f"logging → {self._inner.describe()}"

    # === Timing Helper ===

    def _timed(
        self, op_name: str, fn: Callable[[], T], level: int = logging.DEBUG
    ) -> tuple[T, float]:
        """Execute ``fn``, returning ``(result, elapsed_ms)``.

        On exception, logs the error with latency at the given level
        before re-raising.
        """
        start = time.perf_counter()
        try:
            result = fn()
        except Exception as e:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.log(level, "%s error=%s latency_ms=%.2f", op_name, e, elapsed_ms)
            raise
        elapsed_ms = (time.perf_counter() - start) * 1000
        return result, elapsed_ms

    # === Content Operations (with logging) ===

    def read_content(self, content_id: str, context: "OperationContext | None" = None) -> bytes:
        content, elapsed_ms = self._timed(
            "read_content",
            lambda: self._inner.read_content(content_id, context=context),
        )
        logger.debug(
            "read_content hash=%s success=True latency_ms=%.2f",
            content_id[:12],
            elapsed_ms,
        )
        return content

    def write_content(
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: "OperationContext | None" = None,
    ) -> "WriteResult":
        result, elapsed_ms = self._timed(
            "write_content",
            lambda: self._inner.write_content(content, content_id, offset=offset, context=context),
        )
        logger.debug(
            "write_content size=%d success=True hash=%s latency_ms=%.2f",
            len(content),
            result.content_id[:12],
            elapsed_ms,
        )
        return result

    def delete_content(self, content_id: str, context: "OperationContext | None" = None) -> None:
        _, elapsed_ms = self._timed(
            "delete_content",
            lambda: self._inner.delete_content(content_id, context=context),
        )
        logger.debug(
            "delete_content hash=%s success=True latency_ms=%.2f",
            content_id[:12],
            elapsed_ms,
        )

    def content_exists(self, content_id: str, context: "OperationContext | None" = None) -> bool:
        exists, elapsed_ms = self._timed(
            "content_exists",
            lambda: self._inner.content_exists(content_id, context=context),
        )
        logger.debug(
            "content_exists hash=%s exists=%s latency_ms=%.2f",
            content_id[:12],
            exists,
            elapsed_ms,
        )
        return exists

    def batch_read_content(
        self,
        content_ids: list[str],
        context: "OperationContext | None" = None,
        *,
        contexts: "dict[str, OperationContext] | None" = None,
    ) -> dict[str, bytes | None]:
        results, elapsed_ms = self._timed(
            "batch_read_content",
            lambda: self._inner.batch_read_content(content_ids, context=context, contexts=contexts),
        )
        hit_count = sum(1 for v in results.values() if v is not None)
        logger.debug(
            "batch_read_content count=%d hits=%d latency_ms=%.2f",
            len(content_ids),
            hit_count,
            elapsed_ms,
        )
        return results

    # === Directory Operations (with logging) ===

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        _, elapsed_ms = self._timed(
            "mkdir",
            lambda: self._inner.mkdir(path, parents=parents, exist_ok=exist_ok, context=context),
        )
        logger.debug(
            "mkdir path=%s success=True latency_ms=%.2f",
            path,
            elapsed_ms,
        )

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        _, elapsed_ms = self._timed(
            "rmdir",
            lambda: self._inner.rmdir(path, recursive=recursive, context=context),
        )
        logger.debug(
            "rmdir path=%s recursive=%s success=True latency_ms=%.2f",
            path,
            recursive,
            elapsed_ms,
        )

    # === Connection Lifecycle (INFO level) ===

    def check_connection(
        self, context: "OperationContext | None" = None
    ) -> "HandlerStatusResponse":
        response, elapsed_ms = self._timed(
            "check_connection",
            lambda: self._inner.check_connection(context=context),
        )
        logger.debug(
            "check_connection backend=%s success=%s latency_ms=%.2f",
            self._inner.name,
            response.success,
            elapsed_ms,
        )
        return response
