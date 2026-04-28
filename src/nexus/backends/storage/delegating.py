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

import logging
from typing import TYPE_CHECKING, Any

from nexus.backends.base.backend import Backend

if TYPE_CHECKING:
    from nexus.backends.base.backend import HandlerStatusResponse
    from nexus.contracts.types import OperationContext
    from nexus.core.object_store import WriteResult

logger = logging.getLogger(__name__)


class DelegatingBackend(Backend):
    """Base class for wrappers that implement the same Backend Protocol.

    Delegates every Backend property and method to ``_inner`` by default.
    Concrete wrappers override only the operations they intercept.

    Wrapper Patterns:

        **Data-transform wrappers** (CompressedStorage, EncryptedStorage):
        Override ``_transform_on_write`` and ``_transform_on_read`` hooks.
        DelegatingBackend handles write_content/read_content/batch_read_content
        orchestration, calling the hooks at the right points. These wrappers
        never touch directory ops or connection lifecycle.

        **Behavioral wrappers** (LoggingBackendWrapper):
        Override full methods directly (read_content, write_content, etc.)
        to add logging or other cross-cutting behavior. These
        wrappers bypass the transform hooks entirely and manage delegation
        to ``_inner`` themselves.

    Recursive Wrapping Rules (PART 16):
        1. Wrapper MUST implement the same Protocol as ``inner``.
        2. Wrapper MUST delegate unknown ops to ``inner`` (open/closed).
        3. Wrapper MUST implement ``describe()`` returning the full chain.
        4. Chain assembly is in ``factory.py`` (config-time), never runtime.
        5. Each wrapper is independently testable with a mock ``inner``.
    """

    def __init__(self, inner: Backend) -> None:
        super().__init__()
        self._inner = inner
        self._cached_backend_features = inner.backend_features

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
        return getattr(self._inner, "user_scoped", False)

    @property
    def is_connected(self) -> bool:
        return self._inner.is_connected

    @property
    def thread_safe(self) -> bool:
        return self._inner.thread_safe

    @property
    def has_root_path(self) -> bool:
        return self._inner.has_root_path

    @property
    def has_token_manager(self) -> bool:
        return getattr(self._inner, "has_token_manager", False)

    @property
    def has_data_dir(self) -> bool:
        return self._inner.has_data_dir

    # === Capability Discovery (Issue #2069) ===

    @property
    def backend_features(self) -> frozenset:
        """Delegate to inner backend's capabilities (cached in __init__)."""
        return self._cached_backend_features

    def has_feature(self, cap: object) -> bool:
        """Check capability using cached frozenset."""
        return cap in self._cached_backend_features

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
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: "OperationContext | None" = None,
    ) -> "WriteResult":
        """Transform content via ``_transform_on_write``, then write to inner.

        If ``_transform_on_write`` raises, the exception propagates.
        """
        transformed = self._transform_on_write(content)
        return self._inner.write_content(transformed, content_id, offset=offset, context=context)

    def read_content(self, content_id: str, context: "OperationContext | None" = None) -> bytes:
        """Read from inner, then transform via ``_transform_on_read``."""
        data = self._inner.read_content(content_id, context=context)
        return self._transform_on_read(data)

    def batch_read_content(
        self,
        content_ids: list[str],
        context: "OperationContext | None" = None,
        *,
        contexts: "dict[str, OperationContext] | None" = None,
    ) -> dict[str, bytes | None]:
        """Read batch from inner, then transform each item via ``_transform_on_read``."""
        raw_results = self._inner.batch_read_content(
            content_ids, context=context, contexts=contexts
        )

        transformed: dict[str, bytes | None] = {}
        for content_id, data in raw_results.items():
            if data is None:
                transformed[content_id] = None
                continue

            try:
                transformed[content_id] = self._transform_on_read(data)
            except Exception as e:
                logger.warning(
                    "%s batch read transform failed for hash=%s: %s",
                    self.__class__.__name__,
                    content_id[:12],
                    e,
                )
                transformed[content_id] = None

        return transformed

    def delete_content(self, content_id: str, context: "OperationContext | None" = None) -> None:
        return self._inner.delete_content(content_id, context=context)

    def content_exists(self, content_id: str, context: "OperationContext | None" = None) -> bool:
        return self._inner.content_exists(content_id, context=context)

    def get_content_size(self, content_id: str, context: "OperationContext | None" = None) -> int:
        return self._inner.get_content_size(content_id, context=context)

    # === Directory Operations (delegate to inner) ===

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        return self._inner.mkdir(path, parents=parents, exist_ok=exist_ok, context=context)

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        return self._inner.rmdir(path, recursive=recursive, context=context)

    def is_directory(self, path: str, context: "OperationContext | None" = None) -> bool:
        return self._inner.is_directory(path, context=context)

    def list_dir(self, path: str, context: "OperationContext | None" = None) -> list[str]:
        return self._inner.list_dir(path, context=context)

    # === Connection Lifecycle (delegate to inner) ===

    def check_connection(
        self, context: "OperationContext | None" = None
    ) -> "HandlerStatusResponse":
        return self._inner.check_connection(context=context)

    # === Fallback for any remaining/future Backend methods ===

    def __getattr__(self, name: str) -> Any:
        """Delegate any non-overridden attribute to inner backend.

        Covers: stream_content, write_stream, stream_range,
        get_file_info, get_object_type, get_object_id,
        and any future Backend methods.
        """
        return getattr(self._inner, name)
