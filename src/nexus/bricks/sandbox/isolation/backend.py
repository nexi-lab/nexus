"""IsolatedBackend — fault-isolation wrapper for any Backend implementation.

Follows LEGO Pattern E (Decorator/Wrapper).  The kernel sees ``IsolatedBackend``
as a regular ``Backend`` — no code changes needed in NexusFS or routers.

WARNING: This provides FAULT ISOLATION (state / crash isolation), NOT security
sandboxing.  For untrusted-code security, use Docker / E2B sandbox providers.
"""

import logging
import threading
from collections.abc import Iterator
from typing import Any, cast

from nexus.backends.base.backend import Backend, HandlerStatusResponse
from nexus.bricks.sandbox.isolation._pool import IsolatedPool
from nexus.bricks.sandbox.isolation.config import IsolationConfig
from nexus.bricks.sandbox.isolation.errors import (
    IsolationCallError,
    IsolationError,
    IsolationTimeoutError,
)
from nexus.contracts.exceptions import BackendError
from nexus.contracts.types import WriteResult

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
        super().__init__()
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
        try:
            return bool(self._cached_prop("user_scoped"))
        except IsolationError:
            return False

    @property
    def is_connected(self) -> bool:
        return self._pool.is_alive

    @property
    def thread_safe(self) -> bool:
        return bool(self._cached_prop("thread_safe"))

    @property
    def has_root_path(self) -> bool:
        return bool(self._cached_prop("has_root_path"))

    @property
    def has_token_manager(self) -> bool:
        try:
            return bool(self._cached_prop("has_token_manager"))
        except IsolationError:
            return False

    @property
    def has_data_dir(self) -> bool:
        return bool(self._cached_prop("has_data_dir"))

    # ── Capability Discovery (Issue #2069) ─────────────────────────────

    @property
    def backend_features(self) -> frozenset[Any]:
        """Delegate to inner backend's capabilities (cached via _cached_prop)."""
        result = self._cached_prop("backend_features")
        return result if isinstance(result, frozenset) else frozenset()

    def has_feature(self, cap: object) -> bool:
        """Check capability using cached frozenset."""
        return cap in self.backend_features

    # ── Content operations (CAS) ────────────────────────────────────────

    def write_content(
        self, content: bytes, content_id: str = "", *, offset: int = 0, context: "Any | None" = None
    ) -> WriteResult:
        return cast(
            WriteResult,
            self._call("write_content", content, content_id, offset=offset, context=context),
        )

    def read_content(self, content_id: str, context: "Any | None" = None) -> bytes:
        return cast(bytes, self._call("read_content", content_id, context=context))

    def delete_content(self, content_id: str, context: "Any | None" = None) -> None:
        self._call("delete_content", content_id, context=context)

    def content_exists(self, content_id: str, context: "Any | None" = None) -> bool:
        return cast(bool, self._call("content_exists", content_id, context=context))

    def get_content_size(self, content_id: str, context: "Any | None" = None) -> int:
        return cast(int, self._call("get_content_size", content_id, context=context))

    # ── Directory operations ────────────────────────────────────────────

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "Any | None" = None,
    ) -> None:
        self._call("mkdir", path, parents=parents, exist_ok=exist_ok, context=context)

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "Any | None" = None,
    ) -> None:
        self._call("rmdir", path, recursive=recursive, context=context)

    def is_directory(self, path: str, context: "Any | None" = None) -> bool:
        return cast(bool, self._call("is_directory", path, context=context))

    def list_dir(self, path: str, context: "Any | None" = None) -> list[str]:
        """Delegate to pool — propagates FileNotFoundError / NotImplementedError."""
        try:
            return cast(list[str], self._pool.submit("list_dir", (path,), {"context": context}))
        except IsolationCallError as exc:
            # Re-raise known exceptions that callers expect to catch.
            # Only re-raise well-known stdlib types to avoid unpickling issues
            # with custom exception classes from third-party backends.
            cause = exc.cause
            if cause is not None and isinstance(
                cause,
                FileNotFoundError | NotADirectoryError | PermissionError | NotImplementedError,
            ):
                raise type(cause)(str(cause)) from exc
            raise

    # ── Streaming (buffered — generators cannot cross the boundary) ─────

    def stream_content(
        self,
        content_id: str,
        chunk_size: int = 8192,
        context: "Any | None" = None,
    ) -> Iterator[bytes]:
        """Read full content via pool, then re-chunk locally."""
        content = self.read_content(content_id, context=context)
        for i in range(0, len(content), chunk_size):
            yield content[i : i + chunk_size]

    def write_stream(
        self,
        chunks: Iterator[bytes],
        content_id: str = "",
        *,
        context: "Any | None" = None,
    ) -> WriteResult:
        """Collect chunks locally, then write via pool."""
        content = b"".join(chunks)
        return self.write_content(content, content_id, context=context)

    # ── Connection lifecycle ────────────────────────────────────────────

    def check_connection(self, context: "Any | None" = None) -> HandlerStatusResponse:
        """Probe the worker's backend health."""
        try:
            result = self._pool.submit("check_connection", (), {"context": context})
            if isinstance(result, HandlerStatusResponse):
                return result
            return HandlerStatusResponse(success=True, details={"backend": self.name})
        except IsolationError as exc:
            return HandlerStatusResponse(success=False, error_message=str(exc))

    def close(self) -> None:
        """Shut down the isolation pool and release resources."""
        self._pool.shutdown()

    # ── Internal helpers ────────────────────────────────────────────────

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Delegate to pool.  Convert ``IsolationError`` → ``BackendError``."""
        try:
            return self._pool.submit(method, args, kwargs)
        except IsolationTimeoutError as exc:
            raise BackendError(str(exc), backend=self.name) from exc
        except IsolationCallError as exc:
            cause = exc.cause
            if cause is not None and isinstance(cause, Exception):
                raise cause from exc
            raise BackendError(str(exc), backend=self.name) from exc
        except IsolationError as exc:
            raise BackendError(str(exc), backend=self.name) from exc

    def _cached_prop(self, prop: str) -> Any:
        """Read an immutable property, caching the result after first read.

        Thread-safe: uses ``_prop_lock`` to prevent duplicate pool calls.
        """
        with self._prop_lock:
            if prop not in self._prop_cache:
                self._prop_cache[prop] = self._pool.get_property(prop)
            return self._prop_cache[prop]
