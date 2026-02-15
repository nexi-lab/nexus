"""IsolatedBackend — fault-isolation wrapper for any Backend implementation.

Follows LEGO Pattern E (Decorator/Wrapper).  The kernel sees ``IsolatedBackend``
as a regular ``Backend`` — no code changes needed in NexusFS or routers.

WARNING: This provides FAULT ISOLATION (state / crash isolation), NOT security
sandboxing.  For untrusted-code security, use Docker / E2B sandbox providers.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, cast

from nexus.backends.backend import Backend, HandlerStatusResponse
from nexus.core.response import HandlerResponse
from nexus.isolation._pool import IsolatedPool
from nexus.isolation.config import IsolationConfig
from nexus.isolation.errors import (
    IsolationCallError,
    IsolationError,
    IsolationTimeoutError,
)

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.rebac.permissions_enhanced import EnhancedOperationContext

logger = logging.getLogger(__name__)


class IsolatedBackend(Backend):
    """Fault-isolation wrapper for any Backend implementation.

    All ``Backend`` method calls are serialised via ``pickle``, dispatched to
    an ``InterpreterPoolExecutor`` (Python 3.14+) or ``ProcessPoolExecutor``
    (earlier versions), and the result is deserialised back.  Each worker
    lazily creates a dedicated ``Backend`` instance with its own
    ``sys.modules`` and global state.

    Streaming operations (``stream_content``, ``write_stream``) are buffered:
    generators cannot cross the isolation boundary, so the content is collected
    in the worker and re-chunked on the wrapper side.
    """

    def __init__(self, config: IsolationConfig) -> None:
        self._config = config
        self._pool = IsolatedPool(config)
        self._prop_lock = threading.Lock()
        self._prop_cache: dict[str, Any] = {}

    # ── Cached immutable properties ─────────────────────────────────────

    @property
    def name(self) -> str:
        try:
            inner_name = self._cached_prop("name")
        except IsolationError:
            inner_name = f"{self._config.backend_module}:{self._config.backend_class}"
        return f"isolated({inner_name})"

    @property
    def user_scoped(self) -> bool:
        return bool(self._cached_prop("user_scoped"))

    @property
    def is_connected(self) -> bool:
        return self._pool.is_alive

    @property
    def thread_safe(self) -> bool:
        return bool(self._cached_prop("thread_safe"))

    @property
    def supports_rename(self) -> bool:
        return bool(self._cached_prop("supports_rename"))

    @property
    def has_virtual_filesystem(self) -> bool:
        return bool(self._cached_prop("has_virtual_filesystem"))

    @property
    def has_root_path(self) -> bool:
        return bool(self._cached_prop("has_root_path"))

    @property
    def has_token_manager(self) -> bool:
        return bool(self._cached_prop("has_token_manager"))

    @property
    def has_data_dir(self) -> bool:
        return bool(self._cached_prop("has_data_dir"))

    @property
    def is_passthrough(self) -> bool:
        return bool(self._cached_prop("is_passthrough"))

    @property
    def supports_parallel_mmap_read(self) -> bool:
        return bool(self._cached_prop("supports_parallel_mmap_read"))

    # ── Content operations (CAS) ────────────────────────────────────────

    def write_content(
        self, content: bytes, context: OperationContext | None = None
    ) -> HandlerResponse[str]:
        return self._call("write_content", content, context=context)

    def read_content(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[bytes]:
        return self._call("read_content", content_hash, context=context)

    def delete_content(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[None]:
        return self._call("delete_content", content_hash, context=context)

    def content_exists(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[bool]:
        return self._call("content_exists", content_hash, context=context)

    def get_content_size(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[int]:
        return self._call("get_content_size", content_hash, context=context)

    def get_ref_count(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[int]:
        return self._call("get_ref_count", content_hash, context=context)

    # ── Directory operations ────────────────────────────────────────────

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: OperationContext | EnhancedOperationContext | None = None,
    ) -> HandlerResponse[None]:
        return self._call("mkdir", path, parents=parents, exist_ok=exist_ok, context=context)

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: OperationContext | EnhancedOperationContext | None = None,
    ) -> HandlerResponse[None]:
        return self._call("rmdir", path, recursive=recursive, context=context)

    def is_directory(
        self, path: str, context: OperationContext | None = None
    ) -> HandlerResponse[bool]:
        return self._call("is_directory", path, context=context)

    def list_dir(self, path: str, context: OperationContext | None = None) -> list[str]:
        """Delegate to pool — propagates FileNotFoundError / NotImplementedError."""
        try:
            return cast(list[str], self._pool.submit("list_dir", (path,), {"context": context}))
        except IsolationCallError as exc:
            # Re-raise known exceptions that callers expect to catch.
            # Only re-raise well-known stdlib types to avoid unpickling issues
            # with custom exception classes from third-party backends.
            cause = exc.cause
            if cause is not None and isinstance(
                cause, (FileNotFoundError, NotADirectoryError, PermissionError, NotImplementedError)
            ):
                raise type(cause)(str(cause)) from exc
            raise

    # ── Streaming (buffered — generators cannot cross the boundary) ─────

    def stream_content(
        self,
        content_hash: str,
        chunk_size: int = 8192,
        context: OperationContext | None = None,
    ) -> Iterator[bytes]:
        """Read full content via pool, then re-chunk locally."""
        resp = self.read_content(content_hash, context=context)
        content = resp.unwrap()
        for i in range(0, len(content), chunk_size):
            yield content[i : i + chunk_size]

    def write_stream(
        self,
        chunks: Iterator[bytes],
        context: OperationContext | None = None,
    ) -> HandlerResponse[str]:
        """Collect chunks locally, then write via pool."""
        content = b"".join(chunks)
        return self.write_content(content, context=context)

    # ── Connection lifecycle ────────────────────────────────────────────

    def connect(self, context: OperationContext | None = None) -> HandlerStatusResponse:
        """Probe the worker's backend health.

        The actual ``connect()`` is called lazily by ``worker_call`` on first
        use, so this method just verifies that the pool can reach the backend.
        """
        try:
            result = self._pool.submit("check_connection", (), {"context": context})
            if isinstance(result, HandlerStatusResponse):
                return result
            return HandlerStatusResponse(success=True, details={"backend": self.name})
        except IsolationError as exc:
            return HandlerStatusResponse(success=False, error_message=str(exc))

    def disconnect(self, context: OperationContext | None = None) -> None:  # noqa: ARG002
        self._pool.shutdown()

    # ── Internal helpers ────────────────────────────────────────────────

    def _call(self, method: str, *args: Any, **kwargs: Any) -> HandlerResponse[Any]:
        """Delegate to pool.  Convert ``IsolationError`` → ``HandlerResponse.error()``."""
        try:
            return cast(HandlerResponse[Any], self._pool.submit(method, args, kwargs))
        except IsolationTimeoutError as exc:
            return HandlerResponse.error(str(exc), code=504, backend_name=self.name)
        except IsolationCallError as exc:
            cause = exc.cause
            if cause is not None and isinstance(cause, Exception):
                return HandlerResponse.from_exception(cause, backend_name=self.name)
            return HandlerResponse.error(str(exc), code=500, backend_name=self.name)
        except IsolationError as exc:
            return HandlerResponse.error(str(exc), code=503, backend_name=self.name)

    def _cached_prop(self, prop: str) -> Any:
        """Read an immutable property, caching the result after first read.

        Thread-safe: uses ``_prop_lock`` to prevent duplicate pool calls.
        """
        with self._prop_lock:
            if prop not in self._prop_cache:
                self._prop_cache[prop] = self._pool.get_property(prop)
            return self._prop_cache[prop]
