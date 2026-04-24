"""Error-injection wrapper for any Backend implementation.

Used in tests to simulate backend failures at specific call counts,
enabling verification of error handling and partial-failure recovery.

Usage:
    backend = FailingBackend(CASLocalBackend(tmp_path), fail_on_nth=3)
    # First two calls succeed; third call raises BackendError
    backend.write_content(b"a")  # OK (call 1)
    backend.write_content(b"b")  # OK (call 2)
    backend.write_content(b"c")  # raises BackendError (call 3)
    backend.write_content(b"d")  # OK (call 4, past the failure point)

    # Or fail on specific methods:
    backend = FailingBackend(CASLocalBackend(tmp_path), fail_on_methods=["read_content"])
    backend.write_content(b"a")  # OK
    backend.read_content(hash)   # raises BackendError
"""

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from nexus.backends.base.backend import Backend, FileInfo, HandlerStatusResponse
from nexus.contracts.exceptions import BackendError
from nexus.core.object_store import WriteResult

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext


class FailingBackend(Backend):
    """Backend wrapper that injects failures for testing.

    Args:
        inner: The real backend to delegate to.
        fail_on_nth: Fail on the Nth call (1-indexed). 0 means never fail by count.
        fail_on_methods: Only fail when these method names are called.
            If empty, all methods can trigger the count-based failure.
        error_message: Custom error message for the raised BackendError.
        fail_permanently: If True, fail on every call at or after fail_on_nth.
            If False (default), only fail on the exact Nth call.
    """

    def __init__(
        self,
        inner: Backend,
        *,
        fail_on_nth: int = 0,
        fail_on_methods: list[str] | None = None,
        error_message: str = "Injected backend failure",
        fail_permanently: bool = False,
    ) -> None:
        self._inner = inner
        self._fail_on_nth = fail_on_nth
        self._fail_on_methods = set(fail_on_methods) if fail_on_methods else set()
        self._error_message = error_message
        self._fail_permanently = fail_permanently
        self._call_count = 0

    @property
    def name(self) -> str:
        return f"failing({self._inner.name})"

    @property
    def call_count(self) -> int:
        return self._call_count

    def reset(self) -> None:
        """Reset the call counter."""
        self._call_count = 0

    def _maybe_fail(self, method_name: str) -> None:
        """Increment counter and raise if this is the failing call."""
        if self._fail_on_methods and method_name not in self._fail_on_methods:
            return

        self._call_count += 1

        if self._fail_on_nth <= 0:
            return

        should_fail = (
            self._call_count >= self._fail_on_nth
            if self._fail_permanently
            else self._call_count == self._fail_on_nth
        )

        if should_fail:
            raise BackendError(
                f"{self._error_message} (call #{self._call_count}, method={method_name})",
                backend=self.name,
            )

    # === Content Operations ===

    def write_content(
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        self._maybe_fail("write_content")
        return self._inner.write_content(content, content_id, offset=offset, context=context)

    def read_content(self, content_hash: str, context: "OperationContext | None" = None) -> bytes:
        self._maybe_fail("read_content")
        return self._inner.read_content(content_hash, context)

    def delete_content(self, content_hash: str, context: "OperationContext | None" = None) -> None:
        self._maybe_fail("delete_content")
        return self._inner.delete_content(content_hash, context)

    def content_exists(self, content_hash: str, context: "OperationContext | None" = None) -> bool:
        self._maybe_fail("content_exists")
        return self._inner.content_exists(content_hash, context)

    def get_content_size(self, content_hash: str, context: "OperationContext | None" = None) -> int:
        self._maybe_fail("get_content_size")
        return self._inner.get_content_size(content_hash, context)

    def batch_read_content(
        self,
        content_hashes: list[str],
        context: "OperationContext | None" = None,
        *,
        contexts: "dict[str, OperationContext] | None" = None,
    ) -> dict[str, bytes | None]:
        self._maybe_fail("batch_read_content")
        return self._inner.batch_read_content(content_hashes, context, contexts=contexts)

    def stream_content(
        self,
        content_hash: str,
        chunk_size: int = 8192,
        context: "OperationContext | None" = None,
    ) -> Any:
        self._maybe_fail("stream_content")
        return self._inner.stream_content(content_hash, chunk_size, context)

    def stream_range(
        self,
        content_hash: str,
        start: int,
        end: int,
        chunk_size: int = 8192,
        context: "OperationContext | None" = None,
    ) -> Iterator[bytes]:
        self._maybe_fail("stream_range")
        return self._inner.stream_range(content_hash, start, end, chunk_size, context)

    def write_stream(
        self,
        chunks: Iterator[bytes],
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        self._maybe_fail("write_stream")
        return self._inner.write_stream(chunks, context)

    # === Directory Operations ===

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        self._maybe_fail("mkdir")
        return self._inner.mkdir(path, parents, exist_ok, context)

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        self._maybe_fail("rmdir")
        return self._inner.rmdir(path, recursive, context)

    def is_directory(self, path: str, context: "OperationContext | None" = None) -> bool:
        self._maybe_fail("is_directory")
        return self._inner.is_directory(path, context)

    def list_dir(self, path: str, context: "OperationContext | None" = None) -> list[str]:
        self._maybe_fail("list_dir")
        return self._inner.list_dir(path, context)

    # === Connection Management ===

    def check_connection(self, context: "OperationContext | None" = None) -> HandlerStatusResponse:
        return self._inner.check_connection(context)

    # === Capability Delegation ===

    @property
    def user_scoped(self) -> bool:
        return getattr(self._inner, "user_scoped", False)

    @property
    def is_connected(self) -> bool:
        return self._inner.is_connected

    @property
    def thread_safe(self) -> bool:
        return self._inner.thread_safe

    @property
    def supports_rename(self) -> bool:
        return self._inner.supports_rename

    @property
    def has_root_path(self) -> bool:
        return self._inner.has_root_path

    @property
    def root_path(self) -> Any:
        return getattr(self._inner, "root_path", None)

    @property
    def has_token_manager(self) -> bool:
        return getattr(self._inner, "has_token_manager", False)

    @property
    def has_data_dir(self) -> bool:
        return self._inner.has_data_dir

    @property
    def supports_parallel_mmap_read(self) -> bool:
        return self._inner.supports_parallel_mmap_read

    # === Delta Sync ===

    def get_file_info(self, path: str, context: "OperationContext | None" = None) -> FileInfo:
        self._maybe_fail("get_file_info")
        return self._inner.get_file_info(path, context)

    def get_object_type(self, backend_path: str) -> str:
        return self._inner.get_object_type(backend_path)

    def get_object_id(self, backend_path: str) -> str:
        return self._inner.get_object_id(backend_path)

    def describe(self) -> str:
        return f"failing({self._inner.describe()})"
