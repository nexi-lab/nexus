"""DelegatingBackend — base class for same-Protocol recursive wrappers (#1449).

Provides boilerplate-free property delegation for all Backend capability flags,
connection lifecycle methods, and a ``__getattr__`` fallback for any future
Backend methods added without updating wrappers.

Subclasses only need to:
1. Call ``super().__init__(inner)`` in their ``__init__``.
2. Override ``describe()`` to prepend their layer name.
3. Override ``_transform_on_write`` / ``_transform_on_read`` for data transforms.

Design reference:
    - NEXUS-LEGO-ARCHITECTURE.md PART 16 — Recursive Wrapping Rules 1-5
    - Issue #1449: Recursive Protocol wrapping + describe() for composition chains
    - Issue #2077: Deduplicate backend wrapper boilerplate
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.backends.backend import Backend

if TYPE_CHECKING:
    from nexus.backends.backend import HandlerStatusResponse
    from nexus.core.permissions import OperationContext
    from nexus.core.response import HandlerResponse

logger = logging.getLogger(__name__)


class DelegatingBackend(Backend):
    """Base class for wrappers that implement the same Backend Protocol.

    Delegates every Backend property and method to ``_inner`` by default.
    Concrete wrappers override only the operations they intercept.

    Data-transforming wrappers (compression, encryption) override
    ``_transform_on_write`` and ``_transform_on_read`` instead of
    ``write_content`` / ``read_content`` / ``batch_read_content``.

    Recursive Wrapping Rules (PART 16):
        1. Wrapper MUST implement the same Protocol as ``inner``.
        2. Wrapper MUST delegate unknown ops to ``inner`` (open/closed).
        3. Wrapper MUST implement ``describe()`` returning the full chain.
        4. Chain assembly is in ``factory.py`` (config-time), never runtime.
        5. Each wrapper is independently testable with a mock ``inner``.
    """

    def __init__(self, inner: Backend) -> None:
        self._inner = inner

    # === Name & Chain Introspection ===

    @property
    def name(self) -> str:
        return self._inner.name

    def describe(self) -> str:
        """Default: transparent pass-through. Subclasses prepend their layer."""
        return self._inner.describe()

    # === Capability Flags (explicit delegation — __getattr__ cannot ===
    # === intercept properties that have defaults on the parent ABC) ===

    @property
    def user_scoped(self) -> bool:
        return self._inner.user_scoped

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
    def has_virtual_filesystem(self) -> bool:
        return self._inner.has_virtual_filesystem

    @property
    def has_root_path(self) -> bool:
        return self._inner.has_root_path

    @property
    def has_token_manager(self) -> bool:
        return self._inner.has_token_manager

    @property
    def has_data_dir(self) -> bool:
        return self._inner.has_data_dir

    @property
    def is_passthrough(self) -> bool:
        return self._inner.is_passthrough

    @property
    def supports_parallel_mmap_read(self) -> bool:
        return self._inner.supports_parallel_mmap_read

    # === Transform Hooks (override in data-transforming wrappers) ===

    def _transform_on_write(self, content: bytes) -> bytes:
        """Transform content before writing to inner backend.

        Default: identity (pass-through). Override in subclasses that
        transform data on write (compression, encryption, etc.).

        Convention:
            - Return transformed bytes on success.
            - Raise an exception to signal a hard failure (write aborted).
            - For soft fallback (e.g., compression skipped), return
              the original content without raising.
        """
        return content

    def _transform_on_read(self, data: bytes) -> bytes:
        """Transform data after reading from inner backend.

        Default: identity (pass-through). Override in subclasses that
        transform data on read (decompression, decryption, etc.).

        Convention:
            - Return transformed bytes on success.
            - Raise an exception to signal failure.
        """
        return data

    # === Content Operations (with hook support) ===

    def write_content(
        self, content: bytes, context: OperationContext | None = None
    ) -> HandlerResponse[str]:
        """Transform content via ``_transform_on_write``, then write to inner.

        If ``_transform_on_write`` raises, returns an error response.
        """
        from nexus.core.response import HandlerResponse

        try:
            transformed = self._transform_on_write(content)
        except Exception as e:
            logger.error("%s write transform failed: %s", self.__class__.__name__, e)
            return HandlerResponse.error(
                message=f"{self.__class__.__name__} write transform failed: {e}"
            )
        return self._inner.write_content(transformed, context=context)

    def read_content(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[bytes]:
        """Read from inner, then transform via ``_transform_on_read``."""
        from nexus.core.response import HandlerResponse

        response = self._inner.read_content(content_hash, context=context)
        if not response.success or response.data is None:
            return response

        try:
            transformed = self._transform_on_read(response.data)
        except Exception as e:
            logger.warning("%s read transform failed: %s", self.__class__.__name__, e)
            return HandlerResponse.error(
                message=f"{self.__class__.__name__} read transform failed: {e}"
            )
        return HandlerResponse.ok(data=transformed, backend_name=self.name)

    def batch_read_content(
        self,
        content_hashes: list[str],
        context: OperationContext | None = None,
        *,
        contexts: dict[str, OperationContext] | None = None,
    ) -> dict[str, bytes | None]:
        """Read batch from inner, then transform each item via ``_transform_on_read``."""
        raw_results = self._inner.batch_read_content(
            content_hashes, context=context, contexts=contexts
        )

        transformed: dict[str, bytes | None] = {}
        for content_hash, data in raw_results.items():
            if data is None:
                transformed[content_hash] = None
                continue

            try:
                transformed[content_hash] = self._transform_on_read(data)
            except Exception as e:
                logger.warning(
                    "%s batch read transform failed for hash=%s: %s",
                    self.__class__.__name__,
                    content_hash[:12],
                    e,
                )
                transformed[content_hash] = None

        return transformed

    def delete_content(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[None]:
        return self._inner.delete_content(content_hash, context=context)

    def content_exists(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[bool]:
        return self._inner.content_exists(content_hash, context=context)

    def get_content_size(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[int]:
        return self._inner.get_content_size(content_hash, context=context)

    def get_ref_count(
        self, content_hash: str, context: OperationContext | None = None
    ) -> HandlerResponse[int]:
        return self._inner.get_ref_count(content_hash, context=context)

    # === Directory Operations (delegate to inner) ===

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: OperationContext | None = None,
    ) -> HandlerResponse[None]:
        return self._inner.mkdir(path, parents=parents, exist_ok=exist_ok, context=context)

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: OperationContext | None = None,
    ) -> HandlerResponse[None]:
        return self._inner.rmdir(path, recursive=recursive, context=context)

    def is_directory(
        self, path: str, context: OperationContext | None = None
    ) -> HandlerResponse[bool]:
        return self._inner.is_directory(path, context=context)

    def list_dir(self, path: str, context: OperationContext | None = None) -> list[str]:
        return self._inner.list_dir(path, context=context)

    # === Connection Lifecycle (delegate to inner) ===

    def connect(self, context: OperationContext | None = None) -> HandlerStatusResponse:
        return self._inner.connect(context=context)

    def disconnect(self, context: OperationContext | None = None) -> None:
        self._inner.disconnect(context=context)

    def check_connection(self, context: OperationContext | None = None) -> HandlerStatusResponse:
        return self._inner.check_connection(context=context)

    # === Fallback for any remaining/future Backend methods ===

    def __getattr__(self, name: str) -> Any:
        """Delegate any non-overridden attribute to inner backend.

        Covers: stream_content, write_stream, stream_range,
        get_file_info, get_object_type, get_object_id,
        and any future Backend methods.
        """
        return getattr(self._inner, name)
